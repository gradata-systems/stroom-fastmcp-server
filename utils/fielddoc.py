"""The Field mapping section of a pipeline's documentation.

Two tables, each with both halves of a field mapping: where a value comes from (the mapping: a field, a
constant, a lookup, a transform) and what the sample's events got. Events are matched to the rule that wrote
them by a marker the generator puts in each Event for the documentation run (never in saved XSLT), so the
per-rule rows and counts are exact; events no rule of the mapping wrote are reported rather than dropped.
"""
import re
from collections import Counter
from typing import Any

from lxml import etree

from utils.eventschema import EventSchema
from utils.xsltgen import (RULE_MARK, Condition, EventRule, FieldMapping, TranslationMapping, kept_unknown,  # noqa: F401
                           literal)

EVT = 'event-logging:3'
VALUES_SHOWN = 3
_DATA_SPLITTER_FIELD = re.compile(r"(?:data\[@name='([^']*)'\]/)+@value")
_JSON_FIELD = re.compile(r"\*\[@key='[^']*'\](?:/\*\[@key='[^']*'\])*")


def readable(expr: str) -> str:
    """An xpath with its field selectors written as the field names, for a reader:
    normalize-space(data[@name='username']/@value) -> normalize-space(username)."""
    expr = _DATA_SPLITTER_FIELD.sub(lambda m: '/'.join(re.findall(r"data\[@name='([^']*)'\]", m.group(0))), expr)
    return _JSON_FIELD.sub(lambda m: '.'.join(re.findall(r"@key='([^']*)'", m.group(0))), expr)


def source_text(entry: FieldMapping) -> str:
    """Where an element's value comes from, for a reader: a constant in quotes, a field in code, and how it is
    converted (lookup, dictionary, transform, time pattern, value map, default, one per value)."""
    if entry.value is not None:
        return f'"{entry.value}"'
    if entry.lookup:
        key = f'`{entry.lookup.field}`' if entry.lookup.field else f'`{readable(entry.lookup.xpath)}`'
        text = f"lookup `{entry.lookup.map}` by {key}" + (f" -> `{entry.lookup.path}`" if entry.lookup.path else '')
    elif entry.any_of:
        text = 'first of ' + ', '.join(f'`{f}`' for f in entry.any_of)
    elif entry.field is not None:
        text = f'`{entry.field}`'
    else:
        text = f'`{readable(entry.xpath)}`'
    if entry.dictionary:
        text += f" via dictionary `{entry.dictionary}`"
    if entry.transform:
        text += f" ({entry.transform})"
    if entry.scope == 'record':
        text += " (of the record)"
    if entry.time_format:
        text += f" (time {entry.time_format}{', ' + entry.timezone if entry.timezone else ''})"
    elif entry.timezone:
        text += f' ({entry.timezone})'
    if entry.map:
        text += ': ' + ', '.join(f'{k} -> {v}' for k, v in entry.map.items())
        if entry.default is not None:
            text += f'; otherwise {entry.default}'
    elif entry.default is not None:
        text += f', or "{entry.default}" when empty'
    if entry.repeat:
        text += ', one per value'
    return text


def condition_text(c: Condition) -> str:
    src = (f'`{c.field}`' if c.field is not None else f'`{readable(c.xpath)}`') + (' (of the record)' if c.scope == 'record' else '')
    if c.equals is not None:
        return f'{src} = {c.equals}'
    if c.one_of is not None:
        return f"{src} in {', '.join(c.one_of)}"
    if c.matches is not None:
        return f'{src} matches `{c.matches}`'
    if c.in_dictionary is not None:
        return f'{src} in dictionary `{c.in_dictionary}`'
    return f'{src} {"present" if c.present else "empty"}'


def _cell(text: str) -> str:
    return text.replace('|', '\\|').replace('\n', '<br>')


def _row(*cells: str) -> str:
    return '| ' + ' | '.join(_cell(c) for c in cells) + ' |'


def _shown(values: list[str], quote: bool = True) -> str:
    shown = [f'"{v}"' if quote else v for v in values[:VALUES_SHOWN]]
    more = len(values) - VALUES_SHOWN
    return '\n'.join(shown + ([f'... and {more} more'] if more > 0 else []))


def _xpath(entry: FieldMapping, below: str = '') -> str:
    path = entry.path.strip('/').removeprefix(below)
    return path + (f"[@Name='{entry.data_name}']/@Value" if entry.data_name else '')


def event_values(event: etree._Element, sections: tuple[str, ...]) -> dict[tuple[str, str | None], list[str]]:
    """An event's values in the given top-level sections, keyed as the mapping keys them ((path, None) for an
    element, (path to Data, Name) for a Data), every value of a repeated element in order."""
    found: dict[tuple[str, str | None], list[str]] = {}

    def walk(node: etree._Element, path: str) -> None:
        name = etree.QName(node).localname
        here = f'{path}/{name}'
        children = [c for c in node if isinstance(c.tag, str)]
        if name == 'Data' and node.get('Name') is not None:
            found.setdefault((here, node.get('Name')), []).append(node.get('Value') or '')
        elif not children:
            found.setdefault((here, None), []).append((node.text or '').strip())
        for child in children:
            walk(child, here)
    for section in (c for c in event if isinstance(c.tag, str) and etree.QName(c).localname in sections):
        for child in (c for c in section if isinstance(c.tag, str)):
            walk(child, etree.QName(section).localname)
    return found


def rule_of(event: etree._Element) -> str | None:
    """The rule the generator marked the event with for the documentation run, if any."""
    for node in event:
        if isinstance(node, etree._Comment) and (node.text or '').strip().startswith(RULE_MARK):
            return node.text.strip()[len(RULE_MARK):].strip()
    return None


def sampled_events(outputs: list[str]) -> list[etree._Element]:
    """The Event elements in translation outputs (one per record when stepping)."""
    events = []
    for xml in outputs:
        try:
            root = etree.fromstring(xml.encode('utf-8'))
        except (etree.XMLSyntaxError, ValueError):
            continue
        events += [e for e in root.iter() if isinstance(e.tag, str) and etree.QName(e).localname == 'Event']
    return events


def _attribute_unmarked(rules: list[EventRule], effective: dict[str, dict], event: etree._Element) -> str | None:
    """For an event without a marker (an older XSLT): the first rule whose EventDetail elements include all the
    event's and whose constants agree with it."""
    leaves = event_values(event, ('EventDetail',))
    for rule in rules:
        fields = effective[rule.name]
        if set(leaves) <= set(fields) and not any(
                e.value is not None and k in leaves and leaves[k] != [e.value] for k, e in fields.items()):
            return rule.name
    return None


