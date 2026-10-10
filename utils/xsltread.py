"""Reading a translation XSLT back into a mapping: the mapping kept with it lost (its Documentation tab cleared), or
out of step with it (the XSLT changed by hand). Asked for by the user.

An XSLT this server generated is read back the way the generator writes it: per rule, the condition it is chosen by,
and each element (or Data entry, or attribute) the Event gets, with its constant or the expression that fills it,
variables replaced by what they select and shared templates followed. Then:

- with the mapping kept before (out of step): every element that reads as it did keeps its entry exactly as it was
  (field, extraction, map, time format), and only what differs becomes a new entry;
- with none (lost): the input, records, blank values and key=value extractions come from the XSLT itself, and each
  element's expression is read for the generator's idioms (a field, an extracted value, a time format, a transform, a
  value map, a default); anything else is kept as the expression itself (an xpath entry).

Either way the result is only a candidate: tools/rebuild.py regenerates the XSLT from it and steps both over the
sample, and saves it only when every record's output is the same.
"""
import re
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from utils.xsltgen import (EVT, INPUT_NAMESPACE, JSON_RECORDS, MCP_NS, XSL, Condition, FieldMapping, TranslationMapping,
                           any_of_text, default_text, dictionary_lookup_text, dictionary_text, equals_text, group_text,
                           has_value_text,
                           in_dictionary_text, key_text, keyed_text, literal, lookup_text, map_lookup_text, map_step_text,
                           matches_text, one_of_text, parts_text, time_expr, transform_expr)
from utils.xsltversion import strip

X = f'{{{XSL}}}'
E = f'{{{EVT}}}'
_STRING = r"(?:'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")"
_GUARD = re.compile(r"\[normalize-space\(\.\)\](?:\[not\(normalize-space\(\.\) = \((?:" + _STRING + r"|, )*\)\)\])?")
_NIL = re.compile(r"\[not\(normalize-space\(\.\) = \(((?:" + _STRING + r"|, )*)\)\)\]")
_RULE_HEADER = re.compile(r"^\s*Rule '((?:[^']|'')*)'", re.S)
_DROPPED = re.compile(r"^\s*(.+?): left untranslated on purpose\s*$", re.S)
_UNKNOWN = re.compile(r"^\s*(.+?): EventDetail/Unknown on purpose: (.*?)\s*$", re.S)


def unquote(text: str) -> str:
    """An XPath string literal's value."""
    quote = text[0]
    return text[1:-1].replace(quote * 2, quote)


def _strings(text: str) -> list[str]:
    return [unquote(m.group(0)) for m in re.finditer(_STRING, text)]


def _top_split(text: str, separator: str) -> list[str]:
    """text split on a separator outside brackets and string literals."""
    out, depth, start, i = [], 0, 0, 0
    while i < len(text):
        ch = text[i]
        if ch in '\'"':
            end = text.find(ch, i + 1)
            while end != -1 and end + 1 < len(text) and text[end + 1] == ch:
                end = text.find(ch, end + 2)
            i = len(text) if end == -1 else end + 1
            continue
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        elif depth == 0 and text.startswith(separator, i):
            out.append(text[start:i])
            start = i + len(separator)
            i += len(separator)
            continue
        i += 1
    out.append(text[start:])
    return [part.strip() for part in out]


def _unwrap(text: str) -> str:
    """Outer brackets that hold the whole expression, taken off."""
    text = text.strip()
    while text.startswith('(') and text.endswith(')') and len(_top_split(text[1:-1], '§§')) == 1:
        inner, depth = text[1:-1], 0
        for ch in inner:          # '(a) and (b)' starts and ends with brackets that aren't one pair
            depth += ch == '('
            depth -= ch == ')'
            if depth < 0:
                return text
        text = inner.strip()
    return text


def canonical(expr: str) -> str:
    """An expression as it compares: without the generator's has-a-value predicates (which only decide whether an
    element is written), spaces normalised, brackets around one step taken off."""
    text = _GUARD.sub('', expr or '')
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'(?<![\w:?-])\(((?:\*\[@key=' + _STRING + r'\]/?)+(?:\[1\])?|data\[@name=' + _STRING
                  + r'\]/@value(?:\[1\])?|\$[\w.-]+|QXE\d+QX(?:\[1\])?)\)', r'\1', text)   # not a function call's own
    return _unwrap(text)


@dataclass
class Leaf:
    path: str
    data_name: str | None = None
    value: str | None = None          # a constant
    expr: str | None = None           # an expression, its variables replaced
    repeat: str | None = None         # an xsl:for-each, as written (compared whole)
    guard: str | None = None          # the test of the innermost xsl:if around it (not compared)

    @property
    def key(self) -> tuple[str, str | None]:
        return self.path, self.data_name

    @property
    def form(self) -> tuple:
        return ('value', self.value) if self.value is not None else ('repeat', self.repeat) if self.repeat else \
            ('expr', canonical(self.expr or ''))


@dataclass
class RuleReading:
    key: str                          # its template's mode or name, or 'inline-N'
    name: str | None                  # from the comment the generator writes before it
    test: str | None                  # None: the otherwise branch, or the only rule
    leaves: list[Leaf] = field(default_factory=list)
    drop: bool = False
    unknown: str | None = None        # allow_unknown's reason
    notes: list[str] = field(default_factory=list)
    guards: list[str] = field(default_factory=list)       # while reading: the xsl:if tests around the element
    calls: list['SharedCall'] = field(default_factory=list)   # named templates of an imported XSLT, called in place


