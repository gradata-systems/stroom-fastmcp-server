"""CEF (ArcSight Common Event Format) output from Events, as flattened text: one CEF line per Event.

A plan says which Event value goes to which CEF key: the header (vendor, product, version, class id, name,
severity), the fields every event shares, and per kind of event (its action element: Authenticate, View, Network...)
the rest. It generates the XSLT (a Kafka record per Event whose value is the line, or lines of text), and the
documentation's tables. A draft is inferred from sample Events: ArcSight's standard keys first, then its custom
slots (cs1-cs6, cn1-cn3, cfp1-cfp4, deviceCustomDate1-2, flexString1-2, flexDate1, each with its label), and keys
outside the dictionary only where the user allows them (ArcSight usually indexes none of those). CEF lines another
pipeline writes are parsed and reviewed against the same dictionary.
"""
import ipaddress
import re
from datetime import datetime
from typing import Any, Literal

from lxml import etree
from pydantic import BaseModel, Field, model_validator

from utils.xsltgen import XSL, XsltStyle, _comment, style_name

EL = 'event-logging:3'

# key: (ArcSight full name, type, max length, what it holds). From the CEF implementation standard's dictionary.
KEYS: dict[str, tuple[str, str, int | None, str]] = {
    'act': ('deviceAction', 'string', 63, 'Action the event records (e.g. Logon, View, blocked)'),
    'app': ('applicationProtocol', 'string', 31, 'Application protocol, e.g. HTTP, SSH'),
    'cat': ('deviceEventCategory', 'string', 1023, "Category the device gives the event"),
    'cnt': ('baseEventCount', 'int', None, 'How many times the same event was seen'),
    'dhost': ('destinationHostName', 'string', 1023, 'Destination (target) host name'),
    'dmac': ('destinationMacAddress', 'mac', None, 'Destination MAC address'),
    'dntdom': ('destinationNtDomain', 'string', 255, "Destination (target) user's domain"),
    'dpid': ('destinationProcessId', 'int', None, 'Destination process id'),
    'dpriv': ('destinationUserPrivileges', 'string', 1023, "Destination user's privileges"),
    'dproc': ('destinationProcessName', 'string', 1023, 'Destination process name'),
    'dpt': ('destinationPort', 'int', None, 'Destination port'),
    'dst': ('destinationAddress', 'ip', None, 'Destination IPv4 address'),
    'dtz': ('deviceTimeZone', 'string', 255, "Time zone of the device that generated the event"),
    'duid': ('destinationUserId', 'string', 1023, 'Destination (target) user id'),
    'duser': ('destinationUserName', 'string', 1023, 'Destination (target) user: the account acted on'),
    'dvc': ('deviceAddress', 'ip', None, 'IPv4 address of the device that generated the event'),
    'dvchost': ('deviceHostName', 'string', 100, 'Host name of the device that generated the event'),
    'dvcmac': ('deviceMacAddress', 'mac', None, 'MAC address of the device that generated the event'),
    'dvcpid': ('deviceProcessId', 'int', None, 'Process id on the device that generated the event'),
    'end': ('endTime', 'time', None, 'When the activity ended'),
    'externalId': ('externalId', 'string', 40, "The event's id in the source system"),
    'fileHash': ('fileHash', 'string', 255, "File's hash"),
    'fileId': ('fileId', 'string', 1023, "File's id"),
    'filePath': ('filePath', 'string', 1023, "File's full path"),
    'filePermission': ('filePermission', 'string', 1023, "File's permissions"),
    'fileType': ('fileType', 'string', 1023, "File's type"),
    'fname': ('fileName', 'string', 1023, "File's name"),
    'fsize': ('fileSize', 'int', None, "File's size"),
    'in': ('bytesIn', 'int', None, 'Bytes received'),
    'msg': ('message', 'string', 1023, 'Free text about the event'),
    'oldFileName': ('oldFileName', 'string', 1023, "File's name before the change"),
    'oldFilePath': ('oldFilePath', 'string', 1023, "File's path before the change"),
    'out': ('bytesOut', 'int', None, 'Bytes sent'),
    'outcome': ('eventOutcome', 'string', 63, 'Outcome: success or failure'),
    'proto': ('transportProtocol', 'string', 31, 'Transport protocol, e.g. TCP, UDP'),
    'reason': ('reason', 'string', 1023, 'Why the outcome was what it was'),
    'request': ('requestUrl', 'string', 1023, 'URL requested'),
    'requestClientApplication': ('requestClientApplication', 'string', 1023, 'Client application (user agent)'),
    'requestMethod': ('requestMethod', 'string', 1023, 'Request method, e.g. GET'),
    'rt': ('deviceReceiptTime', 'time', None, 'When the event happened'),
    'shost': ('sourceHostName', 'string', 1023, 'Source host name'),
    'smac': ('sourceMacAddress', 'mac', None, 'Source MAC address'),
    'sntdom': ('sourceNtDomain', 'string', 255, "Source user's domain"),
    'sourceServiceName': ('sourceServiceName', 'string', 1023, 'Service behind the source'),
    'destinationServiceName': ('destinationServiceName', 'string', 1023, 'Service behind the destination'),
    'spid': ('sourceProcessId', 'int', None, 'Source process id'),
    'spriv': ('sourceUserPrivileges', 'string', 1023, "Source user's privileges"),
    'sproc': ('sourceProcessName', 'string', 1023, 'Source process name'),
    'spt': ('sourcePort', 'int', None, 'Source port'),
    'src': ('sourceAddress', 'ip', None, 'Source IPv4 address'),
    'start': ('startTime', 'time', None, 'When the activity started'),
    'suid': ('sourceUserId', 'string', 1023, 'Source user id'),
    'suser': ('sourceUserName', 'string', 1023, 'Source user: the account that acted'),
    'deviceExternalId': ('deviceExternalId', 'string', 255, "The device's own id"),
    'deviceFacility': ('deviceFacility', 'string', 1023, 'Facility that generated the event'),
    'deviceInboundInterface': ('deviceInboundInterface', 'string', 128, 'Interface the traffic came in on'),
    'deviceOutboundInterface': ('deviceOutboundInterface', 'string', 128, 'Interface the traffic went out on'),
    'deviceProcessName': ('deviceProcessName', 'string', 1023, 'Process on the device that generated the event'),
    'deviceDirection': ('deviceDirection', 'int', None, 'Direction: 0 inbound, 1 outbound'),
}
# ArcSight's custom slots: built-in keys whose meaning a label gives, per event.
SLOTS: dict[str, list[str]] = {
    'string': ['cs1', 'cs2', 'cs3', 'cs4', 'cs5', 'cs6', 'flexString1', 'flexString2'],
    'long': ['cn1', 'cn2', 'cn3'],
    'float': ['cfp1', 'cfp2', 'cfp3', 'cfp4'],
    'time': ['deviceCustomDate1', 'deviceCustomDate2', 'flexDate1'],
}
for _n in range(1, 7):
    KEYS[f'cs{_n}'] = (f'deviceCustomString{_n}', 'string', 4000, 'Custom string, named by its label')