# Field mapping layouts: by event type (the default), or the compact two tables. An AGENTS doc's Documentation section
# or the user picks one (asked for by the user).
BY_EVENT_TYPE, COMPACT = 'by_event_type', 'compact'
LAYOUTS = (BY_EVENT_TYPE, COMPACT)


# Groups of fields in the documentation (event types, action elements, CEF kinds) with roughly the same fields are
# shown together, under one heading naming each (asked for by the user: Send and Receive, say). Roughly: at least this
# share of their fields in common, a path's action element counted as any.
SIMILAR = 0.75
_ACTION_STEP = re.compile(r'^(EventDetail/)(?!(?:TypeId|Description|Classification|Purpose|Data)\b)([^/\[]+)(?=/)')


def any_action_path(path: str) -> str:
    """A path with its action element as '*' (EventDetail/Send/Source/User/Id: EventDetail/*/Source/User/Id)."""
    return _ACTION_STEP.sub(r'\1*', path or '')


def similar_groups(signatures: dict[str, set[str]], share: float = SIMILAR) -> list[list[str]]:
    """The groups, in order, those with roughly the same fields together: each joins the first earlier cluster whose
    fields it shares at least `share` of (of the two together); the rest stand alone."""
    clusters: list[tuple[list[str], set[str]]] = []
    for name, fields in signatures.items():
        for members, together in clusters:
            if fields and together and len(fields & together) >= share * len(fields | together):
                members.append(name)
                together |= fields
                break
        else:
            clusters.append(([name], set(fields)))
    return [members for members, _ in clusters]


def combined_path(paths: list[str]) -> str:
    """One path for a row shared by groups: as it is when they agree; with the action elements as (Send|Receive)
    when only those differ; else each on its own line."""
    distinct = list(dict.fromkeys(paths))
    if len(distinct) == 1:
        return distinct[0]
    if len({any_action_path(p) for p in distinct}) == 1:
        actions = [_ACTION_STEP.match(p).group(2) for p in distinct]
        return _ACTION_STEP.sub(lambda m: f"{m.group(1)}({'|'.join(actions)})", distinct[0], count=1)
    return '\n'.join(distinct)


# Fields the server describes exactly itself: never asked of the agent.
_SELF_DESCRIBED = {'@StreamId', '@EventId', 'StreamId', 'EventId'}
_DESCRIPTIONS = re.compile(r'<!-- stroom-mcp field descriptions\n(.*?)\n-->', re.S)


class Descriptions:
    """What each field means for this source, written by the agent from the field's name, where it comes from and its
    sample values (asked for by the user: the schema's generic words, or 'the x value recorded in a Data element of
    Send', add nothing a reader can't see). With ask, the fields not yet described are collected, to ask for."""

    def __init__(self, given: dict[str, str] | None = None, ask: bool = False,
                 notes: list[dict[str, Any]] | None = None):
        self.given = {k: v.strip() for k, v in (given or {}).items() if isinstance(v, str) and v.strip()}
        self.ask, self.wanted = ask, {}
        self.notes = notes or []      # the vendor documentation's field dictionary (record_source_notes)

    def vendor(self, names: list[str], path: str) -> str:
        """What the vendor's documentation says of the source fields a field is read from; else of the source field
        it puts in this event-logging element (an index or CEF field knows only its path)."""
        found = [n for n in self.notes if n.get('field') in names]
        if not found and path:
            name = re.search(r"\[@Name='([^']*)'\]", path)
            bare = re.sub(r'\[[^\]]*\]|/@Value$', '', path)
            found = [n for n in self.notes if n.get('event_logging_path')
                     and re.sub(r'\[[^\]]*\]|/@Value$', '', n['event_logging_path']) == bare
                     and (not name or name.group(1).lower() in (n['event_logging_path'] + ' ' + n['field']).lower())]
        return ' '.join(n['meaning'].rstrip('.') + '.' + (' Codes: ' + '; '.join(f'{k}: {v}' for k, v in list(
            n['values'].items())[:8]) + '.' if n.get('values') else '') for n in found if n.get('meaning'))

    def of(self, field: str, source: str, values: list[str], says: str | dict[str, str], path: str = '',
           names: list[str] = ()) -> str:
        """The field's description: the agent's, else (the fallback) the first that says anything of it: the
        vendor's documentation of the source fields it is read from (names) or of its element (path), then the
        schemas. says: {schema: its words}, e.g. event-logging for where an index field comes from (what the
        pipeline wrote), then ECS for its name; a string is the event-logging schema's. The agent is shown them all,
        to build on (asked for by the user)."""
        said = {k: v for k, v in {'vendor documentation': self.vendor([n for n in names if n], path),
                                  **({'event-logging': says} if isinstance(says, str) else says)}.items() if v}
        if field in self.given:
            return self.given[field]
        if self.ask and field not in _SELF_DESCRIBED and field not in self.wanted:
            self.wanted[field] = {'field': field, 'from': source, 'values': [v[:60] for v in values[:3]],
                                  **({'schemas_say': said} if said else {})}
        return next(iter(said.values()), '')


