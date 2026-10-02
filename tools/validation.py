"""Local validation of XSLT and event XML: well-formedness, schema, quality and field mapping."""
import difflib
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
JSON_NS = 'http://www.w3.org/2013/XSL/json'
FN_NS = 'http://www.w3.org/2005/xpath-functions'
# Stroom's XSLT extension functions (namespace 'stroom'), as its function library registers them. Anything else
# fails to compile when the pipeline runs, so an unknown name is refused here with the nearest real one.
STROOM_FUNCTIONS = {
    'add-meta', 'ask-ai', 'bitmap-lookup', 'cidr-to-numeric-ip', 'cidr-to-numeric-ip-range', 'classification',
    'col-from', 'col-to', 'cosine-similarity', 'current-time', 'current-unixTime', 'current-user', 'dec-to-bin',
    'dec-to-hex', 'dec-to-oct', 'decode-url', 'dictionary', 'encode-url', 'feed-attribute', 'feed-name',
    'fetch-json', 'format-date', 'format-dateTime', 'from-unixTime', 'generate-url', 'get', 'hash', 'hex-to-dec',
    'hex-to-oct', 'hex-to-string', 'host-address', 'host-name', 'http-call', 'ip-in-cidr', 'json-to-xml',
    'line-from', 'line-to', 'link', 'log', 'lookup', 'manifest', 'manifest-for-id', 'meta', 'meta-attribute',
    'meta-keys', 'meta-stream', 'meta-stream-for-id', 'numeric-ip', 'parent-for-id', 'parent-id',
    'parse-dateTime', 'parse-uri', 'part-no', 'pipeline-name', 'plan-b-lookup', 'pointIsInsideXYPolygon', 'put',
    'random', 'record-no', 'search-id', 'source', 'source-id', 'split-document', 'stream-id', 'to-unixTime',
}
_STROOM_CALL = re.compile(r'\bstroom:([A-Za-z][A-Za-z0-9-]*)\s*\(')
_ISO_TIME = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$')
# EventDetail children that describe the event rather than being its action.
_DETAIL_META = {'TypeId', 'Description', 'Classification', 'Purpose'}
# Element names that, in a pipeline, only ever come from one parser or stream type, each in its own namespace.
# Written bare in a match or select with no xpath-default-namespace they select nothing: the XSLT's output is
# empty text, and processing writes nowhere.
_INPUT_NAMESPACES = {
    'records:2': {'records', 'record', 'data'},
    EVT: {'Events', 'Event'},
    JSON_NS: {'map', 'array', 'string', 'number', 'boolean', 'null'},
}
_XPATH_ATTRS = ('match', 'select', 'test', 'group-by', 'use')
_LITERAL = re.compile(r"""'[^']*'|"[^"]*\"""")
# A bare element-name step: not an attribute, variable, prefixed name, function, map/array constructor or axis.
_NAME_STEP = re.compile(r'(?<![\w.:@$-])([A-Za-z_][\w.-]*)(?!\s*[:({]|[\w.-])')
_ELEMENT_AXIS = re.compile(r'\b(?:child|descendant|self|descendant-or-self|following-sibling|preceding-sibling|parent'
                           r'|ancestor|ancestor-or-self|following|preceding)::')

XsltText = Annotated[str, Field(description="Full XSLT document text.")]
EventsXml = Annotated[str, Field(description="An <Events> document, e.g. step output or a record from read_stream.")]


def _parse(text: str, what: str) -> etree._Element:
    try:
        return etree.fromstring(text.encode('utf-8'))
    except etree.XMLSyntaxError as e:
        raise ToolError(f"{what} is not well-formed XML: {e}") from e


def _function_problems(xslt: str, root: etree._Element) -> tuple[list[str], set[str]]:
    calls = set(_STROOM_CALL.findall(xslt))
    errors = []
    if calls and 'stroom' not in (root.nsmap or {}):
        errors.append('stroom: functions are used but xmlns:stroom="stroom" is not declared')
    for name in sorted(calls - STROOM_FUNCTIONS):
        close = difflib.get_close_matches(name, sorted(STROOM_FUNCTIONS), n=2, cutoff=0.6)
        message = f"stroom:{name}() is not a Stroom function and will not compile"
        if 'json' in name.lower():
            message += (": JSON held in a string is parsed with json-to-xml(text), an XPath function with no prefix "
                        "(its output is in the xpath-functions namespace); raw JSON input is parsed by the pipeline's "
                        "JSONParser element, not in XSLT")
        elif close:
            message += f"; did you mean {' or '.join(f'stroom:{c}()' for c in close)}?"
        errors.append(message)
    return errors, calls


def _default_namespace(node: etree._Element) -> str | None:
    """The xpath-default-namespace in force at node (None when none is declared)."""
    while node is not None:
        if node.get('xpath-default-namespace') is not None:
            return node.get('xpath-default-namespace')
        node = node.getparent()
    return None


