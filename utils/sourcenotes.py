"""Source notes: what the user's vendor or event reference documentation says about a source, kept so the tools use it.

record_source_notes keeps the documents themselves verbatim (Documentation docs '<source> reference - <title>', to
read and search later) and the notes condensed from them: a field dictionary (each source field's meaning, its
codes, the event-logging element it belongs in) and an event catalogue (each kind of event, how a record shows it,
and what it is in event-logging terms). The notes doc carries them as a block the server reads back:

- draft_translation_mapping builds its draft from them: fields where the dictionary puts them, one rule per
  catalogued event with its action element, TypeId, description, action and outcome;
- build_translation_xslt checks a mapping against the catalogue, so a rule that contradicts it is reported;
- write_documentation lists each source field with the documentation's meaning and codes.
"""
import json
import re
from typing import Any, Callable


_BLOCK = re.compile(r'<!-- stroom-mcp source notes\n(.*?)\n-->', re.S)
NOTES_SUFFIX = ' source notes'
REFERENCE_INFIX = ' reference - '


def block(notes: dict[str, Any]) -> str:
    return f"<!-- stroom-mcp source notes\n{json.dumps(notes, indent=1, ensure_ascii=False)}\n-->"


def read_notes(text: str | None) -> dict[str, Any] | None:
    found = _BLOCK.search(text or '')
    if not found:
        return None
    try:
        notes = json.loads(found.group(1))
    except ValueError:
        return None
    return notes if isinstance(notes, dict) else None


async def notes_in_build(ctx, build: str) -> list[dict[str, Any]]:
    """The source notes kept in a build (its '<source> source notes' docs)."""
    from tools.builds import _build_docs
    from utils.stroom import body_text, gateway_from
    found = []
    try:
        stroom = gateway_from(ctx)
        for doc in await _build_docs(ctx, build):
            if doc['type'] == 'Documentation' and doc['name'].endswith(NOTES_SUFFIX):
                notes = read_notes(body_text(await stroom.get_doc('Documentation', doc['uuid'])))
                if notes:
                    found.append(notes)
    except Exception:   # notes are optional: a build that cannot be listed has none to use
        return []
    return found


def merged(notes: list[dict[str, Any]]) -> dict[str, Any]:
    """Several notes docs read as one: fields by name, events in order."""
    fields: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    for n in notes:
        for f in n.get('fields') or []:
            fields.setdefault(f['field'], f)
        events += n.get('events') or []
    return {'fields': list(fields.values()), 'events': events}


_SUCCESS = re.compile(r'\b(success|succeeded|successful|ok|allow(ed)?|accept(ed)?|granted|pass(ed)?)\b', re.I)
_FAILURE = re.compile(r'\b(fail(ed|ure)?|denied|den(y|ies)|reject(ed)?|block(ed)?|error|invalid|locked)\b', re.I)


def outcome_map(values: dict[str, str]) -> dict[str, str] | None:
    """A field's codes as Outcome/Success values, when the documentation's meanings say success or failure."""
    out = {}
    for code, meaning in values.items():
        if _FAILURE.search(meaning):
            out[code] = 'false'
        elif _SUCCESS.search(meaning):
            out[code] = 'true'
    return out if out and len(out) == len(values) else None


_NOT_ACTIONS = {'TypeId', 'Description', 'Classification', 'Purpose'}