def read_descriptions(markdown: str | None) -> dict[str, str]:
    """The field descriptions kept in a Documentation doc's text."""
    import json
    match = _DESCRIPTIONS.search(markdown or '')
    try:
        found = json.loads(match.group(1)) if match else {}
    except ValueError:
        return {}
    return {k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(found, dict) else {}


def descriptions_block(descriptions: dict[str, str]) -> str:
    import json
    body = json.dumps(descriptions, indent=1, ensure_ascii=False).replace('--', '-\\u002d')
    return f"<!-- stroom-mcp field descriptions\n{body}\n-->" if descriptions else ''


def _unknown_kind(event: etree._Element) -> str:
    detail = event.find(f'{{{EVT}}}EventDetail')
    children = list(detail) if detail is not None else []
    return next((etree.QName(c).localname for c in children
                 if isinstance(c.tag, str) and etree.QName(c).localname not in ('TypeId', 'Description')), '?')


def _described(schema: EventSchema, key: tuple[str, str | None]) -> str:
    return schema_description(schema, _key_xpath(key))


def _read_from(entry: FieldMapping | None) -> list[str]:
    """The source fields a mapping entry reads."""
    if entry is None:
        return []
    return [entry.field, *(entry.any_of or []), *([entry.lookup.field] if entry.lookup else [])]


def _key_xpath(key: tuple[str, str | None]) -> str:
    return key[0] + (f"[@Name='{key[1]}']/@Value" if key[1] else '')


def _by_event_type(mapping: TranslationMapping, schema: EventSchema, rules: list[EventRule],
                   effective: dict[str, dict[tuple[str, str | None], FieldMapping]],
                   by_rule: dict[str, list[etree._Element]], unexplained: list[etree._Element], sampled: bool,
                   total: int, order, described: Descriptions) -> list[str]:
    """The fields common to every event type in one table, then a section per event type with a table of its own
    fields. Common: an EventTime or EventSource element every rule writes from the same source; EventDetail is
    always the event type's own."""
    sections = ('EventTime', 'EventSource', 'EventDetail')
    head = ['XPath', 'Description', 'From'] + (['Sample values'] if sampled else [])
    common: list[tuple[str, str | None]] = []
    if rules:
        first = effective[rules[0].name]
        common = [k for k, entry in first.items() if k[0].split('/')[0] != 'EventDetail' and all(
            k in effective[r.name] and source_text(effective[r.name][k]) == source_text(entry) for r in rules)]
        common.sort(key=lambda k: order(first[k]))
    every = [e for events in by_rule.values() for e in events]

    def values_of(events: list[etree._Element], key: tuple[str, str | None]) -> tuple[list[str], bool]:
        found, several = [], False
        for e in events:
            got = event_values(e, sections).get(key, [])
            several = several or len(got) > 1
            found += got
        return list(dict.fromkeys(found)), several

    def row(key: tuple[str, str | None], entry: FieldMapping | None, events: list[etree._Element]) -> str:
        source = source_text(entry) if entry else 'not in the mapping (added by hand, or by a later step)'
        values, several = values_of(events, key)
        cells = [f'`{_key_xpath(key)}`', described.of(_key_xpath(key), source, values, _described(schema, key),
                                                      _key_xpath(key), _read_from(entry)), source]
        if sampled:
            cells.append((_shown(values) or '(not in the sample)') + ('\n(several per event)' if several else ''))
        return _row(*cells)

    def merged_row(members: list[tuple[str, tuple[str, str | None], FieldMapping | None]], grouped: int) -> str:
        """A field of event types shown together: its paths and sources together, each source named by its type
        where they differ or not every type writes it, and the values all of them got."""
        first = members[0][1]
        sources = [(name, source_text(entry) if entry else 'not in the mapping') for name, _, entry in members]
        source = (sources[0][1] if len({t for _, t in sources}) == 1 and len(members) == grouped
                  else '\n'.join(f'{t} ({name})' for name, t in sources))
        values, several = [], False
        for name, k, _ in members:
            got, more = values_of(by_rule[name], k)
            values += got
            several = several or more
        values = list(dict.fromkeys(values))
        cells = [f'`{combined_path([_key_xpath(k) for _, k, _ in members])}`'.replace('\n', '`\n`'),
                 described.of(_key_xpath(first), source, values, _described(schema, first), _key_xpath(first),
                              [n for _, _, entry in members for n in _read_from(entry)]), source]
        if sampled:
            cells.append((_shown(values) or '(not in the sample)') + ('\n(several per event)' if several else ''))
        return _row(*cells)

    lines = ['### Common fields', '',
             ("Every event type writes these, from the same source. Sample values are what the sample's events were "
              f'written, {total} events in all.' if sampled else 'Every event type writes these, from the same source.'),
             '', _row(*head), _row(*['---'] * len(head))]
    lines += [row(k, effective[rules[0].name][k], every) for k in common]
    mapped = {k for fields in effective.values() for k in fields}
    if sampled:     # in the events, but no rule of the mapping writes it
        extra = sorted({k for e in every for k in event_values(e, ('EventTime', 'EventSource')) if k not in mapped},
                       key=lambda k: (k[0], k[1] or ''))
        lines += [row(k, None, every) for k in extra]
    lines += ['', '### Event types', '']
    dropped = [r for r in mapping.events if r.drop]
    if dropped:
        lines += ['Left untranslated on purpose: ' + '; '.join(
            f"`{r.name}` ({' and '.join(condition_text(c) for c in r.when) or 'every record'})" for r in dropped) + '.', '']
    own: dict[str, list[tuple[str, str | None]]] = {}
    for rule in rules:
        fields = effective[rule.name]
        own[rule.name] = sorted((k for k in fields if k not in common), key=lambda k: (
            k[0] not in ('EventDetail/TypeId', 'EventDetail/Description'), order(fields[k])))
    by_name = {r.name: r for r in rules}
    # Judged on what they write besides TypeId and Description, which every event type has.
    for names in similar_groups({n: {any_action_path(_key_xpath(k)) for k in keys
                                     if k[0] not in ('EventDetail/TypeId', 'EventDetail/Description')}
                                 for n, keys in own.items()}):
        said = []
        for name in names:
            rule = by_name[name]
            conditions = ' and '.join(condition_text(c) for c in rule.when)
            line = (f'When {conditions}' if conditions else 'Any other record' if len(rules) > 1 else 'Every record') \
                + (f' ({name})' if len(names) > 1 else '')
            if sampled:
                events = by_rule[name]
                line += (f": {len(events)} of the sample's {total} events" if events else ': none in the sample')
            said.append(line + '.')
        lines += [f"#### {', '.join(names)}", '', ' '.join(said), '', _row(*head), _row(*['---'] * len(head))]
        # One row per field, the groups' own paths and sources together.
        rows: dict[str, list[tuple[str, tuple[str, str | None], FieldMapping | None]]] = {}
        for name in names:
            fields = effective[name]
            for k in own[name]:
                rows.setdefault(any_action_path(_key_xpath(k)), []).append((name, k, fields[k]))
            if sampled and by_rule[name]:
                for k in sorted({k for e in by_rule[name] for k in event_values(e, ('EventDetail',)) if k not in fields},
                                key=lambda k: (k[0], k[1] or '')):
                    rows.setdefault(any_action_path(_key_xpath(k)), []).append((name, k, None))
        for members in rows.values():
            if len(names) == 1:
                name, k, entry = members[0]
                lines.append(row(k, entry, by_rule[name]))
            else:
                lines.append(merged_row(members, len(names)))
        lines.append('')
    if unexplained:
        kinds = Counter(_unknown_kind(e) for e in unexplained)
        lines += [f"{len(unexplained)} of the sample's {total} events were not written by a rule of this mapping "
                  f"(the XSLT was edited by hand, or a later step rewrote them): {dict(kinds)}.", '']
    return lines


def field_mapping_markdown(mapping: TranslationMapping, schema: EventSchema,
                           observed: list[etree._Element] | None = None, layout: str = BY_EVENT_TYPE,
                           described: Descriptions | None = None) -> str:
    """The section: how the mapping reads records (items, drops, extractions), then, by event type (the default,
    asked for by the user): a table of the fields common to every event type, and a section per event type with a
    table of the fields particular to it (XPath, description, From, and with a sample the values written).
    Compact: the EventSource table and an event types table, one row per rule with every EventDetail element in a
    cell."""
    def order(entry: FieldMapping) -> tuple:
        try:
            return tuple(c.index for c in schema.resolve(entry.path.strip('/'))), entry.data_name or ''
        except ValueError:
            return (999,), entry.data_name or ''

    rules = [r for r in mapping.events if not r.drop]
    effective: dict[str, dict[tuple[str, str | None], FieldMapping]] = {}
    for rule in rules:
        fields = {(e.path.strip('/'), e.data_name): e for e in mapping.common}
        fields.update({(e.path.strip('/'), e.data_name): e for e in rule.fields})
        effective[rule.name] = fields

    # Attribution: the generator's marker, else the older guess; events no rule wrote are counted apart.
    by_rule: dict[str, list[etree._Element]] = {r.name: [] for r in rules}
    unexplained: list[etree._Element] = []
    for event in observed or []:
        name = rule_of(event) or _attribute_unmarked(rules, effective, event)
        (by_rule[name] if name in by_rule else unexplained).append(event)
    sampled = observed is not None
    total = len(observed or [])

    lines: list[str] = []
    if mapping.for_each:
        lines += [f'Each record holds several events: one per `{mapping.for_each}` item. Sources marked "of the record" '
                  f'read the record round the items.', '']
    kept = kept_unknown(mapping)
    if kept:
        lines += ['### Kept as Unknown', '']
        lines += [f"- `{r.name}` ({' and '.join(condition_text(c) for c in r.when) or 'records no other rule matches'}): "
                  f"{r.allow_unknown}"
                  # A catch-all the sample never reaches is a safety net, not events kept Unknown (seen: read as a
                  # decision about the sample, beside "0 of 1000" in the event types).
                  + (f" None of the {total} sampled records reach it: it catches records the sample doesn't have."
                     if sampled and not by_rule.get(r.name) else '') for r in kept]
        lines.append('')
    if mapping.drop_when:
        lines += ['### Left untranslated', '']
        lines += [f"- {' and '.join(condition_text(c) for c in d.when)}: {d.reason}" for d in mapping.drop_when]
        lines.append('')
    if mapping.shared:
        lines += ['### Written by shared XSLTs', '']
        lines += [f"- `{u.at}`: the named template `{u.template}` of `{u.href}`"
                  + (f", given {', '.join(f'`{n}` = `{readable(v)}`' for n, v in u.with_params.items())}"
                     if u.with_params else '') for u in mapping.shared]
        lines.append('')
    if mapping.extract:
        lines += ['### Extracted fields', '']
        for ex in mapping.extract:
            names = ', '.join(f'`{n}`' for n in ex.names if n)
            lines.append(f"- {names}: parsed from `{ex.field or readable(ex.xpath)}`"
                         f"{' (of the record)' if ex.scope == 'record' else ''} with `{ex.regex}`")
        lines.append('')

    described = described or Descriptions()
    if layout != COMPACT:
        return '\n'.join(lines + _by_event_type(mapping, schema, rules, effective, by_rule, unexplained, sampled,
                                                 total, order, described)) + '\n'

    # --- EventSource and EventTime
    sources: dict[tuple, dict[str, list[str]]] = {}     # element -> from text -> rules that write it so
    entries: dict[tuple, FieldMapping] = {}
    for rule in rules:
        for k, entry in effective[rule.name].items():
            if k[0].split('/')[0] in ('EventSource', 'EventTime'):
                sources.setdefault(k, {}).setdefault(source_text(entry), []).append(rule.name)
                entries.setdefault(k, entry)
    seen: dict[tuple, dict[str, list[str]]] = {}        # element -> value -> rules whose events had it
    multi: set[tuple] = set()
    for rule, events in by_rule.items():
        for event in events:
            for k, values in event_values(event, ('EventTime', 'EventSource')).items():
                if len(values) > 1:
                    multi.add(k)
                for value in values:
                    kinds = seen.setdefault(k, {}).setdefault(value, [])
                    if rule not in kinds:
                        kinds.append(rule)
    rules_sampled = [r for r in by_rule if by_rule[r]]
    lines += ['### EventSource', '']
    lines += [('Every event carries these. From is the mapping; Sample values are what the sample\'s events were '
               f'written, {total} events in all.' if sampled else 'Every event carries these; From is the mapping.'), '']
    lines += (['| XPath | Description | From | Sample values |', '| --- | --- | --- | --- |'] if sampled else
              ['| XPath | Description | From |', '| --- | --- | --- |'])
    for k in sorted(sources, key=lambda k: order(entries[k])):
        froms = sources[k]
        if len(froms) == 1 and len(next(iter(froms.values()))) == len(rules):
            from_text = next(iter(froms))
        else:
            from_text = '\n'.join(f"{f} ({', '.join(names)})" for f, names in froms.items())
        try:
            description = schema.describe(schema.resolve(k[0]))
        except ValueError:
            description = ''
        description = described.of(_xpath(entries[k]), from_text, list(seen.get(k) or {}), description,
                                   _xpath(entries[k]), _read_from(entries[k]))
        if not sampled:
            lines.append(_row(f'`{_xpath(entries[k])}`', description, from_text))
            continue
        values = seen.get(k) or {}
        kinds = list(dict.fromkeys(r for rs in values.values() for r in rs))
        shown = _shown(list(values)) or '(not in the sample)'
        if values and len(kinds) < len(rules_sampled):
            shown += f"\n({', '.join(kinds)} events only)"
        if k in multi:
            shown += '\n(several per event)'
        lines.append(_row(f'`{_xpath(entries[k])}`', description, from_text, shown))
    for k in sorted(set(seen) - set(sources)):
        values = seen[k]
        lines.append(_row(f'`{k[0] + (chr(91) + "@Name=" + repr(k[1]) + "]/@Value" if k[1] else "")}`', '',
                          'not in the mapping (added by hand, or by a later step)', _shown(list(values))))

    # --- Event types
    lines += ['', '### Event types', '']
    lines += [('One row per rule: its conditions, what it writes, how many of the sample\'s events it wrote, and each '
               'EventDetail element as path <- from = a sampled value.' if sampled else
               'One row per rule: its conditions and what it writes, each EventDetail element as path <- from.'), '']
    lines += (['| Rule | TypeId | Description | Events | EventDetail |', '| --- | --- | --- | --- | --- |'] if sampled else
              ['| Rule | TypeId | Description | EventDetail |', '| --- | --- | --- | --- |'])
    for rule in mapping.events:
        if rule.drop:
            conditions = ' and '.join(condition_text(c) for c in rule.when) or 'every record'
            lines.append(_row(f'**{rule.name}**\n{conditions}', '', f'Left untranslated on purpose', *(['0'] if sampled else []), ''))
            continue
        others = [r for r in rules if r is not rule]
        conditions = ' and '.join(condition_text(c) for c in rule.when) or ('every record' if not others else 'any other record')
        fields = effective[rule.name]
        events = by_rule[rule.name]
        first = event_values(events[0], ('EventDetail',)) if events else {}
        type_id = fields.get(('EventDetail/TypeId', None))
        description = fields.get(('EventDetail/Description', None))

        def from_and_values(entry: FieldMapping | None, key: tuple) -> str:
            text = source_text(entry) if entry else ''
            if sampled and events and not (entry and entry.value is not None):   # a constant needs no sample
                values = list(dict.fromkeys(v for e in events for v in event_values(e, ('EventDetail',)).get(key, [])))
                text += ('\n' if text else '') + _shown(values) if values else ''
            return text
        detail = sorted((e for (path, _), e in fields.items() if path.startswith('EventDetail/')
                         and path not in ('EventDetail/TypeId', 'EventDetail/Description')), key=order)
        detail_lines = []
        for e in detail:
            key = (e.path.strip('/'), e.data_name)
            line = f'`{_xpath(e, "EventDetail/")}` <- {source_text(e)}'
            if sampled and events:
                values = first.get(key, [])
                line += (' = ' + ', '.join(f'"{v}"' for v in values[:VALUES_SHOWN])) if values else ' = (empty)'
            detail_lines.append(line)
        count = f'{len(events)} of {total}' if sampled else None
        if sampled and not events:
            count = '0 of ' + str(total) + '\n(not in the sample)'
        cells = [f'**{rule.name}**\n{conditions}', from_and_values(type_id, ('EventDetail/TypeId', None)),
                 from_and_values(description, ('EventDetail/Description', None))]
        if sampled:
            cells.append(count)
        cells.append('\n'.join(detail_lines))
        lines.append(_row(*cells))
    if unexplained:
        def kind_of(e: etree._Element) -> str:
            detail = e.find(f'{{{EVT}}}EventDetail')
            children = list(detail) if detail is not None else []
            return next((etree.QName(c).localname for c in children
                         if isinstance(c.tag, str) and etree.QName(c).localname not in ('TypeId', 'Description')), '?')
        kinds = Counter(kind_of(e) for e in unexplained)
        lines += ['', f"{len(unexplained)} of the sample's {total} events were not written by a rule of this mapping "
                      f"(the XSLT was edited by hand, or a later step rewrote them): {dict(kinds)}."]
    return '\n'.join(lines) + '\n'


def index_documents(outputs: list[str]) -> list[dict[str, list[str]]]:
    """The documents an indexing XSLT wrote, one per output record, as field -> values: records:2 <data name value>
    (Lucene), or the xpath-functions JSON XML the Elasticsearch filter reads (nested maps as dotted names)."""
    from utils.templatecheck import flatten_document, json_xml_documents
    documents = []
    for xml in outputs:
        try:
            root = etree.fromstring(xml.encode('utf-8'))
        except (etree.XMLSyntaxError, ValueError):
            continue
        if etree.QName(root).namespace == 'records:2':
            for record in root.iter('{records:2}record'):
                doc: dict[str, list[str]] = {}
                for data in record.iter('{records:2}data'):
                    if data.get('name'):
                        doc.setdefault(data.get('name'), []).append(data.get('value') or '')
                documents.append(doc)
        else:
            for item in json_xml_documents(xml):
                # Values come as (JSON type, text); a nested map as ('object', None).
                documents.append({k: [str(v[1]) for v in vs if v[0] != 'object' and v[1] is not None]
                                  for k, vs in flatten_document(item).items()})
    return documents


_IP = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$|^[0-9a-fA-F:]+:[0-9a-fA-F:]*$')
_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_TIME = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}')
_NUMBER = re.compile(r'^-?\d+(\.\d+)?$')
_DATA = re.compile(r"Data\[@Name='([^']+)'\]/@Value$")
# The schema's descriptive openings, which say nothing about the field itself.
_BOILERPLATE = re.compile(r'^This element (?:contains (?:information about|data relating to)|provides details of|'
                          r'describes|details)\s+(?:the details of\s+)?(?:the\s+)?')