@dataclass
class Reading:
    rules: list[RuleReading]
    namespace: str
    root: str
    records: str
    nil_values: list[str]
    unmatched_warn: bool
    functions: dict[str, tuple[list[str], str]]       # the XSLT's own single-expression functions
    maps: dict[str, dict[str, str]]                   # stylesheet-level xsl:map variables
    globals: dict[str, str]                           # other stylesheet-level variables (dictionaries)
    notes: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # xsl:import hrefs, in order
    prefixes: dict[str, str] = field(default_factory=dict)     # namespaces bound for imported functions


# The prefixes the generator binds for itself; any other is an imported XSLT's functions (a mapping's functions).
_OWN_PREFIXES = {'xsl', 'xsi', 'stroom', 'xs', 'fn', 'map', 'mcp'}


@dataclass
class SharedCall:
    path: str                         # where it is called: the element it writes goes below this
    template: str
    params: dict[str, str]


def _scope(template: etree._Element) -> dict[str, str]:
    return {v.get('name'): v.get('select') or '' for v in template.iter(f'{X}variable') if v.get('select')}


def resolve(expr: str, scope: dict[str, str]) -> str:
    """The expression with its variables replaced by what they select (repeatedly: one may read another)."""
    for _ in range(10):
        found = False

        def swap(m: re.Match) -> str:
            nonlocal found
            name = m.group(1)
            if name in scope:
                found = True
                return f'({scope[name]})'
            return m.group(0)
        expr = re.sub(r'\$([\w.-]+)(?![\w.-]|\()', swap, expr)
        if not found:
            break
    return expr


def read(code: str) -> Reading:
    """The XSLT read the way the generator writes it."""
    root = etree.fromstring(strip(code).encode('utf-8'), etree.XMLParser(remove_blank_text=True))
    namespace = root.get('xpath-default-namespace') or ''
    templates = {t.get('mode') or t.get('name'): t for t in root.findall(f'{X}template') if t.get('mode') or t.get('name')}
    functions: dict[str, tuple[list[str], str]] = {}
    for fn in root.findall(f'{X}function'):
        body = [c for c in fn if isinstance(c.tag, str) and c.tag != f'{X}param']
        if len(body) == 1 and body[0].tag == f'{X}sequence' and body[0].get('select'):
            functions[fn.get('name')] = ([p.get('name') for p in fn.findall(f'{X}param')], body[0].get('select'))
    maps, globals_ = {}, {}
    for var in root.findall(f'{X}variable'):
        entries = var.find(f'{X}map')
        if entries is not None:
            maps[var.get('name')] = {unquote(e.get('key')): unquote(e.get('select')) for e in entries.findall(f'{X}map-entry')}
        elif var.get('select'):
            globals_[var.get('name')] = var.get('select')
    start = next(t for t in root.findall(f'{X}template') if t.get('match') and not t.get('mode'))
    apply = start.find(f'.//{X}apply-templates')
    records = apply.get('select') if apply is not None else ''
    record_mode = apply.get('mode') if apply is not None else 'event'
    record = templates.get(record_mode)
    notes: list[str] = []
    if record_mode == 'event' and record is not None and record.find(f'{X}apply-templates[@mode="item"]') is not None:
        record = templates.get('item')
        notes.append('one record, several events (for_each): each item read as the record')
    nil = _NIL.search(code)
    reading = Reading(rules=[], namespace=namespace, root=start.get('match'), records=records,
                      nil_values=_strings(nil.group(1)) if nil else [], unmatched_warn=False, functions=functions,
                      maps=maps, globals=globals_, notes=notes,
                      imports=[i.get('href') for i in root.findall(f'{X}import') if i.get('href')],
                      prefixes={k: v for k, v in root.nsmap.items() if k and k not in _OWN_PREFIXES})
    if record is None:
        reading.notes.append('no record template: nothing to read')
        return reading
    headers = _rule_headers(root)
    record_scope = _scope(record)
    choose = record.find(f'{X}choose')
    branches = list(choose) if choose is not None else [record]
    for n, branch in enumerate(b for b in branches if isinstance(b.tag, str)):
        test = branch.get('test') if branch.tag == f'{X}when' else None
        rule = _branch(branch, n, test, record_scope, templates, headers)
        if rule is None:
            if any('stroom:log(\'WARN\'' in (s.get('select') or '') for s in branch.iter(f'{X}sequence')):
                reading.unmatched_warn = True
            continue
        reading.rules.append(rule)
    return reading


def _rule_headers(root: etree._Element) -> dict[str, str]:
    """Each rule template's rule name, from the comment the generator writes before it."""
    out, name = {}, None
    for node in root:
        if isinstance(node, etree._Comment):
            m = _RULE_HEADER.match(node.text or '')
            name = unquote("'" + m.group(1) + "'") if m else None
        elif isinstance(node.tag, str):
            if name and node.tag == f'{X}template':
                out[node.get('mode') or node.get('name')] = name
            name = None
    return out


