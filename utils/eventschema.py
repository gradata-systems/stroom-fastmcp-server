"""What the event-logging XSD allows below Event: element order, required elements, choices and values.

The translation generator uses this to put elements in schema order, to reject paths the schema does not
have (with suggestions), and to check constants against enumerations, so a model only has to say which
input field goes where.
"""
import difflib
from dataclasses import dataclass
from typing import Any

from lxml import etree

XS = 'http://www.w3.org/2001/XMLSchema'


def _q(tag: str) -> str:
    return f'{{{XS}}}{tag}'


def _local(qname: str | None) -> str | None:
    return qname.split(':')[-1] if qname else None


def _occurs(node: etree._Element, attr: str) -> int | None:
    value = node.get(attr, '1')
    return None if value == 'unbounded' else int(value)


@dataclass(frozen=True)
class Child:
    name: str
    decl: Any                 # the xs:element (local, or the global one it refers to)
    index: int                # position in the parent's content model
    required: bool            # must appear (not optional, not one of a choice)
    repeatable: bool
    choice: int | None        # id of the one-of choice it belongs to, if any


class EventSchema:
    def __init__(self, root: etree._Element):
        self.version = root.get('version')
        self._types = {e.get('name'): e for e in root.findall(_q('complexType'))}
        self._simple = {e.get('name'): e for e in root.findall(_q('simpleType'))}
        self._groups = {e.get('name'): e for e in root.findall(_q('group'))}
        self._elements = {e.get('name'): e for e in root.findall(_q('element'))}
        self._children: dict[int, list[Child]] = {}
        self.required_choices: dict[int, list[str]] = {}   # choice id -> member names, for required choices
        self.event = next(c for c in self.children(self._elements['Events']) if c.name == 'Event').decl
        self._paths: dict[str, list[str]] | None = None
        self._values: dict[str, list[str]] = {}

    @classmethod
    def parse(cls, xsd: str | bytes) -> 'EventSchema':
        return cls(etree.fromstring(xsd.encode('utf-8') if isinstance(xsd, str) else xsd))

    # --- content models ---
    def _resolve(self, decl: etree._Element) -> etree._Element:
        return self._elements.get(_local(decl.get('ref')), decl) if decl.get('ref') else decl

    def _complex(self, decl: etree._Element) -> etree._Element | None:
        decl = self._resolve(decl)
        return decl.find(_q('complexType')) if decl.find(_q('complexType')) is not None \
            else self._types.get(_local(decl.get('type')))

    def children(self, decl: etree._Element) -> list[Child]:
        decl = self._resolve(decl)
        key = id(decl)
        if key not in self._children:
            found: list[tuple] = []
            ctype = self._complex(decl)
            if ctype is not None:
                self._walk_type(ctype, found, None, False)
            seen, out = set(), []
            for name, child_decl, required, repeatable, choice in found:
                if name not in seen:
                    seen.add(name)
                    out.append(Child(name, self._resolve(child_decl), len(out), required, repeatable, choice))
            self._children[key] = out
        return self._children[key]

    def _walk_type(self, ctype: etree._Element, out: list, choice: int | None, optional: bool) -> None:
        for node in ctype:
            if node.tag in (_q('complexContent'), _q('simpleContent')):
                for derivation in node:
                    base = self._types.get(_local(derivation.get('base')))
                    if derivation.tag == _q('extension') and base is not None:
                        self._walk_type(base, out, choice, optional)
                    self._walk_type(derivation, out, choice, optional)
            elif node.tag in (_q('sequence'), _q('choice'), _q('all'), _q('group'), _q('element')):
                self._particle(node, out, choice, optional)

    def _particle(self, node: etree._Element, out: list, choice: int | None, optional: bool) -> None:
        optional = optional or _occurs(node, 'minOccurs') == 0
        if node.tag == _q('element'):
            name = node.get('name') or _local(node.get('ref'))
            maximum = _occurs(node, 'maxOccurs')
            out.append((name, node, not optional and choice is None, maximum is None or maximum > 1, choice))
        elif node.tag in (_q('sequence'), _q('all')):
            for child in node:
                self._particle(child, out, choice, optional)
        elif node.tag == _q('choice'):
            # One of (maxOccurs 1), or, when the choice repeats, any number of its members; either way a
            # required choice needs at least one member, and no member is required on its own.
            exclusive = _occurs(node, 'maxOccurs') == 1
            cid = id(node) if exclusive or choice is None else choice
            before = len(out)
            for child in node:
                self._particle(child, out, cid, optional)
            if not exclusive:  # members of a repeating choice can each appear many times
                out[before:] = [(name, decl, False, True, member_choice)
                                for name, decl, _, _, member_choice in out[before:]]
            if not optional and choice is None:
                self.required_choices[cid] = [entry[0] for entry in out[before:]]
        elif node.tag == _q('group'):
            group = self._groups.get(_local(node.get('ref'))) if node.get('ref') else node
            for child in group if group is not None else []:
                if child.tag in (_q('sequence'), _q('choice'), _q('all')):
                    self._particle(child, out, choice, optional)

    # --- leaves ---
    def is_leaf(self, decl: etree._Element) -> bool:
        ctype = self._complex(decl)
        return ctype is None or ctype.find(_q('simpleContent')) is not None

    def _simple_type(self, decl: etree._Element) -> etree._Element | None:
        decl = self._resolve(decl)
        inline = decl.find(_q('simpleType'))
        return inline if inline is not None else self._simple.get(_local(decl.get('type')))

    def base_type(self, decl: etree._Element) -> str | None:
        """The built-in XSD type a leaf ends up as, e.g. 'dateTime', 'boolean', 'string'."""
        decl = self._resolve(decl)
        stype, name = self._simple_type(decl), _local(decl.get('type'))
        for _ in range(10):
            if stype is None:
                return name
            restriction = stype.find(_q('restriction'))
            if restriction is None:
                return 'string'
            name = _local(restriction.get('base'))
            stype = self._simple.get(name)
        return name

    def enumeration(self, decl: etree._Element) -> list[str] | None:
        stype = self._simple_type(decl)
        for _ in range(10):
            if stype is None:
                return None
            restriction = stype.find(_q('restriction'))
            if restriction is None:
                return None
            values = [e.get('value') for e in restriction.findall(_q('enumeration'))]
            if values:
                return values
            stype = self._simple.get(_local(restriction.get('base')))
        return None

    # --- paths ---
    def resolve(self, path: str) -> list[Child]:
        """The chain of children for a path below Event, e.g. 'EventSource/User/Id'. Raises ValueError."""
        chain, decl, walked = [], self.event, 'Event'
        for segment in [s for s in path.strip('/').split('/') if s]:
            if segment == 'Event' and not chain:
                continue
            options = self.children(decl)
            child = next((c for c in options if c.name == segment), None)
            if child is None:
                names = [c.name for c in options]
                close = difflib.get_close_matches(segment, names, n=3)
                named = self.paths_named(segment)
                elsewhere = ([p for p in named if p.startswith(walked + '/')] + [p for p in named if not p.startswith(walked + '/')])[:4]
                value_of = self.paths_with_value(segment)[:3]
                raise ValueError(
                    f"'{walked}' has no child '{segment}'."
                    + (f" Did you mean {close}?" if close else '')
                    + (f" '{segment}' exists at: {elsewhere}." if elsewhere else '')
                    + (f" '{segment}' is a value of: {value_of} (map a constant to it)." if value_of else '')
                    + f" Allowed here: {names}")
            chain.append(child)
            decl, walked = child.decl, f'{walked}/{segment}'
        return chain

    def paths_named(self, name: str, depth: int = 6) -> list[str]:
        """Paths below Event whose last element is `name` (shortest first)."""
        self._index(depth)
        return self._paths.get(name, [])

    def paths_with_value(self, value: str) -> list[str]:
        """Leaf paths whose enumeration allows `value`, e.g. 'Logon' -> Event/EventDetail/Authenticate/Action."""
        self._index()
        return self._values.get(value, [])

    def _index(self, depth: int = 6) -> None:
        if self._paths is not None:
            return
        self._paths, self._values = {}, {}
        queue = [(self.event, 'Event', 0)]
        while queue:
            decl, path, level = queue.pop(0)
            for child in self.children(decl):
                here = f'{path}/{child.name}'
                self._paths.setdefault(child.name, []).append(here)
                for value in (self.enumeration(child.decl) or []) if self.is_leaf(child.decl) else []:
                    self._values.setdefault(value, []).append(here)
                if level < depth:
                    queue.append((child.decl, here, level + 1))