_IDS = {'@StreamId': 'The id of the Events stream the event came from.',
        '@EventId': "The event's number in its stream; with StreamId, it identifies the event."}
# An index Stroom did not build may hold raw records, not events.
_DOCUMENT_IDS = {'StreamId': 'The id of the stream the document came from.',
                 'EventId': "The record's number in that stream; with StreamId, it identifies the document."}


def schema_description(schema: EventSchema | None, source: str) -> str:
    """What the event-logging schema says the source path holds: the first sentence of its documentation."""
    if source in _IDS:
        return _IDS[source]
    data = _DATA.search(source)
    path = re.sub(r'\[[^\]]*\]', '', source.split('/Data[')[0] if data else source)
    attribute = None
    if '/@' in path:
        path, attribute = path.rsplit('/@', 1)
    said = ''
    if schema is not None and '*' in path:
        # Any Network action: what the schema says of the element under the first action it takes (Bind).
        for action in ('Permit', 'Deny', 'Connect', 'Open', 'Close', 'Bind', 'Send', 'Receive', 'Listen'):
            try:
                return schema.describe(schema.resolve(path.replace('*', action, 1)))
            except (ValueError, IndexError, AttributeError):
                continue
    if schema is not None:
        try:
            said = schema.describe(schema.resolve(path))
        except (ValueError, IndexError, AttributeError):
            said = ''
    if said:
        # 'This element contains information about the time the event was created.' -> 'Time the event was created.'
        trimmed = _BOILERPLATE.sub('', said, count=1)
        if trimmed != said and trimmed:
            said = trimmed[0].upper() + trimmed[1:]
    parts = path.strip('/').split('/')
    if said and len(parts) > 1:
        # Ids and names inherit a base type's words ('the object', 'the device'): name the element they belong to.
        parent = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', parts[-2]).lower()
        generic = re.search(r'\bthe (object|device)\b', said)
        if generic and generic.group(1) != parent:
            said = re.sub(r',?\s+e\.?g\.?\s.*$', '.', said.replace(generic.group(0), f'the {parent}', 1))
    if data:
        # The element the Data is under, not its description (seen in testing: every Data under Send was described
        # as 'Used for send events that are at a higher level than a network/send event').
        under = path.strip('/').split('/')[-1] if path.strip('/') else ''
        return f"The `{data.group(1)}` value recorded in a Data element" + (f" of {under}." if under else '.')
    if attribute:
        return f"The `{attribute}` attribute of {path.split('/')[-1]}" + (f": {said}" if said else '.')
    return said


