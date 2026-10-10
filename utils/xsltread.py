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

Expressions are parsed by elementpath (utils/xpathtree.py), not split with regular expressions (asked for by the
user), and read against shapes made by the generator's own functions called with slots, so the reader follows
whatever the generator writes. Either way the result is only a candidate: tools/rebuild.py regenerates the XSLT from
it and steps both over the sample in Stroom, and saves it only when every record's output is the same.
"""
import re
from dataclasses import dataclass, field
from typing import Any

from elementpath.exceptions import ElementPathError
from lxml import etree

from utils.xpathtree import (Node, XPathParser, expr_slot, first_of, is_nil_check, items, normal, parser_for, splice,
                             string_slot, strings, terms, unbracketed, unify)
from utils.xsltgen import (EVT, INPUT_NAMESPACE, JSON_RECORDS, MCP_NS, XSL, Condition, FieldMapping, TranslationMapping,
                           any_of_text, default_text, dictionary_lookup_text, dictionary_text, equals_text, field_text,
                           group_text, has_value_text, in_dictionary_text, key_text, keyed_text, literal, lookup_text,
                           map_lookup_text, map_step_text, matches_text, one_of_text, parts_text, time_expr,
                           transform_expr)
from utils.xsltversion import strip

X = f'{{{XSL}}}'
E = f'{{{EVT}}}'
# The generator's own comments (not XPath): a rule's name, a kind left untranslated, one left Unknown on purpose.
_RULE_HEADER = re.compile(r"^\s*Rule '((?:[^']|'')*)'", re.S)
_DROPPED = re.compile(r"^\s*(.+?): left untranslated on purpose\s*$", re.S)
_UNKNOWN = re.compile(r"^\s*(.+?): EventDetail/Unknown on purpose: (.*?)\s*$", re.S)
_PARSE_ERRORS = (ElementPathError, ValueError, TypeError)


def _parse(text: str | None, xp: XPathParser) -> Node | None:
    """The expression parsed, or None when it isn't one elementpath reads (it is then kept as written)."""
    if not text or not text.strip():
        return None
    try:
        return xp.parse(text)
    except _PARSE_ERRORS:
        return None


def _key(text: str | None, xp: XPathParser) -> tuple:
    """What an expression is, whatever its spacing and the brackets around it: for telling one from another."""
    node = _parse(text, xp)
    return normal(node).key if node is not None else ('text', ' '.join((text or '').split()))


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

    def form(self, xp: XPathParser) -> tuple:
        return ('value', self.value) if self.value is not None else ('repeat', self.repeat) if self.repeat else \
            ('expr', _key(self.expr, xp))


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
    xp: XPathParser                                   # its expressions' parser (its namespace bindings)
    notes: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # xsl:import hrefs, in order
    prefixes: dict[str, str] = field(default_factory=dict)     # namespaces bound for imported functions
    keyed: dict[str, tuple[str, str, str]] = field(default_factory=dict)   # key=value functions: before, after, flags


# The prefixes the generator binds for itself; any other is an imported XSLT's functions (a mapping's functions),
# unless the XSLT defines functions with it (a hand edit's own, local:x say).
_OWN_PREFIXES = {'xsl', 'xsi', 'stroom', 'xs', 'fn', 'map', 'mcp'}


@dataclass
class SharedCall:
    path: str                         # where it is called: the element it writes goes below this
    template: str
    params: dict[str, str]


def _scope(template: etree._Element) -> dict[str, str]:
    return {v.get('name'): v.get('select') or '' for v in template.iter(f'{X}variable') if v.get('select')}


def _param_value(param: etree._Element) -> str:
    """What an xsl:param or xsl:with-param gives, as an expression: its select, else its text as a string (an
    xsl:param with neither is the empty string)."""
    if param.get('select'):
        return param.get('select')
    if len(param) == 0:
        return literal(param.text or '')
    return ''       # content of elements: not an expression; left unread


def _params(template: etree._Element, call: etree._Element, scope: dict[str, str], xp: XPathParser) -> dict[str, str]:
    """A called template's parameters: what the call passes (read in the caller's scope), else their defaults."""
    out = {p.get('name'): _param_value(p) for p in template.findall(f'{X}param') if p.get('name')}
    out.update({w.get('name'): resolve(_param_value(w), scope, xp)
                for w in call.findall(f'{X}with-param') if w.get('name')})
    return {k: v for k, v in out.items() if v}


