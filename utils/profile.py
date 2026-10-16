"""Profile a raw data sample: format, structure, fields, value types and timestamp patterns."""
import csv
import io
import ipaddress
import json
import re
from collections import Counter
from typing import Any

from fastmcp.exceptions import ToolError
from lxml import etree

from utils.timefmt import EPOCH_NAMES, check_time_format, infer_time_pattern

SYSLOG_5424 = re.compile(r'^<\d{1,3}>1 \S+ \S+ \S+ \S+ \S+')
SYSLOG_3164 = re.compile(r'^(<\d{1,3}>)?[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2} \S+ ')
KEY_VALUE = re.compile(r'(\w[\w.-]*)=("[^"]*"|\S*)')
CEF = re.compile(r'CEF:\d+\|')
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
    (re.compile(r'^1\d{15}$'), 'epoch microseconds'),
    (re.compile(r'^1\d{18}$'), 'epoch nanoseconds'),
]


# How a JSON sample reaches the XSLT. The JSONParser reads every top-level value in the stream and, with
# addRootObject (its default), wraps them all in one <map>; so JSON lines parse as /map/map with no text
# converter, and an array as /map/array/map, or /array/map with addRootObject off.
# Lines read when looking for those that aren't what the rest are (a JSON line cut short, a row short of a column).
MALFORMED_SCAN = 5000
# JSON lines some of which aren't JSON: one record a line through a Data Splitter, the JSON parsed in the XSLT.
JSON_LINES_SPEC = "build_data_splitter spec {'kind': 'regex', 'pattern': '^(.*)$', 'names': ['json']}"
JSON_SETUP = {
    'lines': {'text_converter': 'none: the JSONParser element parses JSON lines itself; a text converter cannot',
              'parser_properties': {'jsonParser.addRootObject': True},
              'xslt_input': {'namespace': 'http://www.w3.org/2013/XSL/json', 'root': '/map', 'record': 'map',
                             'mapping': {'input': 'json', 'json_layout': 'lines'}}},
    'lines_with_bad': {'text_converter': f'a Data Splitter, one record a line ({JSON_LINES_SPEC})',
                       'suggested_parser': 'DSParser with a Data Splitter, one record a line, the JSON read by the '
                                           "mapping's json_fields: Stroom's JSONParser stops at a line that isn't JSON",
                       'parser_properties': {},
                       'xslt_input': {'namespace': 'records:2', 'root': '/records', 'record': 'record',
                                      'mapping': {'input': 'data_splitter', 'json_fields': ['json']}}},
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
# Fragments that are already event-logging <Event>s go in an <Events> wrapper, in event-logging:3: in a records one
# they would be records:2 elements. Environments write their own wrappers this way too (seen on live: 'Event Logging
# v3.4.2 Fragments', whose Events root puts every fragment that declares no namespace in event-logging:3).
EVENTS_FRAGMENT_WRAPPER = """<?xml version="1.1" encoding="UTF-8"?>
<!DOCTYPE Events [
<!ENTITY fragment SYSTEM "fragment">
]>
<Events xmlns="event-logging:3" xmlns:stroom="stroom" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:schemaLocation="event-logging:3 file://event-logging-v{version}.xsd" Version="{version}">
&fragment;
</Events>
"""
_XML_DECL = re.compile(r'^\s*<\?xml[^>]*\?>')
_WRAPPER_ROOT = re.compile(r'<([A-Za-z_][\w.-]*)(\s[^>]*)?>\s*&fragment;')


def wrapper_root(code: str) -> tuple[str | None, str]:
    """A fragment wrapper's root element and its default namespace ('' for none): the namespace fragments that
    declare none are read in."""
    m = _WRAPPER_ROOT.search(re.sub(r'<!--.*?-->', '', code, flags=re.S))
    if not m:
        return None, ''
    ns = re.search(r'\sxmlns\s*=\s*["\']([^"\']*)["\']', m.group(2) or '')
    return m.group(1), ns.group(1) if ns else ''


def clean_wrapper(code: str) -> str:
    """An environment's fragment wrapper as a build saves its copy: without comments (seen: live's 'Event Logging
    v3.4.2 Fragments' carries an older version of itself commented out, curly quotes and all, which every copy
    inherited), its DOCTYPE named after the root element it wraps (seen: DOCTYPE records on an <Events> root), and no
    blank lines left behind. The environment's own wrapper is left as it is."""
    text = re.sub(r'<!--.*?-->', '', code, flags=re.S)
    root, _ = wrapper_root(text)
    if root:
        text = re.sub(r'<!DOCTYPE\s+[\w.:-]+', f'<!DOCTYPE {root}', text, count=1)
    lines = [line.rstrip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line.strip()) + '\n'


def event_logging_fragments(fragments: list) -> bool:
    """Fragments that are event-logging <Event>s already (EventTime, EventDetail...), in its namespace or none; not a
    source's own <Event> (Windows' System and EventData)."""
    first = fragments[0]
    parts = {etree.QName(c).localname for c in first if isinstance(c.tag, str)}
    return (etree.QName(first).localname == 'Event' and etree.QName(first).namespace in (None, 'event-logging:3')
            and {'EventTime', 'EventDetail'} <= parts)


def cut_document(text: str) -> etree._Element | None:
    """A single-root XML document of which only the start was read (a big stream's head: no closing root tag): its
    root holding the complete records before the cut. None when the text isn't the start of one document (fragments,
    a syntax error, not XML). Seen in production: a 54 MB <tweets> stream, read to 1 MB, failed to parse as a
    document, wasn't fragments either, and was profiled as CSV with no records."""
    parser = etree.XMLPullParser(events=('start', 'end'), huge_tree=True)
    try:
        parser.feed(_XML_DECL.sub('', text.strip('\ufeff\r\n '), count=1).encode('utf-8'))
        events = list(parser.read_events())
    except etree.XMLSyntaxError:
        return None
    root, depth, records = None, 0, []
    for event, element in events:
        if event == 'start':
            depth += 1
            root = element if root is None else root
        else:
            depth -= 1
            if depth == 1 and isinstance(element.tag, str):
                records.append(element)
            elif depth == 0:
                return None     # the document closed: a whole document, which the XML parser reads (or rejects)
    if root is None or not records:
        return None
    whole = etree.Element(root.tag, attrib=dict(root.attrib), nsmap=root.nsmap)
    whole.extend(records)
    return whole


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


def xml_fragment_setup(namespace: str | None, record: str, wrapper: dict[str, Any] | None = None,
                       others: list[dict[str, Any]] = ()) -> dict[str, Any]:
    """How fragments are read: the wrapper converter (the environment's own when it has one: `wrapper` with its code,
    name and path), the parser, and the namespace the XSLT then reads them in."""
    code = clean_wrapper(wrapper['code']) if (wrapper or {}).get('code') else XML_FRAGMENT_WRAPPER
    root, wrapper_ns = wrapper_root(code)
    effective = namespace or wrapper_ns
    converter: dict[str, Any] = {'type': 'XML_FRAGMENT', 'code': code, 'root': root, 'namespace': wrapper_ns}
    if wrapper and wrapper.get('uuid'):
        converter.update(environment={k: wrapper[k] for k in ('name', 'uuid', 'path') if wrapper.get(k)},
                         note=f"The environment's own wrapper ({wrapper.get('name')}): save this code in the build "
                              f"(build_data_splitter save_as=<name>), as sibling sources do, so the fragments are read "
                              f"the way the environment's other pipelines read theirs.")
    else:
        converter['note'] = ("The wrapper the XMLFragmentParser puts round the fragments (its textConverter); use the "
                             "template's own wrapper if it already sets one.")
    if others:
        converter['other_wrappers'] = [{k: o[k] for k in ('name', 'path', 'root', 'namespace') if k in o} for o in others]
    return {
        'text_converter': converter,
        'parser': "XMLFragmentParser: a template whose chain has one (child_must_supply names its textConverter), "
                  "else create_pipeline from a translation template whose parser is the XMLParser, with "
                  "replace_parser='XMLFragmentParser'",
        'xslt_input': {'namespace': effective, 'root': '/', 'record': f'*/{record}',
                       'note': (f"The fragments declare no namespace, so inside the wrapper they take its default, "
                                f"{wrapper_ns or 'none'}: set xml_namespace to that. The XSLT reads them in it and "
                                f"writes Events in event-logging:3." if not namespace else
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
        # A number can be a time too (epoch seconds, with or without a fraction, or milliseconds): read as its text.
        pattern = infer_time_pattern(repr(value) if isinstance(value, float) else str(value))
        return f'timestamp ({pattern})' if pattern in EPOCH_NAMES else 'number'
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
                                                               'record_element', 'namespace', 'event_logging',
                                                               'malformed_lines', 'malformed_note')} for n, p in profiles.items()},
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


_NAME_CELL = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]{0,40}$')


