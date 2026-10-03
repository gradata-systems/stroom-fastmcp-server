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
# Comment the documentation run puts in each Event, naming the rule that wrote it; never in saved XSLT.
RULE_MARK = 'stroom-mcp rule: '
FN = 'http://www.w3.org/2005/xpath-functions'
MAP_NS = 'http://www.w3.org/2005/xpath-functions/map'
INPUT_NAMESPACE = {'data_splitter': 'records:2', 'json': 'http://www.w3.org/2013/XSL/json'}
DEFAULT_ROOT = {'data_splitter': 'records', 'json': '/', 'xml_fragments': '/'}
DEFAULT_RECORD = {'data_splitter': 'record'}
# The JSONParser's records: an array's maps, with or without the parser's root map (addRootObject) around the
# array; or, for JSON lines, the maps the parser's root map wraps the top-level objects in.
JSON_RECORDS = {'array': '/array/map | /map/array/map', 'lines': '/map/map'}
# Values listed per element (EventSource table) or per TypeId (Descriptions) in the documentation.
DOC_VALUES_SHOWN = 3
# A value map used by one element with at most this many keys is written inline, as an if.
INLINE_MAP_KEYS = 3
# A field read fewer times than this in a template is written where it's used, not held in a variable: an
# element's own guard and value are two reads, so a variable needs a third, such as an enclosing guard.
VARIABLE_MIN_READS = 3
# Pattern letters of Java's SimpleDateFormat and DateTimeFormatter; any other letter must be quoted.
_PATTERN_LETTERS = set('GuyDMLdQqYwWEecFaHkKhmsSAnNVzOXxZp')


Scope = Literal['record', 'item']


class Lookup(BaseModel):
    """Reference data: what stroom:lookup() finds for a key in a map a reference loader provides (find_reference_data
    lists the maps). The pipeline must name the loader as a pipeline reference (create_pipeline references, or
    update_pipeline (references=...)); a key the map lacks gives no value, so the element is left out (or `default`)."""
    map: str = Field(description="The map name, e.g. 'USER_TO_DEPARTMENT'.")
    field: str | None = Field(None, description="Input field holding the key (or a name from extract).")
    xpath: str | None = Field(None, description="Or an XPath giving the key.")
    path: str | None = Field(None, description="Path below the map's value when it holds elements, e.g. 'department'; "
                                               "omit for a text value.")

    @model_validator(mode='after')
    def one_key(self):
        if (self.field is None) == (self.xpath is None):
            raise ValueError(f"lookup '{self.map}': give exactly one of field or xpath for the key")
        return self


Transform = Literal['lower', 'upper', 'trim', 'strip_domain', 'domain', 'digits']


class FieldMapping(BaseModel):
    """One output value. Give exactly one of field, any_of, value, xpath or lookup."""
    path: str = Field(description="Event-logging path below Event, e.g. 'EventSource/User/Id', "
                                  "'EventDetail/Authenticate/Outcome/Success', or '.../Data' with data_name.")
    field: str | None = Field(None, description="Input field: a Data Splitter data name, a JSON key "
                                                "('user.name' for nested keys), an XML path relative to the record, "
                                                "or a name from extract.")
    any_of: list[str] | None = Field(None, description="Input fields tried in order; the first with a value is used, "
                                                       "for sources whose variants name the same thing differently.")
    value: str | None = Field(None, description="A constant, e.g. 'Logon'.")
    xpath: str | None = Field(None, description="Advanced: an XPath expression relative to the record.")
    lookup: Lookup | None = Field(None, description="The value reference data holds for a key field.")
    dictionary: str | None = Field(None, description="With field (the key): the value a Dictionary doc of this name "
                                                     "gives it, one key=value per line (save_dictionary). Keys "
                                                     "the dictionary lacks give no value, or `default`.")
    transform: Transform | None = Field(None, description="Applied to the input value first: lower, upper, trim, "
                                                          "strip_domain (DOMAIN\\user or user@domain -> user), domain "
                                                          "(the DOMAIN or domain part), digits (digits only).")
    repeat: bool = Field(False, description="Write one element per value of the input (a JSON array, an element the "
                                            "record has several of) instead of the first value: the nearest element on "
                                            "the path the schema lets repeat is written once per value, e.g. Group for "
                                            "EventSource/User/Groups/Group/Name, or a Data per value. Nothing else may be "
                                            "mapped below that element; transform is allowed, map and time_format are not.")
    scope: Scope | None = Field(None, description="With for_each: 'record' when the input is the record's, not the "
                                                  "item's (the default inside for_each).")
    time_format: str | None = Field(None, description="Input time pattern (Java, e.g. \"yyyy-MM-dd'T'HH:mm:ss\", "
                                                      "from profile_sample), or 'epoch_ms' / 'epoch_s'.")
    timezone: str | None = Field(None, description="Input time zone when the time has none, e.g. '+10:00' or 'UTC'.")
    map: dict[str, str] | None = Field(None, description="Input value -> output value, e.g. {'ok': 'true', 'fail': 'false'}.")
    default: str | None = Field(None, description="Output when the field is empty (or, with map, matches no key). "
                                                  "Without it the element is left out in that case.")
    data_name: str | None = Field(None, description="For a path ending in Data: the Data element's Name.")

    @model_validator(mode='after')
    def one_source(self):
        if sum(x is not None for x in (self.field, self.any_of, self.value, self.xpath, self.lookup)) != 1:
            raise ValueError(f"'{self.path}': give exactly one of field, any_of, value, xpath or lookup")
        if self.dictionary is not None and self.field is None and self.any_of is None:
            raise ValueError(f"'{self.path}': dictionary needs field (or any_of) as the key")
        if self.transform and self.value is not None:
            raise ValueError(f"'{self.path}': transform applies to an input, not a constant")
        if self.repeat and (self.value is not None or self.map or self.time_format or self.lookup or self.dictionary):
            raise ValueError(f"'{self.path}': repeat takes an input field, any_of or xpath, with transform at most")
        return self