NOT_DESCRIBED = 'Not described: no plan or schema covers it.'


def field_description(what: str, per_document: list[list[str]] | None) -> str:
    """A field's description: what it is (the plan's description, the schema's, or its source), and only what the
    other columns do not show (In sample has how often, Sample values the values): several values in one document.
    A field nothing describes gets the kind of values the sample shows, when they share one, else says so."""
    parts = [what.strip()] if what and what.strip() else []
    if per_document is not None:
        holding = [[v for v in vs if v != ''] for vs in per_document]
        values = list(dict.fromkeys(v for vs in holding for v in vs))
        if not parts:
            kind = next((name for name, pattern in (('IP addresses', _IP), ('Email addresses', _EMAIL),
                                                    ('Timestamps', _TIME), ('Numbers', _NUMBER))
                         if values and all(pattern.match(v) for v in values)), '')
            parts.append(kind or NOT_DESCRIBED)
        if any(len(vs) > 1 for vs in holding):
            parts.append('Several values per document.')
    return ' '.join(p if p.endswith('.') else p + '.' for p in parts)


def existing_index_summary(survey: dict[str, Any]) -> str:
    """'Data surveyed', for an existing index's Purpose and data: what the index holds as a whole, where its
    documents come from (feed, stream type, the pipeline that produced the streams), what writes to it, and what the
    field table was drawn from."""
    index, profile = survey['index'], survey.get('profile') or {}
    lines = ['### Data surveyed', '']
    if profile.get('documents'):
        span = f", from {profile['earliest']} to {profile['latest']}" if profile.get('earliest') else ''
        streams = f", in {profile['streams']} stream{'s' if profile['streams'] != 1 else ''}" if profile.get('streams') else ''
        at_least = 'at least ' if profile.get('partial') else ''
        lines.append(f"- `{index['name']}` holds {at_least}{profile['documents']} "
                     f"document{'s' if profile['documents'] != 1 else ''}{span}{streams}."
                     + (f" ({profile['partial']}.)" if profile.get('partial') else ''))
        sources = profile.get('sources') or []
        examined = profile.get('streams_examined') or 0
        for s in sources[:10]:
            made = f", produced by pipeline `{s['produced_by']}`" if s.get('produced_by') else ''
            said = f" The feed's description: {s['description']}" if s.get('description') else ''
            lines.append(f"- From feed `{s['feed']}` ({s['type']} streams{made}): {s['streams']} "
                         f"stream{'s' if s['streams'] != 1 else ''}, {s['documents']} documents.{said}")
        if sources and profile.get('streams') and examined < profile['streams']:
            lines.append(f"- (Sources from the {examined} streams with the most documents.)")
    elif survey.get('documents_sampled') == 0:
        lines.append(f"- {survey.get('note') or 'No documents came back through Stroom.'}")
    fed = survey.get('fed_by') or []
    for p in fed:
        lines.append(f"- Indexed by pipeline `{p['name']}`" + (', from the index plan kept with its XSLT.' if p.get('plan') else '.'))
    if not fed:
        lines.append('- No pipeline was found that writes to it (pipelines are looked for by the index name they '
                     'write; one that builds the name from values, or writes through an alias, is not found this way).')
    if survey.get('documents_sampled'):
        recent = f" of {survey['sampled_from']}" if (survey.get('sampled_from') or '').startswith('the last') else ''
        lines.append(f"- The field table is drawn from {survey['documents_sampled']} documents{recent}"
                     + (f" ({survey['earliest']} to {survey['latest']})" if survey.get('earliest') else '')
                     + ': In sample and Sample values are what they held.')
    return '\n'.join(lines) + '\n'