def _typed(value: str) -> bool:
    """A value of a type a column name never is: a time, a number, an IP address."""
    if re.fullmatch(r'-?\d+(\.\d+)?', value):
        return True
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    return infer_time_pattern(value) is not None


def _names_a_header(rows: list[list[str]]) -> bool:
    """A first line of names (user, src_ip, eventTime), each unique, over columns whose values aren't names like it:
    a header the sniffer missed because every column is text (seen: user,name,department,site over people's names,
    the header read as a record and the columns called col1, col2 ...)."""
    first, body = rows[0], [r for r in rows[1:] if r]
    if len(set(first)) != len(first) or not all(_NAME_CELL.match(c.strip()) for c in first):
        return False
    # A column whose every value is typed (a time, a number, an IP address) under a first cell that is a plain name:
    # a header, whatever the other columns hold (seen: time,user,host,action over ISO times and words, the header
    # read as a record, as only one column of four differed).
    for n, name in enumerate(first):
        values = [r[n].strip() for r in body if n < len(r) and r[n].strip()]
        if len(values) >= 2 and all(_typed(v) for v in values) and not _typed(name.strip()):
            return True
    differing = 0
    for n, name in enumerate(first):
        values = [r[n].strip() for r in body if n < len(r)]
        if values and name not in values and not all(_NAME_CELL.match(v) and v.islower() == name.islower() for v in values):
            differing += 1
    return differing >= max(1, len(first) // 2)


JSON_START = re.compile(r'(\[\s*)?\{\s*"')


def _not_json(text: str) -> ToolError:
    r"""Text that starts as JSON and doesn't parse, with where and why. Seen: a FortiOS file's text retyped by the
    agent with tz=\"\+1000\" (\+ is no JSON escape), profiled as key=value from its records' body field, with a
    key=value template proposed for a JSON file."""
    first = text if text.lstrip().startswith('[') else text.splitlines()[0]
    try:
        json.loads(first)
        return ToolError('not JSON')     # not reached: called only when it fails
    except json.JSONDecodeError as e:
        near = first[max(0, e.pos - 40):e.pos + 40]
        return ToolError(f"This starts as JSON but isn't valid JSON: {e.msg} at character {e.pos}, near {near!r}. "
                         "Pass the source's text exactly as it is, not retyped (an escape such as \\+ isn't JSON). "
                         f"A file on the user's disk isn't passed through you: upload_sample files= sends it whole, "
                         f"and the server reads it from the stream.")


# How a record of several lines starts: its time, as logs write it (ISO, with a space or T; bracketed; syslog's;
# Apache's day/month/year).
RECORD_STARTS = [r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}', r'\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}',
                 r'[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}', r'\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}',
                 r'\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}']


