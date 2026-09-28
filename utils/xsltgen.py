"""Generate an event-logging translation XSLT from a field mapping.

The model says which input field (or constant) goes to which event-logging path, per event type. This
module writes the XSLT: the right input namespace, one template per record, elements in schema order,
time conversion with stroom:format-date, value maps, and guards so empty inputs leave elements out rather
than writing empty ones. Mistakes the schema can catch (unknown paths, invalid constants, two alternatives
of a choice) come back as problems per mapping entry instead of as XSLT for the model to debug.
"""
from dataclasses import dataclass, field
from typing import Literal

from lxml import etree
from pydantic import BaseModel, Field, model_validator

from utils.eventschema import Child, EventSchema

XSL = 'http://www.w3.org/1999/XSL/Transform'
XSI = 'http://www.w3.org/2001/XMLSchema-instance'
XS = 'http://www.w3.org/2001/XMLSchema'
EVT = 'event-logging:3'
INPUT_NAMESPACE = {'data_splitter': 'records:2', 'json': 'http://www.w3.org/2013/XSL/json'}
DEFAULT_ROOT = {'data_splitter': 'records', 'json': '/array'}
DEFAULT_RECORD = {'data_splitter': 'record', 'json': 'map'}
# Pattern letters of Java's SimpleDateFormat and DateTimeFormatter; any other letter must be quoted.
_PATTERN_LETTERS = set('GuyDMLdQqYwWEecFaHkKhmsSAnNVzOXxZp')


class FieldMapping(BaseModel):
    """One output value. Give exactly one of field, value or xpath."""
    path: str = Field(description="Event-logging path below Event, e.g. 'EventSource/User/Id', "
                                  "'EventDetail/Authenticate/Outcome/Success', or '.../Data' with data_name.")
    field: str | None = Field(None, description="Input field: a Data Splitter data name, a JSON key "
                                                "('user.name' for nested keys), or an XML path relative to the record.")
    value: str | None = Field(None, description="A constant, e.g. 'Logon'.")
    xpath: str | None = Field(None, description="Advanced: an XPath expression relative to the record.")
    time_format: str | None = Field(None, description="Input time pattern (Java, e.g. \"yyyy-MM-dd'T'HH:mm:ss\", "
                                                      "from profile_sample), or 'epoch_ms' / 'epoch_s'.")
    timezone: str | None = Field(None, description="Input time zone when the time has none, e.g. '+10:00' or 'UTC'.")
    map: dict[str, str] | None = Field(None, description="Input value -> output value, e.g. {'ok': 'true', 'fail': 'false'}.")
    default: str | None = Field(None, description="Output when the field is empty (or, with map, matches no key). "
                                                  "Without it the element is left out in that case.")
    data_name: str | None = Field(None, description="For a path ending in Data: the Data element's Name.")

    @model_validator(mode='after')
    def one_source(self):
        if sum(x is not None for x in (self.field, self.value, self.xpath)) != 1:
            raise ValueError(f"'{self.path}': give exactly one of field, value or xpath")
        return self


class Condition(BaseModel):
    """A test on the record; give field or xpath and one of equals, one_of, matches or present."""
    field: str | None = None
    xpath: str | None = None
    equals: str | None = None
    one_of: list[str] | None = None
    matches: str | None = Field(None, description="Regular expression (XPath flavour).")
    present: bool | None = Field(None, description="True: the field has a non-empty value; False: it does not.")

    @model_validator(mode='after')
    def one_test(self):
        if (self.field is None) == (self.xpath is None):
            raise ValueError("A condition needs exactly one of field or xpath")
        if sum(x is not None for x in (self.equals, self.one_of, self.matches, self.present)) != 1:
            raise ValueError("A condition needs exactly one of equals, one_of, matches or present")
        return self


class EventRule(BaseModel):
    name: str = Field(description="Short name for this kind of event, e.g. 'logon'.")
    when: list[Condition] = Field(default_factory=list, description="All must hold. Empty: every record "
                                                                    "(put such a rule last).")
    fields: list[FieldMapping] = Field(default_factory=list, description="Fields for this kind of event; they "
                                                                          "override common fields with the same path.")


class TranslationMapping(BaseModel):
    input: Literal['data_splitter', 'json', 'xml'] = Field(
        description="What the XSLT reads: data_splitter (Event Data (Text)), json (JSONParser), xml (the source XML).")
    root: str | None = Field(None, description="Root element to match. Defaults: 'records' / '/array'; required for xml.")
    record: str | None = Field(None, description="Record elements under the root. Defaults: 'record' / 'map'; "
                                                 "required for xml, e.g. 'logon'.")
    xml_namespace: str = Field('', description="xml input only: the source's default namespace, if it has one.")
    common: list[FieldMapping] = Field(default_factory=list, description="Fields every event gets, e.g. time, "
                                                                        "System, Device.")
    events: list[EventRule] = Field(min_length=1, description="Event kinds, tried in order; the first whose "
                                                              "conditions hold is written.")
    unmatched: Literal['warn', 'skip'] = Field('warn', description="Records no rule matches: log a warning, or skip.")


def literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def pattern_problem(pattern: str) -> str | None:
    """Unquoted letters Java would reject, e.g. the T in yyyy-MM-ddTHH:mm:ss, with the pattern fixed."""
    quoted, bad, fixed = False, [], []
    for ch in pattern:
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch.isalpha() and ch not in _PATTERN_LETTERS:
            bad.append(ch)
            fixed.append(f"'{ch}'")
            continue
        fixed.append(ch)
    if bad:
        return (f"time_format {pattern!r} has unquoted letters {sorted(set(bad))}; quote them, "
                f"e.g. {''.join(fixed)!r}")
    return None


@dataclass
class _Node:
    child: Child | None                                   # None for Event itself
    path: str
    kids: dict[str, '_Node'] = field(default_factory=dict)
    leaf: FieldMapping | None = None
    data: list[tuple[Child, FieldMapping]] = field(default_factory=list)


class _Generator:
    def __init__(self, mapping: TranslationMapping, schema: EventSchema):
        self.m, self.schema = mapping, schema
        self.problems: list[str] = []
        self.warnings: list[str] = []

    def _note(self, bucket: list[str], message: str) -> None:
        if message not in bucket:
            bucket.append(message)

    # --- input addressing ---
    def source(self, field_name: str | None, xpath: str | None) -> str:
        if xpath is not None:
            return xpath
        if self.m.input == 'data_splitter':
            return '/'.join(f"data[@name={literal(p)}]" for p in field_name.split('/')) + '/@value'
        if self.m.input == 'json':
            return '/'.join(f"*[@key={literal(p)}]" for p in field_name.split('.'))
        return field_name

    def condition(self, c: Condition) -> str:
        src = self.source(c.field, c.xpath)
        if c.equals is not None:
            return f"{src} = {literal(c.equals)}"
        if c.one_of is not None:
            return f"{src} = ({', '.join(literal(v) for v in c.one_of)})"
        if c.matches is not None:
            return f"exists({src}[matches(., {literal(c.matches)})])"
        test = f"exists({src}[normalize-space(.)])"
        return test if c.present else f"not({test})"

    # --- values ---
    def leaf_test(self, entry: FieldMapping) -> str | None:
        if entry.value is not None:
            return None
        if entry.default is not None:
            return None
        src = self.source(entry.field, entry.xpath)
        if entry.map:
            return f"{src} = ({', '.join(literal(k) for k in entry.map)})"
        return f"exists({src}[normalize-space(.)])"

    def value_expr(self, entry: FieldMapping) -> str:
        src = self.source(entry.field, entry.xpath)
        if entry.map:
            expr = literal(entry.default) if entry.default is not None else "''"
            for key, out in reversed(list(entry.map.items())):
                expr = f"if ({src} = {literal(key)}) then {literal(out)} else {expr}"
            return expr
        fmt, tz = entry.time_format, entry.timezone
        if fmt == 'epoch_ms':
            expr = f"stroom:format-date(string({src}[1]))"
        elif fmt == 'epoch_s':
            expr = f"stroom:format-date(string(xs:integer(xs:decimal({src}[1]) * 1000)))"
        elif fmt:
            expr = f"stroom:format-date({src}[1], {literal(fmt)}{', ' + literal(tz) if tz else ''})"
        else:
            expr = src
        if entry.default is not None:
            return f"if (exists({src}[normalize-space(.)])) then {expr} else {literal(entry.default)}"
        return expr

    def write_value(self, element: etree._Element, entry: FieldMapping) -> None:
        if entry.value is not None:
            element.text = entry.value
        else:
            etree.SubElement(element, f'{{{XSL}}}value-of', select=self.value_expr(entry))

    # --- checks against the schema ---
    def check_leaf(self, where: str, child: Child, entry: FieldMapping) -> None:
        allowed = self.schema.enumeration(child.decl)
        kind = self.schema.base_type(child.decl)
        outputs = [entry.value] if entry.value is not None else list((entry.map or {}).values()) + (
            [entry.default] if entry.default is not None else [])
        if allowed:
            bad = [v for v in outputs if v not in allowed]
            if bad:
                self._note(self.problems, f"{where}: {bad} not allowed; {child.name} takes one of {allowed}")
            elif entry.value is None and not entry.map:
                self._note(self.warnings, f"{where}: {child.name} only takes {allowed}; the input value is written "
                                          f"as it is, so add a map unless it already uses these values")
        if kind == 'boolean':
            bad = [v for v in outputs if v not in ('true', 'false')]
            if bad:
                self._note(self.problems, f"{where}: {child.name} is true or false, not {bad}")
            elif entry.value is None and not entry.map and entry.xpath is None:
                self._note(self.warnings, f"{where}: {child.name} is true/false; map the input values, e.g. "
                                          f"{{'ok': 'true', 'fail': 'false'}}, unless they already are")
        if kind == 'dateTime' and entry.value is None and not entry.time_format:
            self._note(self.warnings, f"{where}: {child.name} is a date-time; give time_format (from profile_sample) "
                                      f"unless the input is already yyyy-MM-ddTHH:mm:ss.SSSZ")
        if entry.time_format and entry.time_format not in ('epoch_ms', 'epoch_s'):
            problem = pattern_problem(entry.time_format)
            if problem:
                self._note(self.problems, f"{where}: {problem}")
        if (entry.time_format or entry.timezone) and entry.value is not None:
            self._note(self.problems, f"{where}: time_format needs a field or xpath, not a constant")
        if entry.time_format and entry.map:
            self._note(self.problems, f"{where}: use time_format or map, not both")

    def tree(self, rule: EventRule) -> _Node:
        root = _Node(None, 'Event')
        by_key = {(e.path.strip('/'), e.data_name): e for e in self.m.common}
        by_key.update({(e.path.strip('/'), e.data_name): e for e in rule.fields})
        for (path, data_name), entry in by_key.items():
            where = f"[{rule.name}] {path}" + (f" (Data {data_name})" if data_name else '')
            try:
                chain = self.schema.resolve(path)
            except ValueError as e:
                self._note(self.problems, f"[{rule.name}] {path}: {e}")
                continue
            if not chain:
                self._note(self.problems, f"{where}: map a path below Event")
                continue
            last = chain[-1]
            if data_name is not None:
                if last.name != 'Data':
                    self._note(self.problems, f"{where}: data_name only goes with a path ending in Data")
                    continue
                parent = self._walk(root, chain[:-1], where)
                if parent:
                    parent.data.append((last, entry))
                continue
            if last.name == 'Data':
                self._note(self.problems, f"{where}: a Data path needs data_name (the Data element's Name)")
                continue
            if not self.schema.is_leaf(last.decl):
                options = [c.name for c in self.schema.children(last.decl)]
                self._note(self.problems, f"{where}: {last.name} holds other elements; map one of {options}")
                continue
            node = self._walk(root, chain, where)
            if node is None:
                continue
            if node.kids:
                self._note(self.problems, f"{where}: already has child elements mapped")
                continue
            node.leaf = entry
            self.check_leaf(where, last, entry)
        self._conditional: list[str] = []
        self._check_structure(rule.name, root, self.schema.event)
        if self._conditional:
            self._note(self.warnings, f"[{rule.name}] required {self._conditional} are left out when their input "
                                      f"fields are empty, which makes the event invalid. Fine if those fields are "
                                      f"always filled; otherwise give the mapping a default.")
        return root

    def _walk(self, root: _Node, chain: list[Child], where: str) -> _Node | None:
        node = root
        for child in chain:
            if node.leaf is not None:
                self._note(self.problems, f"{where}: {node.path} is mapped to a value and cannot have children")
                return None
            node = node.kids.setdefault(child.name, _Node(child, f'{node.path}/{child.name}'))
        return node

    def _check_structure(self, rule: str, node: _Node, decl) -> None:
        if node.leaf is not None:
            return
        allowed = self.schema.children(decl)
        chosen: dict[int, list[str]] = {}
        for kid in node.kids.values():
            if kid.child.choice is not None and not kid.child.repeatable:
                chosen.setdefault(kid.child.choice, []).append(kid.child.name)
        for names in chosen.values():
            if len(names) > 1:
                self._note(self.problems, f"[{rule}] {node.path}: {names} are alternatives; an event has only one of "
                                          f"them (use separate event rules)")
        present = set(node.kids) | ({'Data'} if node.data else set())
        for c in allowed:
            if c.required and c.name not in present:
                self._note(self.problems, f"[{rule}] {node.path}/{c.name} is required by the schema; map it")
            elif c.required and c.name in node.kids and self.test_of(node.kids[c.name]) not in (None, self.test_of(node)):
                self._conditional.append(f"{node.path}/{c.name}".removeprefix('Event/'))
        for cid, members in self.schema.required_choices.items():
            if any(c.choice == cid for c in allowed) and not present & set(members):
                self._note(self.problems, f"[{rule}] {node.path} needs one of {members}")
        for kid in node.kids.values():
            self._check_structure(rule, kid, kid.child.decl)

    # --- XSLT ---
    def test_of(self, node: _Node) -> str | None:
        if node.leaf is not None:
            return self.leaf_test(node.leaf)
        tests = [self.test_of(k) for k in node.kids.values()] + [self.leaf_test(e) for _, e in node.data]
        if not tests or any(t is None for t in tests):
            return None
        return ' or '.join(dict.fromkeys(tests))

    def emit(self, parent: etree._Element, node: _Node, enclosing: str | None = None) -> None:
        items = [(k.child.index, 0, n, k) for n, k in enumerate(node.kids.values())] + \
                [(c.index, 1, n, (c, e)) for n, (c, e) in enumerate(node.data)]
        for _, is_data, _, item in sorted(items, key=lambda i: i[:3]):
            if is_data:
                child, entry = item
                test = self.leaf_test(entry)
                holder = etree.SubElement(parent, f'{{{XSL}}}if', test=test) if test and test != enclosing else parent
                element = etree.SubElement(holder, f'{{{EVT}}}Data', Name=entry.data_name)
                if entry.value is not None:
                    element.set('Value', entry.value)
                else:
                    etree.SubElement(element, f'{{{XSL}}}attribute', name='Value', select=self.value_expr(entry))
                continue
            test = self.test_of(item)
            holder = etree.SubElement(parent, f'{{{XSL}}}if', test=test) if test and test != enclosing else parent
            element = etree.SubElement(holder, f'{{{EVT}}}{item.child.name}')
            if item.leaf is not None:
                self.write_value(element, item.leaf)
            else:
                self.emit(element, item, test or enclosing)

    def stylesheet(self, version: str) -> tuple[str, list[dict]]:
        m = self.m
        if m.input == 'xml' and not (m.root and m.record):
            self._note(self.problems, "xml input needs root and record, e.g. root='logons', record='logon'")
        trees = [(rule, self.tree(rule)) for rule in m.events]
        catch_all = [r.name for r in m.events[:-1] if not r.when]
        if catch_all:
            self._note(self.problems, f"Rules {catch_all} have no conditions, so the rules after them never run; "
                                      f"put the rule without conditions last")

        nsmap = {None: EVT, 'xsl': XSL, 'xsi': XSI, 'stroom': 'stroom', 'xs': XS}
        sheet = etree.Element(f'{{{XSL}}}stylesheet', nsmap=nsmap, version='3.0')
        sheet.set('xpath-default-namespace', INPUT_NAMESPACE.get(m.input, m.xml_namespace))
        sheet.set('exclude-result-prefixes', 'stroom xs')
        root_template = etree.SubElement(sheet, f'{{{XSL}}}template', match=m.root or DEFAULT_ROOT.get(m.input, ''))
        events = etree.SubElement(root_template, f'{{{EVT}}}Events', Version=version)
        events.set(f'{{{XSI}}}schemaLocation', f'{EVT} file://event-logging-v{version}.xsd')
        etree.SubElement(events, f'{{{XSL}}}apply-templates', select=m.record or DEFAULT_RECORD.get(m.input, ''),
                         mode='event')
        record_template = etree.SubElement(sheet, f'{{{XSL}}}template', match='*', mode='event')
        conditional = any(rule.when for rule in m.events)
        body = etree.SubElement(record_template, f'{{{XSL}}}choose') if conditional else record_template
        summary = []
        for rule, root in trees:
            test = ' and '.join(f'({self.condition(c)})' for c in rule.when)
            holder = (etree.SubElement(body, f'{{{XSL}}}when', test=test) if test
                      else etree.SubElement(body, f'{{{XSL}}}otherwise')) if conditional else body
            self.emit(etree.SubElement(holder, f'{{{EVT}}}Event'), root)
            summary.append({'event': rule.name, 'when': [self.condition(c) for c in rule.when] or 'every record',
                            'fields': sorted({(e.path.strip('/') + (f"[{e.data_name}]" if e.data_name else ''))
                                              for e in m.common + rule.fields})})
            if not test and conditional:
                break
        if conditional and all(rule.when for rule in m.events) and m.unmatched == 'warn':
            otherwise = etree.SubElement(body, f'{{{XSL}}}otherwise')
            etree.SubElement(otherwise, f'{{{XSL}}}sequence',
                             select="stroom:log('WARN', concat('No event mapping matched record ', stroom:record-no()))")
        text = etree.tostring(sheet, pretty_print=True, xml_declaration=True, encoding='UTF-8').decode('utf-8')
        return text, summary


def generate(mapping: TranslationMapping, schema: EventSchema, version: str) -> dict:
    gen = _Generator(mapping, schema)
    xslt, summary = gen.stylesheet(version)
    return {'ok': not gen.problems, 'problems': gen.problems, 'warnings': gen.warnings, 'events': summary,
            'xslt': None if gen.problems else xslt}