def existing_index_markdown(survey: dict[str, Any], planned: dict[str, Any], schema: EventSchema | None,
                            layout: str = BY_EVENT_TYPE, described: Descriptions | None = None) -> str:
    """The Field mapping section for an existing index, from its survey: every field Stroom has for it, with a
    description (the feeding pipeline's plan, else the event-logging schema for the field's source), its type, where it comes from when a feeding pipeline's plan records it, how often the surveyed
    documents held it, and their values."""
    index, documents = survey['index'], survey.get('documents') or []
    target = (f"Elasticsearch index `{survey['index_name']}`" if survey['backend'] == 'elasticsearch'
              else 'a Lucene index')
    # The overall facts (what feeds it, what was surveyed) are in Data surveyed: only what explains the columns here.
    fed = survey.get('fed_by') or []
    intro = f"`{index['name']}` is {target}; the time field is `{survey.get('time_field')}`."
    if not any(p.get('plan') for p in fed):
        intro += ' Where each field comes from is not recorded: no pipeline that writes to it keeps an index plan.'
    if not documents:
        intro += ' No documents came back through Stroom, so no field below was read (see Data surveyed).'
    lines = [intro, '']
    head = ['Index field', 'Description', 'Type', 'From (event-logging path)', 'In sample', 'Sample values']
    rows: list[tuple[str, list[str]]] = []
    fields = survey.get('fields') or []
    first = ('StreamId', 'EventId', survey.get('time_field'), '@timestamp')
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in fields:
        if f['name'] not in first:
            groups.setdefault(f['name'].split('.')[0], []).append(f)
    ordered = [f for name in first for f in fields if f['name'] == name] + [f for g in groups.values() for f in g]
    surveyed = set(survey.get('surveyed_fields') or [x['name'] for x in fields])
    unsurveyed = [x['name'] for x in fields if x['name'] not in surveyed]
    if documents and unsurveyed:
        lines += [f"The survey read {len(surveyed)} of the {len(fields)} fields; the other {len(unsurveyed)} "
                  f"are listed as not surveyed.", '']
    for f in list(dict.fromkeys(f['name'] for f in ordered)):
        field = next(x for x in fields if x['name'] == f)
        plan = planned.get(f)
        source = getattr(plan, 'source', '') if plan else ''
        per_document = [d.get(f, []) for d in documents]
        if field.get('stored') is False:
            description, held, values = 'Indexed but not stored: searchable, its values cannot be shown.', 'not stored', '-'
        elif documents and f not in surveyed:
            what = (getattr(plan, 'description', '') if plan else '') or (schema_description(schema, source) if source else '')
            description = field_description(what, None) or 'Not surveyed: past the fields the survey reads.'
            held, values = 'not surveyed', '-'
        elif not documents:
            what = (getattr(plan, 'description', '') if plan else '') or (schema_description(schema, source) if source else '')
            description = field_description(what, None) or 'Not read: no documents came back through Stroom.'
            held, values = 'not read', '-'
        else:
            hint = ((schema_description(schema, source) if source else _DOCUMENT_IDS.get(f, ''))
                    or ("The index's time field: searches and dashboards filter on it."
                        if f == survey.get('time_field') else ''))
            what = (getattr(plan, 'description', '') if plan else '') or (described or Descriptions()).of(
                f, source, list(dict.fromkeys(v for vs in per_document for v in vs if v != '')),
                {'event-logging': hint, 'ECS': ecs_says(f)}, source)
            description = field_description(what, per_document)
            count = sum(1 for vs in per_document if any(v != '' for v in vs))
            held = f'{100 * count / len(documents):.0f}% of documents' if documents else 'no documents'
            values = _values([v for vs in per_document for v in vs])
        rows.append((source, [f'`{f}`', description, field['type'], f'`{source}`' if source else '(not recorded)', held,
                              values]))
    # Written by a feeding pipeline's plan, but not in the fields Stroom lists for the index: only documented when the
    # user chose to (write_documentation refuses otherwise), and said as such rather than left out.
    for name in not_listed(survey, planned):
        plan = planned[name]
        what = getattr(plan, 'description', '') or (schema_description(schema, plan.source) if plan.source else '')
        rows.append((plan.source, [f'`{name}`', field_description(what, None) or 'Written by the pipeline that feeds the index.',
                                   getattr(plan, 'type', ''), f'`{plan.source}`' if plan.source else '(not recorded)',
                                   'not in the index', '-']))
    return '\n'.join(lines + index_tables(rows, head, layout)) + '\n'


