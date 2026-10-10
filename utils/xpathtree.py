"""XPath expressions parsed by elementpath (a strict XPath 3.1 parser), for reading an XSLT back: asked for by the user
in place of splitting expressions with regular expressions.

Parsing only, nothing is evaluated: Stroom's functions (stroom:), the XSLT's own (mcp:, or a prefix added by hand)
and an imported XSLT's are registered as they are met, as functions of any arity whose body is never run. Stroom
remains what evaluates the XSLT (stepping). A prefix the stylesheet doesn't bind is an error, as it is in Stroom.

A parsed expression is a tree of Nodes, each holding its exact text in the expression (from the token positions
elementpath gives and its own lexer, for the brackets the tree leaves out): an expression read back is written as it
was, not re-serialised. normal() takes off what doesn't change what an expression reads for the reader (brackets
around one expression, the generator's has-a-value predicates), and unify() matches a tree against a pattern made by
the generator's own functions called with slots (utils/xsltread.py), binding each slot to what fills it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from elementpath.exceptions import ElementPathError
from elementpath.xpath31 import XPath31Parser
from elementpath.xpath_tokens import XPathFunction
from elementpath.xpath_tokens.maps import XPathMap

# Slots in a pattern: an expression (a name the generator never writes) and a string.
EXPR_SLOT = re.compile(r'QXE(\d+)QX')
STRING_SLOT = re.compile(r'QXL(\d+)QX')
_OPEN, _CLOSE = '([{', ')]}'


def expr_slot(n: int) -> str:
    return f'QXE{n}QX'


def string_slot(n: int) -> str:
    return f'QXL{n}QX'


def _not_evaluated(a=None, b=None, c=None, d=None, e=None, f=None, g=None, h=None, i=None, j=None):
    """A function met while parsing: any arity up to ten. elementpath folds calls of constants as it parses, so this
    gives an empty result rather than failing; nothing parsed here is evaluated for its value."""
    return None


@dataclass(eq=False)
class Node:
    symbol: str                       # elementpath's: '/', '[', '(name)', '(string)', '$', 'if'...; 'call' for a call
    value: object                     # a name, a string's value, a number, a function's name as written
    children: list['Node'] = field(default_factory=list)
    text: str = ''                    # exactly as written in the expression
    start: int = 0                    # where, in the expression parsed
    end: int = 0

    def __iter__(self):
        return iter(self.children)

    def __len__(self) -> int:
        return len(self.children)

    def __getitem__(self, n: int) -> 'Node':
        return self.children[n]

    @property
    def key(self) -> tuple:
        """What the expression is, whatever its spacing: compared to tell one expression from another."""
        return (self.symbol, self.value, tuple(c.key for c in self.children))

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class XPathParser:
    """Parses the expressions of one stylesheet, with its namespace bindings."""

    def __init__(self, namespaces: dict[str, str]):
        self.namespaces = {k: v for k, v in namespaces.items() if k}
        self._parser = XPath31Parser(namespaces=self.namespaces)
        self._registered: set[tuple[str, str]] = set()

    def parse(self, text: str) -> Node:
        for _ in range(200):
            try:
                token = self._parser.parse(text)
                break
            except ElementPathError as e:
                if not self._register(e, text):
                    raise
        else:
            raise ValueError(f'too many unknown functions in {text[:80]}')
        lexemes = [(m.start(), m.end(), m.group()) for m in self._parser.tokenizer.finditer(text) if m.group().strip()]
        return _node(token, text, lexemes)

    def _register(self, error: ElementPathError, text: str) -> bool:
        """A call of a function the parser doesn't know, with a prefix the stylesheet binds: registered (never run)."""
        token = getattr(error, 'token', None)
        if 'XPST0017' not in str(getattr(error, 'code', '')) or token is None:
            return False
        if token.symbol == ':' and ':' in str(token.value):
            prefix, local = str(token.value).split(':', 1)
        else:
            # A name the standard functions have too (mcp:data, stroom:format-date): the prefix is the lexeme before
            # the colon before it.
            before = [m.group() for m in self._parser.tokenizer.finditer(text[:token.span[0]]) if m.group().strip()]
            if len(before) < 2 or before[-1] != ':':
                return False
            prefix, local = before[-2], token.symbol
        if prefix not in self.namespaces:
            raise ElementPathError(f"prefix {prefix!r} isn't bound in the stylesheet (XPST0081)")
        if (prefix, local) in self._registered:
            return False
        self._parser.external_function(_not_evaluated, name=local, prefix=prefix)
        self._registered.add((prefix, local))
        return True