class Condition(BaseModel):
    """A test on the record; give field or xpath and one of equals, one_of, matches, present or in_dictionary."""
    field: str | None = Field(None, description="An input field, or a name from extract.")
    xpath: str | None = None
    equals: str | None = None
    one_of: list[str] | None = None
    matches: str | None = Field(None, description="Regular expression (XPath flavour).")
    present: bool | None = Field(None, description="True: the field has a non-empty value; False: it does not.")
    in_dictionary: str | None = Field(None, description="The value is one of the lines of the Dictionary doc of "
                                                        "this name (a list, one entry per line).")
    scope: Scope | None = Field(None, description="With for_each: 'record' to test the record rather than the item.")

    @model_validator(mode='after')
    def one_test(self):
        if (self.field is None) == (self.xpath is None):
            raise ValueError("A condition needs exactly one of field or xpath")
        if sum(x is not None for x in (self.equals, self.one_of, self.matches, self.present, self.in_dictionary)) != 1:
            raise ValueError("A condition needs exactly one of equals, one_of, matches, present or in_dictionary")
        return self


class EventRule(BaseModel):
    name: str = Field(description="Short name for this kind of event, e.g. 'logon'.")
    when: list[Condition] = Field(default_factory=list, description="All must hold. Empty: every record "
                                                                    "(put such a rule last).")
    fields: list[FieldMapping] = Field(default_factory=list, description="Fields for this kind of event; they "
                                                                          "override common fields with the same path.")
    drop: bool = Field(False, description="True: records matching this rule are left untranslated on purpose "
                                          "(no Event, no warning), e.g. kinds set_shape_handling marked drop. No fields.")


class DropRule(BaseModel):
    """Records (or, with for_each, items) to leave untranslated on purpose: no Event, no warning."""
    when: list[Condition] = Field(min_length=1, description="All must hold for the record to be dropped.")
    reason: str = Field(description="Why, in the user's words; kept as a comment in the XSLT and in the documentation.")


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


