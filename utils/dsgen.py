"""Generate a Data Splitter from a small spec, and run the spec locally on a sample.

A text format the profiler does not know used to leave the model writing Data Splitter XML by hand, with no
feedback until a feed existed and a stream was stepped. A spec says how the text divides (delimited columns,
a regex with named groups, key=value pairs, syslog with a parsed body); the same spec is written as the
converter Stroom runs and applied here to the sample, so the records, and the field names the mapping may
use, are seen before anything is created. The dry run follows the subset of Data Splitter the generator
writes, not the whole language.
"""
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from utils.profile import KEY_VALUE, SYSLOG_3164, SYSLOG_5424, profile

DS_HEAD = ('<?xml version="1.1" encoding="UTF-8"?>\n'
           '<dataSplitter xmlns="data-splitter:3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
           '              xsi:schemaLocation="data-splitter:3 file://data-splitter-v3.0.xsd" version="3.0">\n')
DS_TAIL = '</dataSplitter>\n'
# Regexes for the syslog header, with the names their groups get; the body is group 'message'.
SYSLOG = {
    'rfc5424': (r'^(?:<(\d+)>)?1 (\S+) (\S+) (\S+) (\S+) (\S+) (-|\[.*?\]) ?(.*)$',
                ['pri', 'time', 'host', 'app', 'pid', 'msgid', 'structured', 'message']),
    'rfc3164': (r'^(?:<(\d+)>)?([A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}) (\S+) ([^:\[ ]+)(?:\[(\d+)\])?:? ?(.*)$',
                ['pri', 'time', 'host', 'app', 'pid', 'message']),
}
# CEF: whatever comes before it (a syslog header), the seven header fields (a | inside one is escaped \|), then the
# extension. Its pairs' values may hold spaces: each value runs to the next ' key=' (an = inside one is escaped \=).
CEF_PATTERN = (r'^(.*?)CEF:(\d+)\|((?:\\.|[^|\\])*)\|((?:\\.|[^|\\])*)\|((?:\\.|[^|\\])*)\|((?:\\.|[^|\\])*)\|'
               r'((?:\\.|[^|\\])*)\|((?:\\.|[^|\\])*)\|(.*)$')
CEF_NAMES = ['cef_version', 'cef_vendor', 'cef_product', 'cef_device_version', 'cef_signature_id', 'cef_name',
             'cef_severity']
CEF_PAIR = r'\s*([^=\s]+)=((?:\\.|[^\\])*?)(?=\s+[^=\s]+=|\s*$)'
# The syslog headers a CEF line may follow, without their message.
CEF_PREFIXES = [(r'^(?:<(\d+)>)?1 (\S+) (\S+) (\S+) (\S+) (\S+) (-|\[.*?\])\s*$',
                 ['pri', 'time', 'host', 'app', 'pid', 'msgid', 'structured']),
                (r'^(?:<(\d+)>)?([A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}) (\S+)\s*$', ['pri', 'time', 'host']),
                (r'^(.*?)\s*$', ['prefix'])]


class SplitterSpec(BaseModel):
    """How one record of text divides into named fields."""
    kind: Literal['delimited', 'regex', 'key_value', 'syslog', 'cef'] = Field(description=(
        "delimited: columns separated by delimiter; regex: pattern with one capture group per name; key_value: "
        "pairs such as a=1 b=\"two words\"; syslog: an RFC 5424 or 3164 header (pri, time, host, app, pid, "
        "message), with the message parsed further by body; cef: CEF:0|vendor|product|version|id|name|severity| "
        "then key=value pairs whose values may hold spaces (fields cef_vendor ... cef_severity, then each key), "
        "with any syslog header before it parsed by body (pri, time, host)."))
    delimiter: str = Field(',', description="delimited: the column separator (',', '\\t', '|', ';'); key_value: "
                                           "what separates pairs (' ' by default).")
    header: bool | list[str] = Field(False, description="delimited: True when the first line names the columns, "
                                                        "or the column names in order.")
    quote: str | None = Field(None, description="delimited and key_value: the character that quotes a value "
                                                "holding the delimiter, e.g. '\"'.")
    pattern: str | None = Field(None, description="regex: the pattern (Java regex, as Data Splitter runs it).")
    names: list[str] = Field(default_factory=list, description="regex: a field name per capture group, in order; "
                                                               "'' skips a group.")
    pair_separator: str = Field('=', description="key_value: what separates a key from its value.")
    rfc: Literal['rfc5424', 'rfc3164'] = Field('rfc3164', description="syslog: the header format.")
    body: 'SplitterSpec | None' = Field(None, description="Parse the field named by body_field further with this "
                                                         "spec (syslog's message, a regex group), adding its fields.")
    body_field: str = Field('message', description="The field body parses.")

    @model_validator(mode='after')
    def consistent(self):
        if self.kind == 'regex':
            if not self.pattern or not self.names:
                raise ValueError("regex needs pattern and names")
        if self.kind == 'delimited' and not self.header:
            raise ValueError("delimited needs header: True (a header line) or the column names")
        if self.kind == 'key_value' and self.delimiter == ',':
            self.delimiter = ' '
        return self