def _branch(branch: etree._Element, n: int, test: str | None, scope: dict[str, str],
            templates: dict[str, etree._Element], headers: dict[str, str]) -> RuleReading | None:
    for node in branch:
        if isinstance(node, etree._Comment):
            m = _DROPPED.match(node.text or '')
            if m:
                return RuleReading(key=f'drop-{m.group(1)}', name=m.group(1), test=_resolved_test(test, scope), drop=True)
    call = next((c for c in branch if isinstance(c.tag, str) and c.tag in (f'{X}apply-templates', f'{X}call-template')), None)
    event = branch.find(f'{E}Event')
    if call is None and event is None:
        return None
    if call is not None:
        key = call.get('mode') or call.get('name')
        template = templates.get(key)
        if template is None:
            return RuleReading(key=key, name=headers.get(key), test=_resolved_test(test, scope),
                               notes=[f"its template {key} isn't in the XSLT"])
        rule = RuleReading(key=key, name=headers.get(key), test=_resolved_test(test, scope))
        for node in template:
            if isinstance(node, etree._Comment):
                m = _UNKNOWN.match(node.text or '')
                if m:
                    rule.unknown = m.group(2)
        event = template.find(f'{E}Event')
        own = _scope(template)
    else:
        rule = RuleReading(key=f'inline-{n}', name=None, test=_resolved_test(test, scope))
        for node in branch:
            if isinstance(node, etree._Comment):
                m = _UNKNOWN.match(node.text or '')
                if m:
                    rule.name, rule.unknown = m.group(1), m.group(2)
        own = scope
    if event is None:
        rule.notes.append('no Event written')
        return rule
    _walk(event, [], {**scope, **own}, templates, rule)
    return rule


def _resolved_test(test: str | None, scope: dict[str, str]) -> str | None:
    return resolve(test, scope) if test else None


def _walk(node: etree._Element, path: list[str], scope: dict[str, str], templates: dict[str, etree._Element],
          rule: RuleReading) -> None:
    for child in node:
        if not isinstance(child.tag, str):
            continue
        tag = child.tag
        written = len(rule.leaves)
        _read_child(child, tag, path, scope, templates, rule)
        for leaf in rule.leaves[written:]:
            if leaf.guard is None and rule.guards:
                leaf.guard = rule.guards[-1]


def _read_child(child: etree._Element, tag: str, path: list[str], scope: dict[str, str],
                templates: dict[str, etree._Element], rule: RuleReading) -> None:
    if True:
        if tag.startswith(E):
            name = tag[len(E):]
            if name == 'Data':
                _data_element(child, path, scope, rule)
                return
            here = path + [name]
            for attribute, value in child.attrib.items():
                if '}' not in attribute:
                    leaf = Leaf('/'.join(here + ['@' + attribute]))
                    _set_value(leaf, value, scope)
                    rule.leaves.append(leaf)
            text = (child.text or '').strip()
            if text and not any(isinstance(c.tag, str) for c in child):
                rule.leaves.append(Leaf('/'.join(here), value=text))
            else:
                _walk(child, here, scope, templates, rule)
        elif tag in (f'{X}if', f'{X}choose', f'{X}when', f'{X}otherwise'):
            if tag == f'{X}choose':
                rule.notes.append(f"{'/'.join(path) or 'Event'}: an xsl:choose inside the event, read as all its branches")
            if tag == f'{X}if':
                rule.guards.append(resolve(child.get('test') or '', scope))
            _walk(child, path, scope, templates, rule)
            if tag == f'{X}if':
                rule.guards.pop()
        elif tag == f'{X}value-of':
            rule.leaves.append(Leaf('/'.join(path), expr=resolve(child.get('select') or '', scope)))
        elif tag == f'{X}attribute':
            leaf = Leaf('/'.join(path + ['@' + child.get('name')]), expr=resolve(child.get('select') or '', scope))
            rule.leaves.append(leaf)
        elif tag in (f'{X}apply-templates', f'{X}call-template'):
            key = child.get('mode') or child.get('name')
            template = templates.get(key)
            if template is None and tag == f'{X}call-template':
                # A named template of an imported XSLT (a mapping's shared entry): what it writes is in that XSLT.
                rule.calls.append(SharedCall('/'.join(path), key, {
                    w.get('name'): w.get('select') or '' for w in child.findall(f'{X}with-param') if w.get('name')}))
            elif template is None:
                rule.notes.append(f"{'/'.join(path)}: template {key} isn't in the XSLT")
            else:
                _walk(template, path, {**scope, **_scope(template)}, templates, rule)
        elif tag == f'{X}sequence':
            select = child.get('select') or ''
            if select.startswith('mcp:') and '(' in select and _is_data_call(select):
                name, expr = _data_call(select)
                rule.leaves.append(Leaf('/'.join(path + ['Data']), data_name=name, expr=resolve(expr, scope)))
            elif not select.startswith('stroom:log('):
                rule.notes.append(f"{'/'.join(path)}: xsl:sequence {select[:80]} not read")
        elif tag == f'{X}for-each':
            rule.leaves.append(Leaf('/'.join(path + [_first_element(child)]), repeat=canonical(
                etree.tostring(child, encoding='unicode'))))
        elif tag == f'{X}variable':
            return
        else:
            rule.notes.append(f"{'/'.join(path)}: {etree.QName(tag).localname} not read")


def _first_element(loop: etree._Element) -> str:
    found = next((c for c in loop.iter() if isinstance(c.tag, str) and c.tag.startswith(E)), None)
    return found.tag[len(E):] if found is not None else '?'


def _is_data_call(select: str) -> bool:
    return bool(re.match(r'mcp:[\w-]+\(\s*(?:' + _STRING + r')\s*,', select))


def _data_call(select: str) -> tuple[str, str]:
    inner = select[select.index('(') + 1:select.rindex(')')]
    name, expr = _top_split(inner, ',')[0], ','.join(_top_split(inner, ',')[1:])
    return unquote(name), expr.strip()


