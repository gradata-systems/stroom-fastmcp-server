"""A hand edit kept through the agent's next change. Asked for by the user: an XSLT the server saved (from a mapping,
an index or CEF plan, or written by the agent), then edited by hand in Stroom's editor (say to write
http.request.body.bytes as well), was saved over by the next regeneration, and the edit was gone.

What the edit changed is what the XSLT writes and reads that the code the server saved doesn't (and the reverse):
the names of the elements and keys it writes (<number key="bytes">, <Data Name="device_class">) and the constants
in them, and the XPath it reads (select, test and {...} in attribute values). New code keeps the edit when it still
writes and reads those, whichever way it is written: a hand edit carried into the plan comes back in the generator's
own style, and an object the generator writes with a template of its own reads its fields by relative paths.

Where the agent's own change is to a field the edit changed too (asked for by the user: "keep their hand edit or
overwrite it with the proposed field"), the two collide: the user decides, field by field. Each thing the edit changed
carries the names of what it is written as (the key or element round an expression), which are matched to the plan's
fields the change touches."""
import re
from collections import Counter
from typing import Any, NamedTuple

from lxml import etree

XSL = 'http://www.w3.org/1999/XSL/Transform'
_AVT = re.compile(r'\{([^{}]+)\}')


def expression(text: str) -> str:
    """An XPath with the differences that are not edits removed: whitespace, and brackets round the whole."""
    text = re.sub(r'\s+', '', text)
    while text.startswith('(') and text.endswith(')'):
        depth = 0
        for i, ch in enumerate(text):
            depth += ch == '('
            depth -= ch == ')'
            if depth == 0 and i < len(text) - 1:
                return text         # (a) and (b): its brackets are not round the whole
        text = text[1:-1]
    return text


def _named(element: etree._Element) -> str | None:
    """The name a result element is written as: its key or Name (a JSON key, a Data element's) or its own name."""
    qname = etree.QName(element)
    if qname.namespace == XSL:
        return element.get('name') if qname.localname in ('element', 'attribute') else None
    return element.get('key') or element.get('Name') or element.get('name') or qname.localname


def _written(element: etree._Element) -> list[str]:
    """What a result element writes: its name and the key or name it is written under, and a constant in it."""
    qname = etree.QName(element)
    if qname.namespace == XSL:
        if qname.localname == 'text' and (element.text or '').strip():
            return [f"text = {element.text.strip()}"]
        name = _named(element)
        return [f"{qname.localname} {name}"] if name else []
    named = element.get('key') or element.get('Name') or element.get('name')
    out = [f"{qname.localname} {named}" if named else qname.localname]
    if (element.text or '').strip():
        out.append(f"{named or qname.localname} = {element.text.strip()}")
    return out


def _context(element: etree._Element) -> set[str]:
    """The names an expression's value is written as: the result element round it, and those directly in it (an
    xsl:if's test reads for what it holds). A variable's: where it is read (a source changed by hand in the variable a
    template reads it into is its field's)."""
    if etree.QName(element).namespace == XSL and etree.QName(element).localname == 'variable' and element.get('name'):
        reading = re.compile(r'\$' + re.escape(element.get('name')) + r'(?![\w.-])')
        scope = element.getparent() if element.getparent() is not None else element
        return {n for el in scope.iter(etree.Element) if el is not element
                and any(reading.search(v) for v in el.attrib.values())
                for n in _context(el)}
    names = set()
    for up in element.iterancestors(etree.Element):
        if etree.QName(up).namespace != XSL and (up.get('key') or up.get('Name') or up.get('name')):
            names.add(_named(up))
            break
        if etree.QName(up).namespace != XSL and etree.QName(up).localname not in ('map', 'array', 'record', 'Event'):
            names.add(_named(up))
            break
    for down in element.iterdescendants(etree.Element):
        if etree.QName(down).namespace != XSL:
            names.add(_named(down))
            break
    return {n for n in names if n}


class Outline(NamedTuple):
    writes: Counter
    reads: Counter
    names: dict[str, set[str]]      # each item: the names it is written as
    within: dict[str, set[str]]     # each item written: the XPaths read inside it, its value's source


