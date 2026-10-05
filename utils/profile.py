"""Profile a raw data sample: format, structure, fields, value types and timestamp patterns."""
import csv
import io
import ipaddress
import json
import re
from collections import Counter
from typing import Any

from lxml import etree

from utils.timefmt import check_time_format, infer_time_pattern

SYSLOG_5424 = re.compile(r'^<\d{1,3}>1 \S+ \S+ \S+ \S+ \S+')
SYSLOG_3164 = re.compile(r'^(<\d{1,3}>)?[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2} \S+ ')
KEY_VALUE = re.compile(r'(\w[\w.-]*)=("[^"]*"|\S*)')
# Timestamp shapes and the Java (stroom:format-date) pattern for each.
TIMESTAMPS = [
    (re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$'), "yyyy-MM-dd'T'HH:mm:ss.SSSX"),
    (re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}$'), "yyyy-MM-dd'T'HH:mm:ss.SSSXXX"),
    (re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$'), "yyyy-MM-dd'T'HH:mm:ssX"),
    (re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$'), "yyyy-MM-dd'T'HH:mm:ssXXX"),
    (re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$'), "yyyy-MM-dd'T'HH:mm:ss"),
    (re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$'), 'yyyy-MM-dd HH:mm:ss'),
    (re.compile(r'^\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}$'), 'dd/MMM/yyyy:HH:mm:ss Z'),
    (re.compile(r'^[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}$'), 'MMM d HH:mm:ss'),
    (re.compile(r'^1\d{9}$'), 'epoch seconds'),
    (re.compile(r'^1\d{12}$'), 'epoch milliseconds'),
]


# How a JSON sample reaches the XSLT. The JSONParser reads every top-level value in the stream and, with
# addRootObject (its default), wraps them all in one <map>; so JSON lines parse as /map/map with no text
# converter, and an array as /map/array/map, or /array/map with addRootObject off.
JSON_SETUP = {
    'lines': {'text_converter': 'none: the JSONParser element parses JSON lines itself; a text converter cannot',
              'parser_properties': {'jsonParser.addRootObject': True},
              'xslt_input': {'namespace': 'http://www.w3.org/2013/XSL/json', 'root': '/map', 'record': 'map',
                             'mapping': {'input': 'json', 'json_layout': 'lines'}}},
    'array': {'text_converter': 'none: the JSONParser element parses the array itself; a text converter cannot',
              'parser_properties': {'jsonParser.addRootObject': False},
              'xslt_input': {'namespace': 'http://www.w3.org/2013/XSL/json', 'root': '/array', 'record': 'map',
                             'mapping': {'input': 'json', 'json_layout': 'array'}}},
}


# The wrapper an XMLFragmentParser puts round XML fragments (its text converter, type XML_FRAGMENT): the entity
# `fragment` is the stream. Fragments that declare no namespace of their own take the wrapper's default, records:2.
XML_FRAGMENT_WRAPPER = """<?xml version="1.1" encoding="UTF-8"?>
<!DOCTYPE records [
<!ENTITY fragment SYSTEM "fragment">
]>
<records xmlns="records:2" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="records:2 file://records-v2.0.xsd" version="2.0">
&fragment;
</records>
"""
_XML_DECL = re.compile(r'^\s*<\?xml[^>]*\?>')


def xml_fragments(text: str) -> list | None:
    """The top-level elements of text that is XML fragments (several root elements, e.g. one <Event> per line);
    None for a single document or anything that is not XML."""
    body = _XML_DECL.sub('', text, count=1)
    try:
        root = etree.fromstring(f'<fragments>{body}</fragments>'.encode('utf-8'))
    except (etree.XMLSyntaxError, ValueError):
        return None
    elements = [c for c in root if isinstance(c.tag, str)]
    return elements if len(elements) > 1 else None


def xml_fragment_setup(namespace: str | None, record: str) -> dict[str, Any]:
    effective = namespace or 'records:2'
    return {
        'text_converter': {'type': 'XML_FRAGMENT', 'code': XML_FRAGMENT_WRAPPER,
                           'note': "The wrapper the XMLFragmentParser puts round the fragments (its textConverter); "
                                   "use the template's own wrapper if it already sets one."},
        'parser': "XMLFragmentParser: a template whose chain has one (child_must_supply names its textConverter), "
                  "else create_pipeline from Event Data (XML) with replace_parser='XMLFragmentParser'",
        'xslt_input': {'namespace': effective, 'root': '/', 'record': f'*/{record}',
                       'note': ("The fragments declare no namespace, so inside the wrapper they take its default "
                                "namespace, records:2: set xml_namespace to that." if not namespace else
                                "The fragments declare their own namespace and keep it inside the wrapper."),
                       'mapping': {'input': 'xml_fragments', 'xml_namespace': effective, 'record': record}},
    }


def _xml_record_fields(record: etree._Element) -> dict[str, str]:
    """A record's leaf texts by element name, and its attributes as name@attribute."""
    out: dict[str, str] = {}
    for node in record.iter():
        if not isinstance(node.tag, str):
            continue
        name = etree.QName(node).localname
        for attr, value in node.attrib.items():
            out[f'{name}@{etree.QName(attr).localname}'] = value
        if len(node) == 0 and (node.text or '').strip():
            out[name] = node.text.strip()
    return out


def value_type(value: Any) -> str:
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, (dict, list)):
        return 'object' if isinstance(value, dict) else 'array'
    text = str(value).strip()
    if not text:
        return 'empty'
    if 6 <= len(text) <= 40 and any(ch.isdigit() for ch in text):
        pattern = infer_time_pattern(text)
        if pattern and check_time_format(pattern, [text]) is None:
            return f'timestamp ({pattern})'
    try:
        ipaddress.ip_address(text)
        return 'ip'
    except ValueError:
        pass
    if re.fullmatch(r'-?\d+', text):
        return 'integer'
    if re.fullmatch(r'-?\d+\.\d+', text):
        return 'decimal'
    if text.lower() in ('true', 'false'):
        return 'boolean'
    if (text.startswith('{') and text.endswith('}')) or (text.startswith('[') and text.endswith(']')):
        try:
            json.loads(text)
            return 'embedded json'
        except ValueError:
            pass
    if re.search(r'\{.+\}$', text):
        try:
            json.loads(text[text.index('{'):])
            return 'embedded json after prefix'
        except ValueError:
            pass
    return 'string'


def _flatten(obj: Any, prefix: str = '', depth: int = 0) -> dict[str, Any]:
    if isinstance(obj, dict) and depth < 4:
        out = {}
        for key, value in obj.items():
            out.update(_flatten(value, f'{prefix}.{key}' if prefix else str(key), depth + 1))
        return out
    return {prefix or '(value)': obj}


def _inventory(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for record in records:
        for name, value in record.items():
            entry = fields.setdefault(name, {'field': name, 'present': 0, 'types': Counter(), 'examples': []})
            entry['present'] += 1
            entry['types'][value_type(value)] += 1
            text = value if isinstance(value, str) else json.dumps(value)
            if text not in entry['examples'] and len(entry['examples']) < 3:
                entry['examples'].append(text[:120])
    total = len(records) or 1
    return [{'field': e['field'], 'fill_rate': round(100 * e['present'] / total),
             'type': e['types'].most_common(1)[0][0], 'examples': e['examples']} for e in fields.values()]


def profile_many(samples: dict[str, str], max_records: int = 200) -> dict[str, Any]:
    """Several sample files of one source, profiled each and together: the fields' fill rates per file, fields and
    timestamp shapes only some files have (what breaks a mapping built from one file), and whether the files
    even share a format."""
    profiles = {name: profile(text, max_records) for name, text in samples.items()}
    formats = {p['format'] for p in profiles.values()}
    merged: dict[str, dict[str, Any]] = {}
    for name, p in profiles.items():
        for f in p.get('fields') or []:
            entry = merged.setdefault(f['field'], {'field': f['field'], 'files': {}, 'types': Counter(), 'examples': []})
            entry['files'][name] = f['fill_rate']
            entry['types'][f['type']] += 1
            entry['examples'] += [e for e in f['examples'] if e not in entry['examples']][:3 - len(entry['examples'])]
    fields = []
    differences = []
    for entry in merged.values():
        missing = [n for n in profiles if n not in entry['files']]
        types = [t for t in entry['types']]
        fields.append({'field': entry['field'], 'fill_rate_by_file': entry['files'], 'type': types[0],
                       'examples': entry['examples'], **({'only_in': sorted(entry['files'])} if missing else {}),
                       **({'types_by_file': types} if len(types) > 1 else {})})
        if missing:
            differences.append(f"field '{entry['field']}' is only in {sorted(entry['files'])}")
        if len(types) > 1 and any(t.startswith('timestamp') for t in types):
            differences.append(f"field '{entry['field']}' has different timestamp shapes across files: {types}")
    if len(formats) > 1:
        differences.insert(0, f"the files are not one format: { {n: p['format'] for n, p in profiles.items()} }")
    first = next(iter(profiles.values()))
    setup = {k: first[k] for k in ('suggested_parser', 'text_converter', 'parser_properties', 'xslt_input', 'parser')
             if k in first}
    return {'files': {n: {k: v for k, v in p.items() if k in ('format', 'records', 'lines', 'note', 'delimiter', 'has_header',
                                                               'record_element', 'namespace')} for n, p in profiles.items()},
            'format': first['format'] if len(formats) == 1 else 'mixed', 'records': sum(p.get('records', 0) for p in profiles.values()),
            'fields': fields, 'differences': differences, **setup,
            'hint': ("Map every field a rule needs with any_of where files name it differently, and give rules for the "
                     "kinds of event each file shows; upload each file as its own stream and step them all."
                     if differences else "The files agree; upload each as its own stream and step them all.")}


def _array_head(text: str, limit: int) -> list[Any] | None:
    """The complete items at the start of a JSON array whose text was cut short (a stream read up to a limit), or
    None when what comes before the cut isn't a JSON array. Seen: a 755 KB one-line array read to 20,000 characters
    profiled as key=value (its records' body field), and create_pipeline refused the JSON parser it needed."""
    decoder = json.JSONDecoder()
    items, at = [], text.index('[') + 1
    while len(items) < limit:
        while at < len(text) and text[at] in ' \t\r\n,':
            at += 1
        if at >= len(text) or text[at] == ']':
            break
        try:
            item, at = decoder.raw_decode(text, at)
        except ValueError:
            break       # the cut, or not JSON at all: what was read so far decides
        items.append(item)
    return items or None


def profile(sample: str, max_records: int = 200) -> dict[str, Any]:
    text = sample.strip('﻿\r\n ')
    lines = [line for line in text.splitlines() if line.strip()]
    result: dict[str, Any] = {'characters': len(sample), 'lines': len(lines)}

    if text.startswith('<') and not SYSLOG_5424.match(text) and not SYSLOG_3164.match(text):
        try:
            root = etree.fromstring(text.encode('utf-8'))
            children = Counter(etree.QName(c).localname for c in root if isinstance(c.tag, str))
            record_tag = children.most_common(1)[0][0] if children else etree.QName(root).localname
            records = [_xml_record_fields(rec) for rec in root if isinstance(rec.tag, str)][:max_records]
            return {**result, 'format': 'xml', 'root': etree.QName(root).localname, 'namespace': etree.QName(root).namespace,
                    'record_element': record_tag, 'records': sum(children.values()), 'fields': _inventory(records),
                    'suggested_parser': 'XMLParser (Event Data (XML) template)', 'text_converter': 'none: the XMLParser reads the document'}
        except etree.XMLSyntaxError:
            fragments = xml_fragments(text)
            if fragments:
                children = Counter(etree.QName(c).localname for c in fragments)
                record_tag = children.most_common(1)[0][0]
                namespace = etree.QName(fragments[0]).namespace
                return {**result, 'format': 'xml fragments', 'record_element': record_tag, 'namespace': namespace,
                        'records': len(fragments), 'fields': _inventory([_xml_record_fields(f) for f in fragments[:max_records]]),
                        'suggested_parser': 'XMLFragmentParser with an XML_FRAGMENT text converter (the wrapper below); '
                                            'no root element, so the XMLParser cannot read it',
                        **xml_fragment_setup(namespace, record_tag)}
            result['note'] = 'Starts with < but is not well-formed XML, as a document or as fragments'

    if text.startswith('['):
        try:
            data = json.loads(text)
            records = [_flatten(r) for r in data[:max_records] if isinstance(r, dict)]
            return {**result, 'format': 'json array', 'records': len(data), 'fields': _inventory(records),
                    'suggested_parser': 'JSONParser (Event Data (JSON) template)', **JSON_SETUP['array']}
        except ValueError:
            head = _array_head(text, max_records)
            if head and all(isinstance(r, dict) for r in head):
                return {**result, 'format': 'json array', 'records': len(head), 'fields': _inventory(
                    [_flatten(r) for r in head]), 'note': 'the array is cut short here: these are its first records',
                    'suggested_parser': 'JSONParser (Event Data (JSON) template)', **JSON_SETUP['array']}
    json_lines = []
    for line in lines[:max_records]:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                json_lines.append(obj)
        except ValueError:
            break
    # A last line cut short by a read limit doesn't make JSON lines something else.
    whole = min(len(lines), max_records)
    if json_lines and (len(json_lines) == whole or (len(json_lines) == whole - 1 and len(lines) <= max_records
                                                    and len(json_lines) >= 2)):
        return {**result, 'format': 'json lines', 'records': len(lines), 'fields': _inventory([_flatten(r) for r in json_lines]),
                'suggested_parser': 'JSONParser, one object per line (Event Data (JSON) template)', **JSON_SETUP['lines']}

    if sum(bool(SYSLOG_5424.match(line)) for line in lines) >= 0.8 * len(lines):
        return {**result, 'format': 'syslog rfc5424', 'records': len(lines), 'examples': lines[:3],
                'suggested_parser': 'Data Splitter with a regex per RFC 5424 part (Event Data (Text) template)'}
    if sum(bool(SYSLOG_3164.match(line)) for line in lines) >= 0.8 * len(lines):
        return {**result, 'format': 'syslog rfc3164', 'records': len(lines), 'examples': lines[:3],
                'suggested_parser': 'Data Splitter with a regex for PRI, timestamp, host, tag and message'}

    kv = [dict((k, v.strip('"')) for k, v in KEY_VALUE.findall(line)) for line in lines[:max_records]]
    if kv and sum(len(r) >= 3 for r in kv) >= 0.8 * len(kv):
        return {**result, 'format': 'key=value', 'records': len(lines), 'fields': _inventory(kv),
                'suggested_parser': 'Data Splitter splitting on spaces then = (Event Data (Text) template)'}

    try:
        dialect = csv.Sniffer().sniff('\n'.join(lines[:20]), delimiters=',\t|;')
        has_header = csv.Sniffer().has_header('\n'.join(lines[:20]))
        rows = list(csv.reader(io.StringIO('\n'.join(lines[:max_records + 1])), dialect))
        header = rows[0] if has_header else [f'col{i + 1}' for i in range(len(rows[0]))]
        body = rows[1:] if has_header else rows
        records = [dict(zip(header, row)) for row in body]
        return {**result, 'format': 'delimited', 'delimiter': dialect.delimiter, 'has_header': has_header,
                'columns': header, 'records': len(lines) - (1 if has_header else 0), 'fields': _inventory(records),
                'suggested_parser': 'Data Splitter for delimited data' + (' with a header row' if has_header else '')
                                    + ' (Event Data (Text) template)'}
    except csv.Error:
        pass
    return {**result, 'format': 'unknown text', 'examples': lines[:5],
            'suggested_parser': 'Data Splitter with regexes; ask the user for the record structure'}
