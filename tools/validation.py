"""Local validation of XSLT and event XML: well-formedness, schema, quality and field mapping."""
import re
from collections import Counter
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import Field

from utils.schemas import SchemaCache, declared_system_id, errors_for, event_logging_system_id
from utils.stroom import gateway_from

XSL = 'http://www.w3.org/1999/XSL/Transform'
EVT = 'event-logging:3'
# Stroom's XSLT extension functions (namespace 'stroom'); an unknown name is usually a typo.
STROOM_FUNCTIONS = {
    'bitmap-lookup', 'cidr-to-numeric-ip', 'classification', 'col-from', 'col-to', 'current-time', 'current-user',
    'dec-to-bin', 'dec-to-hex', 'dec-to-oct', 'decode-url', 'dictionary', 'encode-url', 'feed-attribute',
    'feed-name', 'fetch-json', 'format-date', 'generate-url', 'get', 'hash', 'hex-to-dec', 'hex-to-oct',
    'hex-to-string', 'host-address', 'host-name', 'http-call', 'ip-in-cidr', 'json-to-xml', 'line-from',
    'line-to', 'link', 'log', 'lookup', 'meta', 'meta-keys', 'numeric-ip', 'parse-uri', 'part-no',
    'pipeline-name', 'put', 'random', 'record-no', 'search-id', 'source', 'source-id', 'stream-id',
}
_STROOM_CALL = re.compile(r'\bstroom:([a-z][a-z0-9-]*)\s*\(')
_ISO_TIME = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$')
# EventDetail children that describe the event rather than being its action.
_DETAIL_META = {'TypeId', 'Description', 'Classification', 'Purpose'}

XsltText = Annotated[str, Field(description="Full XSLT document text.")]
EventsXml = Annotated[str, Field(description="An <Events> document, e.g. step output or a record from read_stream.")]


def _parse(text: str, what: str) -> etree._Element:
    try:
        return etree.fromstring(text.encode('utf-8'))
    except etree.XMLSyntaxError as e:
        raise ToolError(f"{what} is not well-formed XML: {e}") from e


async def check_xslt(ctx: Context, xslt: XsltText) -> dict[str, Any]:
    """
    Check an XSLT before saving or stepping it: well-formed, an xsl:stylesheet with a version, the
    stroom namespace declared when stroom: functions are used, known stroom: function names, and
    xsl:import / xsl:include targets that exist as XSLT documents in Stroom.
    Full compilation happens when the pipeline is stepped.
    """
    try:
        root = etree.fromstring(xslt.encode('utf-8'))
    except etree.XMLSyntaxError as e:
        return {'ok': False, 'errors': [f"Not well-formed XML (line {e.lineno}): {e.msg}"], 'warnings': []}
    errors, warnings = [], []
    if root.tag != f'{{{XSL}}}stylesheet' and root.tag != f'{{{XSL}}}transform':
        errors.append("Root element must be xsl:stylesheet")
    if not root.get('version'):
        errors.append("xsl:stylesheet needs a version attribute (Stroom supports 2.0 and 3.0)")
    calls = set(_STROOM_CALL.findall(xslt))
    if calls and 'stroom' not in (root.nsmap or {}):
        errors.append('stroom: functions are used but xmlns:stroom="stroom" is not declared')
    unknown = sorted(calls - STROOM_FUNCTIONS)
    if unknown:
        warnings.append(f"Unrecognised stroom: functions (check spelling): {', '.join(unknown)}")
    imports = [e.get('href') for e in root.iter(f'{{{XSL}}}import', f'{{{XSL}}}include') if e.get('href')]
    missing = []
    for href in imports:
        found = await gateway_from(ctx).find_documents(href, ['XSLT'], 50)
        if not any(v['docRef'].get('name') == href for v in found.get('values') or []):
            missing.append(href)
    if missing:
        errors.append(f"xsl:import/include targets not found as XSLT documents: {', '.join(missing)}")
    return {'ok': not errors, 'errors': errors, 'warnings': warnings, 'imports': imports,
            'stroom_functions': sorted(calls)}


async def validate_events(
        ctx: Context,
        events_xml: EventsXml,
        schema_version: Annotated[str | None, Field(
            description="Event-logging version to validate against, e.g. '3.5.2'. Defaults to the version the "
                        "document declares in xsi:schemaLocation, else the configured version.")] = None,
) -> dict[str, Any]:
    """
    Validate event XML against the event-logging XSD held in this Stroom instance, returning each error
    with its line, element path and message.
    """
    stroom = gateway_from(ctx)
    system_id = (event_logging_system_id(schema_version) if schema_version
                 else declared_system_id(events_xml) or event_logging_system_id(stroom.settings.event_logging_version))
    cache = ctx.lifespan_context.setdefault('schemas', SchemaCache(stroom))
    schema = await cache.get(system_id)
    errors = errors_for(schema, _parse(events_xml, 'Events XML'))
    return {'valid': not errors, 'schema': system_id, 'error_count': len(errors), 'errors': errors[:50]}


def _text(node: etree._Element | None) -> str:
    return (node.text or '').strip() if node is not None else ''


def _path(node: etree._Element, stop: etree._Element) -> str:
    parts = []
    while node is not None and node is not stop:
        parts.append(etree.QName(node).localname)
        node = node.getparent()
    return '/'.join(reversed(parts))