def _read(element: etree._Element) -> list[str]:
    """The XPaths an element reads itself: an XSL instruction's select and test, a result element's {...}."""
    if etree.QName(element).namespace == XSL:
        return [value for attribute, value in element.attrib.items() if attribute in ('select', 'test')]
    return [e for value in element.attrib.values() for e in _AVT.findall(value)]


def outline(code: str) -> Outline:
    """What the XSLT writes (element and key names, constants) and reads (XPaths), each counted."""
    from utils.xsltversion import strip
    writes, reads, names, within = Counter(), Counter(), {}, {}
    try:
        root = etree.fromstring(re.sub(r'^\s*<\?xml[^>]*\?>', '', strip(code or '')).encode('utf-8'))
    except etree.XMLSyntaxError:
        return Outline(writes, reads, names, within)
    # A variable is read as the expression it holds, where it is read: the same field, read once into a variable or
    # three times where it's used (variable_min_reads, or the generator before it wrote variables), is the same.
    held = {v.get('name'): v.get('select') for v in root.iter(f'{{{XSL}}}variable')
            if v.get('name') and v.get('select') and '$' not in v.get('select')}

    def expanded(value: str) -> str:
        def one(m: re.Match) -> str:
            select = held[m.group(1)]
            return select if re.fullmatch(r"[\w@*./:\[\]='-]+", select) else f'({select})'
        return re.sub(r'\$([\w.-]+)', lambda m: one(m) if m.group(1) in held else m.group(0), value)
    for element in root.iter(etree.Element):
        if element.tag == f'{{{XSL}}}variable' and element.get('name') in held:
            continue        # read where it is used
        for item in _written(element):
            writes[item] += 1
            names.setdefault(item, set()).add(item.split(' = ')[0].split(' ')[-1])
            # What it is written from: a key renamed by hand still reads its field's source.
            within.setdefault(item, set()).update(
                expression(expanded(v)) for e in element.iter(etree.Element) for v in _read(e) if expression(v))
        in_xsl = etree.QName(element).namespace == XSL
        for value in _read(element):
            item = expression(expanded(value))
            if item:
                reads[item] += 1
                names.setdefault(item, set()).update(_context(element) if in_xsl else {_named(element)})
    return Outline(writes, reads, names, within)


class Undone(NamedTuple):
    """Something the edit changed that new code undoes: an item it added and new code lacks, or one it took out
    that new code has again. names: what it is written as, and within: what an element written reads inside it (its
    value's source), to match it to a plan's field."""
    verb: str
    item: str
    again: bool
    names: frozenset
    within: frozenset = frozenset()

    def __str__(self) -> str:
        return f"{self.verb} {self.item} again" if self.again else f"no longer {self.verb} {self.item}"


def _related(a: str, b: str) -> bool:
    """The same path, or one a step relative to the other (a template's field read from its own element)."""
    return a == b or a.endswith('/' + b) or b.endswith('/' + a)


def undone(base: str | None, current: str, new: str) -> list[Undone]:
    """What the hand edit (current, against base: what the server saved, regenerated) changed that new undoes.
    With no base (nothing to regenerate it from), everything the XSLT writes and reads counts as the edit's."""
    b, c, n = outline(base or ''), outline(current), outline(new)
    out = []
    for verb, bc, cc, nc in (('writes', b.writes, c.writes, n.writes), ('reads', b.reads, c.reads, n.reads)):
        for item, count in cc.items():
            if count > bc[item]:
                have = sum(k for x, k in nc.items() if (_related(item, x) if verb == 'reads' else x == item))
                if have < count:
                    out.append(Undone(verb, item, False, frozenset(c.names.get(item, ())),
                                      frozenset(c.within.get(item, ()))))
        for item, count in bc.items():
            if cc[item] < count and nc[item] > cc[item]:
                out.append(Undone(verb, item, True, frozenset(n.names.get(item, set()) | b.names.get(item, set())),
                                  frozenset(n.within.get(item, set()) | b.within.get(item, set()))))
    return out


def lost(base: str | None, current: str, new: str) -> list[str]:
    return [str(u) for u in undone(base, current, new)]


