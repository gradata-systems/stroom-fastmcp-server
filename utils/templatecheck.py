"""Check an Elasticsearch index template against the documents an indexing pipeline writes.

The documents come from stepping the candidate indexing pipeline: its XSLT's JSON XML output, the form
Stroom's ElasticIndexingFilter sends. Each document field is compared with the template's mapping:
missing fields under the template's dynamic setting, values the mapped type cannot take, parent/child
clashes, and fields the template expects but the pipeline never writes (often renamed in the template).
Every mismatch comes back as a change to make, to the pipeline or to the template.
"""
import difflib
import fnmatch
import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

FN = 'http://www.w3.org/2005/xpath-functions'
KEYWORD_LIKE = {'keyword', 'text', 'wildcard', 'match_only_text', 'constant_keyword', 'version', 'search_as_you_type'}
INTEGER = {'long', 'integer', 'short', 'byte', 'unsigned_long'}
DECIMAL = {'double', 'float', 'half_float', 'scaled_float'}
OBJECT = {'object', 'nested', 'flattened'}
_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}(T\d{2}(:\d{2}(:\d{2}(\.\d{1,9})?)?)?(Z|[+-]\d{2}(:?\d{2})?)?)?$')
_INT = re.compile(r'^-?\d+$')
_NUM = re.compile(r'^-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$')


def parse_template(text: str) -> tuple[str | None, dict[str, Any]]:
    """A template as the user pastes it: Dev Tools 'PUT _index_template/name {...}', the body, or GET output."""
    text = text.strip()
    name = None
    first, _, rest = text.partition('\n')
    match = re.match(r'^(PUT|POST)\s+/?_index_template/([^\s?]+)', first.strip(), re.I)
    if match:
        name, text = match.group(2), rest
    try:
        body = json.loads(text)
    except ValueError as e:
        raise ValueError(f"The template is not valid JSON: {e}") from e
    if isinstance(body, dict) and 'index_templates' in body:
        entry = (body['index_templates'] or [{}])[0]
        name, body = entry.get('name', name), entry.get('index_template', {})
    if not isinstance(body, dict) or 'index_patterns' not in body:
        raise ValueError("Expected an index template body with index_patterns (and template.mappings)")
    return name, body


@dataclass
class Mapping:
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)   # path -> spec (type, format, ...)
    dynamic: dict[str, str] = field(default_factory=dict)             # object path ('' for root) -> setting

    def dynamic_for(self, path: str) -> str:
        parts = path.split('.')
        for n in range(len(parts) - 1, -1, -1):
            setting = self.dynamic.get('.'.join(parts[:n]))
            if setting is not None:
                return str(setting).lower()
        return 'true'


def read_mapping(mappings: dict[str, Any]) -> Mapping:
    out = Mapping()
    if 'dynamic' in mappings:
        out.dynamic[''] = mappings['dynamic']

    def walk(properties: dict[str, Any], prefix: str) -> None:
        for name, spec in (properties or {}).items():
            path = f'{prefix}.{name}' if prefix else name
            if 'dynamic' in spec:
                out.dynamic[path] = spec['dynamic']
            if 'properties' in spec:
                out.fields[path] = {'type': spec.get('type', 'object')}
                walk(spec['properties'], path)
            else:
                out.fields[path] = spec
    walk(mappings.get('properties'), '')
    return out


def merge_mappings(*mappings: dict[str, Any]) -> dict[str, Any]:
    """Component templates first, then the template's own mappings, as Elasticsearch composes them."""
    merged: dict[str, Any] = {'properties': {}}

    def deep(target: dict, source: dict) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                deep(target[key], value)
            else:
                target[key] = value
    for m in mappings:
        deep(merged, m or {})
    return merged


