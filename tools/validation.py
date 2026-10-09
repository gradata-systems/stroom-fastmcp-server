"""Local validation of XSLT and event XML: well-formedness, schema, quality and field mapping."""
import asyncio
import difflib
import html
import re
from collections import Counter
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import Field

from utils.params import ONE_OR_MORE
from utils.schemas import SchemaCache, declared_system_id, errors_for, event_logging_system_id
from utils.stroom import gateway_from

XSL = 'http://www.w3.org/1999/XSL/Transform'
EVT = 'event-logging:3'
JSON_NS = 'http://www.w3.org/2013/XSL/json'
EVENT_LOGGING_NS = 'event-logging:3'
RECORDS_NS = 'records:2'
RECORDS_SYSTEM_ID = 'file://records-v2.0.xsd'
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

_ESCAPED = "It came HTML-escaped (&lt; for <) and was read unescaped: send XML as it is, not escaped."


def _unescaped(text: str) -> tuple[str, list[str]]:
    """XML sent HTML-escaped, with no < anywhere (seen: Qwen in VS Code, answered only "Start tag expected"): read
    unescaped, and said so."""
    if '<' not in text and text.lstrip().startswith('&lt;'):
        return html.unescape(text), [_ESCAPED]
    return text, []
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


def _namespace_problems(root: etree._Element, input_namespace: str | None = None) -> list[str]:
    """Bare element names of a parser's output read with no xpath-default-namespace. input_namespace '' (the
    build's sample is XML in no namespace): its own records, record or data elements are what they say, not a Data
    Splitter's records:2."""
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
    if input_namespace == '':
        found.pop(RECORDS_NS, None)
    problems = []
    for namespace, where in found.items():
        shown = ', '.join(where[:3]) + (f" and {len(where) - 3} more" if len(where) > 3 else '')
        note = (f" (the JSONParser's output; json-to-xml() output is in {FN_NS})" if namespace == JSON_NS else
                " (a Data Splitter's output, or XML that declares records:2)" if namespace == 'records:2'
                else " (an Events stream)")
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


_JSON_CALL = re.compile(r'(?<![\w-])(?:\w+:)?json-to-xml\(')


def _unguarded_json(root: etree._Element) -> list[str]:
    """json-to-xml on a value that may be empty or not JSON stops processing with a fatal error ("empty sequence",
    seen in a test environment): each call outside an xsl:try, in an expression without an if guard."""
    found = []
    for el in root.iter():
        if not isinstance(el.tag, str) or any(a.tag == f'{{{XSL}}}try' for a in el.iterancestors()):
            continue
        for name, value in el.attrib.items():
            if _JSON_CALL.search(value) and not re.search(r'\bif\s*\(', value):
                found.append(f"{name}=\"{value[:80]}\"")
    if not found:
        return []
    return [f"json-to-xml without a guard ({'; '.join(found[:3])}): a record whose value is empty, or isn't JSON, stops "
            f"processing with a fatal error. Guard it: select=\"if (normalize-space(x)) then json-to-xml(x) else ()\", "
            f"inside xsl:try with an empty xsl:catch for text that isn't JSON. build_translation_xslt does this itself."]


