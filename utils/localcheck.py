"""Check a translation mapping against the sample it is for, before anything is created or stepped.

The sample is read locally into records the way the pipeline's parser would (JSON keys with dots, Data Splitter
names through the splitter spec's dry run, XML paths by local name), and the mapping's inputs are looked up in
them: a field no record has (a typo, or a name from a different variant of the source) and a time_format the
sample's values do not fit are the two mistakes that otherwise only show at stepping, one record at a time.
"""
import difflib
import json
import re
from collections import Counter
from typing import Any

from lxml import etree

from utils.actions import action_rules, described
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


def with_json_fields(records: list[Any], json_fields: list[str]) -> list[Any]:
    """Records with the keys of the JSON their json_fields hold added as <field>.<key>, as the XSLT reads them."""
    if not json_fields:
        return records
    for record in records:
        if not isinstance(record, dict):
            continue
        for field in json_fields:
            try:
                held = json.loads(record.get(field) or '')
            except (TypeError, ValueError):
                continue
            if isinstance(held, dict):
                for key, value in _flatten(held).items():
                    record.setdefault(f'{field}.{key}', value if isinstance(value, str) else json.dumps(value))
    return records


def sample_records(mapping: TranslationMapping, sample: str | list[str], splitter: SplitterSpec | None = None
                   ) -> tuple[list[Any], str | None]:
    found, note = _sample_records(mapping, sample, splitter)
    return with_json_fields(found, mapping.json_fields), note


def _sample_records(mapping: TranslationMapping, sample: str | list[str], splitter: SplitterSpec | None = None
                    ) -> tuple[list[Any], str | None]:
    """(records, note): dicts for data_splitter and json inputs, elements for xml inputs. Several sample files are
    read one by one (each with its own header line). The note says why nothing could be read."""
    if isinstance(sample, list):
        records, notes = [], []
        for text in sample:
            found, note = _sample_records(mapping, text, splitter)
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


_TOKEN = re.compile(r'^[A-Za-z][\w.:-]{0,39}$')


def _holds(c: Any, record: Any, derived: set[str], extracted: dict[str, str] | None = None) -> bool | None:
    """Whether a field condition holds for a record, as the XSLT would test it; None when it cannot be told here (an
    xpath, a dictionary, or a field an extraction produces, unless its values are given)."""
    if c.xpath is not None or c.in_dictionary is not None or c.scope == 'record':
        return None
    if c.field in derived and (extracted is None or c.field not in extracted):
        return None
    value = extracted[c.field] if c.field in derived else _value_of(record, c.field)
    if c.present is not None:
        return bool(value and value.strip()) == c.present
    if value is None:
        return False
    if c.equals is not None:
        return value == c.equals
    if c.one_of is not None:
        return value in c.one_of
    try:
        return re.search(c.matches, value) is not None
    except re.error:
        return None


def extracted_values(mapping: TranslationMapping, record: Any) -> dict[str, str] | None:
    """The values the mapping's extractions give a record (the first match's groups, '' where it doesn't match), as
    the XSLT's analyze-string does; None when one can't be run here (an xpath source, a regex Python can't read)."""
    flags = {'i': re.I, 'm': re.M, 's': re.S, 'x': re.X}
    values: dict[str, str] = {}
    for ex in mapping.extract:
        if ex.xpath is not None or not ex.field:
            return None
        text = values.get(ex.field) if ex.field in values else _value_of(record, ex.field)
        try:
            found = re.search(ex.regex, text or '', sum(flags.get(f, 0) for f in ex.flags))
        except re.error:
            return None
        for i, name in enumerate(ex.names, 1):
            if name:
                values[name] = (found.group(i) or '') if found and i <= (found.re.groups or 0) else ''
    return values