def detail_problem(schema, detail: str) -> str | None:
    """Why an event's event_detail can't be its action element, or None: it must be an EventDetail action element that
    takes Data (where the draft puts the event's other fields), so Network needs its action (Network/Permit)."""
    detail = (detail or '').strip().strip('/')
    if not detail:
        return None
    top = [c.name for c in schema.children(schema.resolve('EventDetail')[-1].decl) if c.name not in _NOT_ACTIONS]
    try:
        chain = schema.resolve(f'EventDetail/{detail}')[1:]     # below EventDetail
    except ValueError:
        chain = None
    if not chain or chain[0].name in _NOT_ACTIONS:
        hint = (" A connection allowed or denied is Network/Permit or Network/Deny."
                if detail.split('/')[0].lower() in {'allow', 'allowed', 'accept', 'permit', 'deny', 'denied', 'block',
                                                     'blocked', 'drop', 'reject', 'traffic'} else '')
        return f"'{detail}' is not an EventDetail action element.{hint} The action elements: {', '.join(top)}"
    below = [c.name for c in schema.children(chain[-1].decl)]
    if 'Data' not in below or [c.name for c in chain] == ['Network']:
        # Network's action is an element of its own (Permit, Deny, Connect...); v4 lets Network hold Data as well.
        below = [b for b in below if b != 'Data']
        return (f"'{detail}' needs its action below it: "
                + ', '.join(f'{detail}/{c}' for c in below[:12]))
    return None


def catalogue_problems(events: list[dict[str, Any]], detail_check: Callable[[str], str | None] | None = None) -> list[str]:
    """What makes an event catalogue unusable for drafting: an action element the schema doesn't have, and events a
    record can't tell apart (the same field and value)."""
    problems = []
    seen: dict[tuple[str, str], str] = {}
    for e in events:
        label = e.get('event') or e.get('value') or '?'
        problem = detail_check(e.get('event_detail') or '') if detail_check else None
        if problem:
            problems.append(f"{label}: {problem}")
        if e.get('field') and e.get('value') not in (None, ''):
            key = (e['field'], str(e['value']))
            if key in seen:
                problems.append(f"{seen[key]} and {label} are both {key[0]}={key[1]}: a record can't show which it is. "
                                f"Give each the field and value that tell them apart (e.g. action=ALLOW and action=DENY), "
                                f"or record them as one event")
            else:
                seen[key] = label
    return problems


def _detail_of(rule: dict[str, Any]) -> set[str]:
    """The action elements a rule writes."""
    paths = [f.get('path') or '' for f in rule.get('fields') or []]
    return {p.split('/')[1] for p in paths if p.startswith('EventDetail/') and p.count('/') >= 2}


def _covers(rule: dict[str, Any], field: str, value: str) -> bool:
    return any(c.get('field') == field and (str(c.get('equals')) == value if c.get('equals') is not None
                                            else value in [str(x) for x in c.get('one_of') or []])
               for c in rule.get('when') or [])