def _variables(tree: Node) -> list[Node]:
    """An expression's variable references ($name): not the variables a for, let, some or every binds."""
    binding = set()
    for node in tree.walk():
        if node.symbol in ('for', 'let', 'some', 'every'):
            binding.update(id(c) for i, c in enumerate(node.children)
                           if c.symbol == '$' and i % 2 == 0 and i < len(node) - 1)
    return [n for n in tree.walk() if n.symbol == '$' and id(n) not in binding]


def resolve(expr: str, scope: dict[str, str], xp: XPathParser) -> str:
    """The expression with its variables replaced by what they select (repeatedly: one may read another)."""
    for _ in range(10):
        if not scope or '$' not in expr:
            return expr
        tree = _parse(expr, xp)
        if tree is None:
            return expr
        found = [(n, f'({scope[n.value]})') for n in _variables(tree) if n.value in scope]
        if not found:
            return expr
        expr = splice(expr, found)
    return expr


def _nil_values(root: etree._Element, xp: XPathParser) -> list[str]:
    """The mapping's nil values, from the predicate the generator leaves them out with."""
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        for attribute in ('select', 'test'):
            text = element.get(attribute) or ''
            if 'not(normalize-space(.)' not in text:
                continue
            tree = _parse(text, xp)
            for node in tree.walk() if tree is not None else []:
                if node.symbol == '[' and len(node) == 2 and is_nil_check(node[1]):
                    return strings(node[1][0][1]) or []
    return []


def read(code: str) -> Reading:
    """The XSLT read the way the generator writes it."""
    root = etree.fromstring(strip(code).encode('utf-8'), etree.XMLParser(remove_blank_text=True))
    xp = parser_for(root.nsmap)
    namespace = root.get('xpath-default-namespace') or ''
    templates = {t.get('mode') or t.get('name'): t for t in root.findall(f'{X}template') if t.get('mode') or t.get('name')}
    functions: dict[str, tuple[list[str], str]] = {}
    own_prefixes = set(_OWN_PREFIXES)
    for fn in root.findall(f'{X}function'):
        own_prefixes.add((fn.get('name') or '').partition(':')[0])
        body = [c for c in fn if isinstance(c.tag, str) and c.tag != f'{X}param']
        if len(body) == 1 and body[0].tag == f'{X}sequence' and body[0].get('select'):
            functions[fn.get('name')] = ([p.get('name') for p in fn.findall(f'{X}param')], body[0].get('select'))
    maps, globals_ = {}, {}
    for var in root.findall(f'{X}variable'):
        entries = var.find(f'{X}map')
        if entries is not None:
            maps[var.get('name')] = {}
            for entry in entries.findall(f'{X}map-entry'):
                key, value = strings(_parse(entry.get('key'), xp)), strings(_parse(entry.get('select'), xp))
                if key and value:
                    maps[var.get('name')][key[0]] = value[0]
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
    reading = Reading(rules=[], namespace=namespace, root=start.get('match'), records=records,
                      nil_values=_nil_values(root, xp), unmatched_warn=False, functions=functions,
                      maps=maps, globals=globals_, xp=xp, notes=notes,
                      imports=[i.get('href') for i in root.findall(f'{X}import') if i.get('href')],
                      prefixes={k: v for k, v in root.nsmap.items() if k and k not in own_prefixes})
    if record is None:
        reading.notes.append('no record template: nothing to read')
        return reading
    headers = _rule_headers(root)
    record_scope = _scope(record)
    choose = record.find(f'{X}choose')
    branches = list(choose) if choose is not None else [record]
    for n, branch in enumerate(b for b in branches if isinstance(b.tag, str)):
        test = branch.get('test') if branch.tag == f'{X}when' else None
        rule = _branch(branch, n, test, record_scope, templates, headers, xp)
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
            name = m.group(1).replace("''", "'") if m else None
        elif isinstance(node.tag, str):
            if name and node.tag == f'{X}template':
                out[node.get('mode') or node.get('name')] = name
            name = None
    return out