def json_xml_documents(xml: str) -> list[dict[str, Any]]:
    """Documents from an indexing XSLT's output: <array> of <map>, or a single <map>."""
    try:
        root = etree.fromstring(re.sub(r'^\s*<\?xml[^>]*\?>', '', xml).encode('utf-8'))
    except etree.XMLSyntaxError:
        return []

    def value(node: etree._Element) -> Any:
        kind = etree.QName(node).localname
        if kind == 'map':
            return {c.get('key'): value(c) for c in node if isinstance(c.tag, str)}
        if kind == 'array':
            return [value(c) for c in node if isinstance(c.tag, str)]
        if kind == 'null':
            return None
        text = node.text or ''
        return ('number', text) if kind == 'number' else ('boolean', text) if kind == 'boolean' else ('string', text)
    data = value(root)
    return [d for d in (data if isinstance(data, list) else [data]) if isinstance(d, dict)]


def flatten_document(doc: dict[str, Any], prefix: str = '') -> dict[str, list[Any]]:
    """path -> values, with dotted keys and nested maps both becoming dotted paths; maps as 'object'."""
    out: dict[str, list[Any]] = {}

    def add(path: str, item: Any) -> None:
        if isinstance(item, dict):
            out.setdefault(path, []).append(('object', None))
            for key, sub in item.items():
                add(f'{path}.{key}', sub)
        elif isinstance(item, list):
            for sub in item:
                add(path, sub)
        elif item is not None:
            out.setdefault(path, []).append(item)
    for key, item in doc.items():
        add(f'{prefix}.{key}' if prefix else key, item)
    return out


def _fits(spec: dict[str, Any], kind: str, text: str) -> str | None:
    """None if the value suits the mapped type, else why not."""
    mapped = spec.get('type', 'object')
    if mapped in OBJECT:
        return None if kind == 'object' else f"is a single value, but the template maps it as {mapped}"
    if kind == 'object':
        return f"is an object, but the template maps it as {mapped}"
    if mapped in KEYWORD_LIKE:
        if mapped == 'constant_keyword' and spec.get('value') is not None and text != spec['value']:
            return f"is {text!r}, but constant_keyword only takes {spec['value']!r}"
        return None
    if mapped in INTEGER:
        return None if _INT.match(text) else f"is {text!r}, not a whole number ({mapped})"
    if mapped in DECIMAL:
        return None if _NUM.match(text) else f"is {text!r}, not a number ({mapped})"
    if mapped == 'boolean':
        return None if text in ('true', 'false') else f"is {text!r}, not true/false"
    if mapped == 'ip':
        try:
            ipaddress.ip_address(text)
            return None
        except ValueError:
            return f"is {text!r}, not an IP address"
    if mapped in ('date', 'date_nanos'):
        formats = [f.strip() for f in str(spec.get('format', 'strict_date_optional_time||epoch_millis')).split('||')]
        checks = {'strict_date_optional_time': _ISO, 'date_optional_time': _ISO, 'strict_date_time': _ISO,
                  'date_time': _ISO, 'strict_date_optional_time_nanos': _ISO, 'epoch_millis': _INT, 'epoch_second': _INT}
        known = [checks[f] for f in formats if f in checks]
        if len(known) < len(formats):
            return None   # a custom format: reported separately as unchecked
        return None if any(p.match(text) for p in known) else f"is {text!r}, which date format {'||'.join(formats)} rejects"
    return None


