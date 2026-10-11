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
           '              xsi:schemaLocation="data-splitter:3 file://data-splitter-v3.0.xsd" version="3.0">\n'
           # A byte order mark before the first record, taken first: Stroom doesn't strip it, and it became part of
           # the first key (seen in eval case 51: '﻿time', so the first event had no time).
           '  <regex pattern="^\\uFEFF" maxMatch="1" />\n')
DS_TAIL = '</dataSplitter>\n'
# Kubernetes CRI container logs: '<time> <stream> <P|F> <content>', a long message split into partial (P) lines
# ended by a full (F) one.
CRI_LINE = r'^\S+ (?:stdout|stderr) [PF] '
CRI_END = r'\S+ (?:stdout|stderr) F '
# The XPath joining a CRI record's parts: each line's prefix after the first taken out, with its new line.
CRI_JOIN = "replace(data[@name='message']/@value, '\\n\\S+ (stdout|stderr) [PF] ', '')"
# Regexes for the syslog header, with the names their groups get; the body is group 'message'.
SYSLOG = {
    'rfc5424': (r'^(?:<(\d+)>)?1 (\S+) (\S+) (\S+) (\S+) (\S+) (-|\[.*?\]) ?(.*)$',
                ['pri', 'time', 'host', 'app', 'pid', 'msgid', 'structured', 'message']),
    # The tag (app[pid]) only before a colon: Palo Alto sends none, and its CSV's first column was read as one.
    'rfc3164': (r'^(?:<(\d+)>)?([A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}) (\S+) (?:([^:\[ ]+)(?:\[(\d+)\])?: ?)?(.*)$',
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
    quoted_newlines: bool = Field(False, description=(
        "delimited, with quote: values in quotes may hold new lines (a comment over two lines), so a record ends at a "
        "new line outside quotes only."))
    record_end: str | None = Field(None, description=(
        "Records whose last line is marked (Kubernetes CRI: a message split into partial 'P' lines ends at its 'F' "
        "line): a regex the record's last line matches at its start, e.g. '\\S+ (?:stdout|stderr) F '. The lines "
        "before it since the last such line are part of the record."))
    record_start: str | None = Field(None, description=(
        "Records that run over several lines (a Java log entry and its stack trace, a wrapped message): a regex the "
        "first line of a record matches at its start, e.g. '\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}'. Lines "
        "that don't match continue the record before them; the record's own pattern then reads its text, new lines "
        "and all (start it with (?s) so .* takes them)."))
    comment_prefix: str | None = Field(None, description="delimited: lines starting with this are not records, e.g. "
                                                        "'#' for W3C (IIS, Blue Coat), Zeek and similar logs' "
                                                        "directive lines.")
    header_directive: str | None = Field(None, description=(
        "delimited, with header true: the directive line that names the columns, e.g. '#Fields:' (W3C) or '#fields' "
        "(Zeek). Its columns hold for the lines after it, until it appears again (a log restarted mid-file)."))
    directives: list[str] = Field(default_factory=list, description=(
        "delimited: other directive lines whose value every record after them carries, as a field named as in the "
        "file, e.g. ['#Software:', '#Remark:'] give fields '#Software' and '#Remark' (the device, its software, the "
        "log's start date: whatever the header says)."))

    @model_validator(mode='after')
    def consistent(self):
        if self.kind == 'regex':
            if not self.pattern or not self.names:
                raise ValueError("regex needs pattern and names")
        if self.kind == 'delimited' and not self.header:
            raise ValueError("delimited needs header: True (a header line) or the column names")
        if self.kind == 'key_value' and self.delimiter == ',':
            self.delimiter = ' '
        if self.header_directive or self.directives:
            if not self.comment_prefix:
                raise ValueError("header_directive and directives need comment_prefix: the prefix of every directive "
                                 "line, e.g. '#'")
            if any(not d.startswith(self.comment_prefix) for d in [self.header_directive or '', *self.directives] if d):
                raise ValueError(f"directives start with the comment_prefix '{self.comment_prefix}'")
        if self.header_directive and self.header is not True:
            raise ValueError("header_directive names the columns: give header: true with it")
        return self


def directive_field(directive: str) -> str:
    """The field a directive's value is kept in: the directive as the file writes it, without a trailing colon."""
    return directive.strip().rstrip(':').strip()


def named_columns_pattern(spec: SplitterSpec) -> str:
    """The regex for a record of named columns (no header line): one group per column, quoted or not. A quoted
    cell's group is its text without the quotes, a doubled quote inside kept doubled (as Data Splitter's containers
    leave it). Seen: a value written as $1$2 from two groups, which Stroom refuses when one took no part."""
    d = re.escape(spec.delimiter)
    if len(spec.delimiter) > 1:
        # A separator of several characters (' ~ ', '||'): a column runs to the next whole separator.
        return '^' + d.join(f'((?:(?!{d}).)*)' for _ in spec.header) + '$'
    if spec.quote:
        q = re.escape(spec.quote)
        cell = f'{q}?((?<={q})(?:[^{q}]|{q}{q})*(?={q})|(?<!{q})[^{d}{q}]*){q}?'
    else:
        cell = f'([^{d}]*)'
    return '^' + d.join(cell for _ in spec.header) + '$'


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
    # Named columns without a header line: one regex over the record, one group per column.
    lines = [f'{indent}<regex pattern="{_attr(named_columns_pattern(spec))}">']
    lines += [f'{indent}  <data name="{_attr(name)}" value="${n}" />' for n, name in enumerate(spec.header, 1)]
    lines.append(f'{indent}</regex>')
    return '\n'.join(lines) + '\n'


def _directives_xml(spec: SplitterSpec) -> str:
    """One record per line of a log with directive lines (W3C, Zeek): the header directive sets the column names for
    the lines after it, each other directive kept is held for the records after it, other comment lines are passed
    over, and each data line is split under the latest column names."""
    lines = ['  <split delimiter="\\n">', '    <group>']
    if spec.header_directive:
        lines += [f'      <regex pattern="^{_attr(re.escape(spec.header_directive))}\\s*(.*)$">',
                  '        <group value="$1">',
                  f'          <split delimiter="{_attr(spec.delimiter)}"{_container(spec)}>',
                  '            <var id="heading" />', '          </split>', '        </group>', '      </regex>']
    for n, directive in enumerate(spec.directives):
        lines += [f'      <regex pattern="^{_attr(re.escape(directive))}\\s*(.*)$">',
                  f'        <var id="directive{n}" />', '      </regex>']
    lines += [f'      <regex pattern="^{_attr(re.escape(spec.comment_prefix))}.*$" />', '      <regex pattern="^(.+)$">']
    lines += [f'        <data name="{_attr(directive_field(d))}" value="$directive{n}$1" />'
              for n, d in enumerate(spec.directives)]
    lines += ['        <group value="$1">', _record_xml(spec, '          ').rstrip('\n'), '        </group>',
              '      </regex>', '    </group>', '  </split>']
    return '\n'.join(lines) + '\n'


def record_regex(start: str) -> str:
    """The regex taking one record of several lines: from a line that starts one to the next such line, or the end."""
    return f'(?sm)^((?:{start}).*?)\\n?(?=^(?:{start})|\\z)'


def record_end_regex(end: str) -> str:
    """The regex taking one record whose last line is marked: lines that aren't an end, then the end line."""
    return f'(?m)^((?:(?!(?:{end}))[^\\n]*\\n)*(?:{end})[^\\n]*)\\n?'


def generate_splitter(spec: SplitterSpec) -> str:
    """The Data Splitter XML: one record per line, divided as the spec says (or per record start, for records of
    several lines)."""
    if spec.kind == 'delimited' and spec.comment_prefix:
        return DS_HEAD + _directives_xml(spec) + DS_TAIL
    if (spec.record_start or spec.record_end) and spec.kind != 'delimited':
        pattern = record_regex(spec.record_start) if spec.record_start else record_end_regex(spec.record_end)
        return (DS_HEAD + f'  <regex pattern="{_attr(pattern)}">\n    <group value="$1">\n'
                + _record_xml(spec, '      ') + '    </group>\n  </regex>\n' + DS_TAIL)
    body = ''
    if spec.kind == 'delimited' and spec.header is True:
        body += (f'  <split delimiter="\\n" maxMatch="1">\n    <group>\n'
                 f'      <split delimiter="{_attr(spec.delimiter)}"{_container(spec)}>\n        <var id="heading" />\n'
                 f'      </split>\n    </group>\n  </split>\n')
    # A new line inside quotes is part of the value when quoted_newlines says so: the records' own split ignores it.
    lines_container = _container(spec) if spec.quoted_newlines else ''
    if lines_container and body:
        body = body.replace('<split delimiter="\\n" maxMatch="1">', f'<split delimiter="\\n" maxMatch="1"{lines_container}>')
    body += (f'  <split delimiter="\\n"{lines_container}>\n    <group>\n{_record_xml(spec, "      ")}    </group>\n'
             f'  </split>\n')
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
            # A key given again (rcpt= once per recipient) is several data elements in Stroom; a field reads the
            # first (repeat: true writes each), so the dry run keeps the first, not the last.
            fields.setdefault(found.group(1), found.group(2))
            at = found.end()
        if not fields:
            return None
    elif spec.header is True:
        cells = _split_quoted(text, spec.delimiter, spec.quote)
        fields = {heading[n]: cell for n, cell in enumerate(cells) if heading and n < len(heading)}
    else:
        # The same regex the converter runs.
        m = re.match(named_columns_pattern(spec), text)
        if not m:
            return None
        fields = dict(zip(spec.header, m.groups()))
    if spec.body and spec.body_field in fields:
        inner = _apply_record(spec.body, fields[spec.body_field], None)
        if not inner and fields[spec.body_field].strip():
            # Stroom reports a body its expressions don't match ("Expressions failed to match all of the content"):
            # so does the dry run, rather than keeping the record without the body's fields (seen: a Palo Alto body
            # short of a column, fine here and refused in Stroom).
            return None
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
    """How syslog message bodies divide further, when they do: key=value pairs, or delimited columns (as Palo Alto
    firewalls send them: a syslog header, then their CSV), named by position for the model to rename."""
    pairs = [len(KEY_VALUE.findall(m)) for m in messages if m.strip()]
    if pairs and sum(1 for n in pairs if n >= 2) >= 0.8 * len(pairs):
        quote = '"' if any('="' in m for m in messages) else None
        return SplitterSpec(kind='key_value', delimiter=' ', quote=quote)
    bodies = [m for m in messages if m.strip()]
    for delimiter in (',', '|', '\t', ';'):
        quote = '"' if any('"' in m for m in bodies) else None
        widths = [len(_split_quoted(m, delimiter, quote)) for m in bodies]
        if widths and min(widths) >= 6 and sum(w == widths[0] for w in widths) >= 0.8 * len(widths):
            # Columns named by position: a vendor's own names (its documentation) are the model's to give.
            return SplitterSpec(kind='delimited', delimiter=delimiter, quote=quote,
                                header=[f'col{i + 1}' for i in range(max(widths))])
    return None


def infer_spec(sample: str) -> tuple['SplitterSpec | None', dict[str, Any]]:
    """A spec for the sample's format, from its profile: (spec, profile). None when the format needs no text
    converter (JSON, XML) or cannot be inferred (free text, which needs a regex from a human)."""
    info = profile(sample)
    fmt = info['format']
    lines = [l for l in sample.splitlines() if l.strip()]
    if fmt == 'delimited' and info.get('header_directive'):
        return SplitterSpec(kind='delimited', delimiter=info['delimiter'], header=True, quote=info.get('quote'),
                            comment_prefix=info['comment_prefix'], header_directive=info['header_directive'],
                            directives=info.get('directives') or []), info
    if fmt == 'delimited' and info.get('quoted_newlines'):
        return SplitterSpec(kind='delimited', delimiter=info.get('delimiter', ','), header=bool(info.get('has_header'))
                            or list(info.get('columns') or []), quote='"', quoted_newlines=True), info
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
    if fmt == 'cri':
        # Kubernetes CRI: a record ends at its 'F' line; the parts after the first carry their own prefixes, which
        # the mapping takes out to join them (CRI_JOIN).
        return SplitterSpec(kind='regex', record_end=CRI_END, pattern=r'(?s)^(\S+) (stdout|stderr) [PF] (.*)$',
                            names=['time', 'stream', 'message']), info
    if fmt == 'multi-line text':
        # The start and the time it begins with; the rest of the record is the message, new lines and all: the
        # record's own pattern (level, thread, logger...) is the model's to give, checked by the dry run.
        start = info['record_start']
        return SplitterSpec(kind='regex', record_start=start, pattern=rf'(?s)^({start}\S*)\s+(.*)$',
                            names=['time', 'message']), info
    if fmt in ('syslog rfc3164', 'syslog rfc5424'):
        rfc = 'rfc3164' if fmt.endswith('3164') else 'rfc5424'
        head = SplitterSpec(kind='syslog', rfc=rfc)
        messages = [r.get('message', '') for r in (dry_run(head, sample)['records'])]
        return SplitterSpec(kind='syslog', rfc=rfc, body=_body_spec(messages)), info
    return None, info


def dry_run(spec: SplitterSpec, sample: str, max_records: int = 2000) -> dict[str, Any]:
    """What the generated splitter makes of the sample: records as name -> value, lines that match nothing,
    and the field names seen (what a mapping's `field` may name). A longer sample is read at lines spread over it:
    seen in an eval case (a day's CEF file), the first 200 lines held only two kinds of event, and a mapping's field
    from a later kind was refused as in none of the sample's records."""
    lines = [line for line in sample.splitlines() if line.strip()]
    heading = None
    if spec.record_start and spec.kind != 'delimited':
        lines = group_lines(lines, spec.record_start)
    elif spec.record_end and spec.kind != 'delimited':
        lines = group_lines_to_end(lines, spec.record_end)
    if spec.kind == 'delimited' and spec.quoted_newlines and spec.quote:
        lines = quoted_lines(sample, spec.quote)
    if spec.kind == 'delimited' and spec.comment_prefix:
        return _dry_run_directives(spec, lines, max_records)
    if spec.kind == 'delimited' and spec.header is True and lines:
        heading = [h.strip() for h in _split_quoted(lines[0], spec.delimiter, spec.quote)]
        lines = lines[1:]
    if len(lines) > max_records:
        lines = [lines[round(i * (len(lines) - 1) / (max_records - 1))] for i in range(max_records)]
    records, unmatched = [], []
    for line in lines:
        fields = _apply_record(spec, line, heading)
        if fields:
            records.append(fields)
        else:
            unmatched.append(line[:200])
    names: list[str] = []
    for record in records:
        names += [k for k in record if k not in names]
    return {'records': records, 'unmatched_lines': unmatched, 'fields': names, 'heading': heading}


def quoted_lines(text: str, quote: str) -> list[str]:
    """Records of delimited text whose quoted values may hold new lines: lines joined while a quote is open."""
    records: list[str] = []
    current, open_ = None, False
    for line in text.splitlines():
        if current is None:
            if not line.strip():
                continue
            current = line
        else:
            current += '\n' + line
        open_ = current.count(quote) % 2 == 1
        if not open_:
            records.append(current)
            current = None
    if current is not None:
        records.append(current)
    return records


def group_lines_to_end(lines: list[str], end: str) -> list[str]:
    """Lines joined into records that end at a line matching `end` (an unfinished last record kept as it is)."""
    ends = re.compile(end)
    records: list[str] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        if ends.match(line):
            records.append('\n'.join(current))
            current = []
    if current:
        records.append('\n'.join(current))
    return records


def group_lines(lines: list[str], start: str) -> list[str]:
    """Lines joined into records: a line matching `start` begins one, the lines after it until the next continue it
    (lines before the first start are a record of their own, as the splitter would leave them)."""
    begins = re.compile(start)
    records: list[str] = []
    for line in lines:
        if begins.match(line) or not records:
            records.append(line)
        else:
            records[-1] += '\n' + line
    return records


def _dry_run_directives(spec: SplitterSpec, lines: list[str], max_records: int) -> dict[str, Any]:
    """dry_run for a log with directive lines: read in order (the columns and directives hold for the lines after
    them), then records spread over it kept, as for any long sample."""
    heading, held, records, unmatched = None, {}, [], []
    for line in lines:
        if spec.header_directive and line.startswith(spec.header_directive):
            heading = [h.strip() for h in _split_quoted(line[len(spec.header_directive):].strip(), spec.delimiter,
                                                        spec.quote)]
            continue
        directive = next((d for d in spec.directives if line.startswith(d)), None)
        if directive:
            held[directive_field(directive)] = line[len(directive):].strip()
            continue
        if line.startswith(spec.comment_prefix):
            continue
        fields = _apply_record(spec, line, heading if spec.header is True else None)
        if fields:
            records.append({**{directive_field(d): held.get(directive_field(d), '') for d in spec.directives}, **fields})
        else:
            unmatched.append(line[:200])
    if len(records) > max_records:
        records = [records[round(i * (len(records) - 1) / (max_records - 1))] for i in range(max_records)]
    names: list[str] = []
    for record in records:
        names += [k for k in record if k not in names]
    return {'records': records, 'unmatched_lines': unmatched, 'fields': names, 'heading': heading}
