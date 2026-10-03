"""Check a translation mapping against the sample it is for, before anything is created or stepped.

The sample is read locally into records the way the pipeline's parser would (JSON keys with dots, Data Splitter
names through the splitter spec's dry run, XML paths by local name), and the mapping's inputs are looked up in
them: a field no record has (a typo, or a name from a different variant of the source) and a time_format the
sample's values do not fit are the two mistakes that otherwise only show at stepping, one record at a time.
"""
import difflib
import json
import re
from typing import Any

from lxml import etree

from utils.dsgen import SplitterSpec, dry_run
from utils.profile import _XML_DECL, _flatten, xml_fragments
from utils.timefmt import check_time_format
from utils.xsltgen import TranslationMapping, reads_input

_STEP = re.compile(r"^(?:@(?P<attr>[\w.-]+)|(?P<name>[\w.-]+|\*)(?:\[@(?P<pattr>[\w.-]+)=(?P<q>['\"])(?P<pval>.*?)(?P=q)\])?)$")


def _local(tag) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ''


def xml_value(record: etree._Element, path: str) -> str | None:
    """The text a simple relative path selects in a record, matching by local name: 'System/Computer',
    'System/TimeCreated/@SystemTime', "EventData/Data[@Name='IpAddress']". None when it selects nothing;
    anything beyond this shape (functions, unions) is not evaluated."""
    nodes: list = [record]
    for segment in [s for s in path.strip('/').split('/') if s and s != '.']:
        m = _STEP.match(segment)
        if not m:
            return None
        if m.group('attr'):
            values = [n.get(m.group('attr')) for n in nodes if isinstance(n, etree._Element)]
            values = [v for v in values if v is not None]
            return values[0] if values else None
        found = []
        for node in nodes:
            if not isinstance(node, etree._Element):
                continue
            for child in node:
                if _local(child.tag) and (m.group('name') == '*' or _local(child.tag) == m.group('name')):
                    if m.group('pattr') and child.get(m.group('pattr')) != m.group('pval'):
                        continue
                    found.append(child)
        nodes = found
        if not nodes:
            return None
    texts = [''.join(n.itertext()).strip() for n in nodes if isinstance(n, etree._Element)]
    return texts[0] if texts else None


def sample_records(mapping: TranslationMapping, sample: str | list[str], splitter: SplitterSpec | None = None
                   ) -> tuple[list[Any], str | None]:
    """(records, note): dicts for data_splitter and json inputs, elements for xml inputs. Several sample files are
    read one by one (each with its own header line). The note says why nothing could be read."""
    if isinstance(sample, list):
        records, notes = [], []
        for text in sample:
            found, note = sample_records(mapping, text, splitter)
            records += found
            if note and note not in notes:
                notes.append(note)
        return records, '; '.join(notes) or None
    text = sample.strip('﻿\r\n ')
    if mapping.input == 'data_splitter':
        if splitter is None:
            return [], "no splitter spec: the sample's fields are not known here (give splitter, from build_data_splitter)"
        return dry_run(splitter, text)['records'], None
    if mapping.input == 'json':
        values: list = []
        if text.startswith('['):
            try:
                values = [v for v in json.loads(text) if isinstance(v, dict)]
            except ValueError:
                return [], 'the sample is not a JSON array'
        else:
            for line in text.splitlines():
                if line.strip():
                    try:
                        value = json.loads(line)
                    except ValueError:
                        return [], 'the sample is neither a JSON array nor JSON lines'
                    if isinstance(value, dict):
                        values.append(value)
        return [_flatten(v) for v in values], None
    try:
        root = etree.fromstring(_XML_DECL.sub('', text, count=1).encode('utf-8')) if mapping.input == 'xml' else None
    except etree.XMLSyntaxError:
        root = None
    if mapping.input == 'xml_fragments' or root is None:
        fragments = xml_fragments(text) or []
        if not fragments:
            return [], 'the sample is not XML a parser could read'
        record = mapping.record or _local(fragments[0].tag)
        return [f for f in fragments if _local(f.tag) == record], None
    record = mapping.record
    return [e for e in root.iter() if isinstance(e.tag, str) and _local(e.tag) == record], None


def _value_of(record: Any, field: str) -> str | None:
    if isinstance(record, dict):
        value = record.get(field)
        return None if value is None else (value if isinstance(value, str) else json.dumps(value))
    return xml_value(record, field)