def _data_element(element: etree._Element, path: list[str], scope: dict[str, str], rule: RuleReading) -> None:
    leaf = Leaf('/'.join(path + ['Data']), data_name=element.get('Name'))
    attribute = element.find(f'{X}attribute[@name="Value"]')
    if attribute is not None:
        leaf.expr = resolve(attribute.get('select') or '', scope)
    else:
        _set_value(leaf, element.get('Value') or '', scope)
    rule.leaves.append(leaf)


def _set_value(leaf: Leaf, template_text: str, scope: dict[str, str]) -> None:
    """An attribute value template: a constant (braces doubled), or one expression in braces."""
    whole = re.fullmatch(r'\{(?!\{)(.*)\}', template_text, re.S)
    if whole and '{' not in whole.group(1).replace('{{', '') and '}' not in whole.group(1).replace('}}', ''):
        leaf.expr = resolve(whole.group(1), scope)
    else:
        leaf.value = template_text.replace('{{', '{').replace('}}', '}')


# --- from a reading to a mapping ---------------------------------------------------------------------------------

@dataclass
class Rebuilt:
    mapping: TranslationMapping | None
    reused: int = 0
    new: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)        # kept as the expression itself (an xpath entry)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {k: v for k, v in {'entries_kept': self.reused, 'entries_new': self.new, 'entries_removed': self.removed,
                                  'kept_as_xpath': self.raw, 'not_read': self.notes, 'problems': self.problems}.items()
                if v or k == 'entries_kept'}


def _e(n: int) -> str:
    """A slot for an expression, in what a renderer writes."""
    return f'QXE{n}QX'


def _l(n: int) -> str:
    """A slot for a string, which the renderer writes as a literal."""
    return f'QXL{n}QX'


_SLOT = re.compile(r"'QXL(\d+)QX'|QXE(\d+)QX")


def shape(rendered: str) -> re.Pattern:
    """A pattern for what a generator function writes, made by calling it with slots and normalising the result as
    the XSLT's expressions are (canonical): a group E<n> for each expression slot (the last as long as it can be, the
    others as short), L<n> for each string. So the reader follows the generator: the shapes come from its own code."""
    text = canonical(rendered)
    slots = list(_SLOT.finditer(text))
    out, at = '', 0
    for n, m in enumerate(slots):
        out += re.escape(text[at:m.start()])
        out += (f'(?P<L{m.group(1)}>{_STRING})' if m.group(1) else
                f"(?P<E{m.group(2)}>.*{'' if n == len(slots) - 1 else '?'})")
        at = m.end()
    return re.compile(out + re.escape(text[at:]), re.S)


SHAPES = {
    'lookup': shape(lookup_text(_l(1), key_text(_e(1)))),
    'lookup below': shape(lookup_text(_l(1), key_text(_e(1)), _e(2))),
    'dictionary': shape(dictionary_lookup_text(_e(1), key_text(_e(2)))),
    'dictionary map': shape(dictionary_text(_l(1), 'map')),
    'dictionary list': shape(dictionary_text(_l(1), 'list')),
    'any of': shape(any_of_text([_e(1)])),
    'has value': shape(has_value_text(_e(1))),
    'map lookup': shape(map_lookup_text('$' + _e(1), _e(2), None)),
    'map lookup, default': shape(map_lookup_text('$' + _e(1), _e(2), _l(1))),
    'map step': shape(map_step_text(_e(1), _l(1), _l(2), _e(2))),
    'default': shape(default_text(_e(1), _e(2), _l(1))),
    'group': shape(group_text(f'({parts_text(_e(1), _l(1), None)})', _e(2))),
    'group, flags': shape(group_text(f'({parts_text(_e(1), _l(1), _l(2))})', _e(2))),
    'keyed': shape(keyed_text(_l(1), _l(2), None)),
    'keyed, flags': shape(keyed_text(_l(1), _l(2), _l(3))),
    'equals': shape(equals_text(_e(1), _l(1))),
    'one of': shape(one_of_text(_e(1), [_l(1)])),
    'matches': shape(matches_text(_e(1), _l(1))),
    'in dictionary': shape(in_dictionary_text(_e(1), _e(2))),
    'time': shape(time_expr(_l(1), None, f'{_e(1)}[1]')),
    'time, zone': shape(time_expr(_l(1), _l(2), f'{_e(1)}[1]')),
    'epoch_ms': shape(time_expr('epoch_ms', None, f'{_e(1)}[1]')),
    'epoch_s': shape(time_expr('epoch_s', None, f'{_e(1)}[1]')),
}
# one_of_text's shape, its list of keys any length: the guard of a value map written without a default.
KEYS = re.compile(SHAPES['one of'].pattern.replace(f'(?P<L1>{_STRING})', f'(?P<L1>{_STRING}(?:, {_STRING})*)'), re.S)
TRANSFORMS = {t: shape(transform_expr(t, f'{_e(1)}[1]')) for t in ('lower', 'upper', 'trim', 'strip_domain', 'domain', 'digits')}


def match(name: str, text: str) -> re.Match | None:
    return SHAPES[name].fullmatch(canonical(text))


