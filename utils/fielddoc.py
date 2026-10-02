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
from utils.xsltgen import (RULE_MARK, Condition, EventRule, FieldMapping, TranslationMapping, literal)  # noqa: F401

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


def field_mapping_markdown(mapping: TranslationMapping, schema: EventSchema,
                           observed: list[etree._Element] | None = None) -> str:
    """The section: how the mapping reads records (items, drops, extractions), then the EventSource table (XPath,
    the schema's description, From, and with a sample the values written) and the event types table (one row per
    rule: its conditions, TypeId and Description from-and-values, how many of the sample's events it wrote, and
    every EventDetail element as path <- from = sample value)."""
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
    if mapping.drop_when:
        lines += ['### Left untranslated', '']
        lines += [f"- {' and '.join(condition_text(c) for c in d.when)}: {d.reason}" for d in mapping.drop_when]
        lines.append('')
    if mapping.extract:
        lines += ['### Extracted fields', '']
        for ex in mapping.extract:
            names = ', '.join(f'`{n}`' for n in ex.names if n)
            lines.append(f"- {names}: parsed from `{ex.field or readable(ex.xpath)}`"
                         f"{' (of the record)' if ex.scope == 'record' else ''} with `{ex.regex}`")
        lines.append('')

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


def index_field_mapping_markdown(plan: Any, population: dict[str, float] | None = None) -> str:
    """The Field mapping section of an indexing pipeline's documentation: index field, type, the event-logging
    path it comes from, and (from the Events sampled) how often that path is populated."""
    lines = [f'Documents for `{plan.index_name}` ({plan.backend}); the time field is `{plan.time_field}`.', '']
    if plan.drop_when:
        lines += ['Events left out of the index:', ''] + [f'- `{t}`' for t in plan.drop_when] + ['']
    sampled = population is not None
    lines += (['| Index field | Type | From (event-logging path) | In sample |', '| --- | --- | --- | --- |'] if sampled else
              ['| Index field | Type | From (event-logging path) |', '| --- | --- | --- |'])
    for f in plan.fields:
        cells = [f'`{f.name}`', f.type, f'`{f.source}`']
        if sampled:
            pct = population.get(f.source)
            cells.append('always' if f.source.startswith('@') else f'{pct:g}% of events' if pct is not None else 'not in the sample')
        lines.append(_row(*cells))
    return '\n'.join(lines) + '\n'
