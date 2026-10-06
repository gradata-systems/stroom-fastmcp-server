"""Action elements the sample's own values show, as mapping rules.

An agent left with EventDetail/Unknown placeholders and a schema it found awkward kept giving up: it marked
firewall traffic and administrator logons Unknown and asked the user to agree. Some kinds are plain from their
values: connections allowed or denied between addresses and ports are Network/Permit and Network/Deny; logons
and logoffs are Authenticate; configuration changes are Update; a service started or stopped is Process; a
device's health and state (CPU over a threshold, a VPN tunnel down) is Alert. For those, the draft writes the
rules, and a rule that keeps them Unknown is given the rules to use instead.
"""
import re
from typing import Any

PERMIT = re.compile(r'^(allow(ed)?|accept(ed)?|permit(ted)?|pass(ed)?)$', re.I)
DENY = re.compile(r'^(deny|denied|drop(ped)?|block(ed)?|reject(ed)?|refused?)$', re.I)
CONNECT = re.compile(r'^(connect(ed|ion)?|establish(ed)?|open(ed)?)$', re.I)
CLOSE = re.compile(r'^(close[d]?|disconnect(ed)?|teardown|terminated)$', re.I)
AUTH = re.compile(r'(login|logon|signin|sign_in|authenticat|logoff|logout|signout|sign_out)', re.I)
LOGOFF = re.compile(r'(logoff|logout|signout|sign_out)', re.I)
FAILED = re.compile(r'(fail|denied|invalid|bad|reject|refused)', re.I)
SUCCEEDED = re.compile(r'(success|succeeded|ok\b|accepted|allowed)', re.I)
CONFIG = re.compile(r'(config|setting|policy)', re.I)
PROCESS = re.compile(r'^(service_?)?(start(ed|ing|up)?|stop(ped|ping)?|shut_?down|restart(ed)?|boot(ed)?|reboot(ed)?)$', re.I)
STOPPED = re.compile(r'(stop|shut)', re.I)
ALERT = re.compile(r'(cpu|memory|disk|threshold|utili[sz]|overload|alarm|alert|tunnel|vpn|link|interface|exceed|'
                   r'(^|_)(down|up|fail(ed|ure)?|error|critical|high|low)($|_))', re.I)
NETWORK_ALERT = re.compile(r'(tunnel|vpn|link|interface|bgp|ospf|failover)', re.I)
ERROR_ALERT = re.compile(r'(fail|error|critical)', re.I)
SEVERITY = re.compile(r'^(severity|sev|level|log_?level|priority|pri)$', re.I)
SEVERITIES = {'Info': r'^(info(rmational)?|notice|debug|low)$', 'Minor': r'^(warn(ing)?|minor|medium)$',
              'Major': r'^(err(or)?|major|high)$', 'Critical': r'^(crit(ical)?|alert|emerg(ency)?|fatal|severe)$'}

SPLITTER = re.compile(r'^(action|act|result|outcome|operation|op|activity|subtype|sub_?type|status|event_?action|verdict|disposition)$', re.I)
FIELDS = {
    'src_ip': r'^(src_?ip|source_?ip|srcaddr|src_?addr|client_?ip|client_?addr(ess)?|source_?address|clientip)$',
    'src_port': r'^(src_?port|source_?port|client_?port|sport)$',
    'dst_ip': r'^(dst_?ip|dest_?ip|destination_?ip|dstaddr|dst_?addr|server_?ip|server_?addr(ess)?|target_?ip|destination_?address)$',
    'dst_port': r'^(dst_?port|dest_?port|destination_?port|server_?port|dport|service_?port)$',
    'protocol': r'^(proto|protocol|transport|ip_?proto|l4_?proto)$',
}
TRANSPORT = ('TCP', 'UDP', 'ICMP', 'IGMP')
MAX_VALUES = 12


def read(source: str | list[str]) -> dict[str, Any]:
    """The input entry for a field, or for several read in turn (any_of): each record shape's user, say."""
    return {'any_of': list(source)} if isinstance(source, (list, tuple)) else {'field': source}


