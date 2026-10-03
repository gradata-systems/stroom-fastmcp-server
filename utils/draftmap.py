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

from utils.dsgen import SplitterSpec, dry_run, infer_spec
from utils.profile import _flatten, profile, profile_many, value_type, xml_fragments
from utils.samples import as_named_samples

# Field-name patterns, tried in order; the first field matching a home takes it.
HOMES = [
    ('EventSource/Device/HostName', r'^(host|hostname|host_?name|devname|device|device_?name|device_?id|computer|server|node)$'),
    ('EventSource/Client/IPAddress', r'^(src_?ip|source_?ip|srcaddr|src_?addr|client_?ip|client_?address|remote_?addr|remote_?ip|source_?address|ip_?address|ipaddress|clientip)$'),
    ('EventSource/Client/Port', r'^(src_?port|source_?port|client_?port|sport)$'),
    ('EventSource/Server/IPAddress', r'^(dst_?ip|dest_?ip|destination_?ip|dstaddr|dst_?addr|server_?ip|target_?ip|destination_?address)$'),
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


def draft_mapping(samples: Any, source_name: str = '', system_name: str | None = None,
                  environment: str | None = None) -> dict[str, Any]:
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
    names: list[str] = []
    for record in records:
        names += [k for k in record if k not in names]
    values = {n: [r[n] for r in records if r.get(n) not in (None, '')] for n in names}
    types = {n: Counter(value_type(v) for v in vals).most_common(1)[0][0] if vals else 'empty' for n, vals in values.items()}

    taken: set[str] = set()
    common: list[dict[str, Any]] = []
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
            common.append({'path': path, 'field': field})
            taken.add(field)
    if not any(e['path'] == 'EventSource/Generator' for e in common):
        common.append({'path': 'EventSource/Generator', 'value': source_name or 'TODO generator'})
    if not any(e['path'] == 'EventSource/Device/HostName' for e in common):
        notes.append("No host field was recognised: EventSource/Device needs HostName or IPAddress; map one, or a constant, "
                     "or stroom:meta('RemoteAddress') through xpath.")

    # Kinds of event: the first naming field with a handful of values.
    naming = next((n for n in names if n not in taken and NAMING.match(_key(n)) and 1 <= len(set(values[n])) <= MAX_RULES), None)
    if naming:
        common.append({'path': 'EventDetail/TypeId', 'field': naming})
        taken.add(naming)
    rest = [n for n in names if n not in taken and types[n] != 'empty']
    kinds = [v for v, _ in Counter(values[naming]).most_common(MAX_RULES)] if naming else []
    rules: list[dict[str, Any]] = []

    def data_entries(action: str) -> list[dict[str, Any]]:
        return [{'path': f'EventDetail/{action}/Data', 'data_name': n, 'field': n} for n in rest]

    for kind in kinds:
        if AUTH.search(kind):
            fields = [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logoff' if LOGOFF.search(kind) else 'Logon'}]
            user = next((e['field'] for e in common if e['path'] == 'EventSource/User/Id'), None)
            if user:
                fields.append({'path': 'EventDetail/Authenticate/User/Id', 'field': user})
            fields += data_entries('Authenticate')
        else:
            fields = data_entries('Unknown')
        rules.append({'name': re.sub(r'[^A-Za-z0-9]+', '_', kind).strip('_').lower() or 'event',
                      'when': [{'field': naming, 'equals': kind}], 'fields': fields})
    rules.append({'name': 'other', 'fields': ([{'path': 'EventDetail/TypeId', 'value': 'Other'}] if not naming else [])
                  + data_entries('Unknown')})
    if kinds:
        notes.append(f"One rule per value of '{naming}' ({', '.join(kinds)}): replace EventDetail/Unknown by the right action "
                     f"element for each (Authenticate, Network, Process, View, Create, Update, Delete, Alert, Send, Receive...) and "
                     f"move its Data entries to the elements that mean them; 'other' catches the rest. build_translation_xslt "
                     f"refuses a rule with conditions that still writes Unknown, unless the rule says allow_unknown: true "
                     f"because no action element fits.")
    else:
        notes.append("No field names the kind of event: every record is one 'other' event with Unknown/Data. Add rules "
                     "with conditions once you know how kinds are told apart.")
    mapping.update(common=common, events=rules)
    return {'mapping': mapping, 'splitter': spec.model_dump(exclude_none=True, exclude_defaults=True) if spec else None,
            'format': fmt, 'records': len(records), 'kinds': kinds, 'notes': notes,
            'unmapped_fields': rest, 'fields_seen': names}
