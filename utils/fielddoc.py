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
    kept = [r for r in mapping.events if r.allow_unknown]
    if kept:
        lines += ['### Kept as Unknown', '']
        lines += [f"- `{r.name}` ({' and '.join(condition_text(c) for c in r.when) or 'records no other rule matches'}): "
                  f"{r.allow_unknown}" for r in kept]
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
_IDS = {'@StreamId': 'The id of the Events stream the event came from.',
        '@EventId': "The event's number in its stream; with StreamId, it identifies the event."}


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
    if schema is not None:
        try:
            said = schema.describe(schema.resolve(path))
        except (ValueError, IndexError, AttributeError):
            said = ''
    parts = path.strip('/').split('/')
    if said and len(parts) > 1:
        # Ids and names inherit a base type's words ('the object', 'the device'): name the element they belong to.
        parent = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', parts[-2]).lower()
        generic = re.search(r'\bthe (object|device)\b', said)
        if generic and generic.group(1) != parent:
            said = re.sub(r',?\s+e\.?g\.?\s.*$', '.', said.replace(generic.group(0), f'the {parent}', 1))
    if data:
        return f"The `{data.group(1)}` value recorded in a Data element" + (f" of {path.split('/')[-1]}: {said}" if said else '.')
    if attribute:
        return f"The `{attribute}` attribute of {path.split('/')[-1]}" + (f": {said}" if said else '.')
    return said


def sample_description(per_document: list[list[str]]) -> str:
    """What the sample shows of a field: what kind of values, and how they vary between documents."""
    holding = [[v for v in vs if v != ''] for vs in per_document]
    holding = [vs for vs in holding if vs]
    values = [v for vs in holding for v in vs]
    if not values:
        return 'Not in the sample.'
    distinct = list(dict.fromkeys(values))
    kind = next((name for name, pattern in (('IP addresses', _IP), ('Email addresses', _EMAIL),
                                            ('Timestamps', _TIME), ('Numbers', _NUMBER))
                 if all(pattern.match(v) for v in distinct)), '')
    if len(per_document) == 1:
        spread = 'from the one sampled document'
    elif len(distinct) == 1:
        spread = (f"the same in every sampled document (`{distinct[0][:40]}`)" if len(holding) == len(per_document)
                  else f"one value in the sample (`{distinct[0][:40]}`), in {len(holding)} of {len(per_document)} documents")
    elif len(distinct) == len(values):
        spread = 'different in each sampled document'
    else:
        spread = f"{len(distinct)} distinct value{'s' if len(distinct) != 1 else ''} in the sample"
    if any(len(vs) > 1 for vs in holding):
        spread += ', several per document'
    text = f"{kind}, {spread}" if kind else spread
    return text[0].upper() + text[1:] + '.'


def field_description(what: str, per_document: list[list[str]] | None) -> str:
    """A field's description: what it is (the plan's description, the schema's, or its source), then what the
    sample shows."""
    parts = [what.strip()] if what and what.strip() else []
    if per_document is not None:
        parts.append(sample_description(per_document))
    return ' '.join(p if p.endswith('.') else p + '.' for p in parts)


def existing_index_markdown(survey: dict[str, Any], planned: dict[str, Any], schema: EventSchema | None) -> str:
    """The Field mapping section for an existing index, from its survey: every field Stroom has for it, with a
    description (the feeding pipeline's plan, else the event-logging schema for the field's source, then what the
    sample shows), its type, where it comes from when a feeding pipeline's plan records it, how often the surveyed
    documents held it, and their values."""
    index, documents = survey['index'], survey.get('documents') or []
    target = (f"Elasticsearch index `{survey['index_name']}`" if survey['backend'] == 'elasticsearch'
              else 'a Lucene index')
    lines = [f"`{index['name']}` is {target}; the time field is `{survey.get('time_field')}`.", '']
    fed = survey.get('fed_by') or []
    lines += ([f"Fed by {', '.join(f'`{p['name']}`' for p in fed)}." +
               ('' if any(p.get('plan') for p in fed) else ' No index plan is kept with its XSLT, so where each '
                'field comes from is not recorded here.'), '']
              if fed else ['No pipeline was found that writes to it (pipelines are looked for by the index name '
                           'they write; one that builds the name from values, or writes through an alias, is not '
                           'found this way).', ''])
    if documents:
        lines += [f"Surveyed through Stroom: the {len(documents)} newest documents"
                  + (f" ({survey['earliest']} to {survey['latest']})" if survey.get('earliest') else '')
                  + '. In sample and Sample values are what they held.', '']
    else:
        lines += [survey.get('note') or 'No documents came back through Stroom.', '']
    head = ['Index field', 'Description', 'Type', 'From (event-logging path)', 'In sample', 'Sample values']
    lines += [_row(*head), _row(*['---'] * len(head))]
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
        lines[-3:-3] = [f"The survey read {len(surveyed)} of the {len(fields)} fields; the other {len(unsurveyed)} "
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
            what = (getattr(plan, 'description', '') if plan else '') or (schema_description(schema, source) if source
                                                                          else _IDS.get('@' + f, '') if f in ('StreamId', 'EventId') else '')
            description = field_description(what, per_document)
            count = sum(1 for vs in per_document if any(v != '' for v in vs))
            held = f'{100 * count / len(documents):.0f}% of documents' if documents else 'no documents'
            values = _values([v for vs in per_document for v in vs])
        lines.append(_row(f'`{f}`', description, field['type'], f'`{source}`' if source else '(not recorded)', held, values))
    return '\n'.join(lines) + '\n'


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
                                 schema: EventSchema | None = None) -> str:
    """The Field mapping section of an indexing pipeline's documentation: index field, a description (the plan's,
    else the event-logging schema's for its source, then what the sample shows), type, the event-logging path it
    comes from, and from the sample: how often that path is populated in the Events, and the values the index
    documents the pipeline wrote got for the field."""
    lines = [f'Documents for `{plan.index_name}` ({plan.backend}); the time field is `{plan.time_field}`.', '']
    if plan.drop_when:
        lines += ['Events left out of the index:', ''] + [f'- `{t}`' for t in plan.drop_when] + ['']
    sampled = population is not None
    if documents is not None:
        lines += [f'Sample values are what the {len(documents)} documents written from the sample got.', '']
    head = ['Index field', 'Description', 'Type', 'From (event-logging path)'] + (['In sample'] if sampled else [])         + (['Sample values'] if documents is not None else [])
    lines += [_row(*head), _row(*['---'] * len(head))]
    for f in grouped_fields(plan):
        shared = plan.written_by(f.name) if hasattr(plan, 'written_by') else None
        what = f.description or (f"Written by the shared template `{shared.template}`." if shared
                                 else schema_description(schema, f.source))
        per_document = [d.get(f.name, []) for d in documents] if documents is not None else None
        cells = [f'`{f.name}`', field_description(what, per_document), shown_type(plan, f.type), f"shared template `{shared.template}` of `{shared.href}`" if shared else f'`{f.source}`']
        if sampled:
            pct = population.get(f.source)
            cells.append('always' if f.source.startswith('@') or shared else f'{pct:g}% of events' if pct is not None
                         else 'not in the sample')
        if documents is not None:
            values = list(dict.fromkeys(v for d in documents for v in d.get(f.name, []) if v != ''))
            cells.append(', '.join(f'`{v[:40]}`' for v in values[:3]) + (f' (+{len(values) - 3})' if len(values) > 3 else '')
                         if values else '(none in the sample)')
        lines.append(_row(*cells))
    return '\n'.join(lines) + '\n'


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