class PlanField(NamedTuple):
    spec: str           # what the plan says of it, to tell a change
    said: str           # the same, for the user
    names: frozenset    # what it is written as
    sources: frozenset  # what it reads, as expressions


def plan_fields(kind: str, payload: dict[str, Any] | None) -> dict[str, PlanField]:
    """A mapping's or plan's fields, by a label the user knows them by: an index field's name, a CEF key (per kind
    of event), a translation's path (per rule)."""
    import json
    if not payload:
        return {}
    out: dict[str, PlanField] = {}

    def spec(value: Any) -> str:
        return json.dumps(value, sort_keys=True)
    if kind == 'index':
        for f in payload.get('fields') or []:
            out[f['name']] = PlanField(spec(f), f"{f['name']} ({f.get('type')}) from {f.get('source')}"
                                       + (f", as {f['transform']}" if f.get('transform') else ''),
                                       frozenset({f['name'].split('.')[-1]}), frozenset({expression(f.get('source') or '')}))
    elif kind == 'cef':
        for header in ('vendor', 'product', 'version', 'signature', 'name', 'severity'):
            value = payload.get(header) or {}
            out[f"header {header}"] = PlanField(spec(value), f"header {header}: " + (
                f"'{value['value']}'" if value.get('value') is not None else f"from {value.get('source')}"),
                frozenset(), frozenset({expression(value['source'])} if value.get('source') else ()))
        for event, fields in [('common', payload.get('common') or [])] + list((payload.get('events') or {}).items()):
            for f in fields:
                out[f"{event}: {f['key']}"] = PlanField(
                    spec(f), f"{f['key']} from {f['path']}" + (f" (label '{f['label']}')" if f.get('label') else ''),
                    frozenset({f['key']}), frozenset({expression(f['path']), f"'{f['key']}'"}))
    elif kind == 'translation':
        from utils.xsltgen import field_text
        mapping = payload.get('mapping', payload)
        kind_of = mapping.get('input') or 'data_splitter'
        rules = [('common', mapping.get('common') or [])] + [(r['name'], r.get('fields') or [])
                                                             for r in mapping.get('events') or []]
        for rule, entries in rules:
            for e in entries:
                label = f"{rule}: {e['path']}" + (f" [{e['data_name']}]" if e.get('data_name') else '')
                inputs = [e[k] for k in ('field',) if e.get(k)] + list(e.get('any_of') or [])
                sources = {expression(field_text(kind_of, i)) for i in inputs} | (
                    {expression(e['xpath'])} if e.get('xpath') else set())
                said = ', '.join(f"{k} {v}" for k, v in e.items() if k != 'path')
                out[label] = PlanField(spec(e), f"{label}: {said}",
                                       frozenset({e['path'].split('/')[-1]} | ({e['data_name']} if e.get('data_name') else set())),
                                       frozenset(sources))
    return out


def touches(u: Undone, f: PlanField) -> bool:
    """Whether something the edit changed is (part of) the field: written as its name, reading what it reads, or
    written from what it reads (a key renamed by hand: <string key="created_at"> still reads event.created's source)."""
    if u.names & f.names:
        return True
    read = [u.item] if u.verb == 'reads' else list(u.within)
    return any(s and r and (s in r or _related(r, s)) for s in f.sources for r in read)


def collisions(kind: str, kept: dict[str, Any] | None, new: dict[str, Any] | None,
               items: list[Undone]) -> dict[str, tuple[str | None, str | None, list[Undone]]]:
    """The fields the agent's change (kept -> new) changes that the hand edit changed too: label -> (what the plan
    had, what the change proposes (None: removed), what of the edit it would undo)."""
    before, after = plan_fields(kind, kept), plan_fields(kind, new)
    out = {}
    for label in sorted(set(before) | set(after)):
        old, proposed = before.get(label), after.get(label)
        if old and proposed and old.spec == proposed.spec:
            continue
        hit = [u for u in items if any(touches(u, f) for f in (old, proposed) if f)]
        if hit:
            out[label] = (old.said if old else None, proposed.said if proposed else None, hit)
    return out