def _branch(branch: etree._Element, n: int, test: str | None, scope: dict[str, str],
            templates: dict[str, etree._Element], headers: dict[str, str], xp: XPathParser) -> RuleReading | None:
    for node in branch:
        if isinstance(node, etree._Comment):
            m = _DROPPED.match(node.text or '')
            if m:
                return RuleReading(key=f'drop-{m.group(1)}', name=m.group(1), test=_resolved_test(test, scope, xp),
                                   drop=True)
    call = next((c for c in branch if isinstance(c.tag, str) and c.tag in (f'{X}apply-templates', f'{X}call-template')), None)
    event = branch.find(f'{E}Event')
    if call is None and event is None:
        return None
    if call is not None:
        key = call.get('mode') or call.get('name')
        template = templates.get(key)
        if template is None:
            return RuleReading(key=key, name=headers.get(key), test=_resolved_test(test, scope, xp),
                               notes=[f"its template {key} isn't in the XSLT"])
        rule = RuleReading(key=key, name=headers.get(key), test=_resolved_test(test, scope, xp))
        for node in template:
            if isinstance(node, etree._Comment):
                m = _UNKNOWN.match(node.text or '')
                if m:
                    rule.unknown = m.group(2)
        event = template.find(f'{E}Event')
        own = _scope(template)
    else:
        rule = RuleReading(key=f'inline-{n}', name=None, test=_resolved_test(test, scope, xp))
        for node in branch:
            if isinstance(node, etree._Comment):
                m = _UNKNOWN.match(node.text or '')
                if m:
                    rule.name, rule.unknown = m.group(1), m.group(2)
        own = scope
    if event is None:
        rule.notes.append('no Event written')
        return rule
    _walk(event, [], {**scope, **own}, templates, rule, xp)
    return rule


def _resolved_test(test: str | None, scope: dict[str, str], xp: XPathParser) -> str | None:
    return resolve(test, scope, xp) if test else None


def _walk(node: etree._Element, path: list[str], scope: dict[str, str], templates: dict[str, etree._Element],
          rule: RuleReading, xp: XPathParser) -> None:
    for child in node:
        if not isinstance(child.tag, str):
            continue
        written = len(rule.leaves)
        _read_child(child, child.tag, path, scope, templates, rule, xp)
        for leaf in rule.leaves[written:]:
            if leaf.guard is None and rule.guards:
                leaf.guard = rule.guards[-1]


def _read_child(child: etree._Element, tag: str, path: list[str], scope: dict[str, str],
                templates: dict[str, etree._Element], rule: RuleReading, xp: XPathParser) -> None:
    if tag.startswith(E):
        name = tag[len(E):]
        if name == 'Data':
            _data_element(child, path, scope, rule, xp)
            return
        here = path + [name]
        for attribute, value in child.attrib.items():
            if '}' not in attribute:
                leaf = Leaf('/'.join(here + ['@' + attribute]))
                _set_value(leaf, value, scope, xp)
                rule.leaves.append(leaf)
        text = (child.text or '').strip()
        if text and not any(isinstance(c.tag, str) for c in child):
            rule.leaves.append(Leaf('/'.join(here), value=text))
        else:
            _walk(child, here, scope, templates, rule, xp)
    elif tag in (f'{X}if', f'{X}choose', f'{X}when', f'{X}otherwise'):
        if tag == f'{X}choose':
            rule.notes.append(f"{'/'.join(path) or 'Event'}: an xsl:choose inside the event, read as all its branches")
        if tag == f'{X}if':
            rule.guards.append(resolve(child.get('test') or '', scope, xp))
        _walk(child, path, scope, templates, rule, xp)
        if tag == f'{X}if':
            rule.guards.pop()
    elif tag == f'{X}value-of':
        rule.leaves.append(Leaf('/'.join(path), expr=resolve(child.get('select') or '', scope, xp)))
    elif tag == f'{X}attribute':
        leaf = Leaf('/'.join(path + ['@' + child.get('name')]), expr=resolve(child.get('select') or '', scope, xp))
        rule.leaves.append(leaf)
    elif tag in (f'{X}apply-templates', f'{X}call-template'):
        key = child.get('mode') or child.get('name')
        template = templates.get(key)
        if template is None and tag == f'{X}call-template':
            # A named template of an imported XSLT (a mapping's shared entry): what it writes is in that XSLT.
            rule.calls.append(SharedCall('/'.join(path), key, {
                w.get('name'): _param_value(w) for w in child.findall(f'{X}with-param') if w.get('name')}))
        elif template is None:
            rule.notes.append(f"{'/'.join(path)}: template {key} isn't in the XSLT")
        else:
            # The XSLT's own template (the generator's, or one added by hand): followed in place, its
            # parameters bound to what the call passes.
            _walk(template, path, {**scope, **_scope(template), **_params(template, child, scope, xp)}, templates,
                  rule, xp)
    elif tag == f'{X}sequence':
        select = child.get('select') or ''
        data = _data_call(select, xp)
        if data:
            rule.leaves.append(Leaf('/'.join(path + ['Data']), data_name=data[0], expr=resolve(data[1], scope, xp)))
        elif not select.startswith('stroom:log('):
            rule.notes.append(f"{'/'.join(path)}: xsl:sequence {select[:80]} not read")
    elif tag == f'{X}for-each':
        rule.leaves.append(Leaf('/'.join(path + [_first_element(child)]),
                                repeat=' '.join(etree.tostring(child, encoding='unicode').split())))
    elif tag == f'{X}variable':
        return
    else:
        rule.notes.append(f"{'/'.join(path)}: {etree.QName(tag).localname} not read")