async def check_xslt(
        ctx: Context,
        xslt: XsltText,
        schema_version: Annotated[str | None, Field(
            description="Event-logging version the output must follow, e.g. '3.5.2'. Defaults to the Version the "
                        "XSLT writes on Events, else the configured version.")] = None,
        input_namespace: Annotated[str | None, Field(
            description="The namespace of the XSLT's input when known ('' for a source's XML in no namespace, whose "
                        "own records/record elements are then not taken for a Data Splitter's records:2).")] = None,
) -> dict[str, Any]:
    """
    Check an XSLT before saving or stepping it: well-formed, an xsl:stylesheet with a version, the stroom
    namespace declared and only real stroom: functions used (an unknown one comes back with the nearest real
    name), match and select expressions that would select nothing because the input's namespace is not
    declared, event-logging elements the schema has no place for (e.g. EventDetail/ServerEvent), and
    xsl:import / xsl:include targets that exist as XSLT documents in Stroom. Full compilation happens when
    the pipeline is stepped.
    """
    xslt, escaped = _unescaped(xslt)
    try:
        root = etree.fromstring(xslt.encode('utf-8'))
    except etree.XMLSyntaxError as e:
        return {'ok': False, 'errors': [f"Not well-formed XML (line {e.lineno}): {e.msg}"], 'warnings': escaped}
    errors, warnings = [], list(escaped)
    if root.tag != f'{{{XSL}}}stylesheet' and root.tag != f'{{{XSL}}}transform':
        errors.append("Root element must be xsl:stylesheet")
    if not root.get('version'):
        errors.append("xsl:stylesheet needs a version attribute (Stroom supports 2.0 and 3.0)")
    function_errors, calls = _function_problems(xslt, root)
    errors += function_errors
    errors += _namespace_problems(root, input_namespace)
    warnings += _unguarded_json(root)
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
    with its line, element path and message. A records:2 document (a Data Splitter's output, a Lucene indexing
    XSLT's) is validated against the records schema instead; any other XML (a source's own, say <records> with no
    namespace) is not Events, and is said to be so rather than failing every element.
    """
    stroom = gateway_from(ctx)
    root = _parse(events_xml, 'Events XML')
    namespace, name = etree.QName(root).namespace, etree.QName(root).localname
    if namespace not in (EVENT_LOGGING_NS, RECORDS_NS):
        return {'valid': False, 'schema': None, 'error_count': 1, 'errors': [{
            'line': None, 'path': f'/{name}', 'message': (
                f"Not an Events document: its root is <{name}> in {f'namespace {namespace}' if namespace else 'no namespace'}, "
                f"neither event-logging:3 Events nor records:2 records. A source's own XML (records of its own, say) is "
                f"the input to a translation: validate what the translation writes (step_pipeline's output).")}]}
    declared = declared_system_id(events_xml)
    if namespace == RECORDS_NS:
        # Records are records: their own schema, whatever version they declare, else the one Stroom ships.
        system_id = declared if declared and 'records' in declared else RECORDS_SYSTEM_ID
    else:
        system_id = (event_logging_system_id(schema_version) if schema_version
                     else declared or event_logging_system_id(stroom.settings.event_logging_version))
    cache = ctx.lifespan_context.setdefault('schemas', SchemaCache(stroom))
    schema = await cache.get(system_id)
    errors = errors_for(schema, root)
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
    unknown = []
    for index, event in enumerate(events):
        for rule, message in _event_findings(event):
            entry = rules.setdefault(rule, {'events_failing': set(), 'examples': []})
            entry['events_failing'].add(index)
            if len(entry['examples']) < 3:
                entry['examples'].append({'event': index, 'message': message})
        if event.find(f'{{{EVT}}}EventDetail/{{{EVT}}}Unknown') is not None:
            unknown.append(index)
    result = {'events_checked': len(events), 'ok': not rules,
              'rules': {r: {'events_failing': len(v['events_failing']), 'examples': v['examples']}
                        for r, v in sorted(rules.items())}}
    if unknown:
        # Advice, not a failure: Unknown is right for records no action element describes.
        result['notes'] = [f"{len(unknown)} of {len(events)} events (e.g. {unknown[:3]}) have EventDetail/Unknown: what "
                           f"happened is not known. If those records are an activity another action element describes "
                           f"(Alert, Authenticate, Network, Process, Create, Update, Delete, View, ...), use it."]
    return result


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


async def _stream_events(ctx: Context, stream_ids: list[int]) -> tuple[str, dict[str, Any]]:
    """The Events of processed streams as one <Events> document, read by the server (up to max_sample_records in
    all), and what was read: seen, an agent reading 50 events back 23 at a time, 47 s a read, to check them."""
    from tools.streams import _root_closed
    stroom = gateway_from(ctx)
    cap = stroom.settings.max_sample_records
    merged, read = None, {}
    for stream_id in stream_ids:
        first = await stroom.fetch_data(stream_id, 0, 1)
        if first.get('errors'):
            raise ToolError(f"Stroom could not read stream {stream_id}: {'; '.join(first['errors'])}")
        if first.get('streamTypeName') not in (None, 'Events'):
            raise ToolError(f"Stream {stream_id} is {first.get('streamTypeName')}, not Events: give the Events streams "
                            f"processing wrote (wait_for_processing lists them)")
        total = (first.get('totalItemCount') or {}).get('count') or 1
        take = min(total, cap - sum(r['checked'] for r in read.values()))
        bodies = [first]
        for start in range(1, take, 20):
            bodies += await asyncio.gather(*(stroom.fetch_data(stream_id, i, 1) for i in range(start, min(start + 20, take))))
        for body in bodies[:take]:
            root = _parse(_root_closed(body.get('data') or ''), f'stream {stream_id}')
            if merged is None:
                merged = root
            else:
                merged.extend(list(root))
        read[stream_id] = {'checked': max(take, 0), 'records': total}
        if sum(r['checked'] for r in read.values()) >= cap:
            break
    if merged is None:
        raise ToolError("No Events records to check in those streams")
    return etree.tostring(merged, encoding='unicode'), read


async def check_events(
        ctx: Context,
        events_xml: Annotated[str | None, Field(
            description="An <Events> document, e.g. step output. For what processing wrote, give stream_ids "
                        "instead.")] = None,
        schema_version: Annotated[str | None, Field(
            description="Event-logging version to validate against, e.g. '3.5.2'. Defaults to the version the "
                        "document declares in xsi:schemaLocation, else the configured version.")] = None,
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="Events streams processing wrote (wait_for_processing lists them): the server reads and "
                        "checks their events itself, up to the server's max_sample_records in all, so they need "
                        "not be read and sent back.")] = [],
) -> dict[str, Any]:
    """
    Check event XML both ways: against the event-logging XSD held in this Stroom instance (each error with
    its line, element path and message), and against the quality rules beyond the schema (TimeCreated a full
    UTC timestamp; System Name, Environment, Generator, Device and TypeId present; exactly one action under
    EventDetail; no empty elements), per rule with the events failing it and examples. Give events_xml, or
    stream_ids for Events streams in Stroom.
    """
    if bool(events_xml) == bool(stream_ids):
        raise ToolError("Give events_xml (an <Events> document) or stream_ids (Events streams), not both")
    if stream_ids:
        events_xml, read = await _stream_events(ctx, stream_ids)
        return {**await check_events(ctx, events_xml, schema_version), 'read': read}
    events_xml, escaped = _unescaped(events_xml)
    if escaped:
        return {**await check_events(ctx, events_xml, schema_version), 'note': _ESCAPED}
    schema = await validate_events(ctx, events_xml, schema_version)
    which = schema.get('schema', '')
    if which is None or 'records' in which:
        # Not Events (records:2 records, or a source's own XML): the event quality rules don't apply.
        return {'ok': schema['valid'], 'schema': schema,
                'quality': {'ok': schema['valid'], 'note': "not event-logging Events: the event quality rules don't apply"}}
    quality = await check_event_quality(ctx, events_xml)
    return {'ok': schema['valid'] and quality['ok'], 'schema': schema, 'quality': quality}


async def describe_event_element(ctx: Context, path: str = '', version: str | None = None) -> dict[str, Any]:
    """What an event-logging element takes, from the schema in this Stroom: its children in order, each with whether
    it is required or repeatable, whether it is one of a choice (and whether one of that choice must be given), and
    for a leaf its type and allowed values, each with the schema's description (describe_document's element=).
    Seen: Qwen in VS Code spent twelve minutes writing PowerShell to read these out of the XSD."""
    from tools.generation import event_schema
    version = version or gateway_from(ctx).settings.event_logging_version
    schema = await event_schema(ctx, version)
    try:
        chain = schema.resolve(path)
    except ValueError as e:
        raise ToolError(str(e))
    decl = chain[-1].decl if chain else schema.event
    where = '/'.join(['Event'] + [c.name for c in chain])
    result: dict[str, Any] = {'path': where, 'schema_version': version}
    if chain and schema.describe(chain):
        result['description'] = schema.describe(chain)
    if schema.is_leaf(decl):
        result['type'] = schema.base_type(decl)
        if schema.enumeration(decl):
            result['values'] = schema.enumeration(decl)
        return result
    children, choices = [], {}
    for child in schema.children(decl):
        item: dict[str, Any] = {'name': child.name}
        if child.required:
            item['required'] = True
        if child.repeatable:
            item['repeatable'] = True
        if child.choice is not None:
            item['choice'] = child.choice
            choices.setdefault(child.choice, []).append(child.name)
        if schema.is_leaf(child.decl):
            item['type'] = schema.base_type(child.decl)
            if schema.enumeration(child.decl):
                item['values'] = schema.enumeration(child.decl)
        described = schema.describe(chain + [child])
        if described:
            item['description'] = described
        children.append(item)
    result['children'] = children
    if choices:
        result['choices'] = [{'choice': n, 'one_of': names, 'one_required': n in schema.required_choices}
                             for n, names in choices.items()]
    return result


ALL_TOOLS = [check_xslt, check_events]