def _items_of(record: Any, path: str) -> list[Any]:
    """The items a record holds under path: a JSON array's objects, or XML elements the path selects."""
    if isinstance(record, dict):
        value = record.get(path)
        return [_flatten(v) for v in value if isinstance(v, dict)] if isinstance(value, list) else []
    nodes: list = [record]
    for segment in [s for s in path.strip('/').split('/') if s]:
        m = _STEP.match(segment)
        if not m or m.group('attr'):
            return []
        nodes = [c for n in nodes if isinstance(n, etree._Element) for c in n
                 if _local(c.tag) and (m.group('name') == '*' or _local(c.tag) == m.group('name'))]
    return nodes


def check_mapping(mapping: TranslationMapping, records: list[Any]) -> dict[str, Any]:
    """Fields the mapping names that no sample record has (with close matches), and time formats the sample's
    values do not fit. Fields an extraction produces are known, not looked for."""
    problems, warnings = [], []
    if not records:
        return {'records': 0, 'problems': problems, 'warnings': warnings, 'fields_seen': []}
    seen: list[str] = []
    for record in records:
        if isinstance(record, dict):
            seen += [k for k in record if k not in seen]
    derived = {n for ex in mapping.extract for n in ex.names if n}
    items: list[Any] = [i for r in records for i in _items_of(r, mapping.for_each)] if mapping.for_each else []
    if mapping.for_each and not items:
        warnings.append(f"for_each '{mapping.for_each}' selects no items in any of the {len(records)} sample records")
    for item in items:
        if isinstance(item, dict):
            seen += [k for k in item if k not in seen]

    def where(scope: str | None) -> list[Any]:
        return items if mapping.for_each and scope != 'record' else records

    entries = list(mapping.common) + [f for rule in mapping.events for f in rule.fields]
    wanted: dict[tuple[str, str | None], list[str]] = {}
    for entry in entries:
        for name in ([entry.field] if entry.field else []) + list(entry.any_of or []) + (
                [entry.lookup.field] if entry.lookup and entry.lookup.field else []):
            wanted.setdefault((name, entry.scope), []).append(entry.path)
    for rule in mapping.events:
        for c in rule.when:
            if c.field:
                wanted.setdefault((c.field, c.scope), []).append(f'[{rule.name}] when')
    for d in mapping.drop_when:
        for c in d.when:
            if c.field:
                wanted.setdefault((c.field, c.scope), []).append(f'drop: {d.reason}')
    for ex in mapping.extract:
        if ex.field:
            wanted.setdefault((ex.field, ex.scope), []).append('extract')
    suggested: set[str] = set()   # offered as 'did you mean': not said again as unread
    for (name, scope), used in wanted.items():
        if name in derived:
            continue
        pool = where(scope)
        values = [v for v in (_value_of(r, name) for r in pool) if v is not None]
        if not values:
            close = difflib.get_close_matches(name, seen, n=3, cutoff=0.6) if seen else []
            suggested.update(close)
            what = 'sample items' if pool is items else 'sample records'
            warnings.append(f"field '{name}' (used for {used[0]}{' and more' if len(used) > 1 else ''}) is in none of "
                            f"the {len(pool)} {what}" + (f"; did you mean {close}?" if close else '')
                            + (f". Fields seen: {seen[:30]}" if seen and not close else ''))
    for entry in entries:
        if not entry.time_format or not entry.field or entry.field in derived:
            continue
        values = [v for v in (_value_of(r, entry.field) for r in where(entry.scope)) if v]
        if not values:
            continue
        message = check_time_format(entry.time_format, values)
        if message:
            fitting = len(values) - len([v for v in values if check_time_format(entry.time_format, [v])])
            (problems if fitting == 0 else warnings).append(f"{entry.path}: {message}")
    unread = [k for k in seen if k not in derived and k not in suggested and not reads_input(mapping, k)]
    if unread:
        # A parsed field no output reads is lost from every event: often one the request asked for (a source address).
        warnings.append(f"fields in the sample that nothing reads: {unread[:12]}{' ...' if len(unread) > 12 else ''}: "
                        f"map each to the element that means it (a Data entry if nothing else fits), or leave it out on "
                        f"purpose.")
    return {'records': len(records), 'problems': problems, 'warnings': warnings, 'fields_seen': seen[:60]}