def _first_element(loop: etree._Element) -> str:
    found = next((c for c in loop.iter() if isinstance(c.tag, str) and c.tag.startswith(E)), None)
    return found.tag[len(E):] if found is not None else '?'


def _data_call(select: str, xp: XPathParser) -> tuple[str, str] | None:
    """mcp:data('name', value): the generator's Data entry, its name and the expression for its value."""
    if not select.startswith('mcp:'):
        return None
    tree = _parse(select, xp)
    node = normal(tree) if tree is not None else None
    if node is None or node.symbol != 'call' or not str(node.value).startswith('mcp:') or len(node) != 2 \
            or normal(node[0]).symbol != '(string)':
        return None
    return str(normal(node[0]).value), node[1].text


def _data_element(element: etree._Element, path: list[str], scope: dict[str, str], rule: RuleReading,
                  xp: XPathParser) -> None:
    leaf = Leaf('/'.join(path + ['Data']), data_name=element.get('Name'))
    attribute = element.find(f'{X}attribute[@name="Value"]')
    if attribute is not None:
        leaf.expr = resolve(attribute.get('select') or '', scope, xp)
    else:
        _set_value(leaf, element.get('Value') or '', scope, xp)
    rule.leaves.append(leaf)


def _set_value(leaf: Leaf, template_text: str, scope: dict[str, str], xp: XPathParser) -> None:
    """An attribute value template (XSLT, not XPath): a constant (braces doubled), or one expression in braces."""
    whole = re.fullmatch(r'\{(?!\{)(.*)\}', template_text, re.S)
    if whole and '{' not in whole.group(1).replace('{{', '') and '}' not in whole.group(1).replace('}}', ''):
        leaf.expr = resolve(whole.group(1), scope, xp)
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
    imported_calls: list[str] = field(default_factory=list)   # xpath entries calling an imported XSLT's functions
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {k: v for k, v in {'entries_kept': self.reused, 'entries_new': self.new, 'entries_removed': self.removed,
                                  'kept_as_xpath': self.raw, 'calls_imported_functions': self.imported_calls,
                                  'not_read': self.notes, 'problems': self.problems}.items()
                if v or k == 'entries_kept'}


# Shapes: what the generator's functions write, called with slots (expr_slot for an expression, string_slot for a
# string) and parsed. Read against them with unify(), the reader follows the generator: one definition, not two
# that drift apart (asked for by the user).
_e, _l = expr_slot, string_slot
_SHAPE_PARSER = parser_for({'stroom': 'stroom', 'mcp': MCP_NS, 'fn': 'http://www.w3.org/2005/xpath-functions',
                            'map': 'http://www.w3.org/2005/xpath-functions/map', 'xs': 'http://www.w3.org/2001/XMLSchema'})


def shape(rendered: str) -> Node:
    return normal(_SHAPE_PARSER.parse(rendered))


def canonical(expr: str, namespaces: dict[str, str] | None = None) -> str:
    """An expression as the reader compares it (normal()), as written: for showing, and for tests."""
    return normal((parser_for(namespaces) if namespaces else _SHAPE_PARSER).parse(expr)).text