@lru_cache(maxsize=32)
def _cached(namespaces: tuple) -> XPathParser:
    return XPathParser(dict(namespaces))


def parser_for(namespaces: dict[str, str]) -> XPathParser:
    return _cached(tuple(sorted((k, v) for k, v in namespaces.items() if k)))


def _tokens(token):
    yield token
    children = list(token)
    if isinstance(token, XPathMap):
        children += list(getattr(token, '_values', []))
    for child in children:
        yield from _tokens(child)


def _span(token, text: str, lexemes: list[tuple[int, int, str]]) -> tuple[int, int]:
    """The token's whole expression in the text: its tokens' extent, with the brackets the tree leaves out."""
    spans = [t.span for t in _tokens(token) if getattr(t, 'span', None)]
    start, end = min(s for s, _ in spans), max(e for _, e in spans)
    before = [lx for lx in lexemes if lx[1] <= start]
    if isinstance(token, XPathMap) and before and before[-1][2] in ('map', 'array'):
        start = before[-1][0]
    elif isinstance(token, XPathFunction) and before and before[-1][2] == token.symbol             and text[start:start + 1] == '(':
        start = before[-1][0]         # elementpath gives a call first in a predicate the span of its bracket
    inside = [lx for lx in lexemes if start <= lx[0] < end]
    after = [lx for lx in lexemes if lx[0] >= end]
    depth = sum(lx[2] in _OPEN for lx in inside) - sum(lx[2] in _CLOSE for lx in inside)
    is_call = isinstance(token, XPathFunction) or (token.symbol == ':' and len(token) == 2
                                                   and isinstance(token[1], XPathFunction))
    if depth == 0 and is_call and not isinstance(token, XPathMap) and after and after[0][2] == '(':
        depth = 0                                          # a call with no arguments: name() taken whole below
        for lx in after:
            depth += (lx[2] in _OPEN) - (lx[2] in _CLOSE)
            end = lx[1]
            if depth == 0:
                break
        return start, end
    for lx in after:
        if depth <= 0:
            break
        depth += (lx[2] in _OPEN) - (lx[2] in _CLOSE)
        end = lx[1]
    return start, end


def _node(token, text: str, lexemes) -> Node:
    if isinstance(token, XPathMap):
        values = list(getattr(token, '_values', []))
        children = [c for pair in zip(list(token), values) for c in pair]
        node = Node('map', None, [_node(c, text, lexemes) for c in children])
    elif token.symbol == ':' and len(token) == 2 and isinstance(token[1], XPathFunction):
        node = Node('call', str(token.value), [_node(c, text, lexemes) for c in token[1]])
    elif isinstance(token, XPathFunction):
        node = Node('call', token.symbol, [_node(c, text, lexemes) for c in token])
    else:
        node = Node(token.symbol, token.value, [_node(c, text, lexemes) for c in token])
    node.start, node.end = _span(token, text, lexemes)
    node.text = text[node.start:node.end]
    return node


# --- what an expression reads, for the reader ---------------------------------------------------------------------

def _is_dot_check(node: Node) -> bool:
    """normalize-space(.)"""
    return node.symbol == 'call' and node.value == 'normalize-space' and len(node) == 1 and node[0].symbol == '.'


def is_nil_check(node: Node) -> bool:
    """not(normalize-space(.) = ('N/A', ...)): the generator's predicate leaving out a mapping's nil values."""
    return (node.symbol == 'call' and node.value == 'not' and len(node) == 1 and node[0].symbol == '='
            and _is_dot_check(node[0][0]))


def _text(node: Node, children: list[Node]) -> str:
    """The node's text with each child's replaced by the child's normal() text (what normal() took out, cut)."""
    out, at = '', node.start
    for old, new in sorted(zip(node.children, children), key=lambda pair: pair[0].start):
        if new.text != old.text and old.start >= at:
            out += node.text[at - node.start:old.start - node.start] + new.text
            at = old.end
    return out + node.text[at - node.start:]