def apply_to_draft(mapping: dict[str, Any], names: list[str], values: dict[str, list[str]],
                   notes: dict[str, Any], detail_check: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    """The draft mapping rebuilt from the notes where they speak: fields the dictionary places, one rule per
    catalogued event the sample's fields can show. Where the catalogue can't be followed (an action element the schema
    doesn't have, Unknown, events with the same field and value), the rules the sample's own values gave are kept.
    Returns what was applied, and what the sample and notes disagree on."""
    common: list[dict[str, Any]] = mapping['common']
    sampled = [r for r in mapping['events'] if r.get('when')]
    applied: dict[str, Any] = {'fields': [], 'events': [], 'not_in_sample': [], 'not_in_catalogue': [],
                               'from_sample': []}
    by_field = {f['field']: f for f in notes.get('fields') or [] if f.get('field') in names}
    detail_fields: list[dict[str, Any]] = []
    for name, note in by_field.items():
        path = (note.get('event_logging_path') or '').strip().strip('/')
        if not path:
            continue
        entry: dict[str, Any] = {'path': path, 'field': name}
        if path.endswith('Outcome/Success') and note.get('values'):
            mapped = outcome_map(note['values'])
            if mapped:
                entry['map'] = mapped
        if path.startswith('EventDetail/') and path != 'EventDetail/TypeId':
            detail_fields.append(entry)
        else:
            # The dictionary's word wins over a guess from the field's name.
            common[:] = [e for e in common if e.get('path') != path and e.get('field') != name]
            common.append(entry)
        applied['fields'].append(f"{name} -> {path}")
    events = [e for e in notes.get('events') or [] if e.get('field') in names and e.get('value') not in (None, '')]
    if not events:
        return applied
    seen = {(e['field'], str(e['value'])) for e in events}
    taken = {e.get('field') for e in common} | {e['field'] for e in detail_fields} | {e['field'] for e in events}
    rest = [n for n in names if n not in taken and values.get(n)]
    twins = {k for k in seen if sum(1 for e in events if (e['field'], str(e['value'])) == k) > 1}
    rules: list[dict[str, Any]] = []
    for e in events:
        detail = (e.get('event_detail') or 'Unknown').strip().strip('/') or 'Unknown'
        key = (e['field'], str(e['value']))
        why = ('the catalogue has more than one event for it' if key in twins
               else (detail_check(detail) if detail_check and detail != 'Unknown' else None)
               or ('the catalogue gives no action element' if detail == 'Unknown' else None))
        if why:
            # The sample's values say more than the catalogue here: keep the rules they gave, when they name an action.
            kept = [r for r in sampled if _covers(r, *key) and _detail_of(r) - {'Unknown'}]
            if kept:
                fresh = [r for r in kept if r not in rules]
                rules += fresh
                if fresh:
                    applied['from_sample'].append(
                        f"{e['field']}={e['value']}: {why}, so the sample's rules are kept ("
                        + '; '.join(f"{r['name']}: {', '.join(sorted(_detail_of(r)))}" for r in fresh) + ')')
                continue
            if detail != 'Unknown' and detail_check and detail_check(detail):
                detail = 'Unknown'
        fields: list[dict[str, Any]] = []
        if e.get('type_id'):
            fields.append({'path': 'EventDetail/TypeId', 'value': str(e['type_id'])})
        else:
            fields.append({'path': 'EventDetail/TypeId', 'field': e['field']})
        if e.get('description'):
            fields.append({'path': 'EventDetail/Description', 'value': e['description']})
        if e.get('action'):
            fields.append({'path': f'EventDetail/{detail}/Action', 'value': e['action']})
        if e.get('success') is not None:
            fields.append({'path': f'EventDetail/{detail}/Outcome/Success', 'value': 'true' if e['success'] else 'false'})
        user = next((c.get('field') for c in common if c.get('path') == 'EventSource/User/Id' and c.get('field')), None)
        if detail == 'Authenticate' and user:
            # The schema wants the user authenticating: the one the event is about.
            fields.append({'path': 'EventDetail/Authenticate/User/Id', 'field': user})
        fields += [f for f in detail_fields if f['path'].startswith(f'EventDetail/{detail}/')
                   and not any(x['path'] == f['path'] for x in fields)]
        # What the schema wants that the catalogue doesn't say (Authenticate's Action, Update's After), from the
        # sample's rule for the same records when it writes the same element.
        like = next((r for r in sampled if _covers(r, *key) and detail.split('/')[0] in _detail_of(r)), None)
        if like:
            fields += [f for f in like['fields'] if (f.get('path') or '').startswith(f'EventDetail/{detail}/')
                       and not (f['path'].endswith('/Data') or any(x['path'] == f['path'] for x in fields))]
        fields += [{'path': f'EventDetail/{detail}/Data', 'data_name': n, 'field': n} for n in rest]
        rule: dict[str, Any] = {'name': re.sub(r'[^A-Za-z0-9]+', '_', str(e.get('event') or e['value'])).strip('_').lower()
                                or 'event', 'when': [{'field': e['field'], 'equals': str(e['value'])}], 'fields': fields}
        # Unknown is not agreed here: build_translation_xslt puts it to the user with the records it catches.
        rules.append(rule)
        shown = str(e['value']) in values.get(e['field'], [])
        applied['events'].append(f"{e.get('event') or e['value']} ({e['field']}={e['value']}) -> {detail}"
                                 + ('' if shown else ', not in the sample'))
        if not shown:
            applied['not_in_sample'].append(f"{e['field']}={e['value']}")
    for field in {e['field'] for e in events}:
        for v in dict.fromkeys(values.get(field, [])):
            if (field, v) not in seen:
                applied['not_in_catalogue'].append(f"{field}={v}")
    # Records of values the catalogue doesn't list keep the sample's rule when it names an action, not Unknown.
    rules += [r for r in sampled if r not in rules and _detail_of(r) - {'Unknown'}
              and not any(_covers(r, *k) for k in seen)]
    other = next((r for r in mapping['events'] if not r.get('when')), None)
    names_used: set[str] = set()
    for r in rules:
        while r['name'] in names_used:
            r['name'] += '_'
        names_used.add(r['name'])
    mapping['events'] = rules + ([other] if other else [])
    return applied


def _rule_for(rules: list[dict[str, Any]], field: str, value: str) -> dict[str, Any] | None:
    """The first rule a record holding only field=value would meet (equals and one_of tests; a rule testing anything
    else is passed over, as it cannot be judged from the catalogue)."""
    for rule in rules:
        when = rule.get('when') or []
        if not when:
            return rule
        ok = True
        for c in when:
            if c.get('field') != field:
                ok = False
                break
            if c.get('equals') is not None and str(c['equals']) != value:
                ok = False
            elif c.get('one_of') is not None and value not in [str(x) for x in c['one_of']]:
                ok = False
            elif c.get('equals') is None and c.get('one_of') is None:
                ok = False
        if ok:
            return rule
    return None


def check_mapping(mapping: dict[str, Any], notes: dict[str, Any]) -> list[str]:
    """Where a mapping contradicts the catalogue: a catalogued event's records meet a rule writing another action
    element or TypeId than the documentation gives."""
    problems = []
    common = {e.get('path'): e for e in mapping.get('common') or []}
    for e in notes.get('events') or []:
        if not e.get('field') or e.get('value') in (None, ''):
            continue
        label = f"{e.get('event') or e['value']} ({e['field']}={e['value']})"
        rule = _rule_for(mapping.get('events') or [], e['field'], str(e['value']))
        if rule is None:
            problems.append(f"{label}: no rule takes it")
            continue
        paths = [f.get('path') or '' for f in rule.get('fields') or []]
        details = {p.split('/')[1] for p in paths if p.startswith('EventDetail/') and p.count('/') >= 2}
        want = (e.get('event_detail') or '').strip().strip('/').split('/')[0]
        if want and want != 'Unknown' and want not in details:
            problems.append(f"{label}: the documentation says {want}; rule '{rule.get('name')}' writes "
                            f"{', '.join(sorted(details)) or 'no action element'}")
        if e.get('type_id'):
            type_id = next((f for f in rule.get('fields') or [] if f.get('path') == 'EventDetail/TypeId'),
                           common.get('EventDetail/TypeId'))
            if type_id is not None and type_id.get('value') is not None and str(type_id['value']) != str(e['type_id']):
                problems.append(f"{label}: the documentation's TypeId is '{e['type_id']}'; rule '{rule.get('name')}' "
                                f"writes '{type_id['value']}'")
    return problems


def fields_markdown(notes: dict[str, Any], used: list[str] | None = None) -> str:
    """'Source fields', for the Field mapping section: each source field the documentation describes, with its
    meaning and codes."""
    fields = [f for f in notes.get('fields') or [] if used is None or f['field'] in used]
    if not fields:
        return ''
    lines = ['### Source fields', '', "What the source's documentation says each field holds.", '',
             '| Source field | Meaning | Codes |', '| --- | --- | --- |']
    for f in fields:
        codes = '; '.join(f"`{k}`: {v}" for k, v in (f.get('values') or {}).items())
        lines.append(f"| `{f['field']}` | {(f.get('meaning') or '').replace('|', '/')} | {codes.replace('|', '/')} |")
    return '\n'.join(lines) + '\n'