for _n in range(1, 4):
    KEYS[f'cn{_n}'] = (f'deviceCustomNumber{_n}', 'long', None, 'Custom number, named by its label')
for _n in range(1, 5):
    KEYS[f'cfp{_n}'] = (f'deviceCustomFloatingPoint{_n}', 'float', None, 'Custom decimal, named by its label')
for _k in ('deviceCustomDate1', 'deviceCustomDate2', 'flexDate1'):
    KEYS[_k] = (_k, 'time', None, 'Custom time, named by its label')
for _k in ('flexString1', 'flexString2'):
    KEYS[_k] = (_k, 'string', 1023, 'Custom string, named by its label')
SLOT_KEYS = {k for keys in SLOTS.values() for k in keys}
for _k in sorted(SLOT_KEYS):
    KEYS[f'{_k}Label'] = (f'{KEYS[_k][0]}Label', 'string', 1023, f'The name of {_k}')
FULL_NAMES = {full: key for key, (full, *_rest) in KEYS.items()}

HEADER = [('vendor', 'Device Vendor', 63), ('product', 'Device Product', 63), ('version', 'Device Version', 31),
          ('signature', 'Device Event Class ID', 1023), ('name', 'Name', 512), ('severity', 'Severity', 10)]
# Elements of EventDetail that are not its action.
NOT_ACTION = {'TypeId', 'Description', 'Classification', 'Purpose', 'Data'}

# Where Event values go by default, first free key wins; '*' is any one step (the action element, or Network's).
HOMES: list[tuple[str, str, str]] = [
    ('EventTime/TimeCreated', 'rt', 'epoch_ms'),
    ('EventSource/Device/HostName', 'dvchost', 'text'), ('EventSource/Device/IPAddress', 'dvc', 'text'),
    ('EventSource/Device/MACAddress', 'dvcmac', 'text'),
    ('EventSource/Client/HostName', 'shost', 'text'), ('EventSource/Client/IPAddress', 'src', 'text'),
    ('EventSource/Client/MACAddress', 'smac', 'text'), ('EventSource/Client/Port', 'spt', 'text'),
    ('EventSource/Server/HostName', 'dhost', 'text'), ('EventSource/Server/IPAddress', 'dst', 'text'),
    ('EventSource/Server/MACAddress', 'dmac', 'text'), ('EventSource/Server/Port', 'dpt', 'text'),
    ('EventSource/User/Id', 'suser', 'text'), ('EventSource/User/Domain', 'sntdom', 'text'),
    ('EventDetail/*/Action', 'act', 'text'),
    ('EventDetail/*/Outcome/Success', 'outcome', 'outcome'),
    ('EventDetail/*/Outcome/Reason', 'reason', 'text'), ('EventDetail/*/Outcome/Description', 'reason', 'text'),
    ('EventDetail/*/User/Id', 'duser', 'text'), ('EventDetail/*/User/Domain', 'dntdom', 'text'),
    ('EventDetail/*/Device/HostName', 'dhost', 'text'), ('EventDetail/*/Device/IPAddress', 'dst', 'text'),
    ('EventDetail/*/Device/MACAddress', 'dmac', 'text'),
    ('EventDetail/*/File/Path', 'filePath', 'text'), ('EventDetail/*/File/Name', 'fname', 'text'),
    ('EventDetail/*/File/Size', 'fsize', 'text'), ('EventDetail/*/File/Hash', 'fileHash', 'text'),
    ('EventDetail/*/File/Type', 'fileType', 'text'), ('EventDetail/*/Folder/Path', 'filePath', 'text'),
    ('EventDetail/*/Source/File/Path', 'oldFilePath', 'text'), ('EventDetail/*/Source/File/Name', 'oldFileName', 'text'),
    ('EventDetail/*/Destination/File/Path', 'filePath', 'text'), ('EventDetail/*/Destination/File/Name', 'fname', 'text'),
    ('EventDetail/*/Resource/URL', 'request', 'text'),
    ('EventDetail/Network/*/Source/Device/IPAddress', 'src', 'text'),
    ('EventDetail/Network/*/Source/Device/HostName', 'shost', 'text'),
    ('EventDetail/Network/*/Source/Device/MACAddress', 'smac', 'text'),
    ('EventDetail/Network/*/Source/Port', 'spt', 'text'),
    ('EventDetail/Network/*/Destination/Device/IPAddress', 'dst', 'text'),
    ('EventDetail/Network/*/Destination/Device/HostName', 'dhost', 'text'),
    ('EventDetail/Network/*/Destination/Device/MACAddress', 'dmac', 'text'),
    ('EventDetail/Network/*/Destination/Port', 'dpt', 'text'),
    ('EventDetail/Network/*/TransportProtocol', 'proto', 'text'),
    ('EventDetail/Network/*/ApplicationProtocol', 'app', 'text'),
    ('EventDetail/Network/*/Outcome/Success', 'outcome', 'outcome'),
    ('EventDetail/Network/*/ProcessName', 'sproc', 'text'),
    ('EventDetail/Process/ProcessId', 'dpid', 'text'),
    ('EventDetail/Alert/Type', 'cat', 'text'), ('EventDetail/Alert/Subject', 'msg', 'text'),
]
# Event values the header carries by default (so not repeated in the extension).
HEADER_PATHS = {'vendor': 'EventSource/System/Organisation', 'product': 'EventSource/System/Name',
                'version': 'EventSource/System/Version', 'signature': 'EventDetail/TypeId',
                'name': 'EventDetail/Description'}
ACTION_NAME = ("string-join((local-name(EventDetail/*[not(self::TypeId or self::Description or self::Classification "
               "or self::Purpose or self::Data)][1]), EventDetail/*/Action), ' ')")


def key_of(name: str) -> str:
    """A CEF key from its short or full name (deviceCustomString6 -> cs6)."""
    return name if name in KEYS else FULL_NAMES.get(name, name)


def described(key: str, label: str | None = None) -> str:
    """'cs6 (deviceCustomString6): Custom string, named by its label, labelled 'Target user''."""
    full, _, _, meaning = KEYS.get(key, (key, '', None, 'Key outside the CEF dictionary'))
    said = f"{key} ({full})" if full != key else key
    return f"{said}: {meaning}" + (f", labelled '{label}' ({key}Label)" if label else '')


class CefValue(BaseModel):
    """A header field: a constant, or an XPath on the Event (with an optional map of its values)."""
    value: str | None = None
    source: str | None = Field(None, description="XPath from the Event, e.g. 'EventSource/System/Name'.")
    map: dict[str, str] = Field(default_factory=dict, description="Source value -> header value, e.g. severities.")
    default: str | None = Field(None, description="When the source is empty, or matches no map entry.")

    @model_validator(mode='after')
    def _one(self) -> 'CefValue':
        if (self.value is None) == (self.source is None):
            raise ValueError("A header field takes value (a constant) or source (an XPath on the Event)")
        return self