def multiline_records(lines: list[str]) -> tuple[str, list[str]] | None:
    """(start, records) for text whose records run over several lines: most records' first lines start with a time,
    and lines between them (a stack trace's 'at ...', a wrapped message) don't. Text whose every line starts one is
    single-line, and left to the other formats."""
    from utils.dsgen import group_lines
    for start in RECORD_STARTS:
        begins = sum(bool(re.match(start, l)) for l in lines)
        if begins >= 2 and begins < len(lines) and re.match(start, lines[0]) and begins >= 0.2 * len(lines):
            return start, group_lines(lines, start)
    return None


def quoted_csv(text: str, max_records: int = 200) -> dict[str, Any] | None:
    """Delimited text with new lines inside quoted values (a comment over two lines): read as CSV over the whole text,
    not a line at a time, which made such a file unknown text. None unless some quoted value holds a new line."""
    if '"' not in text or '\n' not in text:
        return None
    for delimiter in (',', '\t', '|', ';'):
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter, quotechar='"'))
        except csv.Error:
            continue
        rows = [r for r in rows if any(c.strip() for c in r)]
        if len(rows) < 3 or not any('\n' in c for r in rows for c in r):
            continue
        width = Counter(len(r) for r in rows).most_common(1)[0][0]
        if width < 2 or sum(len(r) == width for r in rows) < 0.9 * len(rows):
            continue
        has_header = _names_a_header(rows) or csv.Sniffer().has_header('\n'.join(','.join(r) for r in rows[:20]))
        header = rows[0] if has_header else [f'col{i + 1}' for i in range(width)]
        body = rows[1:] if has_header else rows
        return {'format': 'delimited', 'delimiter': delimiter, 'has_header': has_header, 'columns': header,
                'quoted_newlines': True, 'records': len(body), '_records': [dict(zip(header, r)) for r in body[:max_records]],
                'suggested_parser': 'Data Splitter for delimited data whose quoted values hold new lines '
                                    '(quoted_newlines: a record ends at a new line outside quotes)'}
    return None