def rule_of(mapping: TranslationMapping, record: Any, extracted: dict[str, str] | None = None) -> str | None:
    """The rule a record falls into: a drop rule ('drop: reason'), an event rule's name, '' when none matches, or None
    when a condition on the way cannot be told here."""
    derived = {n for ex in mapping.extract for n in ex.names if n}
    candidates = [(f'drop: {d.reason}', d.when) for d in mapping.drop_when] + [(r.name, r.when) for r in mapping.events]
    for name, when in candidates:
        tests = [_holds(c, record, derived, extracted) for c in when]
        if any(t is None for t in tests):
            return None
        if all(tests):
            return name
    return ''


def traits(records: list[Any]) -> str:
    """What a set of records holds, for a person to judge: each field's distinct short, word-like values (kinds,
    actions, levels, users), then an example of the first free-text field. Times, addresses and numbers are left out."""
    fields: dict[str, list[str]] = {}
    text: tuple[str, str] | None = None
    for record in records:
        if not isinstance(record, dict):
            continue
        for name, raw in record.items():
            value = raw if isinstance(raw, str) else None
            if not value or not value.strip():
                continue
            if _TOKEN.match(value):
                seen = fields.setdefault(name, [])
                if value not in seen:
                    seen.append(value)
            elif text is None and len(value.split()) > 2:
                text = (name, value[:80])
    parts = [f"{name}: {', '.join(values[:8])}{' ...' if len(values) > 8 else ''}" for name, values in list(fields.items())[:6]]
    if text:
        parts.append(f'e.g. {text[0]}: "{text[1]}"')
    return '; '.join(parts)