def kv_patterns(spec: SplitterSpec) -> list[str]:
    """The regexes for one key=value pair, tried in turn as Data Splitter and the dry run both match them along the
    text: with a quote character, a quoted value first (without its quotes), then a bare one."""
    d = re.escape(spec.delimiter) if spec.delimiter.strip() else r'\s'
    s = re.escape(spec.pair_separator)
    key = f'[^{s}{d}]+'
    if spec.quote:
        # One group for the value whether quoted or not (a lookbehind says which), so it always takes part: Stroom
        # neither tries a group's regexes in turn at each place nor lets a value name a group that didn't match.
        q = re.escape(spec.quote)
        return [f'{d}*({key}){s}{q}?((?<={q})[^{q}]*(?={q})|(?<!{q})[^{d}{q}]*){q}?']
    return [f'{d}*({key}){s}([^{d}]*)']


def _attr(text: str) -> str:
    return (text.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('\n', '\\n').replace('\t', '\\t'))


def _container(spec: SplitterSpec) -> str:
    return f' containerStart="{_attr(spec.quote)}" containerEnd="{_attr(spec.quote)}"' if spec.quote else ''


def _body_xml(spec: SplitterSpec, group: str, indent: str) -> str:
    """Elements that parse the text in `group` ($n of the enclosing regex) with spec.body."""
    if not spec.body:
        return ''
    return f'{indent}<group value="{group}">\n{_record_xml(spec.body, indent + "  ")}{indent}</group>\n'


def _record_xml(spec: SplitterSpec, indent: str) -> str:
    """The elements that divide one record (the text in scope) into data elements."""
    if spec.kind == 'regex':
        lines = [f'{indent}<regex pattern="{_attr(spec.pattern)}">']
        body_group = None
        for n, name in enumerate(spec.names, 1):
            if name:
                lines.append(f'{indent}  <data name="{_attr(name)}" value="${n}" />')
            if name == spec.body_field:
                body_group = f'${n}'
        if spec.body and body_group:
            lines.append(_body_xml(spec, body_group, indent + '  ').rstrip('\n'))
        lines.append(f'{indent}</regex>')
        return '\n'.join(lines) + '\n'
    if spec.kind == 'syslog':
        pattern, names = SYSLOG[spec.rfc]
        inner = SplitterSpec(kind='regex', pattern=pattern, names=names, body=spec.body, body_field=spec.body_field)
        return _record_xml(inner, indent)
    if spec.kind == 'cef':
        lines = [f'{indent}<regex pattern="{_attr(CEF_PATTERN)}">']
        if spec.body:
            lines.append(_body_xml(spec, '$1', indent + '  ').rstrip('\n'))
        lines += [f'{indent}  <data name="{name}" value="${n}" />' for n, name in enumerate(CEF_NAMES, 2)]
        lines += [f'{indent}  <group value="$9">', f'{indent}    <regex pattern="{_attr(CEF_PAIR)}">',
                  f'{indent}      <data name="$1" value="$2" />', f'{indent}    </regex>', f'{indent}  </group>',
                  f'{indent}</regex>']
        return '\n'.join(lines) + '\n'
    if spec.kind == 'key_value':
        # One regex, matched again and again along the text: a pair each time, a quoted value without its quotes.
        # Seen: a split on '=' writing $2, which a split doesn't have ("Group number 2 not found"), so every
        # key=value source failed in Stroom while the dry run, parsing on its own, said it was fine.
        return ''.join(f'{indent}<regex pattern="{_attr(p)}">\n{indent}  <data name="$1" value="$2" />\n{indent}</regex>\n'
                       for p in kv_patterns(spec))
    if spec.header is True:
        return f'{indent}<split delimiter="{_attr(spec.delimiter)}"{_container(spec)}>\n' \
               f'{indent}  <data name="$heading$1" value="$1" />\n{indent}</split>\n'
    # Named columns without a header line: one regex over the record, a group per column.
    d = re.escape(spec.delimiter)
    cell = f'"([^"]*)"|([^{d}]*)' if spec.quote == '"' else f'([^{d}]*)'
    pattern = '^' + d.join(cell for _ in spec.header) + '$'
    lines = [f'{indent}<regex pattern="{_attr(pattern)}">']
    per = 2 if spec.quote == '"' else 1
    for n, name in enumerate(spec.header):
        value = f'${n * per + 1}${n * per + 2}' if per == 2 else f'${n + 1}'
        lines.append(f'{indent}  <data name="{_attr(name)}" value="{value}" />')
    lines.append(f'{indent}</regex>')
    return '\n'.join(lines) + '\n'