SHAPES = {
    'lookup': shape(lookup_text(_l(1), key_text(_e(1)))),
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
    'matches': shape(matches_text(_e(1), _l(1))),
    'in dictionary': shape(in_dictionary_text(_e(1), _e(2))),
    'time': shape(time_expr(_l(1), None, f'{_e(1)}[1]')),
    'time, zone': shape(time_expr(_l(1), _l(2), f'{_e(1)}[1]')),
    'epoch_ms': shape(time_expr('epoch_ms', None, f'{_e(1)}[1]')),
    'epoch_s': shape(time_expr('epoch_s', None, f'{_e(1)}[1]')),
}
# one_of_text, its values any number of strings: its shape with one value, the value's place taking any expression.
_ONE = shape(one_of_text(_e(1), [_l(1)]))
SHAPES['one of'] = Node(_ONE.symbol, _ONE.value, [_ONE[0], shape(_e(2))])
# A lookup below the value it finds, one to four steps down (lookup_text writes each step *:name).
LOOKUP_BELOW = [shape(lookup_text(_l(1), key_text(_e(1)), '/'.join(_e(n) for n in range(2, depth + 2))))
                for depth in range(1, 5)]
TRANSFORMS = {t: shape(transform_expr(t, f'{_e(1)}[1]')) for t in ('lower', 'upper', 'trim', 'strip_domain', 'domain', 'digits')}
# An input field, one to six levels deep (field_text): its names, joined as the mapping writes them.
FIELDS = {kind: [shape(field_text(kind, separator.join(_l(n) for n in range(1, depth + 1)))) for depth in range(1, 7)]
          for kind, separator in (('data_splitter', '/'), ('json', '.'))}
SEPARATOR = {'data_splitter': '/', 'json': '.'}


def _regex_groups(regex: str) -> int:
    """How many groups a regular expression has (Stroom's are Java's; Python reads the ones the generator writes)."""
    try:
        return max(1, re.compile(regex).groups)
    except re.error:
        return 1


def _call(node: Node, *names: str) -> Node | None:
    """name(x): x as written, when node is a call of one of these functions with one argument."""
    node = unbracketed(node)
    if node.symbol == 'call' and node.value in names and len(node) == 1:
        return node[0]
    return None