class _Idioms:
    """An expression read for what the generator writes (its shapes, from the generator's own functions): which
    input it reads, and how."""

    def __init__(self, reading: Reading, kind: str, extracts: list[dict[str, Any]]):
        self.r, self.kind = reading, kind
        self.extracts = extracts            # the mapping's extract list, added to as extracted values are read
        # Dictionaries: the stylesheet variables holding them, by kind.
        self.dictionaries: dict[str, tuple[str, str]] = {}
        for name, select in reading.globals.items():
            for kind_ in ('map', 'list'):
                m = match(f'dictionary {kind_}', select)
                if m:
                    self.dictionaries[name] = (unquote(m.group('L1')), kind_)

    # Inputs
    def field_of(self, expr: str) -> str | None:
        text = re.sub(r'\[1\]$', '', canonical(expr))
        if self.kind == 'json':
            m = re.fullmatch(r'((?:\*\[@key=' + _STRING + r'\]/?)+)', text)
            if m:
                return '.'.join(unquote(k) for k in re.findall(_STRING, m.group(1)))
        elif self.kind == 'data_splitter':
            m = re.fullmatch(r'data\[@name=(' + _STRING + r')\]/@value', text)
            if m:
                return unquote(m.group(1))
        return self.extracted(text)

    def extracted(self, text: str) -> str | None:
        """A value an extraction gives: a key=value function's (key="..." in a text field) or an analyze-string group."""
        m = re.fullmatch(r'mcp:([\w-]+)\((.*), (' + _STRING + r')\)', text)
        if m and m.group(1) in getattr(self.r, 'keyed', {}):
            before, after, flags = self.r.keyed[m.group(1)]
            # A rule's condition passes the text as normalize-space(...) when the mapping has no nil values (the
            # generator's own form there, which regenerating writes again).
            plain = re.fullmatch(r'normalize-space\((.*)\)', m.group(2).strip())
            source = self.field_of(plain.group(1) if plain else m.group(2))
            if source:
                key = unquote(m.group(3))
                return self._extraction(source, before + key + after, flags, [key])[0]
        for name in ('group, flags', 'group'):
            g = SHAPES[name].fullmatch(text)
            if g and g.group('E2').isdigit():
                source = self.field_of(g.group('E1'))
                if source:
                    regex, flags = unquote(g.group('L1')), unquote(g.group('L2')) if name == 'group, flags' else None
                    groups = max(1, len(re.findall(r'\((?!\?)', regex)))
                    base = re.sub(r'\W+', '_', source).strip('_') or 'text'
                    names = self._extraction(source, regex, flags, [f'{base}_{n}' for n in range(1, groups + 1)])
                    nr = int(g.group('E2'))
                    while len(names) < nr:
                        names.append(f"{names[0]}_{len(names) + 1}")
                    return names[nr - 1] or None
        return None

    def _extraction(self, source: str, regex: str, flags: str | None, names: list[str]) -> list[str]:
        """The extract entry for this regex on this field (the kept one, or a new one), and its names."""
        entry = next((e for e in self.extracts if e['regex'] == regex and e.get('field') == source), None)
        if entry is None:
            entry = {'field': source, 'regex': regex, 'names': names, **({'flags': flags} if flags else {})}
            self.extracts.append(entry)
        return entry['names']

    def source_of(self, expr: str) -> dict[str, Any] | None:
        """An input, as a mapping entry gives it: a field, the first of several (any_of), a reference data lookup, a
        dictionary value."""
        field_name = self.field_of(expr)
        if field_name:
            return {'field': field_name}
        # A single-valued input written where only its having a value matters (a map key, say): the input itself.
        m = match('has value', expr)
        if m and not m.group('E1').endswith('[1]') and self.kind in ('data_splitter', 'json') and not self.r.nil_values:
            field_name = self.field_of(m.group('E1'))
            if field_name:
                return {'field': field_name}
        m = match('any of', expr)
        if m:
            parts = _top_split(_unwrap(m.group('E1')), ',')
            fields = [self.field_of(p) for p in parts]
            if len(parts) > 1 and all(fields):
                return {'any_of': fields}
        for name in ('lookup below', 'lookup'):
            m = match(name, expr)
            if m:
                key = self.field_of(m.group('E1'))
                lookup: dict[str, Any] = {'map': unquote(m.group('L1')),
                                          **({'field': key} if key else {'xpath': self.portable(_unwrap(m.group('E1')))})}
                if name == 'lookup below':
                    lookup['path'] = '/'.join(step[2:] if step.startswith('*:') else step
                                              for step in m.group('E2').split('/'))
                return {'lookup': lookup}
        m = match('dictionary', expr)
        if m and m.group('E1') in self.dictionaries and self.dictionaries[m.group('E1')][1] == 'map':
            inner = self.source_of(m.group('E2'))
            if inner and set(inner) <= {'field', 'any_of'}:
                return {**inner, 'dictionary': self.dictionaries[m.group('E1')][0]}
        return None

    # Values
    def entry(self, leaf: Leaf) -> tuple[dict[str, Any], bool]:
        """The entry writing this element, and whether it had to keep the expression as it is (an xpath entry)."""
        base: dict[str, Any] = {'path': leaf.path, **({'data_name': leaf.data_name} if leaf.data_name else {})}
        if leaf.value is not None:
            return {**base, 'value': leaf.value}, False
        expr = self.inline(canonical(leaf.expr or ''))
        found = self.idiom(expr, leaf.guard)
        if found:
            return {**base, **found}, False
        return {**base, 'xpath': self.portable(expr)}, True

    def idiom(self, expr: str, guard: str | None = None) -> dict[str, Any] | None:
        found = self.source_of(expr)
        if found:
            return found
        m = match('default', expr)
        if m:
            inner = self.idiom(m.group('E2'))
            has = canonical(m.group('E1'))
            source = self.source_of(re.sub(r'^(?:normalize-space|exists)\((.*)\)$', r'\1', has))
            if inner and source and all(inner.get(k) == v for k, v in source.items()) and 'default' not in inner:
                return {**inner, 'default': unquote(m.group('L1'))}
        mapped = self.inline_map(expr, guard)
        if mapped:
            return mapped
        for name in ('map lookup, default', 'map lookup'):
            m = match(name, expr)
            if m and m.group('E1') in self.r.maps:
                inner = self.converted(m.group('E2'))
                if inner:
                    return {**inner, 'map': self.r.maps[m.group('E1')],
                            **({'default': unquote(m.group('L1'))} if name == 'map lookup, default' else {})}
        for name in ('time, zone', 'time', 'epoch_ms', 'epoch_s'):
            m = match(name, expr)
            if m:
                inner = self.converted(m.group('E1'))
                if inner:
                    fmt = name if name.startswith('epoch') else unquote(m.group('L1'))
                    return {**inner, 'time_format': fmt, **({'timezone': unquote(m.group('L2'))} if name == 'time, zone' else {})}
        return self.converted(expr)

    def converted(self, expr: str) -> dict[str, Any] | None:
        """An input, transformed or not."""
        found = self.source_of(expr)
        if found:
            return found
        for transform, pattern in TRANSFORMS.items():
            m = pattern.fullmatch(canonical(expr))
            if m:
                inner = self.source_of(m.group('E1'))
                if inner and set(inner) <= {'field', 'any_of'}:
                    return {**inner, 'transform': transform}
        return None

    def inline_map(self, expr: str, guard: str | None = None) -> dict[str, Any] | None:
        """A value map written inline (map_step_text, key after key): its keys, and its default or its last key."""
        pairs, rest, key = {}, canonical(expr), None
        while True:
            m = SHAPES['map step'].fullmatch(rest)
            if not m:
                break
            this = canonical(m.group('E1'))
            if key is not None and this != key:
                return None
            key = this
            pairs[unquote(m.group('L1'))] = unquote(m.group('L2'))
            rest = canonical(m.group('E2'))
        if not re.fullmatch(_STRING, rest):
            return None
        # Without a default the generator writes the last key's value as the final else, the element guarded by
        # the keys (one_of_text): that guard naming one key more than the steps is the last key, not a default. A
        # map of one key is then no steps at all: the value, guarded by its key.
        keys = KEYS.fullmatch(canonical(guard or ''))
        if keys and (key is None or canonical(keys.group('E1')) == key):
            key, listed = canonical(keys.group('E1')), _strings(keys.group('L1'))
            extra = [k for k in listed if k not in pairs]
            inner = self.converted(key)
            if inner and len(extra) == 1 and len(listed) == len(pairs) + 1:
                return {**inner, 'map': {**pairs, extra[0]: unquote(rest)}}
        if not pairs:
            return None
        inner = self.converted(key)
        return {**inner, 'map': pairs, 'default': unquote(rest)} if inner else None

    def inline(self, expr: str) -> str:
        """The XSLT's own single-expression functions replaced by their bodies (mcp:data aside)."""
        for _ in range(5):
            changed = False
            for name, (params, body) in self.r.functions.items():
                at = expr.find(name + '(')
                while at != -1 and not (at and (expr[at - 1].isalnum() or expr[at - 1] in ':-_')):
                    end, depth = at + len(name), 0
                    for i in range(end, len(expr)):
                        depth += expr[i] == '('
                        depth -= expr[i] == ')'
                        if depth == 0:
                            end = i
                            break
                    args = _top_split(expr[at + len(name) + 1:end], ',')
                    if len(args) != len(params):
                        break
                    replaced = body
                    for param, arg in zip(params, args):
                        replaced = re.sub(r'\$' + re.escape(param) + r'(?![\w.-])', lambda _m, a=arg: f'({a})', replaced)
                    expr = expr[:at] + f'({replaced})' + expr[end + 1:]
                    changed = True
                    at = expr.find(name + '(')
            if not changed:
                break
        return canonical(expr)

    def portable(self, expr: str) -> str:
        """An expression that reads the same in a regenerated XSLT: its stylesheet variables written in, and JSON
        read through json-to-xml() (the generator puts its guarded helper back)."""
        for name, entries in self.r.maps.items():
            literal_map = 'map{' + ', '.join(f'{literal(k)}: {literal(v)}' for k, v in entries.items()) + '}'
            expr = re.sub(r'\$' + re.escape(name) + r'(?![\w.-])', lambda _m: literal_map, expr)
        expr = resolve(expr, self.r.globals)
        return expr.replace('mcp:json-to-xml(', 'json-to-xml(')

    # Conditions
    def conditions(self, test: str) -> tuple[list[dict[str, Any]], bool]:
        out, raw = [], False
        for part in _top_split(_unwrap(test), ' and '):
            part = canonical(part)
            negated = re.fullmatch(r'not\((.*)\)', part)
            body = canonical(negated.group(1)) if negated else part
            found = None
            if not negated:
                m = KEYS.fullmatch(body)
                if m and self.field_of(m.group('E1')):
                    found = {'field': self.field_of(m.group('E1')), 'one_of': _strings(m.group('L1'))}
            for name in ('equals', 'matches', 'in dictionary'):
                m = None if found or negated else SHAPES[name].fullmatch(body)
                if m and self.field_of(m.group('E1')):
                    if name == 'equals':
                        found = {'field': self.field_of(m.group('E1')), 'equals': unquote(m.group('L1'))}
                    elif name == 'matches':
                        found = {'field': self.field_of(m.group('E1')), 'matches': unquote(m.group('L1'))}
                    elif m.group('E2').lstrip('$') in self.dictionaries:
                        found = {'field': self.field_of(m.group('E1')),
                                 'in_dictionary': self.dictionaries[m.group('E2').lstrip('$')][0]}
            if not found:
                m = re.fullmatch(r'exists\((.*)\)', body) or re.fullmatch(r'normalize-space\((.*)\)', body)
                inner = m.group(1) if m else body
                if self.field_of(inner):
                    found = {'field': self.field_of(inner), 'present': not negated}
            if not found:
                found = {'xpath': f'self::*[{self.portable(part)}]', 'present': True}
                raw = True
            out.append(found)
        return out, raw