def _name(field: str) -> str:
    return re.sub(r'[^A-Za-z0-9]+', '_', field).strip('_').lower() or 'event'


def _fields(names: list[str]) -> dict[str, str]:
    out = {}
    for role, pattern in FIELDS.items():
        found = next((n for n in names if re.match(pattern, n.rsplit('.', 1)[-1], re.I)), None)
        if found:
            out[role] = found
    return out


def _values(records: list[dict[str, str]], field: str) -> list[str]:
    seen: list[str] = []
    for r in records:
        v = (r.get(field) or '').strip()
        if v and v not in seen:
            seen.append(v)
    return seen


def _splitter(records: list[dict[str, str]], names: list[str], skip: set[str]) -> str | None:
    """The field that tells this kind's actions apart: an action-like name with a handful of values, one of them
    recognised."""
    for name in names:
        if name in skip or not SPLITTER.match(name.rsplit('.', 1)[-1]):
            continue
        values = _values(records, name)
        if 1 <= len(values) <= MAX_VALUES and any(PERMIT.match(v) or DENY.match(v) or CONNECT.match(v) or CLOSE.match(v)
                                                   or AUTH.search(v) or CONFIG.search(v) or PROCESS.match(v)
                                                   or ALERT.search(v) for v in values):
            return name
    return None


def action_rules(records: list[Any], names: list[str], base: list[dict[str, Any]], prefix: str,
                 user: str | None = None, description: str | None = None,
                 skip: set[str] = frozenset(), split: str | None = None) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """(rules, values left over, the field that splits them) for records of one kind: a rule per action the values
    show, each with conditions `base` plus the splitter's values. Records that aren't dicts (XML) give none."""
    records = [r for r in records if isinstance(r, dict)]
    if not records:
        return [], [], None
    split = split or _splitter(records, names, set(skip))     # given: the kind's own field names the action
    if not split:
        return [], [], None
    values = _values(records, split)
    net = _fields(names)
    rules: list[dict[str, Any]] = []
    used: set[str] = set()

    def rule(name: str, chosen: list[str], fields: list[dict[str, Any]]) -> None:
        if chosen:
            condition = {'field': split, 'equals': chosen[0]} if len(chosen) == 1 else {'field': split, 'one_of': chosen}
            rules.append({'name': f'{prefix}_{name}' if prefix else name, 'when': [*base, condition], 'fields': fields})
            used.update(chosen)

    if 'src_ip' in net and 'dst_ip' in net:
        def addressed(value: str) -> bool:
            # Only a value whose records carry both addresses: a firewall's SYSTEM START has none.
            held = [r for r in records if r.get(split) == value]
            return bool(held) and all((r.get(net['src_ip']) or '').strip() and (r.get(net['dst_ip']) or '').strip()
                                      for r in held)
        for side, name, pattern in (('Permit', 'permitted', PERMIT), ('Deny', 'denied', DENY),
                                    ('Connect', 'connected', CONNECT), ('Close', 'closed', CLOSE)):
            chosen = [v for v in values if pattern.match(v) and v not in used and addressed(v)]
            rule(name, chosen, network_fields(side, net, records))     # the kind's protocols, seen in any of them
    logons = [v for v in values if AUTH.search(v) and not LOGOFF.search(v) and v not in used]
    outcome = {v: ('false' if FAILED.search(v) else 'true') for v in logons if FAILED.search(v) or SUCCEEDED.search(v)}
    rule('logon', logons, [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                           *([{'path': 'EventDetail/Authenticate/User/Id', **read(user)}] if user else []),
                           *([{'path': 'EventDetail/Authenticate/Outcome/Success', 'field': split, 'map': outcome}]
                             if outcome and len(outcome) == len(logons) else [])])
    logoffs = [v for v in values if LOGOFF.search(v) and v not in used]
    rule('logoff', logoffs, [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logoff'},
                             *([{'path': 'EventDetail/Authenticate/User/Id', **read(user)}] if user else [])])
    changes = [v for v in values if CONFIG.search(v) and v not in used]
    rule('config_change', changes, [{'path': 'EventDetail/Update/After/Configuration/Type', 'field': split},
                                    *([{'path': 'EventDetail/Update/After/Configuration/Description',
                                        **read(description)}] if description else [])])
    processes = [v for v in values if PROCESS.match(v) and v not in used]
    rule('service', processes, [
        {'path': 'EventDetail/Process/Action', 'field': split,
         'map': {v: 'Shutdown' if STOPPED.search(v) else 'Startup' for v in processes}},
        {'path': 'EventDetail/Process/Type', 'value': 'Service'},
        {'path': 'EventDetail/Process/Command', **read(description or split)}])
    alerts = [v for v in values if ALERT.search(v) and v not in used]
    rule('alert', alerts, [
        {'path': 'EventDetail/Alert/Type', 'field': split,
         'map': {v: 'Network' if NETWORK_ALERT.search(v) else 'Error' if ERROR_ALERT.search(v) else 'Other'
                 for v in alerts}},
        *severity_fields(records, names)])
    return rules, [v for v in values if v not in used], split