class CefField(BaseModel):
    path: str = Field(description="XPath from the Event, e.g. 'EventDetail/Authorise/User/Name'; '*' for any one step.")
    key: str = Field(description="CEF key (short or full name: cs6 or deviceCustomString6).")
    label: str | None = Field(None, description="For a custom slot (cs1-6, cn1-3, cfp1-4, deviceCustomDate1-2, "
                                                "flexString1-2, flexDate1): what it holds, sent as its Label key.")
    transform: Literal['text', 'epoch_ms', 'outcome'] = Field('text', description=(
        "epoch_ms: a time as milliseconds since 1970 (rt, end, start and custom dates); outcome: Outcome/Success as "
        "success or failure (no Outcome counts as success, as in event-logging)."))

    @model_validator(mode='after')
    def _key(self) -> 'CefField':
        self.key = key_of(self.key)
        if KEYS.get(self.key, ('', ''))[1] == 'time' and self.transform == 'text':
            self.transform = 'epoch_ms'
        return self


class CefPlan(BaseModel):
    output: Literal['kafka', 'text'] = Field('kafka', description=(
        "kafka: one kafka-records:1 record per Event, its value the CEF line (a StandardKafkaProducer sends it); text: "
        "one CEF line per Event (a TextWriter writes them)."))
    topic: str | None = Field(None, description="kafka: the topic ArcSight reads.")
    kafka_key: str | None = Field(None, description="kafka: an XPath on the Event for each record's key, if any.")
    custom_keys: bool = Field(False, description="Whether keys outside ArcSight's CEF dictionary may be sent.")
    vendor: CefValue
    product: CefValue
    version: CefValue
    signature: CefValue
    name: CefValue
    severity: CefValue = Field(default_factory=lambda: CefValue(value='3'))
    common: list[CefField] = Field(default_factory=list, description="Fields every event gets.")
    events: dict[str, list[CefField]] = Field(default_factory=dict, description=(
        "Per kind of event, by its action element (Authenticate, View, Network, ...): the fields it adds."))
    not_sent: list[dict[str, str]] = Field(default_factory=list, description=(
        "Event values the sample holds that no key takes, with why: documented as not sent."))
    style: XsltStyle = Field(default_factory=XsltStyle, description=(
        "How the CEF XSLT is written, as an Events translation's is (naming, variables, layout): from an XSLT style "
        "section in the standing instructions (AGENTS docs); otherwise the defaults."))

    def problems(self) -> list[str]:
        out = []
        if self.output == 'kafka' and not self.topic:
            out.append("output kafka needs topic: the Kafka topic ArcSight reads (ask the user, or the standing "
                       "instructions may name it)")
        for name, said, _ in HEADER:
            value = getattr(self, name)
            if value.value is not None and value.value.startswith('TODO'):
                out.append(f"header {said}: {value.value}")
        severity = self.severity
        if severity.value is not None and severity.value not in SEVERITIES:
            out.append(f"severity '{severity.value}' is not a CEF severity (0 to 10, or Low, Medium, High, Very-High)")
        for v in list(severity.map.values()) + ([severity.default] if severity.default else []):
            if v not in SEVERITIES:
                out.append(f"severity map value '{v}' is not a CEF severity (0 to 10, or Low, Medium, High, Very-High)")
        for kind, fields in [('common', self.common)] + [(k, v) for k, v in self.events.items()]:
            seen: dict[str, str] = {}
            for f in fields:     # a kind may give a common key: its own value is sent
                if f.key in seen and seen[f.key] != f.path:
                    out.append(f"[{kind}] {f.key} is given twice ({seen[f.key]} and {f.path}): one key, one value")
                seen.setdefault(f.key, f.path)
            for f in fields:
                if f.key not in KEYS:
                    if not self.custom_keys:
                        out.append(f"[{kind}] {f.path}: '{f.key}' is not a key in ArcSight's CEF dictionary, and the "
                                   f"user has not allowed keys outside it: use a standard key or a custom slot "
                                   f"(cs1-cs6, cn1-cn3, ...) with a label, or leave it out (not sent)")
                    elif not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', f.key):
                        out.append(f"[{kind}] '{f.key}': a CEF key is letters and digits")
                elif f.key.endswith('Label'):
                    out.append(f"[{kind}] {f.key}: a label is given as the slot's label, not as a field of its own")
                elif f.key in SLOT_KEYS and not f.label:
                    out.append(f"[{kind}] {f.path} -> {f.key}: a custom slot needs a label saying what it holds")
        return out

    def xslt(self) -> str:
        return render_xslt(self)

    def markdown(self, examples: dict[tuple[str, str], str] | None = None) -> str:
        return plan_markdown(self, examples or {})


SEVERITIES = {str(n) for n in range(11)} | {'Low', 'Medium', 'High', 'Very-High', 'Unknown'}


# ---------------------------------------------------------------------------------------------- sample Events

def _local(el: etree._Element) -> str:
    return etree.QName(el).localname


def event_kind(event: etree._Element) -> str | None:
    detail = next((c for c in event if isinstance(c.tag, str) and _local(c) == 'EventDetail'), None)
    if detail is None:
        return None
    return next((_local(c) for c in detail if isinstance(c.tag, str) and _local(c) not in NOT_ACTION), None)


def event_values(event: etree._Element) -> dict[str, str]:
    """{path from the Event: value} for each value an Event holds (the first, where an element repeats); Data as
    Data[@Name='x']/@Value; Network's action as '*'."""
    out: dict[str, str] = {}

    def walk(el: etree._Element, path: str) -> None:
        children = [c for c in el if isinstance(c.tag, str)]
        name = _local(el)
        if name == 'Data' and el.get('Name'):
            here = f"{path}/Data[@Name='{el.get('Name')}']"
            if el.get('Value') is not None:
                out.setdefault(f"{here}/@Value", el.get('Value'))
            for c in children:
                walk(c, here)
            return
        here = f"{path}/{name}" if path else name
        if not children and (el.text or '').strip():
            out.setdefault(here, el.text.strip())
        for attr, value in el.attrib.items():
            if '}' not in attr:
                out.setdefault(f"{here}/@{attr}", value)
        for c in children:
            walk(c, here)
    for c in event:
        if isinstance(c.tag, str):
            walk(c, '')
    return {re.sub(r'^EventDetail/Network/[^/]+/', 'EventDetail/Network/*/', p): v for p, v in out.items()}


def events_of(xml: str | bytes) -> list[etree._Element]:
    root = etree.fromstring(xml.encode() if isinstance(xml, str) else xml, etree.XMLParser(huge_tree=True))
    return [e for e in root.iter(f'{{{EL}}}Event')]


def _matches(pattern: str, path: str) -> bool:
    want, have = pattern.split('/'), path.split('/')
    return len(want) == len(have) and all(w == '*' or w == h for w, h in zip(want, have))


def _fits(key: str, values: list[str]) -> bool:
    kind = KEYS[key][1]
    try:
        if kind == 'ip':
            return all(isinstance(ipaddress.ip_address(v), ipaddress.IPv4Address) for v in values)
        if kind in ('int', 'long'):
            return all(re.fullmatch(r'-?\d+', v) for v in values)
        if kind == 'float':
            return all(re.fullmatch(r'-?\d+(\.\d+)?', v) for v in values)
        if kind == 'mac':
            return all(re.fullmatch(r'([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}', v) for v in values)
        if kind == 'time':
            return all(_is_time(v) for v in values)
    except ValueError:
        return False
    return True