class _Idioms:
    """An expression read for what the generator writes (its shapes, from the generator's own functions): which
    input it reads, and how."""

    def __init__(self, reading: Reading, kind: str, extracts: list[dict[str, Any]]):
        self.r, self.kind, self.xp = reading, kind, reading.xp
        self.extracts = extracts            # the mapping's extract list, added to as extracted values are read
        # Dictionaries: the stylesheet variables holding them, by kind.
        self.dictionaries: dict[str, tuple[str, str]] = {}
        for name, select in reading.globals.items():
            node = _parse(select, self.xp)
            for kind_ in ('map', 'list'):
                m = unify(SHAPES[f'dictionary {kind_}'], node) if node is not None else None
                if m:
                    self.dictionaries[name] = (m.string(1), kind_)

    # Inputs
    def field_of(self, node: Node) -> str | None:
        node = normal(node)
        node = first_of(node) or node
        for depth, pattern in enumerate(FIELDS.get(self.kind, []), 1):
            m = unify(pattern, node)
            if m:
                return SEPARATOR[self.kind].join(m.string(n) for n in range(1, depth + 1))
        return self.extracted(node)

    def extracted(self, node: Node) -> str | None:
        """A value an extraction gives: a key=value function's (key="..." in a text field) or an analyze-string group."""
        function = str(node.value)[4:] if node.symbol == 'call' and str(node.value).startswith('mcp:') else None
        if function in self.r.keyed and len(node) == 2 and normal(node[1]).symbol == '(string)':
            before, after, flags = self.r.keyed[function]
            # A rule's condition passes the text as normalize-space(...) when the mapping has no nil values (the
            # generator's own form there, which regenerating writes again).
            text = _call(node[0], 'normalize-space') or node[0]
            source = self.field_of(text)
            if source:
                key = str(normal(node[1]).value)
                return self._extraction(source, before + key + after, flags, [key])[0]
        for name in ('group, flags', 'group'):
            m = unify(SHAPES[name], node)
            if m and normal(m.expr(2)).symbol == '(integer)':
                source = self.field_of(m.expr(1))
                if source:
                    regex, flags = m.string(1), m.string(2) if name == 'group, flags' else None
                    base = re.sub(r'\W+', '_', source).strip('_') or 'text'
                    names = self._extraction(source, regex, flags,
                                             [f'{base}_{n}' for n in range(1, _regex_groups(regex) + 1)])
                    nr = int(normal(m.expr(2)).value)
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

    def source_of(self, node: Node) -> dict[str, Any] | None:
        """An input, as a mapping entry gives it: a field, the first of several (any_of), a reference data lookup, a
        dictionary value."""
        field_name = self.field_of(node)
        if field_name:
            return {'field': field_name}
        # A single-valued input written where only its having a value matters (a map key, say): the input itself.
        m = unify(SHAPES['has value'], node)
        if m and first_of(m.expr(1)) is None and self.kind in ('data_splitter', 'json') and not self.r.nil_values:
            field_name = self.field_of(m.expr(1))
            if field_name:
                return {'field': field_name}
        m = unify(SHAPES['any of'], node)
        if m:
            parts = items(m.expr(1))
            fields = [self.field_of(p) for p in parts]
            if len(parts) > 1 and all(fields):
                return {'any_of': fields}
        for depth, pattern in [(0, SHAPES['lookup'])] + list(enumerate(LOOKUP_BELOW, 1)):
            m = unify(pattern, node)
            if m:
                key = self.field_of(m.expr(1))
                lookup: dict[str, Any] = {'map': m.string(1), **({'field': key} if key else
                                                                 {'xpath': self.portable(normal(m.expr(1)).text)})}
                if depth:
                    lookup['path'] = '/'.join(str(m.expr(n).value) for n in range(2, depth + 2))
                return {'lookup': lookup}
        m = unify(SHAPES['dictionary'], node)
        if m and m.expr(1).value in self.dictionaries and self.dictionaries[m.expr(1).value][1] == 'map':
            inner = self.source_of(m.expr(2))
            if inner and set(inner) <= {'field', 'any_of'}:
                return {**inner, 'dictionary': self.dictionaries[m.expr(1).value][0]}
        return None

    # Values
    def entry(self, leaf: Leaf) -> tuple[dict[str, Any], bool]:
        """The entry writing this element, and whether it had to keep the expression as it is (an xpath entry)."""
        base: dict[str, Any] = {'path': leaf.path, **({'data_name': leaf.data_name} if leaf.data_name else {})}
        if leaf.value is not None:
            return {**base, 'value': leaf.value}, False
        text = self.inline(leaf.expr or '')
        node = _parse(text, self.xp)
        if node is None:
            return {**base, 'xpath': text}, True
        found = self.idiom(node, leaf.guard)      # first: a map of one key is its value, guarded by the key
        if found:
            return {**base, **found}, False
        if normal(node).symbol == '(string)':
            return {**base, 'value': str(normal(node).value)}, False      # a string, passed as a parameter, say
        return {**base, 'xpath': self.portable(normal(node).text)}, True

    def idiom(self, node: Node, guard: str | None = None) -> dict[str, Any] | None:
        found = self.source_of(node)
        if found:
            return found
        m = unify(SHAPES['default'], node)
        if m:
            inner = self.idiom(m.expr(2))
            has = _call(m.expr(1), 'normalize-space', 'exists') or m.expr(1)
            source = self.source_of(has)
            if inner and source and all(inner.get(k) == v for k, v in source.items()) and 'default' not in inner:
                return {**inner, 'default': m.string(1)}
        mapped = self.inline_map(node, guard)
        if mapped:
            return mapped
        for name in ('map lookup, default', 'map lookup'):
            m = unify(SHAPES[name], node)
            if m and m.expr(1).value in self.r.maps:
                inner = self.converted(m.expr(2))
                if inner:
                    return {**inner, 'map': self.r.maps[m.expr(1).value],
                            **({'default': m.string(1)} if name == 'map lookup, default' else {})}
        for name in ('time, zone', 'time', 'epoch_ms', 'epoch_s'):
            m = unify(SHAPES[name], node)
            if m:
                inner = self.converted(m.expr(1))
                if inner:
                    fmt = name if name.startswith('epoch') else m.string(1)
                    return {**inner, 'time_format': fmt, **({'timezone': m.string(2)} if name == 'time, zone' else {})}
        return self.converted(node)

    def converted(self, node: Node) -> dict[str, Any] | None:
        """An input, transformed or not."""
        found = self.source_of(node)
        if found:
            return found
        for transform, pattern in TRANSFORMS.items():
            m = unify(pattern, node)
            if m:
                inner = self.source_of(m.expr(1))
                if inner and set(inner) <= {'field', 'any_of'}:
                    return {**inner, 'transform': transform}
        return None

    def keys_listed(self, node: Node | None) -> tuple[Node, list[str]] | None:
        """X = ('a', 'b'): the input and the keys (one_of_text)."""
        m = unify(SHAPES['one of'], node) if node is not None else None
        listed = strings(m.expr(2)) if m else None
        return (normal(m.expr(1)), listed) if listed else None

    def inline_map(self, node: Node, guard: str | None = None) -> dict[str, Any] | None:
        """A value map written inline (map_step_text, key after key): its keys, and its default or its last key."""
        pairs, rest, key = {}, normal(node), None
        while True:
            m = unify(SHAPES['map step'], rest)
            if not m:
                break
            this = normal(m.expr(1))
            if key is not None and this.key != key.key:
                return None
            key = this
            pairs[m.string(1)] = m.string(2)
            rest = normal(m.expr(2))
        if rest.symbol != '(string)':
            return None
        # Without a default the generator writes the last key's value as the final else, the element guarded by
        # the keys (one_of_text): that guard naming one key more than the steps is the last key, not a default. A
        # map of one key is then no steps at all: the value, guarded by its key.
        keys = self.keys_listed(_parse(guard, self.xp)) if guard else None
        if keys and (key is None or keys[0].key == key.key):
            key, listed = keys
            extra = [k for k in listed if k not in pairs]
            inner = self.converted(key)
            if inner and len(extra) == 1 and len(listed) == len(pairs) + 1:
                return {**inner, 'map': {**pairs, extra[0]: str(rest.value)}}
        if not pairs:
            return None
        inner = self.converted(key)
        return {**inner, 'map': pairs, 'default': str(rest.value)} if inner else None

    def inline(self, text: str) -> str:
        """The XSLT's own single-expression functions replaced by their bodies (mcp:data aside)."""
        for _ in range(5):
            tree = _parse(text, self.xp)
            if tree is None:
                return text
            calls = []
            for node in tree.walk():
                if node.symbol == 'call' and node.value in self.r.functions:
                    params, body = self.r.functions[node.value]
                    if len(params) == len(node):
                        bound = dict(zip(params, (arg.text for arg in node.children)))
                        calls.append((node, f'({resolve(body, bound, self.xp)})'))
            if not calls:
                break
            text = splice(text, calls)
        return text

    def portable(self, text: str) -> str:
        """An expression that reads the same in a regenerated XSLT: its stylesheet variables written in, and JSON
        read through json-to-xml() (the generator puts its guarded helper back)."""
        tree = _parse(text, self.xp)
        if tree is not None:
            maps = [(n, 'map{' + ', '.join(f'{literal(k)}: {literal(v)}' for k, v in self.r.maps[n.value].items()) + '}')
                    for n in _variables(tree) if n.value in self.r.maps]
            text = splice(text, maps) if maps else text
        text = resolve(text, self.r.globals, self.xp)
        tree = _parse(text, self.xp)
        if tree is not None:
            helper = [(n, n.text[len('mcp:'):]) for n in tree.walk()
                      if n.symbol == 'call' and n.value == 'mcp:json-to-xml']
            text = splice(text, helper) if helper else text
        return text

    # Conditions
    def conditions(self, test: str) -> tuple[list[dict[str, Any]], bool]:
        tree = _parse(test, self.xp)
        if tree is None:
            return [{'xpath': f'self::*[{test}]', 'present': True}], True
        out, raw = [], False
        for part in terms(tree, 'and'):
            negated = _call(part, 'not')
            body = unbracketed(negated) if negated is not None else part
            found = None
            if negated is None:
                found = self.comparison(body)
            if not found:
                inner = _call(body, 'exists', 'normalize-space') or body
                if self.field_of(inner):
                    found = {'field': self.field_of(inner), 'present': negated is None}
            if not found:
                found = {'xpath': f'self::*[{self.portable(part.text)}]', 'present': True}
                raw = True
            out.append(found)
        return out, raw

    def comparison(self, node: Node) -> dict[str, Any] | None:
        """A condition on one field: equal to a value (equals_text), one of several (one_of_text, its values in
        brackets), matching a regex, or in a dictionary."""
        m = unify(SHAPES['in dictionary'], node)
        if m and self.field_of(m.expr(1)) and m.expr(2).value in self.dictionaries:
            return {'field': self.field_of(m.expr(1)), 'in_dictionary': self.dictionaries[m.expr(2).value][0]}
        m = unify(SHAPES['matches'], node)
        if m and self.field_of(m.expr(1)):
            return {'field': self.field_of(m.expr(1)), 'matches': m.string(1)}
        # equals_text and one_of_text differ only by the brackets around the values: told apart as written.
        written = unbracketed(node)
        bracketed = written.symbol == '=' and len(written) == 2 and written[1].symbol == '('
        m = None if bracketed else unify(SHAPES['equals'], node)
        if m and self.field_of(m.expr(1)):
            return {'field': self.field_of(m.expr(1)), 'equals': m.string(1)}
        keys = self.keys_listed(node)
        if keys and self.field_of(keys[0]):
            return {'field': self.field_of(keys[0]), 'one_of': keys[1]}
        return None