def _effective(mapping: TranslationMapping) -> list[dict[tuple[str, str | None], FieldMapping]]:
    """Per rule, the entries it writes: the common ones, then its own over them."""
    out = []
    for rule in mapping.events:
        fields = {(e.path.strip('/'), e.data_name): e for e in mapping.common}
        fields.update({(e.path.strip('/'), e.data_name): e for e in rule.fields})
        out.append(fields)
    return out


def _keyed_shapes(code: str) -> dict[str, tuple[str, str, str]]:
    """The XSLT's key=value functions (keyed_text): name -> (regex before the key, after it, flags)."""
    out = {}
    root = etree.fromstring(strip(code).encode('utf-8'))
    for fn in root.findall(f'{X}function'):
        body = fn.find(f'{X}sequence')
        if body is None or not (fn.get('name') or '').startswith('mcp:'):
            continue
        for name in ('keyed, flags', 'keyed'):
            m = SHAPES[name].fullmatch(canonical(body.get('select') or ''))
            if m:
                out[fn.get('name')[4:]] = (unquote(m.group('L1')), unquote(m.group('L2')),
                                           unquote(m.group('L3')) if name == 'keyed, flags' else '')
                break
    return out


def _imported(reading: Reading, imported: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """The mapping's shared and functions entries, from the XSLT's imports and the imported XSLTs themselves (by
    name, as Stroom resolves them): which defines each named template called, and the element it writes; which
    declares each bound prefix's namespace."""
    problems: list[str] = []
    parsed: dict[str, etree._Element] = {}
    for href in reading.imports:
        try:
            parsed[href] = etree.fromstring(strip(imported[href]).encode('utf-8'))
        except (KeyError, etree.XMLSyntaxError):
            continue

    def home_of(template: str) -> tuple[str | None, etree._Element | None]:
        for href, root in parsed.items():
            found = root.find(f"{X}template[@name='{template}']")
            if found is not None:
                return href, found
        return (reading.imports[0], None) if len(reading.imports) == 1 else (None, None)
    shared, seen = [], set()
    for rule in reading.rules:
        for call in rule.calls:
            if (call.template, call.path) in seen:
                continue
            seen.add((call.template, call.path))
            href, template = home_of(call.template)
            first = next((c for c in template.iter() if isinstance(c.tag, str) and c.tag.startswith(E)), None) \
                if template is not None else None
            if not href or first is None:
                problems.append(f"template {call.template}, called at {call.path or 'Event'}: the XSLT it is imported "
                                f"from couldn't be read, so the element it writes isn't known")
                continue
            entry = {'href': href, 'template': call.template,
                     'at': '/'.join(p for p in (call.path, first.tag[len(E):]) if p)}
            if call.params:
                entry['with_params'] = call.params
            shared.append(entry)
    functions = []
    for prefix, namespace in reading.prefixes.items():
        href = next((h for h, root in parsed.items() if namespace in (root.nsmap or {}).values()), None) \
            or (reading.imports[0] if len(reading.imports) == 1 else None)
        if href:
            functions.append({'href': href, 'prefix': prefix, 'namespace': namespace})
        else:
            problems.append(f"prefix {prefix} ({namespace}): no imported XSLT read declares it")
    return shared, functions, problems


def rebuild(code: str, kept: TranslationMapping | None, kept_code: str | None,
            imported: dict[str, str] | None = None) -> Rebuilt:
    """A mapping for this XSLT: the kept mapping where the XSLT still reads as it generates (kept_code, what it
    generates now), the generator's idioms read from the XSLT elsewhere."""
    reading = read(code)
    reading.keyed = _keyed_shapes(code)                       # type: ignore[attr-defined]
    # Key=value functions are read as extractions, not written into the expression.
    for name in reading.keyed:
        reading.functions.pop(f'mcp:{name}', None)
    out = Rebuilt(mapping=None, notes=list(reading.notes))
    kind = kept.input if kept else next((k for k, ns in INPUT_NAMESPACE.items() if ns == reading.namespace), 'xml')
    if kind == 'xml' and reading.records.startswith('*/'):
        kind = 'xml_fragments'
    extracts = [e.model_dump(exclude_none=True, exclude_defaults=True) for e in kept.extract] if kept else []
    idioms = _Idioms(reading, kind, extracts)
    before: dict[str, tuple[RuleReading, Any, dict]] = {}
    if kept and kept_code:
        base = read(kept_code)
        effective = _effective(kept)
        for n, rule in enumerate(base.rules):
            if n < len(kept.events):
                before[rule.key] = (rule, kept.events[n], effective[n])
    rules_out: list[dict[str, Any]] = []
    per_rule: list[list[tuple[tuple, dict[str, Any]]]] = []
    for rule in reading.rules:
        out.notes += [f"rule {rule.name or rule.key}: {note}" for note in rule.notes]
        old = before.get(rule.key)
        name = (old[1].name if old else None) or rule.name or re.sub(r'^event[_-]type[_-]', '', rule.key).replace('_', '-')
        spec: dict[str, Any] = {'name': name}
        if old and _same_test(old[0].test, rule.test):
            spec['when'] = [c.model_dump(exclude_none=True) for c in old[1].when]
        elif rule.test:
            spec['when'], raw = idioms.conditions(rule.test)
            if raw:
                out.raw.append(f"rule {name}: its condition")
        if rule.drop:
            spec['drop'] = True
            rules_out.append(spec)
            per_rule.append([])
            continue
        if old:
            for attr in ('allow_unknown', 'keep_unknown', 'description'):
                value = getattr(old[1], attr, None)
                if value:
                    spec[attr] = value
        elif rule.unknown:
            spec.update(allow_unknown=rule.unknown, keep_unknown=True)
        entries: list[tuple[tuple, dict[str, Any]]] = []
        reused: set[int] = set()
        old_leaves = {leaf.key: leaf for leaf in old[0].leaves} if old else {}
        for leaf in rule.leaves:
            previous = old_leaves.get(leaf.key)
            if previous is not None and previous.form == leaf.form and leaf.key in old[2]:
                entries.append(((leaf.key, leaf.form), old[2][leaf.key].model_dump(exclude_none=True)))
                reused.add(len(entries) - 1)
                out.reused += 1
                continue
            if leaf.repeat:
                out.problems.append(f"rule {name}: {leaf.path} repeats (xsl:for-each) in a way no entry it had writes")
                continue
            entry, raw = idioms.entry(leaf)
            entries.append(((leaf.key, leaf.form), entry))
            where = f"rule {name}: {leaf.path}" + (f" Data {leaf.data_name}" if leaf.data_name else '')
            out.new.append(where)
            if raw:
                out.raw.append(where)
        if old:
            now = {leaf.key for leaf in rule.leaves}
            out.removed += [f"rule {name}: {k[0]}" + (f" Data {k[1]}" if k[1] else '') for k in old_leaves if k not in now]
            # The kept mapping's order (a rule's Data are written in it), each new entry just after the one it
            # follows in the XSLT: where a hand edit put it.
            order, at, places = {k: i for i, k in enumerate(old[2])}, -1.0, []
            for n, ((key, _), _) in enumerate(entries):
                if n in reused and key in order:
                    at = float(order[key])
                    places.append(at)
                else:
                    at += 1e-3
                    places.append(at)
            entries = [e for _, e in sorted(zip(places, entries), key=lambda pair: pair[0])]
        rules_out.append(spec)
        per_rule.append(entries)
    gone = [key for key in before if key not in {r.key for r in reading.rules}]
    out.removed += [f"rule {before[key][1].name} (no longer in the XSLT)" for key in gone]
    # Common: what every rule writing an event writes the same way, as the kept mapping had it where it can.
    written = [entries for spec, entries in zip(rules_out, per_rule) if not spec.get('drop')]
    common_keys: list[tuple] = []
    if written and (kept or len(written) > 1):      # one rule alone: its entries stay its own
        shared = set(k for k, _ in written[0])
        for entries in written[1:]:
            shared &= set(k for k, _ in entries)
        kept_common = {(e.path.strip('/'), e.data_name) for e in kept.common} if kept else None
        common_keys = [k for k, _ in written[0] if k in shared and not (k[0][1] and kept_common is None)
                       and (kept_common is None or k[0] in kept_common)]
    common = [entry for k, entry in (written[0] if written else []) if k in common_keys]
    if kept:
        position = {(e.path.strip('/'), e.data_name): i for i, e in enumerate(kept.common)}
        common.sort(key=lambda e: position.get((e['path'].strip('/'), e.get('data_name')), len(position)))
    for spec, entries in zip(rules_out, per_rule):
        if not spec.get('drop'):
            spec['fields'] = [entry for k, entry in entries if k not in common_keys]
    payload: dict[str, Any] = (kept.model_dump(exclude_none=True, exclude_defaults=True) if kept else {})
    payload.update(input=kind, common=common, events=rules_out, extract=idioms.extracts)
    if not kept and (reading.imports or reading.prefixes):
        shared, functions, problems = _imported(reading, imported or {})
        payload.update(**({'shared': shared} if shared else {}), **({'functions': functions} if functions else {}))
        out.problems += problems
    if not kept:
        if reading.nil_values:
            payload['nil_values'] = reading.nil_values
        payload['unmatched'] = 'warn' if reading.unmatched_warn else 'skip'
        if kind == 'json':
            layout = next((k for k, v in JSON_RECORDS.items() if v == reading.records), None)
            if layout:
                payload['json_layout'] = layout
            else:
                payload['record'] = reading.records
        elif kind not in ('data_splitter',) or reading.records != 'record':
            payload['record'] = reading.records
        if reading.root not in ('/', 'records'):
            payload['root'] = reading.root
    try:
        out.mapping = TranslationMapping.model_validate(payload)
    except Exception as e:
        out.problems.append(f"the mapping read back doesn't validate: {str(e)[:400]}")
    return out


def _same_test(a: str | None, b: str | None) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and canonical(a) == canonical(b))


__all__ = ['Leaf', 'Reading', 'Rebuilt', 'RuleReading', 'canonical', 'read', 'rebuild', 'resolve', 'MCP_NS',
           'Condition', 'time_expr']