def not_listed(survey: dict[str, Any], planned: dict[str, Any]) -> list[str]:
    """Fields a feeding pipeline's plan writes that Stroom doesn't list for the index."""
    listed = {f['name'] for f in survey.get('fields') or []}
    return [n for n in planned if n not in listed]


def written_fields_markdown(documents: list[dict[str, list[str]]]) -> str:
    """The Field mapping section of an indexing pipeline whose XSLT keeps no plan (written by hand): the fields the
    documents it wrote from the sample hold, how often, and their values. No source paths: only a plan records those."""
    seen: dict[str, int] = {}
    for doc in documents:
        for key, values in doc.items():
            if any(v != '' for v in values):
                seen[key] = seen.get(key, 0) + 1
    lines = [f'The fields in the {len(documents)} documents the pipeline wrote from the sample. Its XSLT keeps no '
             f'index plan, so where each comes from is not recorded here (save the XSLT with its plan to add it).', '',
             '| Index field | Description | In sample | Sample values |', '| --- | --- | --- | --- |']
    for key in sorted(seen)[:120]:
        what = _IDS.get('@' + key, '') if key in ('StreamId', 'EventId') else ''
        lines.append(_row(f'`{key}`', field_description(what, [doc.get(key, []) for doc in documents]),
                          f'{100 * seen[key] / len(documents):.0f}% of documents',
                          _values([v for doc in documents for v in doc.get(key, [])])))
    return '\n'.join(lines) + '\n'


def shown_type(plan: Any, kind: str) -> str:
    """A field's type as its backend has it: Elasticsearch maps ids as long; Lucene keeps Stroom's own types."""
    if getattr(plan, 'backend', None) == 'elasticsearch':
        from utils.fieldplan import ELASTIC
        return ELASTIC.get(kind, kind)
    return kind


def ecs_says(name: str) -> str:
    """What ECS says of a field of that name ('' when it is not ECS's)."""
    from utils import ecs
    return (ecs.field(name) or {}).get('short') or ''


def action_of(source: str | None) -> str | None:
    """The action element an event-logging path is under (EventDetail/Authenticate/User/Id: Authenticate); None for
    what any kind of event has (EventTime, EventSource, TypeId, Description, the ids) or no path at all."""
    from utils.cef import NOT_ACTION
    parts = re.sub(r'\[[^\]]*\]', '', source or '').strip('/').split('/')
    return parts[1] if len(parts) > 1 and parts[0] == 'EventDetail' and parts[1] not in NOT_ACTION | {'*'} else None


def index_tables(rows: list[tuple[str, list[str]]], head: list[str], layout: str = BY_EVENT_TYPE,
                 population: dict[str, float] | None = None) -> list[str]:
    """An index's field rows (each with the event-logging path it comes from), by event type (the default, asked for
    by the user): the fields any kind of event has in one table, then a section per action element with a table of
    the fields from it. Compact, or with no field from an action element: one table."""
    def table(cells: list[list[str]]) -> list[str]:
        return [_row(*head), _row(*['---'] * len(head))] + [_row(*c) for c in cells]
    by_action: dict[str, list[list[str]]] = {}
    for source, cells in rows:
        if action_of(source):
            by_action.setdefault(action_of(source), []).append(cells)
    if layout == COMPACT or not by_action:
        return table([cells for _, cells in rows])
    lines = ['### Common fields', '', 'Any kind of event can have these: from EventTime, EventSource, the event type, '
             'and the ids.', '', *table([cells for source, cells in rows if not action_of(source)]), '',
             '### Event types', '', "Each from the action element that kind of event has: only those events have them.", '']
    sources = {a: {any_action_path(source) for source, _ in rows if action_of(source) == a} for a in by_action}
    for actions in similar_groups(sources):
        shares = [(a, max((v for path, v in (population or {}).items() if path.startswith(f'EventDetail/{a}/')),
                          default=None)) for a in actions]
        said = ', '.join(f'{v:g}%' + (f' ({a})' if len(actions) > 1 else '') for a, v in shares if v is not None)
        lines += [f"#### {', '.join(actions)}", '', *([f"In {said} of the sample's events.", ''] if said else []),
                  *table([c for a in actions for c in by_action[a]]), '']
    return lines[:-1]


def grouped_fields(plan: Any) -> list[Any]:
    """The plan's fields for the table: the document's ids and time field first, then grouped by their top-level
    object (host.*, user.*), each group where its first field comes in the plan."""
    head = ('StreamId', 'EventId', plan.time_field, '@timestamp')
    first = [f for f in plan.fields if f.name in head]
    groups: dict[str, list[Any]] = {}
    for f in plan.fields:
        if f.name not in head:
            groups.setdefault(f.name.split('.')[0], []).append(f)
    return first + [f for members in groups.values() for f in members]


