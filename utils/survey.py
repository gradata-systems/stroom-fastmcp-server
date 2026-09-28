"""Surveying raw data for the kinds of event it holds.

A feed's streams rarely show every event type in one stream, so the agent samples stream after stream and
groups records into shapes, one per kind of event, until a few more streams add nothing new. A shape is:

- JSON, XML, key=value: which fields a record has, plus the values of fields that usually name the event
  (action, event, type, category and similar).
- Delimited: the values of those naming columns, or of the low-variety columns when none is named that way.
- Syslog and other text: the message with its variable parts masked (numbers, addresses, quoted strings),
  merged with other messages that differ only in a few words (user names, hosts), in the style of Drain.
"""
import csv
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from utils.profile import SYSLOG_3164, SYSLOG_5424, _flatten, profile

NAMING = re.compile(r'(^|[._-])(type|event|event_?type|event_?name|event_?id|action|category|activity|operation|'
                    r'op|kind|result_?type|msg_?id|message_?id|log_?type|subtype)$', re.I)
_MASKS = [(re.compile(r'"[^"]*"'), '<s>'), (re.compile(r"'[^']*'"), '<s>'),
          (re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.]+\b'), '<email>'),
          (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b'), '<ip>'),
          (re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'), '<uuid>'),
          (re.compile(r'\b(?:0x)?[0-9a-fA-F]{12,}\b'), '<hex>')]
_TOKEN_WITH_DIGIT = re.compile(r'\S*\d\S*')
WILD = '<*>'


@dataclass
class Chunk:
    """One stream part split into records, with what is needed to write a sample back in the same form."""
    format: str
    records: list[str]                         # each record's text, as it would appear in a sample
    parsed: list[dict[str, Any] | None]        # structured view where the format has one
    header: str | None = None                  # delimited: the header line
    root: str | None = None                    # xml: the root element's opening and closing tags
    naming_columns: list[str] = field(default_factory=list)


def split_records(text: str, max_records: int = 5000) -> Chunk:
    """Split one stream part into records, using the same detection as profile_sample."""
    text = text.strip('﻿\r\n ')
    info = profile(text[:200_000], max_records=200)
    fmt = info['format']
    lines = [line for line in text.splitlines() if line.strip()]
    if fmt == 'xml':
        root = etree.fromstring(text.encode('utf-8'))
        records = [r for r in root if isinstance(r.tag, str)][:max_records]
        qname = etree.QName(root)
        opening = f'<{qname.localname}' + (f' xmlns="{qname.namespace}"' if qname.namespace else '') + '>'
        return Chunk(fmt, [_xml_text(r) for r in records],
                     [_xml_fields(r) for r in records], root=f'{opening}\n{{records}}\n</{qname.localname}>')
    if fmt == 'json array':
        data = [r for r in json.loads(text) if isinstance(r, dict)][:max_records]
        return Chunk(fmt, [json.dumps(r) for r in data], [_flatten(r) for r in data])
    if fmt == 'json lines':
        data = [json.loads(line) for line in lines[:max_records]]
        return Chunk(fmt, lines[:max_records], [_flatten(r) for r in data])
    if fmt == 'key=value':
        from utils.profile import KEY_VALUE
        return Chunk(fmt, lines[:max_records],
                     [dict((k, v.strip('"')) for k, v in KEY_VALUE.findall(line)) for line in lines[:max_records]])
    if fmt == 'delimited':
        header = lines[0] if info.get('has_header') else None
        body = lines[1:] if header else lines
        rows = list(csv.reader(io.StringIO('\n'.join(body[:max_records])), delimiter=info.get('delimiter', ',')))
        columns = info.get('columns') or []
        parsed = [dict(zip(columns, row)) for row in rows]
        return Chunk(fmt, body[:max_records], parsed, header=header, naming_columns=_naming_columns(columns, parsed))
    return Chunk(fmt, lines[:max_records], [None] * min(len(lines), max_records))


def _xml_text(node: etree._Element) -> str:
    text = etree.tostring(node, encoding='unicode', with_tail=False)
    return re.sub(r'\s+xmlns(:\w+)?="[^"]*"', '', text, count=1) if node.nsmap else text


def _xml_fields(node: etree._Element) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for el in node.iter():
        if not isinstance(el.tag, str):
            continue
        path = etree.QName(el).localname
        for attr, value in el.attrib.items():
            out[f'{path}@{attr}'] = value
        if len(el) == 0 and (el.text or '').strip():
            out[path] = el.text.strip()
    return out


def _naming_columns(columns: list[str], rows: list[dict[str, Any]]) -> list[str]:
    named = [c for c in columns if NAMING.search(c)]
    if named or not rows:
        return named
    out = []
    for column in columns:
        values = [r.get(column) for r in rows if r.get(column)]
        distinct = set(values)
        if 2 <= len(distinct) <= 8 and len(values) >= 4 * len(distinct) and not any(
                _TOKEN_WITH_DIGIT.fullmatch(v or '') for v in distinct):
            out.append(column)
    return out


def mask(message: str) -> list[str]:
    for pattern, token in _MASKS:
        message = pattern.sub(token, message)
    return ['<n>' if _TOKEN_WITH_DIGIT.fullmatch(t) and not t.startswith('<') else t for t in message.split()]


def _syslog_parts(line: str) -> tuple[str, str]:
    """(program, message) for syslog lines; ('', line) otherwise."""
    m = re.match(r'^<\d+>1 \S+ \S+ (\S+) \S+ \S+ (?:\[.*?\]|-) ?(.*)$', line)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r'^(?:<\d+>)?\w{3} [ \d]\d \d{2}:\d{2}:\d{2} \S+ ([^:\[\s]+)(?:\[\d+\])?: ?(.*)$', line)
    if m:
        return m.group(1), m.group(2)
    return '', line


class Shapes:
    """Groups records into shapes across calls; `known` signatures (from an earlier survey) are recognised."""

    def __init__(self, known: list[str] | None = None, examples: int = 2):
        self.shapes: dict[str, dict[str, Any]] = {}
        self.examples = examples
        self._templates: dict[tuple[str, int], list[list[str]]] = {}
        for signature in known or []:
            self._add_shape(signature, known=True)
            if signature.startswith('text:'):
                program, _, template = signature[5:].partition('|')
                tokens = template.split(' ')
                self._templates.setdefault((program, len(tokens)), []).append(tokens)

    def _add_shape(self, signature: str, known: bool = False) -> dict[str, Any]:
        return self.shapes.setdefault(signature, {'signature': signature, 'known': known, 'count': 0,
                                                  'examples': [], 'streams': []})

    def signature(self, chunk: Chunk, index: int) -> str:
        parsed, record = chunk.parsed[index], chunk.records[index]
        if chunk.format == 'delimited':
            columns = chunk.naming_columns
            return 'row:' + (', '.join(f'{c}={parsed.get(c, "")}' for c in columns) if columns else 'all')
        if parsed is not None:
            keys = sorted(parsed)
            naming = sorted(f'{k}={parsed[k]}' for k in keys if NAMING.search(k.split('@')[-1]) and
                            isinstance(parsed[k], (str, int, bool)) and len(str(parsed[k])) <= 60)
            return 'fields:' + ','.join(keys) + (' | ' + ', '.join(naming) if naming else '')
        program, message = _syslog_parts(record)
        tokens = mask(message)
        bucket = self._templates.setdefault((program, len(tokens)), [])
        for template in bucket:
            same = sum(a == b for a, b in zip(template, tokens))
            if tokens and same / len(tokens) >= 0.6 and template[0] == tokens[0]:
                merged = [a if a == b else WILD for a, b in zip(template, tokens)]
                old = f"text:{program}|{' '.join(template)}"
                new = f"text:{program}|{' '.join(merged)}"
                if new != old and old in self.shapes:
                    self.shapes[new] = {**self.shapes.pop(old), 'signature': new}
                template[:] = merged
                return new
        bucket.append(tokens)
        return f"text:{program}|{' '.join(tokens)}"

    def add(self, chunk: Chunk, index: int, stream_id: int) -> bool:
        """Count a record; True if it started a shape not seen before (known ones included)."""
        signature = self.signature(chunk, index)
        new = signature not in self.shapes
        shape = self._add_shape(signature)
        shape['count'] += 1
        if stream_id not in shape['streams']:
            shape['streams'].append(stream_id)
        if len(shape['examples']) < self.examples and chunk.records[index] not in shape['examples']:
            shape['examples'].append(chunk.records[index])
        return new


def sample_text(chunk: Chunk, examples: list[str]) -> str:
    """Records written back in the form their stream had, ready for upload_sample."""
    if chunk.format == 'json array':
        return '[' + ',\n'.join(examples) + ']'
    if chunk.format == 'xml' and chunk.root:
        return chunk.root.replace('{records}', '\n'.join(examples))
    if chunk.format == 'delimited' and chunk.header:
        return '\n'.join([chunk.header, *examples]) + '\n'
    return '\n'.join(examples) + '\n'


def share(counts: Counter, key: str) -> float:
    total = sum(counts.values())
    return round(100 * counts[key] / total, 1) if total else 0.0


__all__ = ['Chunk', 'Shapes', 'mask', 'sample_text', 'split_records', 'SYSLOG_3164', 'SYSLOG_5424']