_SEPARATOR = re.compile(r' *[^\w\s"\'.:/@()\[\]{}<>=+-]{1,3} *| +:: +| +- +')


def custom_delimiter(lines: list[str]) -> str | None:
    """A separator the CSV sniffer doesn't try: longer than one character (' ~ ', '||', ' :: ') or an unusual one
    ('~', '^'), found the same number of times (two or more) on every line. Seen unknown text: '... ~ alice ~ LOGIN
    ~ OK ~ ...'."""
    if len(lines) < 2:
        return None
    counts: Counter = Counter()
    for token in set(_SEPARATOR.findall(lines[0])):
        if token.strip() in (',', ';', '|', '') and token == token.strip():
            continue        # the sniffer's own, unpadded
        found = [line.count(token) for line in lines]
        if min(found) >= 2 and len(set(found)) == 1:
            counts[token] = found[0]
    if not counts:
        return None
    return max(counts, key=lambda t: (len(t), counts[t]))


def steady_delimiter(lines: list[str]) -> str | None:
    """The separator ('|', ',', tab, ';') that splits at least four in five lines into the same number (two or
    more) of columns."""
    from utils.dsgen import _split_quoted
    for delimiter in (',', '\t', '|', ';'):
        widths = Counter(len(_split_quoted(l, delimiter, '"')) for l in lines)
        width, seen = widths.most_common(1)[0]
        if width >= 2 and seen >= 0.8 * len(lines):
            return delimiter
    return None


def off_rows(lines: list[str], delimiter: str, width: int, has_header: bool) -> list[dict[str, Any]]:
    """Delimited lines that aren't like the rest: a quote opened and not closed, or another number of columns."""
    from utils.dsgen import _split_quoted
    out = []
    for n, line in enumerate(lines[1:] if has_header else lines, 2 if has_header else 1):
        if line.count('"') % 2:
            out.append({'line': n, 'reason': 'a quote opened and not closed', 'text': line[:160]})
            continue
        cells = len(_split_quoted(line, delimiter, '"'))
        if cells != width:
            out.append({'line': n, 'reason': f'{cells} columns where the rest have {width}', 'text': line[:160]})
    return out


def _malformed(bad: list[dict[str, Any]], what_stroom_does: str) -> dict[str, Any]:
    """The lines that aren't like the rest, said rather than left to be found in processing."""
    if not bad:
        return {}
    return {'malformed_lines': bad[:20],
            'malformed_note': (f"{len(bad)} line(s) aren't like the rest (first: line {bad[0]['line']}, "
                               f"{bad[0]['reason']}). {what_stroom_does}. Show them to the user: fixed at the source, "
                               f"left out on purpose (drop_when), or mapped as they are.")}