def index_field_mapping_markdown(plan: Any, population: dict[str, float] | None = None,
                                 documents: list[dict[str, list[str]]] | None = None,
                                 schema: EventSchema | None = None, layout: str = BY_EVENT_TYPE,
                                 described: Descriptions | None = None) -> str:
    """The Field mapping section of an indexing pipeline's documentation: index field, a description (the plan's,
    else the event-logging schema's for its source), type, the event-logging path it
    comes from, and from the sample: how often that path is populated in the Events, and the values the index
    documents the pipeline wrote got for the field."""
    lines = [f'Documents for `{plan.index_name}` ({plan.backend}); the time field is `{plan.time_field}`.', '']
    if plan.drop_when:
        lines += ['Events left out of the index:', ''] + [f'- `{t}`' for t in plan.drop_when] + ['']
    sampled = population is not None
    if documents is not None:
        lines += [f'Sample values are what the {len(documents)} documents written from the sample got.', '']
    head = ['Index field', 'Description', 'Type', 'From (event-logging path)'] + (['In sample'] if sampled else [])         + (['Sample values'] if documents is not None else [])
    rows: list[tuple[str, list[str]]] = []
    for f in grouped_fields(plan):
        shared = plan.written_by(f.name) if hasattr(plan, 'written_by') else None
        per_document = [d.get(f.name, []) for d in documents] if documents is not None else None
        sampled_values = list(dict.fromkeys(v for vs in per_document or [] for v in vs if v != ''))
        what = f.description or (described or Descriptions()).of(
            f.name, f.source if not shared else f'shared template {shared.template}', sampled_values,
            {'shared template': f"Written by the shared template `{shared.template}`.", 'ECS': ecs_says(f.name)}
            if shared else {'event-logging': schema_description(schema, f.source), 'ECS': ecs_says(f.name)}, f.source)
        cells = [f'`{f.name}`', field_description(what, per_document), shown_type(plan, f.type), f"shared template `{shared.template}` of `{shared.href}`" if shared else f'`{f.source}`']
        if sampled:
            from utils.fieldplan import population_of
            pct = population_of(f.source, population)
            cells.append('always' if f.source.startswith('@') or shared else f'{pct:g}% of events' if pct is not None
                         else 'not in the sample')
        if documents is not None:
            values = list(dict.fromkeys(v for d in documents for v in d.get(f.name, []) if v != ''))
            cells.append(', '.join(f'`{v[:40]}`' for v in values[:3]) + (f' (+{len(values) - 3})' if len(values) > 3 else '')
                         if values else '(none in the sample)')
        rows.append(('' if shared else f.source, cells))
    return '\n'.join(lines + index_tables(rows, head, layout, population)) + '\n'


def _values(values: list[str]) -> str:
    distinct = list(dict.fromkeys(v for v in values if v != ''))
    return (', '.join(f'`{v[:40]}`' for v in distinct[:3]) + (f' (+{len(distinct) - 3})' if len(distinct) > 3 else '')
            if distinct else '(none in the sample)')


def object_arrays(outputs: list[str]) -> list[str]:
    """Fields the documents hold as arrays of objects (dotted paths). Elasticsearch flattens these: items.sku and
    items.qty are searchable, but not which sku went with which qty."""
    from utils.templatecheck import json_xml_documents
    found: set[str] = set()

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, sub in value.items():
                walk(sub, f'{path}.{key}' if path else key)
        elif isinstance(value, list):
            if any(isinstance(v, dict) for v in value):
                found.add(path)
            for sub in value:
                walk(sub, path)
    for xml in outputs:
        for document in json_xml_documents(xml):
            walk(document, '')
    return sorted(found)


def discovery_field_markdown(plan: Any, documents: list[dict[str, list[str]]] | None,
                             arrays: list[str] | None = None) -> str:
    """The Field mapping section of a discovery pipeline: how records become documents, the fields mapped
    explicitly, and the fields the sample's documents held (Elasticsearch maps those dynamically)."""
    d = plan.discovery
    lines = [f'Documents for `{plan.index_name}` (Elasticsearch, discovery): each raw record indexed as it is, with '
             f"the source's field names. Elasticsearch maps fields dynamically as documents arrive: strings as "
             f'keywords (up to {d.ignore_above} characters), at most {d.total_fields_limit} fields, malformed values '
             f'ignored.', '']
    if d.unpack_json:
        lines += ['A string holding a JSON object is also indexed parsed, as `<field>_json`; the string is kept.', '']
    if d.drop:
        lines += ['Left out: ' + ', '.join(f'`{k}`' for k in d.drop) + '.', '']
    lines += ['Fields starting with `_` (Elasticsearch reserves `_id` and others; Stroom drops the rest) or top-level ones '
              'named like a field written here are kept as `<field>_original`, without the leading `_` (`_id` is '
              '`id_original`); keys Elasticsearch cannot take (empty, or with an empty dotted part) are repaired.', '']
    if arrays:
        lines += ['Arrays of objects are indexed flattened: each field inside is searchable, but not which values went '
                  'together in one object: ' + ', '.join(f'`{a}`' for a in arrays) + '.', '']
    explicit = {'StreamId': 'the stream id', 'EventId': 'the record number in the stream',
                '@timestamp': f"`{d.timestamp_field}`" + (f' (format `{d.timestamp_format}`)' if d.timestamp_format else '')}
    explicit.update({name: f'stream meta `{attr}`' for name, attr in d.meta.items()})
    lines += ['| Index field | Description | Mapping | From |' + (' Sample values |' if documents is not None else ''),
              '| --- | --- | --- | --- |' + (' --- |' if documents is not None else '')]
    types = {f.name: f.type for f in plan.fields}
    meaning = {'StreamId': 'The id of the raw stream the record came from.',
               'EventId': "The record's number in its stream; with StreamId, it identifies the record.",
               '@timestamp': f"The record's time, from its `{d.timestamp_field}` field."}
    for name, source in explicit.items():
        what = meaning.get(name) or f"The stream's `{d.meta.get(name)}` meta attribute."
        per_document = [doc.get(name, []) for doc in documents] if documents is not None else None
        cells = [f'`{name}`', field_description(what, per_document),
                 {'id': 'long', 'date': 'date'}.get(types.get(name), 'dynamic'), source]
        if documents is not None:
            cells.append(_values([v for doc in documents for v in doc.get(name, [])]))
        lines.append(_row(*cells))
    if documents:
        seen: dict[str, int] = {}
        for doc in documents:
            for key, values in doc.items():
                if key not in explicit and any(v != '' for v in values):
                    seen[key] = seen.get(key, 0) + 1
        lines += ['', f'Fields in the {len(documents)} documents written from the sample, mapped dynamically:', '',
                  '| Field | Description | In sample | Sample values |', '| --- | --- | --- | --- |']
        for key in sorted(seen)[:80]:
            top = key.split('.')[0]
            what = (f"Parsed from the JSON held in the source's `{top[:-5]}` field." if top.endswith('_json') and d.unpack_json
                    else f"The source's `{key}` field.")
            lines.append(_row(f'`{key}`', field_description(what, [doc.get(key, []) for doc in documents]),
                              f'{100 * seen[key] / len(documents):.0f}% of documents',
                              _values([v for doc in documents for v in doc.get(key, [])])))
        if len(seen) > 80:
            lines.append(f'\n(+{len(seen) - 80} more fields)')
    return '\n'.join(lines) + '\n'
