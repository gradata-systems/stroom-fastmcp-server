"""A starting translation mapping, drafted from the sample.

A model asked to write a mapping from nothing tends to send back the field inventory instead. The draft gives
it a valid mapping to edit: the input kind from the profile, the obvious event-logging homes for fields by
name (time, host, addresses, ports, user, event type, message), one rule per kind of event the naming field
shows, and every other field carried as Data, with notes on what a person or model must still decide: the
action element for each kind of event, the system name and environment, a time zone for naive timestamps.
The result is checked like any mapping, so a wrong guess shows up as a problem, not as a silent mistake.
"""
import re
from collections import Counter
from typing import Any

from utils.actions import action_rules, described, read
from utils.dsgen import SplitterSpec, dry_run, infer_spec
from utils.profile import _flatten, profile, profile_many, value_type, xml_fragments
from utils.samples import as_named_samples

# Field-name patterns, tried in order; the first field matching a home takes it.
HOMES = [
    ('EventSource/Device/HostName', r'^(host|hostname|host_?name|devname|device|device_?name|device_?id|computer|server|node|sensor|appliance|probe)$'),
    ('EventSource/Client/IPAddress', r'^(src_?ip|source_?ip|srcaddr|src_?addr|client_?ip|client_?addr(ess)?|remote_?addr|remote_?ip|source_?address|ip_?address|ipaddress|clientip)$'),
    ('EventSource/Client/Port', r'^(src_?port|source_?port|client_?port|sport)$'),
    ('EventSource/Server/IPAddress', r'^(dst_?ip|dest_?ip|destination_?ip|dstaddr|dst_?addr|server_?ip|server_?addr(ess)?|target_?ip|destination_?address)$'),
    ('EventSource/Server/Port', r'^(dst_?port|dest_?port|destination_?port|server_?port|dport|service_?port)$'),
    ('EventSource/User/Id', r'^(user|username|user_?name|user_?id|userid|account|account_?name|login|subject|actor|principal|uid|target_?user_?name)$'),
    ('EventSource/Generator', r'^(generator|app|app_?name|application|program|process_?name|logger|service|source)$'),
    ('EventDetail/Description', r'^(message|msg|description|desc|text|summary|details)$'),
]
TIME = re.compile(r'^(@?timestamp|time|ts|date_?time|datetime|event_?time|eventtime|created|created_?at|logged_?at|time_?created|occurred|when|_time)$', re.I)
NAMING = re.compile(r'^(event_?type|eventtype|type|event|event_?name|event_?id|eventid|action|activity|category|subtype|sub_?type|operation|op|logid|log_?type|msg_?id|kind|result_?type)$', re.I)
AUTH = re.compile(r'(login|logon|signin|sign_in|authenticat|logoff|logout|signout|sign_out|password)', re.I)
LOGOFF = re.compile(r'(logoff|logout|signout|sign_out)', re.I)
MAX_RULES = 12


def _key(name: str) -> str:
    return name.rsplit('.', 1)[-1].lower()


def _selector(mapping: dict[str, Any], field: str) -> str:
    """The XPath for an input field, as the generator writes it for the mapping's input kind."""
    if mapping.get('input') == 'data_splitter':
        return '/'.join(f"data[@name='{p}']" for p in field.split('/')) + '/@value'
    if mapping.get('input') == 'json':
        return '/'.join(f"*[@key='{p}']" for p in field.split('.'))
    return field