def _is_time(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return 'T' in value
    except ValueError:
        return False


def _slot_kind(values: list[str]) -> str:
    if values and all(re.fullmatch(r'-?\d{1,18}', v) for v in values):
        return 'long'
    if values and all(re.fullmatch(r'-?\d+\.\d+', v) for v in values):
        return 'float'
    if values and all(_is_time(v) for v in values):
        return 'time'
    return 'string'


def label_for(path: str) -> str:
    data = re.search(r"Data\[@Name='([^']+)'\]", path)
    if data:
        return data.group(1)
    steps = [s for s in path.split('/') if s not in ('EventDetail', 'EventSource', '*') and not s.startswith('@')]
    return ' '.join(steps[-3:])


def _camel(path: str) -> str:
    words = re.findall(r'[A-Za-z0-9]+', label_for(path))
    return (words[0][:1].lower() + words[0][1:] + ''.join(w[:1].upper() + w[1:] for w in words[1:])) if words else 'value'


class Override(BaseModel):
    """A mapping the user (or a standing instruction) gives: where an Event value goes, or that it is not sent."""
    path: str
    key: str | None = None
    label: str | None = None
    event_type: str | None = Field(None, description="Only for this kind of event (its action element); else wherever "
                                                     "the path occurs.")
    drop: bool = False


def draft(events: list[etree._Element], custom_keys: bool, overrides: list[Override] = (),
          header: dict[str, CefValue] | None = None, output: str = 'kafka', topic: str | None = None,
          kafka_key: str | None = None) -> tuple[CefPlan, list[str]]:
    """A plan inferred from sample Events, and notes on what was decided and why."""
    notes: list[str] = []
    by_kind: dict[str, list[dict[str, str]]] = {}
    for event in events:
        by_kind.setdefault(event_kind(event) or 'other', []).append(event_values(event))
    every = [v for vs in by_kind.values() for v in vs]
    seen_paths = {p for v in every for p in v}

    def values_of(pool: list[dict[str, str]], path: str) -> list[str]:
        return [v[path] for v in pool if path in v][:200]

    # The header: constants where the sample has no value for it.
    head: dict[str, CefValue] = {}
    for name, said, _ in HEADER:
        if name == 'severity':
            continue
        path = HEADER_PATHS[name]
        share = sum(path in v for v in every) / max(len(every), 1)
        if share > 0 and name in ('name', 'signature'):
            # Most events have it; the rest are named by their action rather than left empty.
            head[name] = CefValue(source=f"({path}, {ACTION_NAME})[normalize-space(.) != ''][1]")
        elif share >= 0.5:
            head[name] = CefValue(source=path)
        elif name == 'name':
            head[name] = CefValue(source=ACTION_NAME)
            notes.append("Name: the action element and its Action (no EventDetail/Description in most events)")
        elif name == 'signature':
            head[name] = CefValue(source=f"(EventDetail/TypeId, {ACTION_NAME})[normalize-space(.) != ''][1]")
        elif name == 'version':
            head[name] = CefValue(value='')
        else:
            head[name] = CefValue(value=f"TODO {said}: the sample's Events have no {path}; ask the user")
    head.update(header or {})
    # Values the header carries are not sent again in the extension.
    taken_by_header = {v.source for v in head.values() if v.source} | {
        path for name, path in HEADER_PATHS.items() if head[name].source and path in head[name].source}

    # Overrides: per kind first, then everywhere.
    def override_for(path: str, kind: str | None) -> Override | None:
        return (next((o for o in overrides if o.event_type == kind and _matches(o.path, path)), None)
                or next((o for o in overrides if o.event_type is None and _matches(o.path, path)), None))

    not_sent: list[dict[str, str]] = []
    common: list[CefField] = []
    used_common: set[str] = set()
    common_paths = sorted({p for p in seen_paths if not p.startswith('EventDetail/')
                           or p in ('EventDetail/TypeId', 'EventDetail/Description')},
                          key=lambda p: (-sum(p in v for v in every), p))
    free = {kind: list(keys) for kind, keys in SLOTS.items()}

    def place(path: str, pool: list[dict[str, str]], used: set[str], slots: dict[str, list[str]], kind: str | None,
              out: list[CefField], where: str) -> None:
        if path in taken_by_header:
            return
        given = override_for(path, kind)
        if given and given.drop:
            not_sent.append({'path': path, 'event_type': where, 'why': 'left out as instructed'})
            return
        if given and given.key:
            key = key_of(given.key)
            if key in used:
                notes.append(f"[{where}] {path} -> {key} as instructed, but {key} was already taken: check the overrides")
            out.append(CefField(path=path, key=key, label=given.label or (label_for(path) if key in SLOT_KEYS else None)))
            used.add(key)
            for slot_list in slots.values():
                if key in slot_list:
                    slot_list.remove(key)
            return
        values = values_of(pool, path)
        home = next(((key, transform) for pattern, key, transform in HOMES
                     if _matches(pattern, path) and key not in used), None)
        if home and (home[1] != 'text' or _fits(home[0], values)):
            out.append(CefField(path=path, key=home[0], transform=home[1]))
            used.add(home[0])
            return
        if home:
            notes.append(f"[{where}] {path}: values like {values[:2]} don't fit {home[0]} ({KEYS[home[0]][1]}), so it "
                         f"goes to a custom slot")
        kind_of = _slot_kind(values)
        slot = next((s for s in slots.get(kind_of, []) if s not in used), None) or \
            next((s for s in slots['string'] if s not in used), None)
        if slot:
            out.append(CefField(path=path, key=slot, label=label_for(path),
                                transform='epoch_ms' if KEYS[slot][1] == 'time' else 'text'))
            used.add(slot)
            for slot_list in slots.values():
                if slot in slot_list:
                    slot_list.remove(slot)
            return
        if custom_keys:
            key = _camel(path)
            while key in KEYS or key in used:
                key += 'X'
            out.append(CefField(path=path, key=key))
            used.add(key)
            notes.append(f"[{where}] {path} -> {key}: a key outside the CEF dictionary (allowed by the user)")
            return
        not_sent.append({'path': path, 'event_type': where,
                         'why': "no standard CEF key fits it and the custom slots are all used; keys outside the "
                                "dictionary are not allowed"})

    for path in common_paths:
        place(path, every, used_common, free, None, common, 'every event')
    events_out: dict[str, list[CefField]] = {}
    for kind, pool in sorted(by_kind.items()):
        # A common key is taken for this kind only where its events hold the common value (Network events have a
        # Source address, not a Client one: src is theirs). Should an event hold both, its kind's value is sent.
        held = {p for v in pool for p in v}
        used = {f.key for f in common if any(_matches(f.path, p) for p in held)}
        slots = {k: list(v) for k, v in free.items()}
        fields: list[CefField] = []
        paths = sorted({p for v in pool for p in v if p.startswith('EventDetail/') and p not in common_paths},
                       key=lambda p: (-sum(p in v for v in pool), p))
        for path in paths:
            place(path, pool, used, slots, kind, fields, kind)
        if kind != 'other' and 'act' not in used and not (override_for(f'EventDetail/{kind}', kind) or Override(path='')).drop:
            # No Action element (View, Create, Delete...): the action is the event's kind.
            fields.insert(0, CefField(path="local-name(EventDetail/Network/*[1])" if kind == 'Network' else f"'{kind}'",
                                      key='act'))
        if fields:
            events_out[kind] = fields
    if any(o.event_type is None and o.key and not any(_matches(o.path, p) for p in seen_paths) for o in overrides):
        missing = [o.path for o in overrides if o.key and not any(_matches(o.path, p) for p in seen_paths)]
        notes.append(f"overrides for paths no sample Event holds, not used: {missing}")
    plan = CefPlan(output=output, topic=topic, kafka_key=kafka_key, custom_keys=custom_keys, **head,
                   common=common, events=events_out, not_sent=not_sent)
    return plan, notes


# ---------------------------------------------------------------------------------------------- the XSLT

FUNCTIONS = r'''  <!-- A header field, escaped: backslash and pipe, on one line. -->
  <xsl:function name="cef:h" as="xs:string">
    <xsl:param name="v" as="item()*" />
    <xsl:param name="max" as="xs:integer" />
    <xsl:sequence select="replace(replace(substring(normalize-space(string(($v)[1])), 1, $max), '\\', '\\\\'), '\|', '\\|')" />
  </xsl:function>
  <!-- key=value, the value escaped (backslash, equals sign, line breaks) and cut to the key's length; nothing when empty. -->
  <xsl:function name="cef:kv" as="xs:string*">
    <xsl:param name="key" as="xs:string" />
    <xsl:param name="v" as="item()*" />
    <xsl:param name="max" as="xs:integer" />
    <xsl:variable name="s" select="replace(string(($v)[1]), '^\s+|\s+$', '')" />
    <xsl:if test="$s != ''">
      <xsl:sequence select="concat($key, '=', replace(replace(replace(substring($s, 1, $max), '\\', '\\\\'), '=', '\\='), '\r\n|\n|\r', '\\n'))" />
    </xsl:if>
  </xsl:function>
  <!-- A custom slot: key=value and its label, only when there is a value. -->
  <xsl:function name="cef:kv" as="xs:string*">
    <xsl:param name="key" as="xs:string" />
    <xsl:param name="v" as="item()*" />
    <xsl:param name="max" as="xs:integer" />
    <xsl:param name="label" as="xs:string" />
    <xsl:variable name="pair" select="cef:kv($key, $v, $max)" />
    <xsl:if test="exists($pair)">
      <xsl:sequence select="($pair, cef:kv(concat($key, 'Label'), $label, 1023))" />
    </xsl:if>
  </xsl:function>
  <!-- A time as milliseconds since 1970 (UTC). -->
  <xsl:function name="cef:ms" as="xs:string?">
    <xsl:param name="t" as="item()*" />
    <xsl:sequence select="if (string(($t)[1]) castable as xs:dateTime)
                          then string(xs:integer((xs:dateTime(string(($t)[1])) - xs:dateTime('1970-01-01T00:00:00Z')) div xs:dayTimeDuration('PT0.001S')))
                          else ()" />
  </xsl:function>
  <!-- Outcome/Success as CEF's outcome: no Outcome means success, as in event-logging. -->
  <xsl:function name="cef:outcome" as="xs:string">
    <xsl:param name="s" as="item()*" />
    <xsl:sequence select="if (string(($s)[1]) = 'false') then 'failure' else 'success'" />
  </xsl:function>
'''


def _attr(text: str) -> str:
    return (text.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;'))


def _lit(text: str) -> str:
    """An XPath string literal, inside a double-quoted attribute."""
    return "'" + _attr(text).replace("'", "''") + "'"


def _header_expr(value: CefValue) -> str:
    if value.value is not None:
        return _lit(value.value)
    source = _attr(value.source or '')
    if not value.map:
        return f"({source}, {_lit(value.default)})[normalize-space(string(.)) != ''][1]" if value.default else source
    chain = ' else '.join(f"if (string(({source})[1]) = {_lit(k)}) then {_lit(v)}" for k, v in value.map.items())
    return f"{chain} else {_lit(value.default or '')}"


def _field_line(f: CefField, indent: str) -> str:
    max_len = KEYS.get(f.key, ('', '', 1023, ''))[2] or 1023
    expr = _attr(f.path)
    if f.transform == 'epoch_ms':
        expr = f"cef:ms({expr})"
    elif f.transform == 'outcome':
        expr = f"cef:outcome({expr})"
    label = f", {_lit(f.label)}" if f.key in SLOT_KEYS and f.label else ''
    return f'{indent}<xsl:sequence select="cef:kv({_lit(f.key)}, {expr}, {max_len}{label})" />'


def _keys_said(fields: list[CefField]) -> str:
    return ', '.join(f"{f.key} from {f.path}" for f in fields) or 'none'


def render_xslt(plan: CefPlan) -> str:
    """The CEF XSLT, written as an Events translation is (asked for by the user): a template a part (the line, each
    kind of event's keys, the keys every event gets), in its own mode, or named, or inline (plan.style.layout), named
    in the style's naming, each with a comment; an input read often, a variable."""
    from utils.xsltstyle import parse, part_name, serialize, variables
    style, taken = plan.style, set()
    header = ', '.join(f"cef:h({_header_expr(getattr(plan, name))}, {length})" for name, _, length in HEADER)
    line_mode, ext_mode, common_mode = (part_name(n, style, taken) for n in ('cef-line', 'cef-extension', 'cef-common'))

    def apply(name: str) -> str:
        return (f'<xsl:call-template name="{name}" />' if style.layout == 'named' else
                f'<xsl:apply-templates select="." mode="{name}" />')

    def own(name: str, match: str = 'Event', returns: str = 'xs:string*') -> str:
        return (f'<xsl:template name="{name}" as="{returns}">' if style.layout == 'named' else
                f'<xsl:template match="{match}" mode="{name}" as="{returns}">')
    parts = []
    if style.layout == 'inline':
        # Every kind of event in one choose, as the inline layout writes the Events translation's rules.
        whens = ''.join(f'<xsl:when test="EventDetail/{kind}">' + ''.join(_field_line(f, '') for f in fields)
                        + '</xsl:when>' for kind, fields in plan.events.items() if kind != 'other')
        otherwise = ''.join(_field_line(f, '') for f in plan.events.get('other', []))
        pairs = (f'<xsl:choose>{whens}<xsl:otherwise>{otherwise}</xsl:otherwise></xsl:choose>' if whens else otherwise) \
            + ''.join(_field_line(f, '') for f in plan.common)
    else:
        # Each kind of event's keys: a template rule matching it (a named template can't choose by kind).
        for kind, fields in plan.events.items():
            match = f"Event[EventDetail/{kind}]" if kind != 'other' else "Event"
            parts.append(f'<!--{_comment(f"{kind} events: {_keys_said(fields)}")}-->'
                         f'<xsl:template match="{match}" mode="{ext_mode}" as="xs:string*">'
                         + ''.join(_field_line(f, '') for f in fields) + '</xsl:template>')
        if 'other' not in plan.events:
            parts.append(f'<xsl:template match="Event" mode="{ext_mode}" as="xs:string*" />')
        if plan.common:
            parts.append(f'<!--{_comment(f"{common_mode}: every event: {_keys_said(plan.common)}")}-->'
                         + own(common_mode) + ''.join(_field_line(f, '') for f in plan.common) + '</xsl:template>')
        pairs = f'<xsl:apply-templates select="." mode="{ext_mode}" />' + (apply(common_mode) if plan.common else '')
    line = f'''  <!-- {line_mode}: header|...|extension, the event kind's own fields then the common ones. -->
  {own(line_mode, returns='xs:string')}
    <xsl:variable name="header" as="xs:string*" select="('CEF:0', {header})" />
    <xsl:variable name="pairs" as="xs:string*">{pairs}</xsl:variable>
    <!-- One value a key: the event kind's own, where a common field gives the same key. -->
    <xsl:variable name="extension" select="for $i in 1 to count($pairs) return $pairs[$i][not(substring-before(., '=') = (for $j in 1 to $i - 1 return substring-before($pairs[$j], '=')))]" />
    <xsl:sequence select="concat(string-join($header, '|'), '|', string-join($extension, ' '))" />
  </xsl:template>
'''
    if plan.output == 'kafka':
        key = (f'\n      <key><xsl:value-of select="{_attr(plan.kafka_key)}" /></key>' if plan.kafka_key else '')
        top = f'''<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="event-logging:3" xmlns="kafka-records:1" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:cef="urn:stroom-mcp:cef" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    exclude-result-prefixes="xs cef" version="3.0">
  <!-- CEF for ArcSight: one Kafka record per Event (kafka-records:1 takes one a document: split one Event a record),
       its value the flattened CEF line. -->
  <xsl:template match="/Events">
    <kafkaRecords xsi:schemaLocation="kafka-records:1 file://kafka-records-v1.1.xsd">
      <xsl:apply-templates select="Event" />
    </kafkaRecords>
  </xsl:template>
  <xsl:template match="Event">
    <kafkaRecord topic="{_attr((plan.topic or '').replace('{', '{{').replace('}', '}}'))}">
      <xsl:if test="string(EventTime/TimeCreated) castable as xs:dateTime">
        <xsl:attribute name="timestamp" select="format-dateTime(adjust-dateTime-to-timezone(xs:dateTime(string(EventTime/TimeCreated)), xs:dayTimeDuration('PT0H')), '[Y0001]-[M01]-[D01]T[H01]:[m01]:[s01].[f001]Z')" />
      </xsl:if>{key}
      <xsl:variable name="line" as="xs:string">{apply(line_mode)}</xsl:variable>
      <value><xsl:value-of select="$line" /></value>
    </kafkaRecord>
  </xsl:template>
'''
    else:
        top = f'''<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="event-logging:3" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:cef="urn:stroom-mcp:cef" exclude-result-prefixes="xs cef" version="3.0">
  <!-- CEF for ArcSight: one flattened CEF line per Event, as text. -->
  <xsl:output method="text" />
  <xsl:template match="/Events">
    <xsl:for-each select="Event">
      {apply(line_mode)}
      <xsl:text>&#10;</xsl:text>
    </xsl:for-each>
  </xsl:template>
'''
    sheet = parse(top + line + ''.join(parts) + FUNCTIONS + '</xsl:stylesheet>')
    # An input a template reads often, read once into a variable: a header field's source its map tests in turn.
    every = plan.common + [f for fields in plan.events.values() for f in fields]
    reads = {**{value.source: name for name, _, _ in HEADER if (value := getattr(plan, name)).source},
             **{f.path: KEYS.get(f.key, (f.key,))[0] for f in every}, 'EventTime/TimeCreated': 'event-time'}
    for template in sheet.iterfind(f'{{{XSL}}}template'):
        variables(template, reads, style, set())
    return serialize(sheet)


# ---------------------------------------------------------------------------------------------- reading CEF lines

def lines_in(output: str) -> list[str]:
    """CEF lines in a pipeline element's output: Kafka records' values, or lines of text."""
    text = output or ''
    if '<kafkaRecord' in text:
        try:
            root = etree.fromstring(text.encode(), etree.XMLParser(huge_tree=True, recover=True))
            values = [''.join(v.itertext()) for v in root.iter('{kafka-records:1}value', '{kafka-records:2}value')
                      if v.getparent() is not None and etree.QName(v.getparent()).localname == 'kafkaRecord']
            return [v for v in values if 'CEF:' in v]
        except etree.XMLSyntaxError:
            pass
    # Stroom's stepping puts an XML declaration before a text XSLT's output (seen: '<?xml version="1.1"
    # encoding="UTF-8"?>CEF:0|...'): not part of the line. A syslog prefix is, and stays.
    text = re.sub(r'<\?xml[^>]*\?>', '', text)
    return [line.strip() for line in text.splitlines() if line.strip().startswith('CEF:')
            or re.search(r'CEF:\d+\|', line)]


def _split_header(line: str) -> tuple[list[str], str]:
    start = line.find('CEF:')
    fields, current, i = [], '', start
    while i < len(line) and len(fields) < 7:
        ch = line[i]
        if ch == '\\' and i + 1 < len(line):
            current += line[i + 1]
            i += 2
            continue
        if ch == '|':
            fields.append(current)
            current = ''
        else:
            current += ch
        i += 1
    return fields, line[i:] if len(fields) == 7 else ''


_PAIR = re.compile(r'([A-Za-z0-9_.]+)=((?:[^\\]|\\.)*?)(?=\s+[A-Za-z0-9_.]+=|\s*$)', re.S)


def parse(line: str) -> dict[str, Any]:
    """{'header': [CEF:n, vendor, product, version, class id, name, severity], 'extension': [(key, value)...]}."""
    header, rest = _split_header(line)
    pairs = []
    for m in _PAIR.finditer(rest):
        value = re.sub(r'\\(.)', lambda x: {'n': '\n', 'r': '\r'}.get(x.group(1), x.group(1)), m.group(2))
        pairs.append((m.group(1), value))
    return {'header': header, 'extension': pairs}


def review(lines: list[str], events: list[etree._Element], custom_keys: bool | None) -> dict[str, Any]:
    """What is wrong with these CEF lines, how much of each Event they carry, and the mapping they imply.

    events, when given, are the Events the lines were made from, in the same order."""
    problems: dict[str, int] = {}
    examples: dict[str, str] = {}

    def flag(what: str, example: str) -> None:
        problems[what] = problems.get(what, 0) + 1
        examples.setdefault(what, example[:300])
    implied: dict[tuple[str, str], dict[str, int]] = {}
    labels: dict[tuple[str, str], str] = {}
    unsent: dict[tuple[str, str], int] = {}
    kinds: dict[str, int] = {}
    for n, line in enumerate(lines):
        if '\n' in line.strip():
            flag("a raw line break inside a CEF line (escape it as \\n)", line)
        parsed = parse(line)
        header, pairs = parsed['header'], parsed['extension']
        if len(header) != 7:
            flag(f"a header of {len(header)} fields, where CEF has 7 (CEF:n|vendor|product|version|class id|name|severity), "
                 f"or an unescaped | in one", line)
        elif header[6] not in SEVERITIES:
            rest = _split_header(line)[1]
            shifted = re.match(r'(\d{1,2}|Low|Medium|High|Very-High)\|', rest)
            if shifted and shifted.group(1) in SEVERITIES:
                # Seen in a pipeline written by hand: 'Secret viewed: a=b|c' as the name, its | not escaped, so 'c'
                # was read as the severity and the real one began the extension.
                flag(f"an unescaped | in the header's name or class id (in '{header[5]}|{header[6]}'): escape it as \\|",
                     line)
            else:
                flag(f"severity '{header[6]}' is not 0 to 10 (or Low, Medium, High, Very-High)", line)
        keys = [k for k, _ in pairs]
        for k in sorted({k for k in keys if keys.count(k) > 1}):
            flag(f"key {k} given twice in a line", line)
        present = dict(pairs)
        for k, v in pairs:
            base = k[:-5] if k.endswith('Label') else None
            if k not in KEYS:
                flag(f"key '{k}' is not in ArcSight's CEF dictionary"
                     + (" (keys outside it aren't allowed: ArcSight won't index it)" if custom_keys is False else ''), line)
                continue
            if base and base not in present:
                flag(f"{k} without its {base}", line)
            if k in SLOT_KEYS and f'{k}Label' not in present:
                flag(f"{k} has no {k}Label: ArcSight shows it with no name", line)
            kind, length = KEYS[k][1], KEYS[k][2]
            if length and len(v) > length:
                flag(f"{k} longer than its {length} characters (ArcSight cuts it)", line)
            if kind in ('ip', 'int', 'long', 'float', 'mac') and not _fits(k, [v]):
                flag(f"{k} holds '{v[:40]}', not a{'n IPv4 address' if kind == 'ip' else ' ' + kind}", line)
            if kind == 'time' and not (re.fullmatch(r'\d{10,13}', v) or re.fullmatch(r'[A-Z][a-z]{2} \d{2} \d{4} .*', v)):
                flag(f"{k} holds '{v[:40]}': CEF times are milliseconds since 1970 or 'MMM dd yyyy HH:mm:ss'", line)
        if n < len(events):
            event = events[n]
            kind = event_kind(event) or 'other'
            kinds[kind] = kinds.get(kind, 0) + 1
            values = event_values(event)
            sent = {v for _, v in pairs} | set(header)
            millis = {}
            for path, value in values.items():
                if _is_time(value):
                    try:
                        millis[path] = str(int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000))
                    except ValueError:
                        pass
            for path, value in values.items():
                carriers = [k for k, v in pairs if v == value or v == millis.get(path)]
                if not carriers and value not in sent:
                    unsent[(kind, path)] = unsent.get((kind, path), 0) + 1
                for k in carriers:
                    implied.setdefault((kind, k), {})
                    implied[(kind, k)][path] = implied[(kind, k)].get(path, 0) + 1
            for k, v in pairs:
                if k.endswith('Label'):
                    labels[(kind, k[:-5])] = v
    mapping: dict[str, list[dict[str, str]]] = {}
    for (kind, key), paths in sorted(implied.items()):
        if key.endswith('Label'):
            continue
        path, hits = max(paths.items(), key=lambda x: (x[1], -len(x[0])))
        mapping.setdefault(kind, []).append({'path': path, 'key': key, **({'label': labels[(kind, key)]}
                                                                          if (kind, key) in labels else {}),
                                             'seen': f"{hits} of {kinds.get(kind, 0)}"})
    return {
        'lines': len(lines),
        'problems': [f"{what} ({count} line{'s' if count > 1 else ''}; e.g. {examples[what]})"
                     for what, count in sorted(problems.items(), key=lambda x: -x[1])],
        'implied_mapping': mapping,
        'not_sent': [{'event_type': kind, 'path': path, 'events': f"{count} of {kinds.get(kind, 0)}"}
                     for (kind, path), count in sorted(unsent.items())],
    }


def examples_from(plan: CefPlan, lines: list[str], events: list[etree._Element]) -> dict[tuple[str, str], str]:
    """{(event kind or 'common', key): an example value} from sampled lines."""
    out: dict[tuple[str, str], str] = {}
    common = {f.key for f in plan.common}
    for n, line in enumerate(lines):
        parsed = parse(line)
        kind = (event_kind(events[n]) if n < len(events) else None) or 'other'
        for k, v in parsed['extension']:
            out.setdefault(('common' if k in common else kind, k), v)
        for (name, _, _), value in zip(HEADER, parsed['header'][1:]):
            out.setdefault(('header', name), value)
    return out


# ---------------------------------------------------------------------------------------------- documentation

def _cell(text: str | None) -> str:
    return (text or '').replace('|', '\\|').replace('\n', ' ')


def _path_said(path: str) -> str:
    """An Event path as the documentation shows it: the fallbacks and constants the draft writes, in words."""
    if ACTION_NAME in path:
        first = path[1:path.index(',')] if path.startswith('(') else path
        return ("the event's action element and its Action" if first == ACTION_NAME else
                f"`{first}`, else the event's action element and its Action")
    if re.fullmatch(r"'[^']*'", path):
        return f"the event's kind ({path[1:-1]})"
    if path.startswith('local-name('):
        return "the name of the event's action element (" + path[len('local-name('):-1] + ")"
    return f"`{path}`"


def _header_from(value: CefValue) -> str:
    if value.value is not None:
        return f"the constant '{value.value}'" if value.value else 'empty'
    said = _path_said(value.source or '')
    if value.map:
        said += ', mapped ' + ', '.join(f"{k} -> {v}" for k, v in value.map.items())
    if value.default:
        said += f" (else '{value.default}')"
    return said


def plan_markdown(plan: CefPlan, examples: dict[tuple[str, str], str]) -> str:
    out = ["Each Event becomes one CEF (ArcSight Common Event Format) line: `CEF:0|Device Vendor|Device Product|Device "
           "Version|Device Event Class ID|Name|Severity|` then key=value pairs. "
           + (f"Each line is the value of one Kafka record on topic `{plan.topic}`, one Event a record"
              + (f", keyed by `{plan.kafka_key}`" if plan.kafka_key else '') + "."
              if plan.output == 'kafka' else "The lines are written as text, one an Event."),
           "Keys outside ArcSight's CEF dictionary are " + ("allowed." if plan.custom_keys else
                                                             "not used: ArcSight indexes only its dictionary's keys. "
                                                             "Its custom slots (cs1-cs6 and the rest) carry the values "
                                                             "no standard key fits, each named by its Label key."), "",
           "### Header", "", "| Position | CEF field | From | Example |", "| --- | --- | --- | --- |",
           "| 1 | Version | the constant 'CEF:0' | CEF:0 |"]
    for n, (name, said, _) in enumerate(HEADER, 2):
        out.append(f"| {n} | {said} | {_cell(_header_from(getattr(plan, name)))} | "
                   f"{_cell(examples.get(('header', name), ''))} |")
    out += ['']

    def table(fields: list[CefField], kind: str) -> list[str]:
        rows = ["| Event path | CEF key | ArcSight field | What it holds | Example |", "| --- | --- | --- | --- | --- |"]
        for f in fields:
            full, _, length, meaning = KEYS.get(f.key, (f.key, '', None, 'key outside the CEF dictionary'))
            what = meaning + (f"; labelled '{f.label}' ({f.key}Label = {KEYS.get(f.key + 'Label', (f.key + 'Label',))[0]})"
                              if f.label and f.key in SLOT_KEYS else '')
            what += {'epoch_ms': '; milliseconds since 1970',
                     'outcome': '; from Outcome/Success (no Outcome: success)'}.get(f.transform, '')
            if length:
                what += f'; at most {length} characters'
            rows.append(f"| {_cell(_path_said(f.path))} | {f.key} | {full} | {_cell(what)} | {_cell(examples.get((kind, f.key), ''))} |")
        return rows
    if plan.common:
        out += ["### Every event", "", *table(plan.common, 'common'), '']
    for kind, fields in plan.events.items():
        out += [f"### {kind} events", "", *table(fields, kind), '']
    if plan.not_sent:
        out += ["### Not sent", "", "Event values the sample holds that no CEF key carries.", "",
                "| Event path | Event type | Why |", "| --- | --- | --- |"]
        out += [f"| `{_cell(n['path'])}` | {_cell(n.get('event_type'))} | {_cell(n.get('why'))} |" for n in plan.not_sent]
        out += ['']
    return '\n'.join(out).rstrip() + '\n'


# ---------------------------------------------------------------------------------------------- standing instructions

_MAPPING_LINE = re.compile(r"((?:EventSource|EventDetail|EventTime|Meta)/[\w/@\[\]='*.:-]+)\s*(?:->|=>|→|maps? to|goes? to)\s*"
                           r"`?([A-Za-z][A-Za-z0-9]*)`?(?:\s*\(?\s*(?:label(?:led)?|as)\s*[:=]?\s*['\"“]([^'\"”]+)['\"”]\)?)?",
                           re.I)


def from_instructions(texts: list[str]) -> dict[str, Any]:
    """What standing instructions (AGENTS docs) say about CEF output: mappings (path -> key, with a label), whether
    keys outside the dictionary are allowed, the topic, the vendor, and pipeline templates they name."""
    overrides, found = [], {}
    for text in texts:
        for m in _MAPPING_LINE.finditer(text or ''):
            key = key_of(m.group(2))
            if key in KEYS or re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', key):
                overrides.append(Override(path=m.group(1), key=key, label=m.group(3)))
        lowered = (text or '').lower()
        if re.search(r'custom (cef )?(keys|fields)[^.\n]*(not allowed|aren\'t allowed|are not allowed|forbidden|never)', lowered) \
                or re.search(r'(no|never|don\'t use|do not use) custom (cef )?(keys|fields)', lowered):
            found['custom_keys'] = False
        elif re.search(r'custom (cef )?(keys|fields)[^.\n]*(allowed|may be used|are fine|permitted)', lowered):
            found['custom_keys'] = True
        topic = re.search(r'(?:kafka )?topic\s*[:=]\s*`?([A-Za-z0-9._-]+)`?', text or '', re.I)
        if topic:
            found['topic'] = topic.group(1)
        vendor = re.search(r'(?:device )?vendor\s*[:=]\s*`?([^`\n]+?)`?\s*$', text or '', re.I | re.M)
        if vendor:
            found['vendor'] = vendor.group(1).strip()
        template = re.search(r'(?:pipeline )?template\s*[:=]\s*`?([^`\n]+?)`?\s*$', text or '', re.I | re.M)
        if template:
            found['template'] = template.group(1).strip()
    return {'overrides': overrides, **found}


def extend(plan: CefPlan, events: list[etree._Element]) -> tuple[list[dict[str, Any]], list[str]]:
    """What a plan lacks for these Events (event kinds it has no fields for, values a kind's fields don't take), as
    overrides placing each where the draft would: every key the plan gives stays where it is."""
    kept = [Override(path=f.path, key=f.key, label=f.label) for f in plan.common]
    kept += [Override(path=f.path, key=f.key, label=f.label, event_type=kind)
             for kind, fields in plan.events.items() for f in fields]
    kept += [Override(path=n['path'], drop=True, event_type=n.get('event_type') if n.get('event_type') != 'every event'
                      else None) for n in plan.not_sent if 'instructed' in (n.get('why') or '')]
    redrafted, _ = draft(events, plan.custom_keys, kept, {name: getattr(plan, name) for name, _, _ in HEADER},
                         plan.output, plan.topic, plan.kafka_key)
    had = {(None, f.path) for f in plan.common} | {(kind, f.path) for kind, fields in plan.events.items() for f in fields}
    added, notes = [], []
    for f in redrafted.common:
        if (None, f.path) not in had:
            added.append({'path': f.path, 'key': f.key, **({'label': f.label} if f.label else {})})
    for kind, fields in redrafted.events.items():
        if kind not in plan.events:
            notes.append(f"{kind} events: a kind the CEF plan has no fields for (only the common ones are sent)")
        for f in fields:
            if (kind, f.path) not in had and (None, f.path) not in had:
                added.append({'path': f.path, 'key': f.key, 'event_type': kind, **({'label': f.label} if f.label else {})})
    # The draft reserves only the keys of paths these Events hold: a key the plan gives a value they don't hold is
    # taken all the same, so an addition landing on one moves to a slot that is free in the plan.
    common_keys = {f.key for f in plan.common}
    for entry in list(added):
        kind = entry.get('event_type')
        taken = common_keys | {f.key for f in plan.events.get(kind, [])} | {
            a['key'] for a in added if a is not entry and a.get('event_type') in (kind, None)}
        if entry['key'] not in taken:
            continue
        kind_of = next((k for k, keys in SLOTS.items() if entry['key'] in keys), 'string')
        free = next((k for k in SLOTS[kind_of] + SLOTS['string'] if k not in taken), None)
        if free:
            entry['key'] = free
            entry.setdefault('label', label_for(entry['path']))
        else:
            added.remove(entry)
            notes.append(f"{entry['path']} ({kind or 'every event'}): no free slot left beside the plan's own")
    unsent = [n for n in redrafted.not_sent if n not in plan.not_sent]
    if unsent:
        notes.append(f"{len(unsent)} new value(s) no key or free slot takes: {[n['path'] for n in unsent][:8]}")
    return added, notes