class Extraction(BaseModel):
    """Fields parsed out of one text field with a regular expression: a message string holding a time, a user, an
    action and a description, say. Each capture group becomes a field (names, in group order) that common, events
    and when use like any input field. Records the pattern does not match get no values from it, so the elements
    are left out, which a rule's conditions can test with present."""
    field: str | None = Field(None, description="The input field holding the text (a Data Splitter name, JSON key, "
                                                "XML path, or a name from an earlier extraction).")
    xpath: str | None = Field(None, description="Or an XPath expression giving the text.")
    regex: str = Field(description="XPath regular expression with one capture group per field, e.g. "
                                   "'^(\\S+ \\S+) (\\S+) (\\S+) (.*)$'. Anchor it, and use (?:...) for groups that are "
                                   "not fields. XPath regexes have no lookaround and no named groups.")
    names: list[str] = Field(min_length=1, description="A field name per capture group, in order; '' skips a group.")
    flags: str = Field('', description="XPath regex flags: i (ignore case), m (multi-line), s (dot matches newline), "
                                       "x (ignore whitespace in the pattern).")
    scope: Scope | None = Field(None, description="With for_each: 'record' when the text is the record's, not the item's.")

    @model_validator(mode='after')
    def one_source(self):
        if (self.field is None) == (self.xpath is None):
            raise ValueError("An extraction needs exactly one of field or xpath")
        return self


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
    input: Literal['data_splitter', 'json', 'xml', 'xml_fragments'] = Field(
        description="What the XSLT reads: data_splitter (Event Data (Text)), json (JSONParser), xml (the source XML, "
                    "one document), xml_fragments (XMLFragmentParser: several root elements, one record each, inside "
                    "its converter's wrapper).")
    root: str | None = Field(None, description="Root element to match. Defaults: 'records' (data_splitter), '/' (json, "
                                               "xml_fragments); required for xml.")
    record: str | None = Field(None, description="Record elements under the root. Defaults: 'record' (data_splitter), "
                                                 "the JSON layout's maps; required for xml and xml_fragments, e.g. "
                                                 "'logon' or 'Event'.")
    xml_namespace: str = Field('', description="xml and xml_fragments: the records' namespace, if any. Fragments that "
                                               "declare none take the wrapper's default namespace, records:2 with the "
                                               "standard wrapper (profile_sample says which).")
    json_layout: Literal['array', 'lines'] = Field('array', description=(
        "json input only, from profile_sample: 'array' (one JSON array; set jsonParser.addRootObject=false on the "
        "pipeline, or leave it: both are matched) or 'lines' (one object per line, or concatenated objects; "
        "jsonParser.addRootObject must stay true, which wraps them all in one map)."))
    extract: list[Extraction] = Field(default_factory=list, description=(
        "Fields parsed out of text fields with regular expressions, e.g. a message string holding the time, user "
        "and action; their names are then used as fields in common, events and when."))
    for_each: str | None = Field(None, description=(
        "When one record holds several events: the field (JSON key such as 'events', dotted for nested keys) or "
        "XPath selecting the items, each of which becomes an Event. Fields, conditions and extractions then read the "
        "item; mark those that read the record itself (the batch's host, say) with scope: record."))
    drop_when: list[DropRule] = Field(default_factory=list, description=(
        "Records (items, with for_each) to leave untranslated, tried before the event rules: heartbeats, test "
        "traffic, service accounts. Each entry's conditions must all hold; any entry drops."))
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
    repeat: FieldMapping | None = None       # this element is written once per value of the entry's input