def _records(named: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, str]], SplitterSpec | None]:
    """The profile (merged across files), the records as flat name -> value dicts, and the splitter spec for text."""
    info = profile_many(named) if len(named) > 1 else profile(next(iter(named.values())))
    fmt = info['format']
    records: list[dict[str, str]] = []
    spec = None
    for text in named.values():
        if fmt in ('json array', 'json lines'):
            import json
            stripped = text.strip()
            values = json.loads(stripped) if stripped.startswith('[') else [json.loads(l) for l in stripped.splitlines() if l.strip()]
            records += [{k: (v if isinstance(v, str) else json.dumps(v)) for k, v in _flatten(r).items()} for r in values if isinstance(r, dict)]
        elif fmt in ('xml', 'xml fragments'):
            from utils.localcheck import _local
            from lxml import etree
            if fmt == 'xml fragments':
                elements = xml_fragments(text) or []
            else:
                root = etree.fromstring(text.strip().encode('utf-8'))
                elements = [c for c in root if isinstance(c.tag, str)]
            for rec in elements:
                flat: dict[str, str] = {}
                for node in rec.iter():
                    if not isinstance(node.tag, str):
                        continue
                    path = '/'.join(_local(a.tag) for a in reversed(list(node.iterancestors())) if a is not rec and a is not rec.getparent()) or ''
                    here = f"{path}/{_local(node.tag)}".strip('/') if node is not rec else ''
                    for attr, value in node.attrib.items():
                        flat[f"{here + '/' if here else ''}@{attr}"] = value
                    if node is not rec and len(node) == 0 and (node.text or '').strip():
                        flat[here] = node.text.strip()
                records.append(flat)
        else:
            if spec is None:
                spec, _ = infer_spec(text)
            if spec is not None:
                records += dry_run(spec, text)['records']
    return info, records, spec


SOURCE_SIDE = re.compile(r'^(src|source|client|orig|origin)(_|$)', re.I)
DESTINATION_SIDE = re.compile(r'^(dst|dest|destination|server|target|resp|responder)(_|$)', re.I)


def _data_home(element: str, field: str) -> str:
    """Where a field with no element of its own goes as Data: on a network action, the side it names (a
    destination_key under Destination, a source_zone under Source); otherwise the action element itself."""
    if element.startswith('EventDetail/Network/'):
        name = field.rsplit('.', 1)[-1]
        if SOURCE_SIDE.match(name):
            return f'{element}/Source'
        if DESTINATION_SIDE.match(name):
            return f'{element}/Destination'
    return element