def _namespace_problems(root: etree._Element) -> list[str]:
    found: dict[str, list[str]] = {}
    for node in root.iter(f'{{{XSL}}}*'):
        if _default_namespace(node) is not None:
            continue
        for attr in _XPATH_ATTRS:
            expr = node.get(attr)
            if not expr:
                continue
            names = set(_NAME_STEP.findall(_ELEMENT_AXIS.sub('', _LITERAL.sub("''", expr))))
            for namespace, known in _INPUT_NAMESPACES.items():
                if namespace == JSON_NS and any(f in expr for f in ('json-to-xml', 'parse-xml', 'analyze-string')):
                    continue
                if names & known:
                    found.setdefault(namespace, []).append(f'{attr}="{expr}"')
    problems = []
    for namespace, where in found.items():
        shown = ', '.join(where[:3]) + (f" and {len(where) - 3} more" if len(where) > 3 else '')
        note = (f" (the JSONParser's output; json-to-xml() output is in {FN_NS})" if namespace == JSON_NS else
                " (a Data Splitter's output)" if namespace == 'records:2' else " (an Events stream)")
        problems.append(f"{shown} select nothing: those elements are in namespace {namespace}{note}, and no "
                        f"xpath-default-namespace is declared. Set xpath-default-namespace=\"{namespace}\" on "
                        f"xsl:stylesheet, or bind a prefix to it. If the input really has no namespace, declare "
                        f"xpath-default-namespace=\"\" to say so.")
    return problems


def _literal_children(node: etree._Element):
    """Literal event-logging elements written directly below node, looking through xsl:if, xsl:choose and so on."""
    for child in node:
        if not isinstance(child.tag, str):
            continue
        if child.tag.startswith(f'{{{EVT}}}'):
            yield child
        elif child.tag in (f'{{{XSL}}}variable', f'{{{XSL}}}param'):
            continue
        else:
            yield from _literal_children(child)


def _event_element_problems(root: etree._Element, schema) -> list[str]:
    """Literal result elements in the event-logging namespace that the schema has no place for, e.g.
    Event/EventDetail/ServerEvent. A fragment written in a named template (an EventSource on its own) is
    accepted wherever the schema allows an element of that name."""
    problems: list[str] = []

    def options_below(options: list[str], name: str) -> tuple[list[str], str | None]:
        kept, reason = [], None
        for option in options:
            if option == 'Events':
                if name == 'Event':
                    kept.append('Event')
                else:
                    reason = f"Events holds Event elements, not {name}"
                continue
            try:
                schema.resolve(f'{option}/{name}')
                kept.append(f'{option}/{name}')
            except ValueError as e:
                reason = str(e)
        return kept, reason

    def walk(node: etree._Element, options: list[str]) -> None:
        for child in _literal_children(node):
            name = etree.QName(child).localname
            kept, reason = options_below(options, name)
            if not kept:
                if len(options) == 1:
                    message = f"{options[0]}/{name} is not in the event-logging schema" + (f": {reason}" if reason else '')
                else:
                    message = f"{name} is not allowed below any of {options[:4]} in the event-logging schema"
                if message not in problems:
                    problems.append(message)
                continue
            walk(child, kept)

    for node in root.iter(f'{{{EVT}}}*'):
        if any(isinstance(a.tag, str) and a.tag.startswith(f'{{{EVT}}}') for a in node.iterancestors()):
            continue
        name = etree.QName(node).localname
        if name in ('Events', 'Event'):
            options = [name]
        else:
            options = schema.paths_named(name)
            if not options:
                problems.append(f"No element '{name}' exists anywhere in the event-logging schema")
                continue
        walk(node, options)
    return problems


async def check_xslt(
        ctx: Context,
        xslt: XsltText,
        schema_version: Annotated[str | None, Field(
            description="Event-logging version the output must follow, e.g. '3.5.2'. Defaults to the Version the "
                        "XSLT writes on Events, else the configured version.")] = None,
) -> dict[str, Any]:
    """
    Check an XSLT before saving or stepping it: well-formed, an xsl:stylesheet with a version, the stroom
    namespace declared and only real stroom: functions used (an unknown one comes back with the nearest real
    name), match and select expressions that would select nothing because the input's namespace is not
    declared, event-logging elements the schema has no place for (e.g. EventDetail/ServerEvent), and
    xsl:import / xsl:include targets that exist as XSLT documents in Stroom. Full compilation happens when
    the pipeline is stepped.
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
    function_errors, calls = _function_problems(xslt, root)
    errors += function_errors
    errors += _namespace_problems(root)
    if root.find(f'.//{{{EVT}}}*') is not None:
        from tools.generation import event_schema
        events = root.find(f'.//{{{EVT}}}Events')
        version = schema_version or (events.get('Version') if events is not None else None) \
            or gateway_from(ctx).settings.event_logging_version
        try:
            schema = await event_schema(ctx, version)
        except ToolError as e:
            warnings.append(f"Event-logging element names not checked: {e}")
        else:
            errors += _event_element_problems(root, schema)
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


async def check_events(
        ctx: Context,
        events_xml: EventsXml,
        schema_version: Annotated[str | None, Field(
            description="Event-logging version to validate against, e.g. '3.5.2'. Defaults to the version the "
                        "document declares in xsi:schemaLocation, else the configured version.")] = None,
) -> dict[str, Any]:
    """
    Check event XML both ways: against the event-logging XSD held in this Stroom instance (each error with
    its line, element path and message), and against the quality rules beyond the schema (TimeCreated a full
    UTC timestamp; System Name, Environment, Generator, Device and TypeId present; exactly one action under
    EventDetail; no empty elements), per rule with the events failing it and examples.
    """
    schema = await validate_events(ctx, events_xml, schema_version)
    quality = await check_event_quality(ctx, events_xml)
    return {'ok': schema['valid'] and quality['ok'], 'schema': schema, 'quality': quality}


ALL_TOOLS = [check_xslt, check_events]