def severity_fields(records: list[dict[str, str]], names: list[str]) -> list[dict[str, Any]]:
    """Alert/Severity from the record's severity field, when every value it holds is one the schema has a word for."""
    field = next((n for n in names if SEVERITY.match(n.rsplit('.', 1)[-1])), None)
    if not field:
        return []
    seen = _values(records, field)
    mapped = {}
    for value in seen:
        word = next((w for w, pattern in SEVERITIES.items() if re.match(pattern, value, re.I)), None)
        if word is None:
            return []
        mapped[value] = word
    # The other usual words too, written as the sample writes them, so a CRITICAL the sample lacks still validates.
    case = str.upper if all(v.isupper() for v in seen) else str.lower if all(v.islower() for v in seen) else None
    if case:
        for word in ('debug', 'info', 'notice', 'warning', 'warn', 'error', 'err', 'critical', 'crit', 'alert',
                     'emergency', 'fatal'):
            mapped.setdefault(case(word), next(w for w, pattern in SEVERITIES.items() if re.match(pattern, word, re.I)))
    return [{'path': 'EventDetail/Alert/Severity', 'field': field, 'map': mapped}] if mapped else []


def network_fields(side: str, net: dict[str, str], records: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Network/<side> (Permit, Deny, Open...) with its source and destination, the protocol mapped to the values
    TransportProtocol takes."""
    at = f'EventDetail/Network/{side}'
    fields = []
    if 'src_ip' in net:
        fields.append({'path': f'{at}/Source/Device/IPAddress', 'field': net['src_ip']})
    if 'src_port' in net:
        fields.append({'path': f'{at}/Source/Port', 'field': net['src_port']})
    if 'protocol' in net:
        seen = _values(records, net['protocol'])
        fields.append({'path': f'{at}/Source/TransportProtocol', 'field': net['protocol'],
                       'map': {v: v.upper() for v in seen if v.upper() in TRANSPORT}, 'default': 'Other'})
    if 'dst_ip' in net:
        fields.append({'path': f'{at}/Destination/Device/IPAddress', 'field': net['dst_ip']})
    if 'dst_port' in net:
        fields.append({'path': f'{at}/Destination/Port', 'field': net['dst_port']})
    return fields


def described(rules: list[dict[str, Any]]) -> str:
    """What the rules say, in a line: 'Network/Permit for action ALLOW; Network/Deny for action DENY'."""
    out = []
    for r in rules:
        element = next((f['path'].split('/')[1] + ('/' + f['path'].split('/')[2]
                        if f['path'].split('/')[1] == 'Network' else '')
                        for f in r['fields'] if f['path'].startswith('EventDetail/')), '?')
        condition = r['when'][-1]
        values = condition.get('one_of') or [condition.get('equals')]
        out.append(f"{element} for {condition['field']} {', '.join(values)}")
    return '; '.join(out)