def unknown_coverage(mapping: TranslationMapping, records: list[Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """(problems, kept): sample records that rules writing EventDetail/Unknown catch. A rule without allow_unknown that
    catches any is a problem naming what the records hold (the conditions' rule is refused by the generator too; the
    rule for the rest is refused here); a rule with it is reported for the user to confirm."""
    if mapping.for_each or not records:
        return [], []
    from utils.xsltgen import writes_unknown as unknown_in
    writes_unknown = {r.name: r for r in mapping.events if unknown_in(mapping, r)}
    caught: dict[str, list[Any]] = {}
    for record in records:
        name = rule_of(mapping, record)
        if name in writes_unknown:
            caught.setdefault(name, []).append(record)
    problems, kept = [], []
    common = {f.path.strip('/'): f.field for f in mapping.common if f.field}
    for name, found in caught.items():
        rule, held = writes_unknown[name], traits(found)
        # Values that show the action (ALLOW/DENY between addresses, LOGIN/LOGOUT, CONFIG_CHANGE) get the rules for it.
        names = list(dict.fromkeys(k for r in found if isinstance(r, dict) for k in r))
        shown, left, split = action_rules(found, names, [c.model_dump(exclude_none=True) for c in rule.when], name,
                                          common.get('EventSource/User/Id'), common.get('EventDetail/Description'),
                                          skip={c.field for c in rule.when if c.field})
        use = (f" Their values show the action: {described(shown)}. Add these rules before '{name}' (they validate as "
               f"they are; add Data entries as you like){f', leaving {split} {left} to it' if left else ''}: "
               f"{json.dumps(shown)}") if shown else ''
        # Connections allowed or denied, and logons, are never unknown; a configuration change is the user's call.
        plain = [r for r in shown if any(f['path'].startswith(('EventDetail/Network/', 'EventDetail/Authenticate/'))
                                         for f in r['fields'])]
        # Every record it catches has a rule from its own values (an alert, a service starting, a configuration
        # saved): seen, an agent folded the draft's rules into 'other' as Unknown and the user had to decline the form.
        covered = bool(shown) and not left
        if rule.allow_unknown and (plain or covered) and not rule.keep_unknown:
            values = {v for r in (shown if covered else plain)
                      for v in (r['when'][-1].get('one_of') or [r['when'][-1].get('equals')])}
            known = sum(1 for r in found if isinstance(r, dict) and r.get(split) in values)
            problems.append(f"[{name}] can't be kept as Unknown: {known} of its {len(found)} sample records are not "
                            f"unknown events. Only if the user, shown the rules below, still wants them Unknown (they "
                            f"can say so in the chat): keep_unknown: true on the rule with allow_unknown, and they "
                            f"confirm it in the form.{use}")
        elif rule.allow_unknown:
            # keep_unknown: the user chose Unknown over the action elements the values suggest; the form shows them.
            kept.append({'rule': name, 'reason': rule.allow_unknown, 'records': len(found), 'sample': held,
                         **({'suggested': f"{described(shown)} (rather than Unknown)"} if shown else {}),
                         **({'against_suggestion': True} if rule.keep_unknown and shown else {})})
        elif rule.when:
            problems.append(f"[{name}] keeps EventDetail/Unknown for {len(found)} of the {len(records)} sample records: "
                            f"{held}. Give them the action element these values describe, a rule per kind if they "
                            f"differ (split on the field that tells them apart).{use}")
        else:
            problems.append(f"[{name}] (the rule for records no other rule matches) writes EventDetail/Unknown for "
                            f"{len(found)} of the {len(records)} sample records: {held}. Give them rules with the action "
                            f"elements these values describe; only if none fits, set allow_unknown on this rule to the "
                            f"reason, which the user confirms.")
    return problems, kept


def shared_type_ids(mapping: TranslationMapping, records: list[Any]) -> list[str]:
    """A TypeId read from one field for every rule that, in the sample, is the same for records of different rules:
    events of different kinds share it. Seen: TypeId the SecretServer category ('User') for logons, logoffs and role
    changes alike. Told from the sample, not the mapping's shape: a TypeId field whose values already differ per kind
    (a firewall's action, FortiGate's logid) is fine though the rules test other fields as well."""
    common = next((e for e in mapping.common if e.path.strip('/') == 'EventDetail/TypeId'), None)
    if common is None or not common.field or mapping.for_each:
        return []
    own = {r.name for r in mapping.events if any(f.path.strip('/') == 'EventDetail/TypeId' for f in r.fields)}
    derived = {n for ex in mapping.extract for n in ex.names if n}
    by_value: dict[str, Counter] = {}
    for record in records:
        extracted = extracted_values(mapping, record) if mapping.extract else {}
        name = rule_of(mapping, record, extracted)
        if not name or name.startswith('drop: ') or name in own:
            continue
        value = (extracted or {}).get(common.field) if common.field in derived else _value_of(record, common.field)
        if value:
            by_value.setdefault(value, Counter())[name] += 1
    shared = {v: rules for v, rules in by_value.items() if len(rules) > 1}
    if not shared:
        return []
    examples = '; '.join(f"'{v}' for rules {', '.join(sorted(r))}" for v, r in list(shared.items())[:3])
    return [f"EventDetail/TypeId reads '{common.field}', which in the sample is the same for events of different kinds "
            f"({examples}). TypeId names the kind of event: give each rule its own (a value per rule, or an xpath "
            f"joining the fields)."]


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
            message = (f"field '{name}' (used for {used[0]}{' and more' if len(used) > 1 else ''}) is in none of "
                       f"the {len(pool)} {what}" + (f"; did you mean {close}?" if close else '')
                       + (f". Fields seen: {seen[:30]}" if seen and not close else ''))
            # A near miss of a field the sample has is a slip (srcip for src_ip, or names recalled from another
            # source's logs), and its element would be empty in every event; with nothing close, the field may be
            # one other data carries.
            if close:
                problems.append(message + " Use the sample's field names; a field only other data carries needs a "
                                          "sample that has it.")
            else:
                warnings.append(message)
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
    warnings += shared_type_ids(mapping, records)
    unknown_problems, kept = unknown_coverage(mapping, records)
    problems += unknown_problems
    return {'records': len(records), 'problems': problems, 'warnings': warnings, 'fields_seen': seen[:60],
            **({'kept_unknown': kept} if kept else {})}