def generate_splitter(spec: SplitterSpec) -> str:
    """The Data Splitter XML: one record per line, divided as the spec says."""
    body = ''
    if spec.kind == 'delimited' and spec.header is True:
        body += (f'  <split delimiter="\\n" maxMatch="1">\n    <group>\n'
                 f'      <split delimiter="{_attr(spec.delimiter)}"{_container(spec)}>\n        <var id="heading" />\n'
                 f'      </split>\n    </group>\n  </split>\n')
    body += f'  <split delimiter="\\n">\n    <group>\n{_record_xml(spec, "      ")}    </group>\n  </split>\n'
    return DS_HEAD + body + DS_TAIL


# --- the dry run ---

def _split_quoted(line: str, delimiter: str, quote: str | None) -> list[str]:
    if not quote or quote not in line:
        return line.split(delimiter)
    out, cell, inside, i = [], '', False, 0
    while i < len(line):
        ch = line[i]
        if ch == quote and inside and line.startswith(quote * 2, i):
            # A doubled quote inside a quoted value stays doubled, as Data Splitter leaves it (seen in Stroom: "Said
            # ""bye""" is Said ""bye""); the mapping's unescape_quotes transform makes it one.
            cell, i = cell + quote * 2, i + 2
            continue
        if ch == quote:
            inside = not inside
        elif line.startswith(delimiter, i) and not inside:
            out.append(cell)
            cell, i = '', i + len(delimiter)
            continue
        else:
            cell += ch
        i += 1
    out.append(cell)
    return out


def _apply_record(spec: SplitterSpec, text: str, heading: list[str] | None) -> dict[str, str] | None:
    """One record's fields, or None when the record does not match (no data elements would be written)."""
    if spec.kind == 'syslog':
        pattern, names = SYSLOG[spec.rfc]
        spec = SplitterSpec(kind='regex', pattern=pattern, names=names, body=spec.body, body_field=spec.body_field)
    if spec.kind == 'cef':
        m = re.match(CEF_PATTERN, text)
        if not m:
            return None
        fields = dict(zip(CEF_NAMES, m.groups()[1:8]))
        fields.update({pair.group(1): pair.group(2) for pair in re.finditer(CEF_PAIR, m.group(9))})
        if spec.body and m.group(1).strip():
            fields = {**(_apply_record(spec.body, m.group(1), None) or {}), **fields}
        return fields
    if spec.kind == 'regex':
        m = re.match(spec.pattern, text)
        if not m:
            return None
        fields = {name: m.group(n) or '' for n, name in enumerate(spec.names, 1) if name and n <= m.re.groups}
    elif spec.kind == 'key_value':
        # The regexes the converter runs, matched along the text as Data Splitter does: at each place the first
        # that matches.
        fields, at = {}, 0
        patterns = [re.compile(p) for p in kv_patterns(spec)]
        while at < len(text):
            found = next((m for m in (p.match(text, at) for p in patterns) if m and m.end() > at), None)
            if not found:
                break
            fields[found.group(1)] = found.group(2)
            at = found.end()
        if not fields:
            return None
    elif spec.header is True:
        cells = _split_quoted(text, spec.delimiter, spec.quote)
        fields = {heading[n]: cell for n, cell in enumerate(cells) if heading and n < len(heading)}
    else:
        cells = _split_quoted(text, spec.delimiter, spec.quote)
        if len(cells) != len(spec.header):
            return None
        fields = dict(zip(spec.header, cells))
    if spec.body and spec.body_field in fields:
        inner = _apply_record(spec.body, fields[spec.body_field], None)
        if inner:
            fields = {**fields, **inner}
    return fields