def normal(node: Node) -> Node:
    """The expression as the reader compares it: brackets around one expression taken off, and the generator's
    has-a-value predicates ([normalize-space(.)], and the one leaving out nil values), which only decide whether an
    element is written."""
    while node.symbol == '(' and len(node) == 1:
        node = node[0]
    children = [normal(c) for c in node.children]
    if node.symbol == '[' and len(children) == 2 and (_is_dot_check(children[1]) or is_nil_check(children[1])):
        return children[0]
    if node.symbol == '(' and len(children) == 1:
        return children[0]
    text = _text(node, children)
    if node.symbol == '/' and len(children) == 2 and first_of(children[1]) is not None and children[1].symbol == '[':
        # a/b[1] read as (a/b)[1]: the generator writes {input}[1] for an input's first value, which binds to the
        # input's last step; for the reader both are the first value.
        path = Node('/', node.value, [children[0], children[1][0]], text[:text.rindex('[')], node.start, node.end)
        return Node('[', '[', [path, children[1][1]], text, node.start, node.end)
    return Node(node.symbol, node.value, children, text, node.start, node.end)


def items(node: Node) -> list[Node]:
    """A sequence's members: (a, b, c) as a, b, c."""
    node = normal(node)
    if node.symbol == ',':
        return [m for c in node.children for m in items(c)]
    return [node]


def unbracketed(node: Node) -> Node:
    """The node without brackets around it, otherwise as written (unlike normal(), which reads through more)."""
    while node.symbol == '(' and len(node) == 1:
        node = node[0]
    return node


def terms(node: Node, operator: str) -> list[Node]:
    """An expression's operands for one operator at the top, as written: a and b and c as a, b, c."""
    node = unbracketed(node)
    if node.symbol == operator and len(node) == 2:
        return terms(node[0], operator) + terms(node[1], operator)
    return [node]


def strings(node: Node) -> list[str] | None:
    """A string, or a sequence of strings: their values; None for anything else."""
    out = []
    for member in items(node):
        if member.symbol != '(string)':
            return None
        out.append(str(member.value))
    return out


def first_of(node: Node) -> Node | None:
    """X[1]: X."""
    node = normal(node)
    if node.symbol == '[' and len(node) == 2 and node[1].symbol == '(integer)' and node[1].value == 1:
        return node[0]
    return None


@dataclass
class Match:
    nodes: dict[str, Node] = field(default_factory=dict)

    def expr(self, n: int) -> Node:
        return self.nodes[f'E{n}']

    def string(self, n: int) -> str:
        return str(self.nodes[f'L{n}'].value)

    def has(self, name: str) -> bool:
        return name in self.nodes


def unify(pattern: Node, node: Node, found: Match | None = None) -> Match | None:
    """The node matched against a pattern (both as normal() leaves them): what fills each slot, or None."""
    found = found if found is not None else Match()
    return found if _unify(normal(pattern), normal(node), found) else None


def _unify(pattern: Node, node: Node, found: Match) -> bool:
    slot = EXPR_SLOT.fullmatch(str(pattern.value)) if pattern.symbol in ('(name)', '$') else None
    if slot:
        if pattern.symbol == '$' and node.symbol != '$':
            return False
        name = f'E{slot.group(1)}'
        if name in found.nodes:
            return found.nodes[name].key == node.key
        found.nodes[name] = node
        return True
    if pattern.symbol == '(string)' and STRING_SLOT.fullmatch(str(pattern.value)):
        if node.symbol != '(string)':
            return False
        found.nodes[f'L{STRING_SLOT.fullmatch(str(pattern.value)).group(1)}'] = node
        return True
    # A prefixed name holding a slot (*:QXE2QX) has the slot's own node to match, not its whole name.
    same_value = pattern.value == node.value or (pattern.symbol == ':' and EXPR_SLOT.search(str(pattern.value)))
    if pattern.symbol != node.symbol or not same_value or len(pattern) != len(node):
        return False
    return all(_unify(p, n, found) for p, n in zip(pattern.children, node.children))


def splice(text: str, replacements: list[tuple[Node, str]]) -> str:
    """The expression's text (the one its nodes were parsed from) with some nodes' text replaced."""
    out, at = '', 0
    for target, by in sorted(replacements, key=lambda r: r[0].start):
        if target.start < at:
            continue                  # inside one already replaced
        out += text[at:target.start] + by
        at = target.end
    return out + text[at:]