def _effective(mapping: TranslationMapping) -> list[dict[tuple[str, str | None], FieldMapping]]:
    """Per rule, the entries it writes: the common ones, then its own over them."""
    out = []
    for rule in mapping.events:
        fields = {(e.path.strip('/'), e.data_name): e for e in mapping.common}
        fields.update({(e.path.strip('/'), e.data_name): e for e in rule.fields})
        out.append(fields)
    return out


def _keyed_shapes(code: str, xp: XPathParser) -> dict[str, tuple[str, str, str]]:
    """The XSLT's key=value functions (keyed_text): name -> (regex before the key, after it, flags)."""
    out = {}
    root = etree.fromstring(strip(code).encode('utf-8'))
    for fn in root.findall(f'{X}function'):
        body = fn.find(f'{X}sequence')
        if body is None or not (fn.get('name') or '').startswith('mcp:'):
            continue
        node = _parse(body.get('select'), xp)
        for name in ('keyed, flags', 'keyed'):
            m = unify(SHAPES[name], node) if node is not None else None
            if m:
                out[fn.get('name')[4:]] = (m.string(1), m.string(2), m.string(3) if name == 'keyed, flags' else '')
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
            found = next((t for t in root.findall(f'{X}template') if t.get('name') == template), None)
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


def _calls_imported(xpath: str | None, reading: Reading) -> bool:
    """Whether an expression calls a function of an XSLT it imports (one of the prefixes bound for them)."""
    tree = _parse(xpath, reading.xp)
    return tree is not None and any(n.symbol == 'call' and str(n.value).partition(':')[0] in reading.prefixes
                                    and ':' in str(n.value) for n in tree.walk())