EXAMPLES = {
    'delimited': {'kind': 'delimited', 'delimiter': ',', 'header': True, 'quote': '"'},
    'key=value': {'kind': 'key_value', 'delimiter': ' ', 'pair_separator': '=', 'quote': '"'},
    'syslog rfc3164': {'kind': 'syslog', 'rfc': 'rfc3164', 'body': {'kind': 'key_value'}},
    'syslog rfc5424': {'kind': 'syslog', 'rfc': 'rfc5424', 'body': {'kind': 'key_value'}},
    'unknown text': {'kind': 'regex', 'pattern': '^(\\S+ \\S+) (\\S+) (.*)$', 'names': ['time', 'host', 'message']},
}


def _body_spec(messages: list[str]) -> 'SplitterSpec | None':
    """How syslog message bodies divide further, when they do: key=value pairs."""
    pairs = [len(KEY_VALUE.findall(m)) for m in messages if m.strip()]
    if pairs and sum(1 for n in pairs if n >= 2) >= 0.8 * len(pairs):
        quote = '"' if any('="' in m for m in messages) else None
        return SplitterSpec(kind='key_value', delimiter=' ', quote=quote)
    return None


def infer_spec(sample: str) -> tuple['SplitterSpec | None', dict[str, Any]]:
    """A spec for the sample's format, from its profile: (spec, profile). None when the format needs no text
    converter (JSON, XML) or cannot be inferred (free text, which needs a regex from a human)."""
    info = profile(sample)
    fmt = info['format']
    lines = [l for l in sample.splitlines() if l.strip()]
    if fmt == 'delimited':
        quote = '"' if any('"' in l for l in lines[:50]) else None
        header = bool(info.get('has_header'))
        return SplitterSpec(kind='delimited', delimiter=info.get('delimiter', ','), header=header or list(info.get('columns') or []),
                            quote=quote), info
    if fmt == 'key=value':
        quote = '"' if any('="' in l for l in lines[:50]) else None
        return SplitterSpec(kind='key_value', delimiter=' ', pair_separator='=', quote=quote), info
    if fmt == 'cef':
        # A syslog header before CEF: the first header pattern that fits every line's text before CEF.
        prefixes = [re.match(CEF_PATTERN, l).group(1) for l in lines if re.match(CEF_PATTERN, l)]
        body = None
        if any(p.strip() for p in prefixes):
            pattern, names = next(((p, n) for p, n in CEF_PREFIXES if all(re.match(p, x) for x in prefixes)),
                                  CEF_PREFIXES[-1])
            body = SplitterSpec(kind='regex', pattern=pattern, names=names)
        return SplitterSpec(kind='cef', body=body), info
    if fmt in ('syslog rfc3164', 'syslog rfc5424'):
        rfc = 'rfc3164' if fmt.endswith('3164') else 'rfc5424'
        head = SplitterSpec(kind='syslog', rfc=rfc)
        messages = [r.get('message', '') for r in (dry_run(head, sample)['records'])]
        return SplitterSpec(kind='syslog', rfc=rfc, body=_body_spec(messages)), info
    return None, info


def dry_run(spec: SplitterSpec, sample: str, max_records: int = 200) -> dict[str, Any]:
    """What the generated splitter makes of the sample: records as name -> value, lines that match nothing,
    and the field names seen (what a mapping's `field` may name)."""
    lines = [line for line in sample.splitlines() if line.strip()]
    heading = None
    if spec.kind == 'delimited' and spec.header is True and lines:
        heading = [h.strip() for h in _split_quoted(lines[0], spec.delimiter, spec.quote)]
        lines = lines[1:]
    records, unmatched = [], []
    for line in lines[:max_records]:
        fields = _apply_record(spec, line, heading)
        if fields:
            records.append(fields)
        else:
            unmatched.append(line[:200])
    names: list[str] = []
    for record in records:
        names += [k for k in record if k not in names]
    return {'records': records, 'unmatched_lines': unmatched, 'fields': names, 'heading': heading}