def compare(body: dict[str, Any], docs: list[dict[str, Any]], index_name: str | None,
            component_mappings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    blocking, changes, notes = [], [], []
    patterns = body.get('index_patterns') or []
    patterns = [patterns] if isinstance(patterns, str) else patterns
    if index_name and not any(fnmatch.fnmatchcase(index_name, p) for p in patterns):
        blocking.append(f"index_patterns {patterns} do not match the pipeline's index '{index_name}', so this template "
                        f"would not apply to it")
        changes.append({'field': None, 'problem': f"template does not cover index '{index_name}'",
                        'change': f"change index_patterns to include '{index_name}*', or set the indexing pipeline's "
                                  f"indexName to an index they match"})
    mapping = read_mapping(merge_mappings(*(component_mappings or []),
                                          (body.get('template') or {}).get('mappings') or {}))
    for path, spec in mapping.fields.items():
        fmt = spec.get('format')
        if spec.get('type') in ('date', 'date_nanos') and fmt and any(
                f.strip() not in ('strict_date_optional_time', 'date_optional_time', 'strict_date_time', 'date_time',
                                  'strict_date_optional_time_nanos', 'epoch_millis', 'epoch_second')
                for f in str(fmt).split('||')):
            notes.append(f"{path}: custom date format {fmt!r} not checked here; stepping output is ISO 8601")
    emitted: dict[str, list[Any]] = {}
    for doc in docs:
        for path, values in flatten_document(doc).items():
            emitted.setdefault(path, []).extend(values)
    if body.get('data_stream') is not None and '@timestamp' not in emitted:
        blocking.append("the template makes a data stream, which needs @timestamp, but the pipeline does not write it")
        changes.append({'field': '@timestamp', 'problem': 'missing for a data stream',
                        'change': "write @timestamp from EventTime/TimeCreated in the indexing XSLT"})

    unmapped = []
    for path in sorted(emitted):
        values = emitted[path]
        spec = mapping.fields.get(path)
        if spec is None:
            parent = next((p for p in _parents(path) if p in mapping.fields
                           and mapping.fields[p].get('type', 'object') not in OBJECT), None)
            if parent:
                blocking.append(f"{path}: the template maps '{parent}' as {mapping.fields[parent]['type']}, so it "
                                f"cannot also hold '{path}'")
                changes.append({'field': path, 'problem': f"'{parent}' is a {mapping.fields[parent]['type']}",
                                'change': f"rename '{path}' in the indexing XSLT, or map '{parent}' as an object"})
                continue
            if all(kind == 'object' for kind, _ in values):
                continue   # an object: its leaves are checked on their own
            unmapped.append(path)
            continue
        bad = next(((text, why) for kind, text in values for why in [_fits(spec, kind, text or '')] if why), None)
        if bad:
            blocking.append(f"{path} {bad[1]}")
            changes.append({'field': path, 'problem': bad[1],
                            'change': f"change the indexing XSLT to write '{path}' as a valid {spec.get('type')}, or "
                                      f"map it with a type that takes these values"})

    expected = [p for p, s in mapping.fields.items() if s.get('type', 'object') not in OBJECT and p not in emitted]
    for path in unmapped:
        dynamic = mapping.dynamic_for(path)
        renamed = difflib.get_close_matches(path, expected, n=1, cutoff=0.6)
        if renamed:
            changes.append({'field': path, 'problem': f"not in the template, which has '{renamed[0]}' instead",
                            'change': f"rename '{path}' to '{renamed[0]}' in the indexing XSLT"})
            expected.remove(renamed[0])
            if dynamic == 'strict':
                blocking.append(f"{path}: not in the template and dynamic is strict, so documents are rejected")
            continue
        if dynamic == 'strict':
            blocking.append(f"{path}: not in the template and dynamic is strict, so documents are rejected")
            changes.append({'field': path, 'problem': 'not mapped (dynamic: strict)',
                            'change': f"add '{path}' to the template, or stop writing it in the indexing XSLT"})
        elif dynamic in ('false', 'runtime'):
            notes.append(f"{path}: written by the pipeline but not mapped (dynamic: {dynamic}), so it is kept in "
                         f"_source but not searchable")
        else:
            notes.append(f"{path}: not mapped; Elasticsearch will add it with a guessed type (dynamic mapping)")
    for path in expected:
        changes.append({'field': path, 'problem': 'in the template but the pipeline never writes it',
                        'change': f"add '{path}' to the indexing XSLT (from the right event-logging path), or drop "
                                  f"it from the template"})
    return {'compatible': not blocking, 'blocking': blocking, 'pipeline_changes': changes, 'notes': notes,
            'documents_checked': len(docs), 'fields_written': sorted(emitted)}


def _parents(path: str) -> list[str]:
    parts = path.split('.')
    return ['.'.join(parts[:n]) for n in range(len(parts) - 1, 0, -1)]