def draft_mapping(samples: Any, source_name: str = '', system_name: str | None = None,
                  environment: str | None = None, source_notes: dict[str, Any] | None = None,
                  detail_check: Any = None) -> dict[str, Any]:
    """{'mapping', 'splitter', 'notes', 'unmapped_fields', 'kinds'}: a valid mapping to edit, and what is left to decide."""
    named = as_named_samples(samples)
    info, records, spec = _records(named)
    fmt = info['format']
    notes: list[str] = []
    mapping: dict[str, Any] = {}
    if fmt in ('json array', 'json lines'):
        mapping.update(input='json', json_layout='lines' if fmt == 'json lines' else 'array')
    elif fmt == 'xml':
        mapping.update(input='xml', root=info.get('root'), record=info.get('record_element'), xml_namespace=info.get('namespace') or '')
    elif fmt == 'xml fragments':
        mapping.update(input='xml_fragments', record=info.get('record_element'), xml_namespace=info.get('namespace') or 'records:2')
    else:
        mapping['input'] = 'data_splitter'
        if spec is None:
            notes.append(f"The text's format could not be inferred ({fmt}): build_data_splitter with a regex spec first, "
                         f"then name its fields here.")
    shapes = Counter(frozenset(k for k, v in r.items() if v not in (None, '')) for r in records if isinstance(r, dict))

    def survey() -> tuple[list[str], dict[str, list[str]], dict[str, str]]:
        names: list[str] = []
        for record in records:
            names += [k for k in record if k not in names]
        values = {n: [r[n] for r in records if r.get(n) not in (None, '')] for n in names}
        types = {n: Counter(value_type(v) for v in vals).most_common(1)[0][0] if vals else 'empty'
                 for n, vals in values.items()}
        return names, values, types
    names, values, types = survey()
    # Fields holding JSON text: their keys are fields of their own (<field>.<key>, read through json_fields), and the
    # text itself is never mapped into an element. Seen: a whole JSON line written into TypeId.
    json_fields = [n for n in names if types[n] == 'embedded json']
    if json_fields:
        from utils.localcheck import with_json_fields
        with_json_fields(records, json_fields)
        names, values, types = survey()
        mapping['json_fields'] = json_fields
        inside = [n for n in names if any(n.startswith(j + '.') for j in json_fields)]
        notes.append(f"{', '.join(repr(j) for j in json_fields)} {'holds' if len(json_fields) == 1 else 'hold'} JSON: "
                     f"its keys are fields of their own, e.g. "
                     f"{', '.join(repr(n) for n in inside[:4])} (json_fields). The text itself isn't mapped.")

    taken: set[str] = set(json_fields)
    common: list[dict[str, Any]] = []

    def covering(first: str, fits) -> list[str]:
        """first, then the fields fitting the same test that the records lacking it hold: one record shape's time
        field, then another's. Seen: two JSON shapes in one sample, and every event of the second without a time."""
        chosen, lacking = [first], [r for r in records if isinstance(r, dict) and not r.get(first)]
        while lacking:
            more = next((n for n in names if n not in taken and n not in chosen and fits(n)
                         and any(r.get(n) for r in lacking)), None)
            if more is None:
                break
            chosen.append(more)
            lacking = [r for r in lacking if not r.get(more)]
        return chosen

    def read_from(entry: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        if len(fields) > 1:
            entry.pop('field', None)
            entry['any_of'] = fields
        taken.update(fields)
        return entry
    # Time: the first timestamp-typed field whose name says time, else the first timestamp-typed field; a date-only
    # field beside a time-of-day field (FortiOS's date= and time=) is joined.
    time_field = next((n for n in names if TIME.match(_key(n)) and types[n].startswith('timestamp')), None) \
        or next((n for n in names if types[n].startswith('timestamp')), None)
    date_only = time_field and 'H' not in types[time_field] and 'h' not in types[time_field]
    clock = next((n for n in names if n != time_field and values[n]
                  and all(re.fullmatch(r'\d{1,2}:\d{2}(:\d{2})?(\.\d+)?', v.strip()) for v in values[n])), None)
    if time_field and date_only and clock:
        date_pattern = types[time_field][len('timestamp ('):-1]
        seconds = ':ss' if all(v.count(':') == 2 for v in values[clock]) else ''
        common.append({'path': 'EventTime/TimeCreated',
                       'xpath': f"concat({_selector(mapping, time_field)}, ' ', {_selector(mapping, clock)})",
                       'time_format': f'{date_pattern} HH:mm{seconds}', 'timezone': 'UTC'})
        taken.update({time_field, clock})
        notes.append(f"'{time_field}' and '{clock}' are joined for EventTime/TimeCreated, read as UTC: change timezone if the "
                     f"source writes local time.")
    elif time_field:
        pattern = types[time_field][len('timestamp ('):-1]
        entry: dict[str, Any] = {'path': 'EventTime/TimeCreated', 'field': time_field}
        read_from(entry, covering(time_field, lambda n: types[n] == types[time_field]))
        if pattern == 'epoch seconds':
            entry['time_format'] = 'epoch_s'
        elif pattern == 'epoch milliseconds':
            entry['time_format'] = 'epoch_ms'
        else:
            entry['time_format'] = pattern
            if not any(z in pattern for z in ('X', 'Z', 'z', 'O', 'V')):
                entry['timezone'] = 'UTC'
                notes.append(f"'{time_field}' has no time zone: timezone is set to UTC; change it if the source writes local time.")
        common.append(entry)
        taken.add(time_field)
    else:
        notes.append("No timestamp field was recognised: map EventTime/TimeCreated yourself (time_format from the profile).")
    common.append({'path': 'EventSource/System/Name', 'value': system_name or source_name or 'TODO system name'})
    common.append({'path': 'EventSource/System/Environment', 'value': environment or 'TODO e.g. Prod'})
    if not system_name or not environment:
        notes.append("Set EventSource/System/Name and Environment to what the user confirms (or the standing instructions say).")
    for path, pattern in HOMES:
        regex = re.compile(pattern, re.I)
        field = next((n for n in names if n not in taken and regex.match(_key(n)) and types[n] != 'empty'), None)
        if field:
            common.append(read_from({'path': path, 'field': field},
                                    covering(field, lambda n, rx=regex: rx.match(_key(n)) and types[n] != 'empty')))
    if not any(e['path'] == 'EventSource/Generator' for e in common):
        common.append({'path': 'EventSource/Generator', 'value': source_name or 'TODO generator'})
    if not any(e['path'] == 'EventSource/Device/HostName' for e in common):
        notes.append("No host field was recognised: EventSource/Device needs HostName or IPAddress; map one, or a constant, "
                     "or stroom:meta('RemoteAddress') through xpath.")

    # Kinds of event: the first naming field with a handful of values.
    def names_kinds(n: str) -> bool:
        return bool(NAMING.match(_key(n))) and 1 <= len(set(values[n])) <= MAX_RULES
    naming = next((n for n in names if n not in taken and names_kinds(n)), None)
    namings = covering(naming, names_kinds) if naming else []
    if naming:
        common.append(read_from({'path': 'EventDetail/TypeId', 'field': naming}, namings))
    rest = [n for n in names if n not in taken and types[n] != 'empty']
    # (naming field, kind): each shape's kinds, from the records the earlier naming fields leave out.
    pairs: list[tuple[str, str]] = []
    for i, n in enumerate(namings):
        own = [r[n] for r in records if isinstance(r, dict) and r.get(n) and not any(r.get(m) for m in namings[:i])]
        pairs += [(n, v) for v, _ in Counter(own).most_common(MAX_RULES)]
    kinds = [k for _, k in pairs]
    rules: list[dict[str, Any]] = []

    def data_entries(action: str) -> list[dict[str, Any]]:
        return [{'path': f'EventDetail/{action}/Data', 'data_name': n, 'field': n} for n in rest]

    # A field, or across record shapes several read in turn (any_of).
    user = next((e.get('field') or e.get('any_of') for e in common if e['path'] == 'EventSource/User/Id'), None)
    message = next((e.get('field') or e.get('any_of') for e in common if e['path'] == 'EventDetail/Description'), None)
    recognised: list[str] = []
    used_names: set[str] = set()
    for naming, kind in pairs:
        name = re.sub(r'[^A-Za-z0-9]+', '_', kind).strip('_').lower() or 'event'
        while name in used_names:
            name += '_'
        used_names.add(name)
        if AUTH.search(kind):
            fields = [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logoff' if LOGOFF.search(kind) else 'Logon'}]
            if user:
                fields.append({'path': 'EventDetail/Authenticate/User/Id', **read(user)})
            fields += data_entries('Authenticate')
            rules.append({'name': name, 'when': [{'field': naming, 'equals': kind}], 'fields': fields})
            continue
        # The kind's own records often show its actions in another field (a firewall's TRAFFIC: action ALLOW or
        # DENY; ADMIN: LOGIN_SUCCESS, LOGOUT, CONFIG_CHANGE): a rule per action, before the kind's own rule.
        of_kind = [r for r in records if isinstance(r, dict) and r.get(naming) == kind]
        found, left, split = action_rules(of_kind, names, [{'field': naming, 'equals': kind}], name, user, message,
                                          skip={naming})
        if not found:
            # The kind itself may be the action (CONNECT, DENY): split on the naming field.
            found, left, split = action_rules(of_kind, names, [], '', user, message, split=naming)
        for found_rule in found:
            # The rest of the record is carried as the action element's Data, as the Unknown rule did.
            element = '/'.join(found_rule['fields'][0]['path'].split('/')[:3 if 'Network' in found_rule['fields'][0]['path'] else 2])
            mapped = {f.get('field') for f in found_rule['fields']} | {split}
            found_rule['fields'] += [{'path': f'{_data_home(element, n)}/Data', 'data_name': n, 'field': n}
                                     for n in rest if n not in mapped]
        rules += found
        if found:
            recognised.append(f"{kind}: {described(found)}")
        if not found or left:
            rules.append({'name': name, 'when': [{'field': naming, 'equals': kind}], 'fields': data_entries('Unknown')})
    if recognised:
        notes.append("Drafted from the sample's values (check them): " + '; '.join(recognised) + ".")
    naming = namings[0] if namings else None
    rules.append({'name': 'other', 'fields': ([{'path': 'EventDetail/TypeId', 'value': 'Other'}] if not naming else [])
                  + data_entries('Unknown')})
    if len(shapes) > 1 and len(namings) + sum(1 for e in common if e.get('any_of')) > 0 and \
            (len(namings) > 1 or any(e.get('any_of') for e in common)):
        shared = [f"{e['path']} from {' or '.join(e['any_of'])}" for e in common if e.get('any_of')]
        notes.append(f"The sample's records come in {len(shapes)} shapes (their fields differ): each element reads every "
                     f"shape's field in turn ({'; '.join(shared)}), and each shape's kinds have rules. Check them "
                     f"against each file.")
    unknown_kinds = [r for r in rules if r.get('when') and any(f['path'].startswith('EventDetail/Unknown') for f in r['fields'])]
    if kinds and not unknown_kinds:
        notes.append(f"One rule per value of {' or '.join(repr(n) for n in namings)} ({', '.join(kinds)}), each with its "
                     f"action element; 'other' "
                     f"catches the rest. Work through the schema problems build_translation_xslt reports: each names "
                     f"what the element takes.")
    elif kinds:
        notes.append(f"One rule per value of '{naming}' ({', '.join(kinds)}): replace each EventDetail/Unknown left by the "
                     f"action element that describes the kind (Authenticate, Network, Process, View, Create, Update, Delete, "
                     f"Alert, Send, Receive...), and move its Data entries into that element (e.g. EventDetail/Alert/Data); "
                     f"'other' catches the rest. Work through the schema problems build_translation_xslt reports: each "
                     f"names what the element takes. Unknown is for records no action element describes, not a way "
                     f"round a schema error; build_translation_xslt refuses it where the values show an action.")
    else:
        notes.append("No field names the kind of event: every record is one 'other' event with Unknown/Data. Add rules "
                     "with conditions once you know how kinds are told apart.")
    mapping.update(common=common, events=rules)
    applied = None
    if source_notes and (source_notes.get('fields') or source_notes.get('events')):
        # The user's documentation says where fields belong and what each event is: it wins over guesses from names.
        from utils.sourcenotes import apply_to_draft
        applied = apply_to_draft(mapping, names, values, source_notes, detail_check)
        if applied['events']:
            notes[:] = [n for n in notes if not n.startswith(('One rule per value of', 'No field names the kind'))]
        if applied['fields'] or applied['events']:
            rest = [n for n in rest if not any(e.get('field') == n for e in mapping['common'])]
            notes.insert(0, "Drafted from the source documentation: " + '; '.join(applied['fields'] + applied['events']) + '.')
        if applied['from_sample']:
            notes.insert(1, "Kept from the sample's values, where the catalogue can't be followed: "
                         + '; '.join(applied['from_sample']) + '.')
        if applied['not_in_catalogue']:
            notes.append(f"The catalogue does not list {', '.join(applied['not_in_catalogue'])}: those records fall to the "
                         f"rule for the rest; read the reference documents (find_documents content=the value) or ask the user.")
    return {'mapping': mapping, 'source_notes': applied, 'splitter': spec.model_dump(exclude_none=True, exclude_defaults=True) if spec else None,
            'format': fmt, 'records': len(records), 'kinds': kinds, 'notes': notes,
            'unmapped_fields': rest, 'fields_seen': names}