def rebuild(code: str, kept: TranslationMapping | None, kept_code: str | None,
            imported: dict[str, str] | None = None) -> Rebuilt:
    """A mapping for this XSLT: the kept mapping where the XSLT still reads as it generates (kept_code, what it
    generates now), the generator's idioms read from the XSLT elsewhere."""
    reading = read(code)
    reading.keyed = _keyed_shapes(code, reading.xp)
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
    base_xp = reading.xp
    if kept and kept_code:
        base = read(kept_code)
        base_xp = base.xp
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
        if old and _same_test(old[0].test, rule.test, base_xp, reading.xp):
            spec['when'] = [c.model_dump(exclude_none=True) for c in old[1].when]
        elif rule.test:
            spec['when'], raw = idioms.conditions(rule.test)
            if raw:
                calls = any(_calls_imported(c.get('xpath'), reading) for c in spec['when'])
                (out.imported_calls if calls else out.raw).append(f"rule {name}: its condition")
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
            form = leaf.form(reading.xp)
            if previous is not None and previous.form(base_xp) == form and leaf.key in old[2]:
                entries.append(((leaf.key, form), old[2][leaf.key].model_dump(exclude_none=True)))
                reused.add(len(entries) - 1)
                out.reused += 1
                continue
            if leaf.repeat:
                out.problems.append(f"rule {name}: {leaf.path} repeats (xsl:for-each) in a way no entry it had writes")
                continue
            entry, raw = idioms.entry(leaf)
            entries.append(((leaf.key, form), entry))
            where = f"rule {name}: {leaf.path}" + (f" Data {leaf.data_name}" if leaf.data_name else '')
            out.new.append(where)
            if raw:
                # An imported XSLT's function is only called through xpath, so that entry is as the mapping had it.
                (out.imported_calls if _calls_imported(entry.get('xpath'), reading) else out.raw).append(where)
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


def _same_test(a: str | None, b: str | None, xp_a: XPathParser, xp_b: XPathParser) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and _key(a, xp_a) == _key(b, xp_b))


__all__ = ['Leaf', 'Reading', 'Rebuilt', 'RuleReading', 'canonical', 'read', 'rebuild', 'resolve', 'MCP_NS',
           'Condition', 'time_expr']
