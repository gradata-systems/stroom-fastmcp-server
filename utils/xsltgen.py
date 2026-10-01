"""Generate an event-logging translation XSLT from a field mapping.

The model says which input field (or constant) goes to which event-logging path, per event type. This
module writes the XSLT: the right input namespace, one template per record, elements in schema order,
time conversion with stroom:format-date, value maps, and guards so empty inputs leave elements out rather
than writing empty ones. Elements that come out the same in several places (EventTime, EventSource, ...)
are written once, as named templates. A field read several times in a template (in enclosing guards as well
as its own element) goes into a variable holding its non-blank values, declared in the rule that uses it, so
guards read `$src_ip or $src_port`; one read only by its element stays inline. Value maps shared by several
elements, or too long to read as an if, are declared once as xsl:map variables and read with the lookup
operator, $action_to_success?($action), which gives nothing rather than an error for an empty input. Names
and these thresholds follow the mapping's style, which a style guide in the AGENTS docs can set.

Mistakes the schema can catch (unknown paths, invalid constants, two alternatives of a choice) come back as
problems per mapping entry instead of as XSLT for the model to debug.
"""
import re
from collections import Counter
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
# A value map used by one element with at most this many keys is written inline, as an if.
INLINE_MAP_KEYS = 3
# A field read fewer times than this in a template is written where it's used, not held in a variable: an
# element's own guard and value are two reads, so a variable needs a third, such as an enclosing guard.
VARIABLE_MIN_READS = 3
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
    drop: bool = Field(False, description="True: records matching this rule are left untranslated on purpose "
                                          "(no Event, no warning), e.g. kinds set_shape_handling marked drop. No fields.")


class XsltStyle(BaseModel):
    """How the XSLT is written. Take these from a style guide in the standing instructions (AGENTS docs) when
    one says how XSLT should look; otherwise leave the defaults."""
    naming: Literal['snake_case', 'camelCase', 'PascalCase', 'kebab-case'] = Field(
        'snake_case', description="Names of variables and named templates, e.g. client_ip and event_source.")
    variable_min_reads: int = Field(VARIABLE_MIN_READS, ge=1, description=(
        "Read a field into a variable only when a template reads it at least this many times; fewer are written "
        "where they're used. An element's guard and value are two reads. 1: always use variables."))
    inline_map_max_keys: int = Field(INLINE_MAP_KEYS, ge=0, description=(
        "A value map used by one element with at most this many keys is written inline as an if; longer or "
        "shared maps are declared once as an xsl:map. 0: always an xsl:map."))


