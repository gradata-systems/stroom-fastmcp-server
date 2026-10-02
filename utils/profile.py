"""Profile a raw data sample: format, structure, fields, value types and timestamp patterns."""
import csv
import io
import ipaddress
import json
import re
from collections import Counter
from typing import Any

from lxml import etree

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
    for pattern, java in TIMESTAMPS:
        if pattern.match(text):
            return f'timestamp ({java})'
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


def profile(sample: str, max_records: int = 200) -> dict[str, Any]:
    text = sample.strip('﻿\r\n ')
    lines = [line for line in text.splitlines() if line.strip()]
    result: dict[str, Any] = {'characters': len(sample), 'lines': len(lines)}

    if text.startswith('<') and not SYSLOG_5424.match(text) and not SYSLOG_3164.match(text):
        try:
            root = etree.fromstring(text.encode('utf-8'))
            children = Counter(etree.QName(c).localname for c in root if isinstance(c.tag, str))
            record_tag = children.most_common(1)[0][0] if children else etree.QName(root).localname
            records = [{etree.QName(n).localname: (n.text or '').strip() for n in rec.iter() if isinstance(n.tag, str)
                        and len(n) == 0} for rec in root if isinstance(rec.tag, str)][:max_records]
            return {**result, 'format': 'xml', 'root': etree.QName(root).localname, 'namespace': etree.QName(root).namespace,
                    'record_element': record_tag, 'records': sum(children.values()), 'fields': _inventory(records),
                    'suggested_parser': 'XMLParser (Event Data (XML) template)'}
        except etree.XMLSyntaxError:
            result['note'] = 'Starts with < but is not a single well-formed XML document; may be XML fragments'

    if text.startswith('['):
        try:
            data = json.loads(text)
            records = [_flatten(r) for r in data[:max_records] if isinstance(r, dict)]
            return {**result, 'format': 'json array', 'records': len(data), 'fields': _inventory(records),
                    'suggested_parser': 'JSONParser (Event Data (JSON) template)', **JSON_SETUP['array']}
        except ValueError:
            pass
    json_lines = []
    for line in lines[:max_records]:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                json_lines.append(obj)
        except ValueError:
            break
    if json_lines and len(json_lines) == min(len(lines), max_records):
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