_WRAPPER_HEAD = re.compile(r'^\{\s*(?:"[^"]*"\s*:\s*(?:"(?:\\.|[^"\\])*"|-?[\d.eE+-]+|true|false|null)\s*,\s*)*'
                           r'"([^"]+)"\s*:\s*\[\s*(?=\{)')


def wrapped_records(text: str) -> tuple[str, list[dict[str, Any]]] | None:
    """(key, records) for one JSON object whose array holds the records, as AWS CloudTrail writes them
    ({"Records": [...]}) and many APIs do ("data", "events", "items"...): the key whose array of objects is the
    longest. Seen: such a file refused as "starts as JSON but isn't valid JSON", read a line at a time."""
    if not text.startswith('{'):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        # Cut off (a stream's head): the array key among the object's first keys, and the items whole before the cut.
        m = _WRAPPER_HEAD.match(text)
        if not m:
            return None
        decoder, items, at = json.JSONDecoder(), [], m.end()
        while True:
            while at < len(text) and text[at] in ' \t\r\n,':
                at += 1
            if at >= len(text) or text[at] != '{':
                break
            try:
                item, at = decoder.raw_decode(text, at)
            except ValueError:
                break
            items.append(item)
        return (m.group(1), items) if items else None
    if not isinstance(value, dict):
        return None
    arrays = [(k, v) for k, v in value.items() if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)]
    return max(arrays, key=lambda kv: len(kv[1])) if arrays else None


def json_setup_wrapped(key: str) -> dict[str, Any]:
    """How a wrapped JSON object reaches the XSLT: the JSONParser's record is the whole object (addRootObject on, as
    for JSON lines), and for_each makes an Event of each item of its array."""
    setup = json.loads(json.dumps(JSON_SETUP['lines']))
    setup['text_converter'] = 'none: the JSONParser element parses the object itself; a text converter cannot'
    setup['xslt_input']['mapping'] = {'input': 'json', 'json_layout': 'lines', 'for_each': key}
    return setup


_DIRECTIVE = re.compile(r'^(#[A-Za-z][\w-]*:?)(?:[ \t]+(.*))?$')
_COLUMN_NAME = re.compile(r'^[A-Za-z_][\w.()\-/:]*$')