def style_name(text: str, naming: str) -> str:
    """text (a field name, a path such as 'Authenticate-User', or 'src_ip') in the naming style, as an
    XML name: 'EventSource' -> event_source, eventSource, EventSource or event-source."""
    words = [w.lower() for w in re.findall(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+', text)] or ['field']
    if naming == 'camelCase':
        name = words[0] + ''.join(w.capitalize() for w in words[1:])
    elif naming == 'PascalCase':
        name = ''.join(w.capitalize() for w in words)
    else:
        name = ('-' if naming == 'kebab-case' else '_').join(words)
    return name if not name[0].isdigit() else '_' + name


def unique_name(base: str, taken: set[str], naming: str) -> str:
    """base, or base with a number (event_source_2, eventSource2) when that is taken."""
    sep = {'snake_case': '_', 'kebab-case': '-'}.get(naming, '')
    return base if base not in taken else next(f'{base}{sep}{n}' for n in range(2, 1000) if f'{base}{sep}{n}' not in taken)


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
    style: XsltStyle = Field(default_factory=XsltStyle, description="How the XSLT is written: naming, and when "
                                                                    "to use variables and xsl:maps.")


def is_call(expr: str) -> bool:
    """Whether an XPath expression is one function call, such as concat(a, '-', b), which a predicate can
    follow without brackets; 'a or b' or 'x | y' would need them."""
    start = re.match(r'[\w:.-]+\(', expr.strip())
    if not start:
        return False
    text, depth, quote = expr.strip(), 0, None
    for n, ch in enumerate(text[start.end() - 1:], start.end() - 1):
        if quote:
            quote = None if ch == quote else quote
        elif ch in '\'"':
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return n == len(text) - 1
    return False


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
        self._names: dict[str, str] = {}          # selector -> variable name, the same in every template
        self._xpath_names: set[str] = set()      # those holding an xpath entry rather than a field
        self._scope: dict[str, str] = {}          # variables the template being written uses: name -> select
        self._maps: dict[tuple, str] = {}         # value map items -> name of the stylesheet variable holding it
        # A value map becomes an xsl:map when several elements use it or it is too long to read as an if.
        paths: dict[tuple, set[str]] = {}
        for entry in mapping.common + [f for rule in mapping.events for f in rule.fields]:
            if entry.map:
                paths.setdefault(tuple(entry.map.items()), set()).add(entry.path.strip('/'))
        self._xsl_maps = {items for items, used in paths.items()
                          if len(used) > 1 or len(items) > mapping.style.inline_map_max_keys}

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

    def ref(self, field_name: str | None, xpath: str | None, label: str) -> str:
        """A variable holding the input's non-blank values, declared at the top of the template being written,
        so each selector appears once per template. Named after the field, or for an xpath after `label`."""
        raw = self.source(field_name, xpath)
        if raw not in self._names:
            naming = self.m.style.naming
            base = style_name(label if xpath is not None else field_name, naming)
            self._names[raw] = unique_name(base, set(self._names.values()) | set(self._maps.values()), naming)
        name = self._names[raw]
        if xpath is not None:
            self._xpath_names.add(name)
        wrap = (xpath is not None and not is_call(raw)) or self.m.input == 'xml'
        self._scope.setdefault(name, f"({raw})[normalize-space(.)]" if wrap else f"{raw}[normalize-space(.)]")
        return '$' + name

    def has(self, field_name: str | None, xpath: str | None, label: str) -> str:
        """Test that the input has a value. Fields select nodes, which are true when present; an xpath may give
        a number or boolean, which XPath would test by its value, so that needs exists()."""
        v = self.ref(field_name, xpath, label)
        return v if xpath is None else f'exists({v})'

    def condition(self, c: Condition, raw: bool = False) -> str:
        """The rule's test; raw: with the input's own selectors, for the summary returned to the model."""
        src = self.source(c.field, c.xpath) if raw else self.ref(c.field, c.xpath, 'condition')
        if c.equals is not None:
            return f"{src} = {literal(c.equals)}"
        if c.one_of is not None:
            return f"{src} = ({', '.join(literal(v) for v in c.one_of)})"
        if c.matches is not None:
            return f"exists({src}[matches(., {literal(c.matches)})])"
        test = f"exists({src}[normalize-space(.)])" if raw else self.has(c.field, c.xpath, 'condition')
        return test if c.present else f"not({test})"

    # --- values ---
    @staticmethod
    def label(entry: FieldMapping) -> str:
        return '-'.join(entry.path.strip('/').split('/')[-2:])

    def map_ref(self, entry: FieldMapping, src: str) -> str:
        """A stylesheet-level variable holding the entry's value map, declared once however many elements use
        it. Named after the input and the first element it fills, e.g. $action_to_success."""
        items = tuple(entry.map.items())
        if items not in self._maps:
            naming = self.m.style.naming
            base = style_name(f"{src[1:]}-to-{entry.path.strip('/').split('/')[-1]}", naming)
            self._maps[items] = unique_name(base, set(self._names.values()) | set(self._maps.values()), naming)
        return '$' + self._maps[items]

    def as_xsl_map(self, entry: FieldMapping) -> bool:
        """Whether the entry's value map is declared as an xsl:map rather than written inline as an if."""
        return tuple(entry.map.items()) in self._xsl_maps

    def leaf_test(self, entry: FieldMapping) -> str | None:
        if entry.value is not None:
            return None
        if entry.default is not None:
            return None
        if entry.map:
            src = self.ref(entry.field, entry.xpath, self.label(entry))
            if self.as_xsl_map(entry):
                return f"exists({self.map_ref(entry, src)}?({src}))"
            return f"{src} = ({', '.join(literal(k) for k in entry.map)})"
        return self.has(entry.field, entry.xpath, self.label(entry))

    def value_expr(self, entry: FieldMapping) -> str:
        src = self.ref(entry.field, entry.xpath, self.label(entry))
        if entry.map and self.as_xsl_map(entry):
            # The lookup operator takes any number of keys, so an empty input gives no value rather than an error.
            lookup = f"{self.map_ref(entry, src)}?({src})"
            return f"({lookup}, {literal(entry.default)})[1]" if entry.default is not None else f"{lookup}[1]"
        if entry.map:
            items = list(entry.map.items())
            if entry.default is not None:
                expr = literal(entry.default)
            else:
                # Without a default the element is only written when the input is one of the keys, so the
                # last key needs no test of its own.
                *items, (_, last) = items
                expr = literal(last)
            for key, out in reversed(items):
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
            return f"if ({self.has(entry.field, entry.xpath, self.label(entry))}) then {expr} else {literal(entry.default)}"
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

    def emit(self, parent: etree._Element, node: _Node, enclosing: str | None = None, inline: bool = False) -> None:
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
            elif not inline and self.shareable(item) and self.key(item) in self.shared:
                etree.SubElement(parent, f'{{{XSL}}}call-template', name=self.template_for(item))
            else:
                self.emit_element(parent, item, enclosing, inline)

    def emit_element(self, parent: etree._Element, node: _Node, enclosing: str | None, inline: bool) -> None:
        # Working out the guard reads every field below; declare them only if the guard is written.
        outer, self._scope = self._scope, {}
        test = self.test_of(node)
        used, self._scope = self._scope, outer
        if test and test != enclosing:
            for name, select in used.items():
                self._scope.setdefault(name, select)
        holder = etree.SubElement(parent, f'{{{XSL}}}if', test=test) if test and test != enclosing else parent
        element = etree.SubElement(holder, f'{{{EVT}}}{node.child.name}')
        if node.leaf is not None:
            self.write_value(element, node.leaf)
        else:
            self.emit(element, node, test or enclosing, inline)

    # --- named templates for fragments that repeat ---
    @staticmethod
    def shareable(node: _Node) -> bool:
        """Elements with children, or leaves read from the record; constant leaves aren't worth a template."""
        return node.leaf is None or node.leaf.value is None

    def key(self, node: _Node) -> str:
        """The node's path and the node written out in full with its guard, so equal keys mean the same element
        with the same output for any record. Only the same path is shared: an EventSource/Client/IPAddress and a
        Destination/Device/IPAddress read from one field stay apart, as they mean different things."""
        if id(node) not in self._keys:
            holder = etree.Element('fragment')
            self.in_scope(holder, lambda: self.emit_element(holder, node, None, inline=True))
            self._keys[id(node)] = node.path + '\n' + etree.tostring(holder, encoding='unicode')
        return self._keys[id(node)]

    def tidy_variables(self, template: etree._Element) -> None:
        """Keep a variable only where it helps. Every field is first read into one at the top of the template;
        a field read fewer than style.variable_min_reads times goes back inline, and a variable whose reads all sit
        in one rule (one xsl:when) is declared at the start of that rule instead of the top."""
        branches = [b for b in template.iterfind(f'{{{XSL}}}choose/*')]
        homes = {el: branch for branch in branches for el in branch.iter() if el is not branch}
        raw = {name: selector for selector, name in self._names.items()}
        placed: Counter = Counter()
        for variable in template.findall(f'{{{XSL}}}variable'):
            name, select = variable.get('name'), variable.get('select')
            pattern = re.compile(r'\$' + re.escape(name) + r'(?![\w.-])')
            reads = [(el, attr) for el in template.iter() if el is not variable
                     for attr in ('test', 'select') if pattern.search(el.get(attr) or '')]
            if sum(len(pattern.findall(el.get(attr))) for el, attr in reads) < self.m.style.variable_min_reads:
                template.remove(variable)
                plain = raw[name] if name not in self._xpath_names or is_call(raw[name]) else f'({raw[name]})'
                # A Data Splitter or JSON field has one value per record, so its test reads naturally as
                # normalize-space(field); an XML path or an xpath may give several, which normalize-space()
                # would refuse, so those keep the predicate.
                single = name not in self._xpath_names and self.m.input in ('data_splitter', 'json')
                has_value = f'normalize-space({raw[name]})' if single else select

                def inline(m: re.Match) -> str:
                    # Only a test of whether the field has a value needs blank values left out. A value
                    # (after its guard, or after 'then'), a comparison and a [1] read the selector as it is.
                    after, before = m.string[m.end():], m.string[:m.start()].rstrip()
                    if not before and not after and attr == 'select' or after.startswith('[') \
                            or re.match(r'\s*!?=', after) or before.endswith('then'):
                        return plain
                    return has_value

                for el, attr in reads:
                    el.set(attr, pattern.sub(inline, el.get(attr)))
                continue
            rules = {homes.get(el) for el, _ in reads}
            if len(rules) == 1 and None not in rules:
                # A rule's own test sits on its xsl:when, outside the branch, so it counts as the template's.
                branch = rules.pop()
                branch.insert(placed[branch], variable)
                placed[branch] += 1

    def in_scope(self, template: etree._Element, write) -> None:
        """Run write() for a template's body, then declare the variables it used at the top of the template."""
        outer, self._scope = self._scope, {}
        try:
            write()
            for n, (name, select) in enumerate(self._scope.items()):
                template.insert(n, etree.Element(f'{{{XSL}}}variable', name=name, select=select))
        finally:
            self._scope = outer

    def choose_shared(self, roots: list[tuple[str, _Node]]) -> None:
        """Pick the fragments to write once as named templates: the largest that still occurs more than once,
        counting a template's body once however often it is called, until none repeats."""
        self._keys: dict[int, str] = {}
        self.shared: dict[str, None] = {}
        self.users: dict[str, list[str]] = {}

        def walk(node: _Node, rule: str, counts: Counter, seen: set[str] | None) -> None:
            for kid in node.kids.values():
                if not self.shareable(kid):
                    continue
                k = self.key(kid)
                counts[k] += 1
                if seen is None:
                    self.users.setdefault(k, [])
                    if rule not in self.users[k]:
                        self.users[k].append(rule)
                elif k in self.shared:
                    if k in seen:
                        continue
                    seen.add(k)
                walk(kid, rule, counts, seen)

        for rule, root in roots:
            walk(root, rule, Counter(), None)
        while True:
            counts: Counter = Counter()
            seen: set[str] = set()
            for rule, root in roots:
                walk(root, rule, counts, seen)
            repeated = [k for k, n in counts.items() if n > 1 and k not in self.shared]
            if not repeated:
                return
            self.shared[max(repeated, key=len)] = None

    def template_for(self, node: _Node) -> str:
        k = self.key(node)
        if k not in self._templates:
            # The element's name, or as much of its path as tells it apart, e.g. authenticate_user; variants
            # of one path are numbered.
            naming = self.m.style.naming
            taken = {name for name, _ in self._templates.values()}
            parts = node.path.split('/')[1:]
            names = [style_name('-'.join(parts[-n:]), naming) for n in range(1, len(parts) + 1)]
            name = next((n for n in names if n not in taken), None) or unique_name(names[-1], taken, naming)
            template = etree.Element(f'{{{XSL}}}template', name=name)
            self._templates[k] = (name, template)
            self.in_scope(template, lambda: self.emit_element(template, node, None, inline=False))
        return self._templates[k][0]

    def stylesheet(self, version: str) -> tuple[str, list[dict]]:
        m = self.m
        if m.input == 'xml' and not (m.root and m.record):
            self._note(self.problems, "xml input needs root and record, e.g. root='logons', record='logon'")
        for rule in m.events:
            if rule.drop and rule.fields:
                self._note(self.problems, f"[{rule.name}] a drop rule writes no Event, so it takes no fields")
        trees = [(rule, None if rule.drop else self.tree(rule)) for rule in m.events]
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
        self.choose_shared([(rule.name, root) for rule, root in trees if root is not None])
        self._templates: dict[str, tuple[str, etree._Element]] = {}
        record_template = etree.SubElement(sheet, f'{{{XSL}}}template', match='*', mode='event')
        conditional = any(rule.when for rule in m.events)
        summary = []

        def write_rules() -> None:
            body = etree.SubElement(record_template, f'{{{XSL}}}choose') if conditional else record_template
            for rule, root in trees:
                test = ' and '.join(f'({self.condition(c)})' for c in rule.when)
                holder = (etree.SubElement(body, f'{{{XSL}}}when', test=test) if test
                          else etree.SubElement(body, f'{{{XSL}}}otherwise')) if conditional else body
                when = [self.condition(c, raw=True) for c in rule.when] or 'every record'
                if rule.drop:
                    holder.append(etree.Comment(f' {rule.name}: left untranslated on purpose '))
                    summary.append({'event': rule.name, 'when': when, 'dropped': True})
                else:
                    self.emit(etree.SubElement(holder, f'{{{EVT}}}Event'), root)
                    summary.append({'event': rule.name, 'when': when,
                                    'fields': sorted({(e.path.strip('/') + (f"[{e.data_name}]" if e.data_name else ''))
                                                      for e in m.common + rule.fields})})
                if not test and conditional:
                    break
            if conditional and all(rule.when for rule in m.events) and m.unmatched == 'warn':
                otherwise = etree.SubElement(body, f'{{{XSL}}}otherwise')
                etree.SubElement(otherwise, f'{{{XSL}}}sequence',
                                 select="stroom:log('WARN', concat('No event mapping matched record ', stroom:record-no()))")

        self.in_scope(record_template, write_rules)
        self.tidy_variables(record_template)
        for _, template in self._templates.values():
            self.tidy_variables(template)
        for n, (items, name) in enumerate(self._maps.items()):
            variable = etree.Element(f'{{{XSL}}}variable', name=name, **{'as': 'map(xs:string, xs:string)'})
            entries = etree.SubElement(variable, f'{{{XSL}}}map')
            for key, out in items:
                etree.SubElement(entries, f'{{{XSL}}}map-entry', key=literal(key), select=literal(out))
            sheet.insert(n, variable)
        # Called with the record as context, so they read its fields just as the event rules do.
        for k, (name, template) in self._templates.items():
            sheet.append(etree.Comment(f" {name}: {', '.join(self.users[k])} "))
            sheet.append(template)
        text = etree.tostring(sheet, pretty_print=True, xml_declaration=True, encoding='UTF-8').decode('utf-8')
        return text, summary


def generate(mapping: TranslationMapping, schema: EventSchema, version: str) -> dict:
    gen = _Generator(mapping, schema)
    xslt, summary = gen.stylesheet(version)
    return {'ok': not gen.problems, 'problems': gen.problems, 'warnings': gen.warnings, 'events': summary,
            'xslt': None if gen.problems else xslt}