def _event_findings(event: etree._Element) -> list[tuple[str, str]]:
    ns = {'e': EVT}
    found = []
    created = _text(event.find('e:EventTime/e:TimeCreated', ns))
    if not _ISO_TIME.match(created):
        found.append(('time_created', f"EventTime/TimeCreated is {created!r}, not yyyy-MM-ddTHH:mm:ss.SSSZ"))
    for path, rule in (('e:EventSource/e:System/e:Name', 'system_name'),
                       ('e:EventSource/e:System/e:Environment', 'system_environment'),
                       ('e:EventSource/e:Generator', 'generator'),
                       ('e:EventDetail/e:TypeId', 'type_id')):
        if not _text(event.find(path, ns)):
            found.append((rule, f"{path.replace('e:', '')} is missing or empty"))
    if event.find('e:EventSource/e:Device', ns) is None:
        found.append(('device', "EventSource/Device is missing"))
    detail = event.find('e:EventDetail', ns)
    actions = [etree.QName(c).localname for c in (detail if detail is not None else [])
               if isinstance(c.tag, str) and etree.QName(c).localname not in _DETAIL_META]
    if len(actions) != 1:
        found.append(('action', f"EventDetail should hold exactly one action element, found {actions or 'none'}"))
    for node in event.iter():
        if isinstance(node.tag, str) and len(node) == 0 and not _text(node) and not node.attrib:
            found.append(('empty_element', f"{_path(node, event.getparent())} is empty"))
    return found


async def check_event_quality(ctx: Context, events_xml: EventsXml) -> dict[str, Any]:
    """
    Quality checks beyond the schema, per event: TimeCreated is a full UTC timestamp, System Name,
    Environment, Generator, Device and TypeId are present, EventDetail has exactly one action, and no
    element is empty. Returns each rule with the number of events failing it and examples.
    """
    root = _parse(events_xml, 'Events XML')
    events = root.findall(f'{{{EVT}}}Event') if etree.QName(root).localname == 'Events' else [root]
    rules: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(events):
        for rule, message in _event_findings(event):
            entry = rules.setdefault(rule, {'events_failing': set(), 'examples': []})
            entry['events_failing'].add(index)
            if len(entry['examples']) < 3:
                entry['examples'].append({'event': index, 'message': message})
    return {'events_checked': len(events), 'ok': not rules,
            'rules': {r: {'events_failing': len(v['events_failing']), 'examples': v['examples']}
                      for r, v in sorted(rules.items())}}


_INPUT_FIELD = re.compile(r"""(?:data|string|number|boolean|map|array)\[@(?:name|key)\s*=\s*['"]([^'"]+)['"]\]""")


def _describe(root: etree._Element) -> dict[str, Any]:
    mappings: list[dict[str, str]] = []

    def output_path(node: etree._Element) -> str:
        parts = []
        current = node
        while current is not None and current.tag != f'{{{XSL}}}template':
            if isinstance(current.tag, str) and not current.tag.startswith(f'{{{XSL}}}'):
                parts.append(etree.QName(current).localname)
            current = current.getparent()
        template = current.get('match') or current.get('name') if current is not None else None
        path = '/'.join(reversed(parts))
        return f"[{template}] {path}" if template and path else path or f"[{template}]"

    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        if node.tag == f'{{{XSL}}}value-of' or node.tag == f'{{{XSL}}}sequence':
            mappings.append({'output': output_path(node.getparent()), 'source': node.get('select', ''), 'kind': 'value'})
        elif node.tag == f'{{{XSL}}}attribute':
            mappings.append({'output': f"{output_path(node.getparent())}/@{node.get('name')}",
                             'source': node.get('select') or ''.join(node.itertext()).strip(), 'kind': 'attribute'})
        elif not node.tag.startswith(f'{{{XSL}}}'):
            for name, value in node.attrib.items():
                if '{' in value and not name.startswith('{'):
                    mappings.append({'output': f"{output_path(node)}/@{name}", 'source': value, 'kind': 'attribute'})
            if _text(node) and len(node) == 0:
                mappings.append({'output': output_path(node), 'source': _text(node), 'kind': 'constant'})
    source_text = etree.tostring(root, encoding='unicode')
    return {
        'mappings': mappings,
        'input_fields': sorted(set(_INPUT_FIELD.findall(source_text))),
        'imports': [e.get('href') for e in root.iter(f'{{{XSL}}}import', f'{{{XSL}}}include')],
        'dictionaries': sorted(set(re.findall(r"stroom:dictionary\(\s*'([^']+)'", source_text))),
        'lookups': sorted(set(re.findall(r"stroom:(?:lookup|bitmap-lookup)\(\s*'([^']+)'", source_text))),
    }


async def describe_translation(
        ctx: Context,
        xslt: Annotated[str | None, Field(description="XSLT text to describe.")] = None,
        xslt_uuid: Annotated[str | None, Field(description="Or the UUID of an XSLT document in Stroom.")] = None,
) -> dict[str, Any]:
    """
    Describe what a translation XSLT does: for each output element or attribute, the input field,
    expression or constant that fills it (grouped by the template it is written in), the input fields
    it reads, and the XSLT libraries, dictionaries and reference lookups it uses. Use it to document a
    pipeline or to find input fields that are never used.
    """
    if not xslt and not xslt_uuid:
        raise ToolError("Give xslt text or xslt_uuid")
    name = None
    if xslt_uuid:
        doc = await gateway_from(ctx).get(f'/xslt/v1/{xslt_uuid}')
        xslt, name = doc.get('data') or '', doc.get('name')
    result = _describe(_parse(xslt, 'XSLT'))
    kinds = Counter(m['kind'] for m in result['mappings'])
    return {'xslt': name, 'mapping_counts': dict(kinds), **result}


ALL_TOOLS = [check_xslt, validate_events, check_event_quality, describe_translation]