def directive_log(lines: list[str]) -> dict[str, Any] | None:
    """A log whose columns are named by a directive line rather than a first line: W3C extended logs (IIS, Blue
    Coat: #Software, #Fields: date time ...), Zeek (#separator, #fields ts uid ...) and the like. The data lines'
    delimiter and quoting, and the first directive whose value splits into as many names as they have columns."""
    from utils.dsgen import _split_quoted
    comments = [l for l in lines if l.startswith('#')]
    data = [l for l in lines if not l.startswith('#')]
    if not comments or not data:
        return None
    quote = '"' if any('"' in l for l in data[:50]) else None
    parsed = [(l, _DIRECTIVE.match(l)) for l in lines[:2000]]
    for delimiter in ('\t', ' ', ',', '|', ';'):
        def names_of(m: re.Match | None) -> list[str]:
            return [n.strip() for n in _split_quoted((m.group(2) or '').strip(), delimiter, quote)] if m else []
        for prefix in dict.fromkeys(m.group(1) for _, m in parsed if m):
            # Each data line read under the latest of these directives above it (a log restarted mid-file names its
            # columns again): it names the columns if most data lines then have as many as it names.
            heading, fits, read = None, 0, 0
            for line, m in parsed:
                if m and m.group(1) == prefix:
                    names = names_of(m)
                    heading = names if len(names) >= 3 and all(_COLUMN_NAME.match(n) for n in names) else None
                elif not line.startswith('#') and heading:
                    read += 1
                    fits += len(_split_quoted(line, delimiter, quote)) == len(heading)
            if not read or fits < 0.8 * len([l for l, _ in parsed if not l.startswith('#')]):
                continue
            directives = list(dict.fromkeys(m.group(1) for _, m in parsed if m and m.group(1) != prefix))
            from utils.dsgen import SplitterSpec, dry_run
            spec = SplitterSpec(kind='delimited', delimiter=delimiter, header=True, quote=quote, comment_prefix='#',
                                header_directive=prefix, directives=directives)
            first = next(names_of(m) for _, m in parsed if m and m.group(1) == prefix)
            return {'format': 'delimited', 'delimiter': delimiter, 'has_header': True, 'quote': quote,
                    'comment_prefix': '#', 'header_directive': prefix, 'directives': directives,
                    'columns': first, 'records': len(data), '_records': dry_run(spec, '\n'.join(lines))['records']}
    return None


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
                    'suggested_parser': 'XMLParser (a translation template with one: find_pipeline_templates)', 'text_converter': 'none: the XMLParser reads the document'}
        except etree.XMLSyntaxError:
            fragments = xml_fragments(text)
            cut = None if fragments else cut_document(text)
            if cut is not None:
                children = Counter(etree.QName(c).localname for c in cut)
                records = [_xml_record_fields(rec) for rec in cut][:max_records]
                return {**result, 'format': 'xml', 'root': etree.QName(cut).localname,
                        'namespace': etree.QName(cut).namespace, 'record_element': children.most_common(1)[0][0],
                        'records': len(cut), 'fields': _inventory(records),
                        'note': f"one XML document, read only to here (no closing </{etree.QName(cut).localname}>): "
                                f"these are its first {len(cut)} complete records",
                        'suggested_parser': 'XMLParser (a translation template with one: find_pipeline_templates)',
                        'text_converter': 'none: the XMLParser reads the document'}
            if fragments:
                children = Counter(etree.QName(c).localname for c in fragments)
                record_tag = children.most_common(1)[0][0]
                namespace = etree.QName(fragments[0]).namespace
                events = event_logging_fragments(fragments)
                # The version is the default's here, where no settings are read: the tools write the configured one.
                wrapper = {'code': EVENTS_FRAGMENT_WRAPPER.format(version='3.5.2')} if events else None
                return {**result, 'format': 'xml fragments', 'record_element': record_tag, 'namespace': namespace,
                        **({'event_logging': True} if events else {}),
                        'records': len(fragments), 'fields': _inventory([_xml_record_fields(f) for f in fragments[:max_records]]),
                        'suggested_parser': 'XMLFragmentParser with an XML_FRAGMENT text converter (the wrapper below); '
                                            'no root element, so the XMLParser cannot read it',
                        **xml_fragment_setup(namespace, record_tag, wrapper)}
            result['note'] = 'Starts with < but is not well-formed XML, as a document or as fragments'

    if text.startswith('['):
        try:
            data = json.loads(text)
            records = [_flatten(r) for r in data[:max_records] if isinstance(r, dict)]
            return {**result, 'format': 'json array', 'records': len(data), 'fields': _inventory(records),
                    'suggested_parser': 'JSONParser (a translation template with one: find_pipeline_templates)', **JSON_SETUP['array']}
        except ValueError:
            head = _array_head(text, max_records)
            if head and all(isinstance(r, dict) for r in head):
                return {**result, 'format': 'json array', 'records': len(head), 'fields': _inventory(
                    [_flatten(r) for r in head]), 'note': 'the array is cut short here: these are its first records',
                    'suggested_parser': 'JSONParser (a translation template with one: find_pipeline_templates)', **JSON_SETUP['array']}
    wrapped = wrapped_records(text)
    if wrapped:
        key, items = wrapped
        return {**result, 'format': 'json object', 'records_key': key, 'records': len(items),
                'fields': _inventory([_flatten(r) for r in items[:max_records]]),
                'suggested_parser': f"JSONParser: one object whose '{key}' array holds the records (as AWS CloudTrail "
                                    f"writes them); the mapping makes an Event of each with for_each='{key}' (a "
                                    f"translation template with a JSONParser: find_pipeline_templates)",
                **json_setup_wrapped(key)}
    json_lines, bad = [], []
    for n, line in enumerate(lines[:max(max_records, MALFORMED_SCAN)], 1):
        try:
            obj = json.loads(line)
        except ValueError as e:
            if not json_lines and n == 1:
                break           # not JSON lines at all (or not JSON): the other formats are tried
            bad.append({'line': n, 'reason': f"isn't JSON: {str(e).split(':')[0]}", 'text': line[:160]})
            continue
        if isinstance(obj, dict):
            json_lines.append(obj)
        else:
            bad.append({'line': n, 'reason': "isn't a JSON object", 'text': line[:160]})
    read = min(len(lines), max(max_records, MALFORMED_SCAN))
    if bad and bad[-1]['line'] == read == len(lines) and len(bad) == 1 and json_lines:
        bad, read = [], read - 1    # only the last line, cut short by a read limit
    if len(json_lines) >= 2 and len(json_lines) >= 0.8 * (read - len(bad)) and len(json_lines) >= 0.8 * read:
        found = {**result, 'format': 'json lines', 'records': len(lines),
                 'fields': _inventory([_flatten(r) for r in json_lines[:max_records]]),
                 'suggested_parser': 'JSONParser, one object per line (a translation template with one: '
                                     'find_pipeline_templates)', **JSON_SETUP['lines']}
        if bad:
            # Seen on the local Stroom: a line cut short stopped the JSONParser with a fatal error, and every record
            # after it in the stream was lost.
            found.update(JSON_SETUP['lines_with_bad'])
            found['malformed_lines'] = bad[:20]
            found['note'] = (f"{len(bad)} of the {read} lines read aren't JSON objects (first: line {bad[0]['line']}, "
                             f"{bad[0]['reason']}). Stroom's JSONParser stops the stream at the first such line, a "
                             f"fatal error, and every record after it is lost. Read the lines with a Data Splitter "
                             f"instead, one record a line ({JSON_LINES_SPEC}), and map their keys through json_fields: "
                             f"['json'] (a key is then json.<key>): a line that isn't JSON gives no fields, matches no "
                             f"rule and is logged, and the rest are read.")
        return found

    if not json_lines and JSON_START.match(text):
        raise _not_json(text)
    directed = directive_log(lines)
    if directed:
        return {**result, **directed, 'fields': _inventory(directed.pop('_records')[:max_records]),
                'suggested_parser': "Data Splitter for delimited data whose columns are named by a directive line "
                                    f"({directed['header_directive']}): build_data_splitter infers it (a translation "
                                    "template with a DSParser: find_pipeline_templates)"}
    if sum(bool(CEF.search(line)) for line in lines) >= 0.8 * len(lines):
        # CEF, alone or after a syslog header: seen read as key=value, its header fields taken for keys and its
        # values cut at their first space.
        return {**result, 'format': 'cef', 'records': len(lines), 'examples': lines[:3],
                'suggested_parser': "Data Splitter for CEF: the header's fields, then the extension's key=value pairs, "
                                    "whose values may hold spaces (build_data_splitter infers it; a translation template with a "
                                    "DSParser)"}
    if sum(bool(SYSLOG_5424.match(line)) for line in lines) >= 0.8 * len(lines):
        return {**result, 'format': 'syslog rfc5424', 'records': len(lines), 'examples': lines[:3],
                'suggested_parser': 'Data Splitter with a regex per RFC 5424 part (a translation template with a DSParser: find_pipeline_templates)'}
    if sum(bool(SYSLOG_3164.match(line)) for line in lines) >= 0.8 * len(lines):
        return {**result, 'format': 'syslog rfc3164', 'records': len(lines), 'examples': lines[:3],
                'suggested_parser': 'Data Splitter with a regex for PRI, timestamp, host, tag and message'}

    from utils.dsgen import CRI_LINE, CRI_END, CRI_JOIN, group_lines_to_end
    if lines and sum(bool(re.match(CRI_LINE, l)) for l in lines) >= 0.9 * len(lines):
        records = group_lines_to_end(lines, CRI_END)
        return {**result, 'format': 'cri', 'record_end': CRI_END, 'records': len(records), 'examples': records[:3],
                'suggested_parser': "Data Splitter with record_end: Kubernetes CRI container log lines, a message split "
                                    "into partial (P) lines ended by its full (F) line (build_data_splitter infers it); "
                                    f"the mapping joins the parts with xpath {CRI_JOIN}"}
    multi = multiline_records(lines)
    if multi:
        start, records = multi
        return {**result, 'format': 'multi-line text', 'record_start': start, 'records': len(records),
                'examples': records[:3],
                'suggested_parser': "Data Splitter with record_start: a record starts at a line beginning with its "
                                    "time, and the lines after it (a stack trace, a wrapped message) are part of it "
                                    "(build_data_splitter infers the start; give the record's pattern, starting (?s))"}
    kv = [dict((k, v.strip('"')) for k, v in KEY_VALUE.findall(line)) for line in lines[:max_records]]
    if kv and sum(len(r) >= 3 for r in kv) >= 0.8 * len(kv):
        found = {**result, 'format': 'key=value', 'records': len(lines), 'fields': _inventory(kv),
                 'suggested_parser': 'Data Splitter splitting on spaces then = (a translation template with a DSParser: find_pipeline_templates)'}
        bad = [{'line': n, 'reason': 'holds no key=value pairs', 'text': line[:160]}
               for n, line in enumerate(lines[:MALFORMED_SCAN], 1) if not KEY_VALUE.search(line)]
        return {**found, **_malformed(bad, "Stroom's Data Splitter writes no record for such a line and reports it as "
                                           "content its expressions failed to match")}

    quoted = quoted_csv(text, max_records)
    if quoted:
        return {**result, **quoted, 'fields': _inventory(quoted.pop('_records'))}
    custom = custom_delimiter(lines[:max_records])
    if custom:
        rows = [line.split(custom) for line in lines[:max_records]]
        header = [c.strip() for c in rows[0]] if _names_a_header(rows) else [f'col{i + 1}' for i in range(len(rows[0]))]
        body = rows[1:] if header == [c.strip() for c in rows[0]] else rows
        return {**result, 'format': 'delimited', 'delimiter': custom, 'has_header': header == [c.strip() for c in rows[0]],
                'columns': header, 'records': len(lines) - (len(rows) - len(body)),
                'fields': _inventory([dict(zip(header, r)) for r in body]),
                'suggested_parser': f'Data Splitter for delimited data, columns separated by {custom!r}'}
    try:
        delimiter = csv.Sniffer().sniff('\n'.join(lines[:20]), delimiters=',\t|;').delimiter
    except csv.Error:
        # The sniffer gives up on a few bad rows (an unbalanced quote, a row short of a column): the separator that
        # splits most lines into the same number of columns.
        delimiter = steady_delimiter(lines[:max_records])
    if delimiter:
        try:
            has_header = csv.Sniffer().has_header('\n'.join(lines[:20]))
        except csv.Error:
            has_header = False
        rows = list(csv.reader(io.StringIO('\n'.join(lines[:max_records + 1])), delimiter=delimiter))
        if not has_header and len(rows) > 1:
            has_header = _names_a_header(rows)
        header = rows[0] if has_header else [f'col{i + 1}' for i in range(len(rows[0]))]
        body = rows[1:] if has_header else rows
        records = [dict(zip(header, row)) for row in body]
        found = {**result, 'format': 'delimited', 'delimiter': delimiter, 'has_header': has_header,
                 'columns': header, 'records': len(lines) - (1 if has_header else 0), 'fields': _inventory(records),
                 'suggested_parser': 'Data Splitter for delimited data' + (' with a header row' if has_header else '')
                                     + ' (a translation template with a DSParser: find_pipeline_templates)'}
        return {**found, **_malformed(off_rows(lines[:MALFORMED_SCAN], delimiter, len(header), has_header),
                                      "Stroom's Data Splitter still writes a record for such a line, its fields as the "
                                      "line splits: a missing column is a missing field, an unclosed quote runs to the "
                                      "line's end")}
    return {**result, 'format': 'unknown text', 'examples': lines[:5],
            'suggested_parser': 'Data Splitter with regexes; ask the user for the record structure'}