class _Generator:
    def __init__(self, mapping: TranslationMapping, schema: EventSchema, mark_rules: bool = False):
        self.m, self.schema = mapping, schema
        self.mark_rules = mark_rules
        self.problems: list[str] = []
        self.warnings: list[str] = []
        self._names: dict[str, str] = {}          # selector -> variable name, the same in every template
        self._xpath_names: set[str] = set()      # those holding an xpath entry rather than a field
        self._scope: dict[str, str] = {}          # variables the template being written uses: name -> select
        self._maps: dict[tuple, str] = {}         # value map items -> name of the stylesheet variable holding it
        # Fields an extraction parses out of a text field: name -> (extraction, capture group). Each extraction's
        # analyze-string() result is held in a variable (parts) declared ahead of the fields read from it.
        self.derived: dict[str, tuple[Extraction, int]] = {}
        self._parts: dict[int, str] = {}
        self._keep: set[str] = set()
        # Dictionary docs read at run time: (name, 'map' for key=value lines, 'set' for a list) -> variable.
        self._dicts: dict[tuple[str, str], str] = {}
        self.reference_maps: set[str] = set()
        # for_each: the templates run with an item as context; $record (a tunnel parameter) is the record.
        self._item_mode = bool(mapping.for_each)
        self.uses_record = False
        for n, extraction in enumerate(mapping.extract):
            self._check_extraction(n, extraction)
            base = style_name(f"{extraction.field or 'text'}-parts", mapping.style.naming)
            self._parts[id(extraction)] = unique_name(base, self._keep, mapping.style.naming)
            self._keep.add(self._parts[id(extraction)])
            for nr, name in enumerate(extraction.names, 1):
                if name in self.derived:
                    self._note(self.problems, f"extract: '{name}' is the name of two extracted fields")
                elif name:
                    self.derived[name] = (extraction, nr)
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

    def _check_extraction(self, n: int, ex: Extraction) -> None:
        where = f"extract[{n}] ({ex.field or ex.xpath})"
        if re.search(r'\(\?(P?<[A-Za-z_]|<?[=!])', ex.regex):
            self._note(self.problems, f"{where}: XPath regular expressions have no named groups and no lookaround; "
                                      f"use plain groups, (?:...) for ones that are not fields, and anchors")
            return
        try:
            groups = re.compile(ex.regex).groups
        except re.error as e:
            self._note(self.problems, f"{where}: the regex does not compile: {e}")
            return
        if len(ex.names) > groups:
            self._note(self.problems, f"{where}: {len(ex.names)} names but the regex has {groups} capture groups")
        elif len(ex.names) < groups:
            self._note(self.warnings, f"{where}: the regex has {groups} capture groups and {len(ex.names)} names; "
                                      f"the later groups are not fields")
        if not any(ex.names):
            self._note(self.problems, f"{where}: names every group '', so nothing is extracted")
        if set(ex.flags) - set('imsxq'):
            self._note(self.problems, f"{where}: flags are any of i, m, s, x, q; not {ex.flags!r}")

    def parts_name(self, ex: Extraction) -> str:
        return self._parts[id(ex)]

    def declare_parts(self, ex: Extraction) -> None:
        """Declare the variable holding the extraction's analyze-string() result in the template being written,
        before anything that reads it (an extraction of an extracted field declares its source's first)."""
        name = self.parts_name(ex)
        if name in self._scope:
            return
        if ex.field in self.derived:
            self.declare_parts(self.derived[ex.field][0])
        text = self.source(ex.field, ex.xpath, ex.scope)
        flags = f", {literal(ex.flags)}" if ex.flags else ''
        self._scope[name] = f"analyze-string(string(({text})[1]), {literal(ex.regex)}{flags})"

    # --- input addressing ---
    def source(self, field_name: str | None, xpath: str | None, scope: str | None = None) -> str:
        """The selector for an input, relative to the context node: the record, or with for_each the item, where
        scope 'record' reads the record through $record instead."""
        if field_name in self.derived:
            ex, nr = self.derived[field_name]
            return f"${self.parts_name(ex)}/fn:match//fn:group[@nr={nr}]"
        if xpath is not None:
            selector = xpath
        elif self.m.input == 'data_splitter':
            selector = '/'.join(f"data[@name={literal(p)}]" for p in field_name.split('/')) + '/@value'
        elif self.m.input == 'json':
            selector = '/'.join(f"*[@key={literal(p)}]" for p in field_name.split('.'))
        else:
            selector = field_name
        if self._item_mode and scope == 'record':
            self.uses_record = True
            return f"$record/({selector})" if xpath is not None else f"$record/{selector}"
        return selector

    def items_of(self, field_name: str | None, xpath: str | None, scope: str | None = None) -> str:
        """The nodes an input has several of: a JSON array's members, else the input's own nodes."""
        selector = self.source(field_name, xpath, scope)
        if self.m.input == 'json' and xpath is None and field_name not in self.derived:
            return f"{selector}/*"
        return selector

    def entry_ref(self, entry: FieldMapping) -> str:
        field_name, xpath, scope = self.src_of(entry)
        return self.ref(field_name, xpath, self.label(entry), scope)

    def entry_has(self, entry: FieldMapping) -> str:
        field_name, xpath, scope = self.src_of(entry)
        return self.has(field_name, xpath, self.label(entry), scope)

    def dict_var(self, name: str, kind: str) -> str:
        if (name, kind) not in self._dicts:
            naming = self.m.style.naming
            base = style_name(f"{name}-{'map' if kind == 'map' else 'list'}", naming)
            self._dicts[(name, kind)] = unique_name(base, set(self._names.values()) | set(self._maps.values())
                                                    | self._keep | set(self._dicts.values()), naming)
        return self._dicts[(name, kind)]

    def key_expr(self, field_name: str | None, xpath: str | None, scope: str | None = None) -> str:
        return f"string(({self.source(field_name, xpath, scope)})[1])"

    def src_of(self, entry: FieldMapping) -> tuple[str | None, str | None, str | None]:
        """The entry's input as (field, xpath, scope): a field as it is; any_of, lookup and dictionary as the XPath
        that computes them (scope already applied), so the rest of the generator treats them like any computed value."""
        scope = entry.scope
        if entry.lookup:
            self.reference_maps.add(entry.lookup.map)
            key = self.key_expr(entry.lookup.field, entry.lookup.xpath, scope)
            found = f"stroom:lookup({literal(entry.lookup.map)}, {key})"
            if not entry.lookup.path:
                return None, found, None
            # The value's elements are in no namespace, while the stylesheet's default XPath namespace is the
            # input's: *:name selects them whatever that is, at any depth below the value.
            steps = [s if (':' in s or s.startswith('@') or s in ('.', '*')) else f'*:{s}'
                     for s in entry.lookup.path.strip('/').split('/') if s]
            return None, f"{found}//{'/'.join(steps)}", None
        if entry.any_of:
            first = ', '.join(self.source(f, None, scope) for f in entry.any_of)
            key_src = f"({first})[normalize-space(.)][1]"
        else:
            key_src = None
        if entry.dictionary:
            key = f"string(({key_src or self.source(entry.field, None, scope)})[1])"
            return None, f"${self.dict_var(entry.dictionary, 'map')}?({key})", None
        if entry.any_of:
            return None, key_src, None
        return entry.field, entry.xpath, scope

    def scalar(self, entry: FieldMapping, src: str) -> str:
        """The entry's one value, transformed as asked; src is the variable or selector holding its values."""
        one = f"{src}[1]"
        t = entry.transform
        if t == 'lower':
            return f"lower-case({one})"
        if t == 'upper':
            return f"upper-case({one})"
        if t == 'trim':
            return f"normalize-space({one})"
        if t == 'strip_domain':
            return f"replace(replace({one}, '^[^\\\\]*\\\\', ''), '@.*$', '')"
        if t == 'domain':
            return f"replace({one}, '^(?:([^\\\\]*)\\\\.*|[^@]*@(.*))$', '$1$2')"
        if t == 'digits':
            return f"replace({one}, '[^0-9]', '')"
        return one

    def ref(self, field_name: str | None, xpath: str | None, label: str, scope: str | None = None) -> str:
        """A variable holding the input's non-blank values, declared at the top of the template being written,
        so each selector appears once per template. Named after the field, or for an xpath after `label`."""
        raw = self.source(field_name, xpath, scope)
        if raw not in self._names:
            naming = self.m.style.naming
            base = style_name(label if xpath is not None else field_name, naming)
            self._names[raw] = unique_name(base, set(self._names.values()) | set(self._maps.values()) | self._keep, naming)
        name = self._names[raw]
        if xpath is not None:
            self._xpath_names.add(name)
        if field_name in self.derived:
            self.declare_parts(self.derived[field_name][0])
        wrap = (xpath is not None and not is_call(raw)) or self.m.input in ('xml', 'xml_fragments')
        self._scope.setdefault(name, f"({raw})[normalize-space(.)]" if wrap else f"{raw}[normalize-space(.)]")
        return '$' + name

    def has(self, field_name: str | None, xpath: str | None, label: str, scope: str | None = None) -> str:
        """Test that the input has a value. Fields select nodes, which are true when present; an xpath may give
        a number or boolean, which XPath would test by its value, so that needs exists()."""
        v = self.ref(field_name, xpath, label, scope)
        return v if xpath is None else f'exists({v})'

    def condition(self, c: Condition, raw: bool = False) -> str:
        """The rule's test; raw: with the input's own selectors, for the summary returned to the model."""
        src = (c.field if c.field in self.derived else self.source(c.field, c.xpath, c.scope)) if raw \
            else self.ref(c.field, c.xpath, 'condition', c.scope)
        if c.equals is not None:
            return f"{src} = {literal(c.equals)}"
        if c.one_of is not None:
            return f"{src} = ({', '.join(literal(v) for v in c.one_of)})"
        if c.matches is not None:
            return f"exists({src}[matches(., {literal(c.matches)})])"
        if c.in_dictionary is not None:
            if raw:
                return f"{src} in dictionary {literal(c.in_dictionary)}"
            return f"{src} = ${self.dict_var(c.in_dictionary, 'set')}"
        test = f"exists({src}[normalize-space(.)])" if raw else self.has(c.field, c.xpath, 'condition', c.scope)
        return test if c.present else f"not({test})"

    # --- values ---
    @staticmethod
    def label(entry: FieldMapping) -> str:
        if entry.lookup:
            return entry.lookup.map
        if entry.dictionary:
            return f"{(entry.field or entry.any_of[0])}-{entry.dictionary}"
        if entry.any_of:
            return entry.any_of[0]
        return '-'.join(entry.path.strip('/').split('/')[-2:])

    def map_ref(self, entry: FieldMapping, src: str) -> str:
        """A stylesheet-level variable holding the entry's value map, declared once however many elements use
        it. Named after the input and the first element it fills, e.g. $action_to_success."""
        items = tuple(entry.map.items())
        if items not in self._maps:
            naming = self.m.style.naming
            base = style_name(f"{src[1:]}-to-{entry.path.strip('/').split('/')[-1]}", naming)
            self._maps[items] = unique_name(base, set(self._names.values()) | set(self._maps.values()) | self._keep, naming)
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
            src = self.entry_ref(entry)
            key = self.scalar(entry, src) if entry.transform else src
            if self.as_xsl_map(entry):
                return f"exists({self.map_ref(entry, src)}?({key}))"
            return f"{key} = ({', '.join(literal(k) for k in entry.map)})"
        return self.entry_has(entry)

    def value_expr(self, entry: FieldMapping) -> str:
        if entry.repeat:
            # Written inside xsl:for-each over the values: the current value, transformed if asked.
            return self.scalar(entry, '.') if entry.transform else '.'
        src = self.entry_ref(entry)
        key = self.scalar(entry, src) if entry.transform else src
        if entry.map and self.as_xsl_map(entry):
            # The lookup operator takes any number of keys, so an empty input gives no value rather than an error.
            lookup = f"{self.map_ref(entry, src)}?({key})"
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
            for k, out in reversed(items):
                expr = f"if ({key} = {literal(k)}) then {literal(out)} else {expr}"
            return expr
        fmt, tz = entry.time_format, entry.timezone
        one = self.scalar(entry, src) if entry.transform else f"{src}[1]"
        if fmt == 'epoch_ms':
            expr = f"stroom:format-date(string({one}))"
        elif fmt == 'epoch_s':
            expr = f"stroom:format-date(string(xs:integer(xs:decimal({one}) * 1000)))"
        elif fmt:
            expr = f"stroom:format-date({one}, {literal(fmt)}{', ' + literal(tz) if tz else ''})"
        else:
            expr = one if entry.transform else src
        if entry.default is not None:
            return f"if ({self.entry_has(entry)}) then {expr} else {literal(entry.default)}"
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
            elif entry.value is None and not entry.map and self.src_of(entry)[1] is None:
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
            if entry.repeat:
                anchor = max((i for i, c in enumerate(chain) if c.repeatable), default=None)
                if anchor is None:
                    self._note(self.problems, f"{where}: repeat needs an element on the path the schema lets repeat; "
                                              f"none of {[c.name for c in chain]} may occur more than once")
                else:
                    self._walk(root, chain[:anchor + 1], where).repeat = entry
        for anchor in self._anchors(root):
            leaves = self._leaves(anchor)
            if len(leaves) > 1 or any(n.data for n in self._nodes(anchor)):
                self._note(self.problems, f"[{rule.name}] {anchor.path} is written once per value of "
                                          f"{anchor.repeat.field or anchor.repeat.xpath or anchor.repeat.any_of}; map "
                                          f"nothing else below it (found {[n.path for n in leaves]})")
        self._conditional: list[str] = []
        self._root = root
        self._check_structure(rule.name, root, self.schema.event)
        detail = root.kids.get('EventDetail')
        if rule.when and detail is not None and 'Unknown' in detail.kids:
            # A catch-all rule (no conditions) may rightly say Unknown; a kind told apart by conditions usually has an action.
            self._note(self.warnings, f"[{rule.name}] writes EventDetail/Unknown, which says what happened is not known, "
                                      f"yet its conditions single these records out. If they are an activity another "
                                      f"action element describes (Alert, Authenticate, Network, Process, Create, Update, "
                                      f"Delete, View, ...), use that; keep Unknown only when none fits.")
        if self._conditional:
            self._note(self.warnings, f"[{rule.name}] required {self._conditional} are left out when their input "
                                      f"fields are empty, which makes the event invalid. Fine if those fields are "
                                      f"always filled; otherwise give the mapping a default.")
        return root

    def _mapped_elsewhere(self, node: _Node, members: list[str]) -> str:
        """For a missing required choice: the members the event already maps outside node (EventSource/User/Id when
        Authenticate lacks its User, say), as paths under node the same input could fill; '' when there are none."""
        mapped, candidates = [], []
        for leaf in self._leaves(self._root):
            if leaf.path.startswith(node.path + '/'):
                continue
            parts = leaf.path.split('/')
            member = next((i for i in range(len(parts) - 2, 0, -1) if parts[i] in members), None)
            if member is None:
                continue
            candidate = f"{node.path}/{'/'.join(parts[member:])}".removeprefix('Event/')
            try:
                self.schema.resolve(candidate)
            except ValueError:
                continue
            entry = leaf.leaf
            source = (f"field '{entry.field}'" if entry.field else f"any_of {entry.any_of}" if entry.any_of else
                      f"xpath {entry.xpath!r}" if entry.xpath else f"value {entry.value!r}" if entry.value is not None
                      else 'a lookup')
            mapped.append(f"{leaf.path.removeprefix('Event/')} ({source})")
            candidates.append(candidate)
        if not mapped:
            return ''
        return (f". Already mapped elsewhere in the event: {', '.join(mapped[:4])}. If the action is about one of "
                f"them, map the same input here as well ({' or '.join(candidates[:4])}), and keep the existing "
                f"mapping: it says something different")

    def _nodes(self, node: _Node) -> list[_Node]:
        out = [node]
        for kid in node.kids.values():
            out += self._nodes(kid)
        return out

    def _leaves(self, node: _Node) -> list[_Node]:
        return [n for n in self._nodes(node) if n.leaf is not None]

    def _anchors(self, node: _Node) -> list[_Node]:
        return [n for n in self._nodes(node) if n.repeat is not None]

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
                self._note(self.problems, f"[{rule}] {node.path} needs one of {members}" + self._mapped_elsewhere(node, members))
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
                if entry.repeat:
                    holder = etree.SubElement(parent, f'{{{XSL}}}for-each', select=self.repeat_items(entry))
                else:
                    test = self.leaf_test(entry)
                    holder = etree.SubElement(parent, f'{{{XSL}}}if', test=test) if test and test != enclosing else parent
                element = etree.SubElement(holder, f'{{{EVT}}}Data', Name=entry.data_name)
                if entry.value is not None:
                    element.set('Value', entry.value)
                else:
                    etree.SubElement(element, f'{{{XSL}}}attribute', name='Value', select=self.value_expr(entry))
            elif item.repeat is not None:
                # One element per value: the loop stands in for the guard, and the leaf below reads the current value.
                loop = etree.SubElement(parent, f'{{{XSL}}}for-each', select=self.repeat_items(item.repeat))
                self.emit_element(loop, item, self.test_of(item), inline=True)
            elif not inline and self.shareable(item) and self.key(item) in self.shared:
                etree.SubElement(parent, f'{{{XSL}}}call-template', name=self.template_for(item))
            else:
                self.emit_element(parent, item, enclosing, inline)

    def repeat_items(self, entry: FieldMapping) -> str:
        field_name, xpath, scope = self.src_of(entry)
        return self.items_of(field_name, xpath, scope)

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
            if name in self._keep:
                continue
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
        if m.input == 'xml_fragments' and not m.record:
            self._note(self.problems, "xml_fragments input needs record, the fragment element, e.g. record='Event'")
        for rule in m.events:
            if rule.drop and rule.fields:
                self._note(self.problems, f"[{rule.name}] a drop rule writes no Event, so it takes no fields")
        # Drop conditions are rules without an event, tried first.
        rules = [EventRule(name=f'drop: {d.reason}', when=d.when, drop=True) for d in m.drop_when] + list(m.events)
        trees = [(rule, None if rule.drop else self.tree(rule)) for rule in rules]
        catch_all = [r.name for r in rules[:-1] if not r.when]
        if catch_all:
            self._note(self.problems, f"Rules {catch_all} have no conditions, so the rules after them never run; "
                                      f"put the rule without conditions last")

        uses_dict_map = any(e.dictionary for e in m.common + [f for r in m.events for f in r.fields])
        nsmap = {None: EVT, 'xsl': XSL, 'xsi': XSI, 'stroom': 'stroom', 'xs': XS, **({'fn': FN} if m.extract else {}),
                 **({'map': MAP_NS} if uses_dict_map else {})}
        sheet = etree.Element(f'{{{XSL}}}stylesheet', nsmap=nsmap, version='3.0')
        sheet.set('xpath-default-namespace', INPUT_NAMESPACE.get(m.input, m.xml_namespace))
        sheet.set('exclude-result-prefixes', 'stroom xs' + (' fn' if m.extract else '') + (' map' if uses_dict_map else ''))
        root_template = etree.SubElement(sheet, f'{{{XSL}}}template', match=m.root or DEFAULT_ROOT.get(m.input, ''))
        events = etree.SubElement(root_template, f'{{{EVT}}}Events', Version=version)
        events.set(f'{{{XSI}}}schemaLocation', f'{EVT} file://event-logging-v{version}.xsd')
        records = m.record or (JSON_RECORDS[m.json_layout] if m.input == 'json' else DEFAULT_RECORD.get(m.input, ''))
        if m.input == 'xml_fragments' and not m.root:
            records = f'*/{m.record}'   # the fragments sit under the wrapper's root, whatever its namespace
        etree.SubElement(events, f'{{{XSL}}}apply-templates', select=records, mode='event')
        self.choose_shared([(rule.name, root) for rule, root in trees if root is not None])
        self._templates: dict[str, tuple[str, etree._Element]] = {}
        record_template = etree.SubElement(sheet, f'{{{XSL}}}template', match='*', mode='event')
        if m.for_each:
            # One record, several events: each item is handed to the rules with the record as a tunnel parameter.
            select = m.for_each if re.search(r'[/\[(*@$]', m.for_each) else self.items_of(m.for_each, None)
            apply = etree.SubElement(record_template, f'{{{XSL}}}apply-templates', select=select, mode='item')
            etree.SubElement(apply, f'{{{XSL}}}with-param', name='record', select='.', tunnel='yes')
            record_template = etree.SubElement(sheet, f'{{{XSL}}}template', match='*', mode='item')
        conditional = any(rule.when for rule in rules)
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
                    event = etree.SubElement(holder, f'{{{EVT}}}Event')
                    if self.mark_rules:
                        event.append(etree.Comment(f'{RULE_MARK}{rule.name}'))
                    self.emit(event, root)
                    summary.append({'event': rule.name, 'when': when,
                                    'fields': sorted({(e.path.strip('/') + (f"[{e.data_name}]" if e.data_name else ''))
                                                      for e in m.common + rule.fields})})
                if not test and conditional:
                    break
            if conditional and all(rule.when for rule in rules) and m.unmatched == 'warn':
                otherwise = etree.SubElement(body, f'{{{XSL}}}otherwise')
                etree.SubElement(otherwise, f'{{{XSL}}}sequence',
                                 select="stroom:log('WARN', concat('No event mapping matched record ', stroom:record-no()))")

        self.in_scope(record_template, write_rules)
        self.tidy_variables(record_template)
        for _, template in self._templates.values():
            self.tidy_variables(template)
        if self.uses_record:
            for template in [record_template] + [t for _, t in self._templates.values()]:
                if '$record' in etree.tostring(template, encoding='unicode'):
                    template.insert(0, etree.Element(f'{{{XSL}}}param', name='record', tunnel='yes'))
        for n, (items, name) in enumerate(self._maps.items()):
            variable = etree.Element(f'{{{XSL}}}variable', name=name, **{'as': 'map(xs:string, xs:string)'})
            entries = etree.SubElement(variable, f'{{{XSL}}}map')
            for key, out in items:
                etree.SubElement(entries, f'{{{XSL}}}map-entry', key=literal(key), select=literal(out))
            sheet.insert(n, variable)
        for n, ((name, kind), var) in enumerate(self._dicts.items(), len(self._maps)):
            lines = f"tokenize(stroom:dictionary({literal(name)}), '\\r?\\n')"
            if kind == 'map':
                select = (f"map:merge(for $line in {lines}[contains(., '=')] return map{{normalize-space("
                          f"substring-before($line, '=')): normalize-space(substring-after($line, '='))}})")
            else:
                select = f"{lines} ! normalize-space(.)"
            sheet.insert(n, etree.Element(f'{{{XSL}}}variable', name=var, select=select))
        if self.reference_maps:
            self._note(self.warnings, f"Lookups read reference map(s) {sorted(self.reference_maps)}: the pipeline needs "
                                      f"the feed that loads each as a pipeline reference (create_pipeline references, or "
                                      f"update_pipeline (references=...)); find_reference_data lists the maps and their feeds.")
        # Called with the record as context, so they read its fields just as the event rules do.
        for k, (name, template) in self._templates.items():
            sheet.append(etree.Comment(f" {name}: {', '.join(self.users[k])} "))
            sheet.append(template)
        text = etree.tostring(sheet, pretty_print=True, xml_declaration=True, encoding='UTF-8').decode('utf-8')
        return text, summary


def generate(mapping: TranslationMapping, schema: EventSchema, version: str, mark_rules: bool = False) -> dict:
    """The XSLT for a mapping. mark_rules puts a comment naming the rule in each Event, for the documentation run
    (stepping with draft code, nothing saved): the Field mapping tables then know exactly which rule wrote what."""
    gen = _Generator(mapping, schema, mark_rules)
    xslt, summary = gen.stylesheet(version)
    return {'ok': not gen.problems, 'problems': gen.problems, 'warnings': gen.warnings, 'events': summary,
            'xslt': None if gen.problems else xslt, 'reference_maps': sorted(gen.reference_maps),
            'dictionaries': sorted({name for name, _ in gen._dicts}),
            **({'items': mapping.for_each} if mapping.for_each else {})}
