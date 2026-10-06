"""Shared XSLTs: documents a pipeline's XSLT pulls in with xsl:import or xsl:include, and what it uses them for.

An environment often keeps common steps in one XSLT that every translation (or indexing) XSLT imports, e.g. a
named template writing EventSource/Device from stroom:meta() attributes, or Event/Meta with the stream's GUID.
New XSLTs should call the same templates where their siblings do, for maintainability, and must then not write
those elements themselves: a second EventSource/Device fails schema validation, a second JSON key is a
duplicate field.

Read here from the XSLT text: each named template a shared document defines and the elements (or JSON keys) it
writes at its top level, and each call to one of them in a calling XSLT, with the elements it sits inside. The
call's place is the path of the element it writes, below Event (EventSource/Device, Meta) or below the
document's map for an indexing XSLT (a key such as guid).
"""
import re
from typing import Any

from lxml import etree

XSL = 'http://www.w3.org/1999/XSL/Transform'
EVT = 'event-logging:3'
FN = 'http://www.w3.org/2005/xpath-functions'
_FLOW = {'if', 'choose', 'when', 'otherwise', 'for-each', 'for-each-group', 'variable', 'sequence', 'where-populated'}


def _parse(text: str) -> etree._Element | None:
    try:
        return etree.fromstring((text or '').encode('utf-8'))
    except (etree.XMLSyntaxError, ValueError):
        return None


def imports_of(xslt: str) -> list[str]:
    """The hrefs an XSLT imports or includes (Stroom resolves each to the XSLT document of that name)."""
    root = _parse(xslt)
    if root is None:
        return []
    return [e.get('href') for e in root.iter(f'{{{XSL}}}import', f'{{{XSL}}}include') if e.get('href')]


def _written(element: etree._Element) -> list[str]:
    """The names of the elements an XSLT fragment writes at its top level: event-logging elements by name, JSON
    (xpath-functions) values by key; looking through xsl:if, xsl:choose and the like, not into xsl:attribute."""
    out: list[str] = []
    for child in element:
        if not isinstance(child.tag, str):
            continue
        q = etree.QName(child)
        if q.namespace == XSL:
            if q.localname in _FLOW:
                out += _written(child)
            elif q.localname == 'element':
                out.append(child.get('name', '').split(':')[-1])
        elif q.namespace == FN and child.get('key'):
            out.append(child.get('key'))
        elif q.namespace == 'records:2' and q.localname == 'data' and child.get('name'):
            out.append(child.get('name'))
        elif q.namespace != FN:
            out.append(q.localname)
    return list(dict.fromkeys(n for n in out if n))


def _paths(element: etree._Element, prefix: list[str]) -> list[str]:
    """Every element path (or JSON key path) an XSLT fragment writes, e.g. Device/HostName, Device/IPAddress."""
    out: list[str] = []
    for child in element:
        if not isinstance(child.tag, str):
            continue
        q = etree.QName(child)
        if q.namespace == XSL:
            if q.localname in _FLOW:
                out += _paths(child, prefix)
            elif q.localname == 'element':
                name = child.get('name', '').split(':')[-1]
                out += _paths(child, prefix + [name]) or ['/'.join(prefix + [name])]
            continue
        if q.namespace == FN:
            name = child.get('key')
            if q.localname in ('map', 'array'):
                out += _paths(child, prefix + [name] if name else prefix) or (['/'.join(prefix + [name])] if name else [])
            elif name:
                out.append('/'.join(prefix + [name]))
            continue
        if q.namespace == 'records:2' and q.localname == 'data' and child.get('name'):
            out.append(child.get('name'))
            continue
        out += _paths(child, prefix + [q.localname]) or ['/'.join(prefix + [q.localname])]
    return list(dict.fromkeys(out))


def json_values(xslt: str, template: str) -> dict[str, str]:
    """The JSON values a shared indexing template writes: dotted key path -> element (string, number, boolean)."""
    root = _parse(xslt)
    found = next((t for t in root.iter(f'{{{XSL}}}template') if t.get('name') == template), None) if root is not None else None
    out: dict[str, str] = {}

    def walk(element: etree._Element, prefix: list[str]) -> None:
        for child in element:
            if not isinstance(child.tag, str):
                continue
            q = etree.QName(child)
            if q.namespace == XSL and q.localname in _FLOW:
                walk(child, prefix)
            elif q.namespace == FN and q.localname in ('map', 'array'):
                walk(child, prefix + ([child.get('key')] if child.get('key') else []))
            elif q.namespace == FN and child.get('key'):
                out['.'.join(prefix + [child.get('key')])] = q.localname
    if found is not None:
        walk(found, [])
    return out


_META = re.compile(r"stroom:meta\(\s*'([^']+)'\s*\)")


def describe(xslt: str) -> dict[str, Any]:
    """What a shared XSLT does, read from its text: each named template with the elements (or JSON keys) it writes
    at its top level and in full, the stream meta it reads (stroom:meta) and its parameters (required="yes" or
    optional); and the template rules it supplies (match, mode), which apply without a call."""
    root = _parse(xslt)
    if root is None:
        return {'unreadable': True}
    templates = {}
    rules = []
    for t in root.iter(f'{{{XSL}}}template'):
        text = etree.tostring(t, encoding='unicode')
        meta = sorted(set(_META.findall(text)))
        if t.get('name'):
            # Without required="yes", a parameter the call doesn't pass is its default (empty if none given).
            params = {p.get('name'): 'required' if p.get('required') == 'yes' else 'optional'
                      for p in t.findall(f'{{{XSL}}}param')}
            templates[t.get('name')] = {'writes': _written(t), 'paths': _paths(t, []),
                                        **({'reads_meta': meta} if meta else {}), **({'params': params} if params else {})}
        else:
            rules.append({'match': t.get('match'), **({'mode': t.get('mode')} if t.get('mode') else {}),
                          'writes': _paths(t, [])[:10], **({'reads_meta': meta} if meta else {})})
    functions = functions_of(root)
    return {'templates': templates, **({'template_rules': rules} if rules else {}),
            **({'functions': functions} if functions else {}),
            **({'imports': imports_of(xslt)} if imports_of(xslt) else {})}


def functions_of(root: etree._Element | None) -> dict[str, dict[str, Any]]:
    """The xsl:functions a stylesheet defines: name -> its prefix's namespace, parameters and return type, for a
    mapping's functions entry (href, prefix, namespace) and the calls that pass the right arguments."""
    out: dict[str, dict[str, Any]] = {}
    for f in (root.iter(f'{{{XSL}}}function') if root is not None else []):
        name = f.get('name') or ''
        prefix = name.split(':')[0] if ':' in name else None
        params = [f"{p.get('name')} as {p.get('as')}" if p.get('as') else p.get('name')
                  for p in f.findall(f'{{{XSL}}}param')]
        out[name] = {'prefix': prefix, 'namespace': f.nsmap.get(prefix), 'params': params,
                     **({'returns': f.get('as')} if f.get('as') else {})}
    return out


def function_calls(xslt: str, functions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Each shared function a calling XSLT calls: how often, and one call as written (its select or test)."""
    root = _parse(xslt)
    if root is None:
        return []
    own = functions_of(root)
    out = []
    for name, about in functions.items():
        call = re.compile(re.escape(name) + r'\s*\(')
        where = [v for el in root.iter() if isinstance(el.tag, str) for v in el.attrib.values() if call.search(v)]
        if where:
            out.append({'function': name, **about, 'calls': len(where), 'example': where[0][:200],
                        **({'own_copy': 'the calling XSLT defines its own, which wins over the import'}
                           if name in own else {})})
    return out


def templates_of(xslt: str) -> dict[str, list[str]]:
    """Named templates a shared XSLT defines -> the elements (or JSON keys) each writes at its top level."""
    root = _parse(xslt)
    if root is None:
        return {}
    return {t.get('name'): _written(t) for t in root.iter(f'{{{XSL}}}template') if t.get('name')}


def _within(call: etree._Element, seen: frozenset = frozenset()) -> list[str]:
    """The literal elements (or JSON keys) a call sits inside, outermost first, below Event or the document map.
    A call in a template of its own (a mode, or a named template) continues from where that template is applied:
    seen live, shared templates called in a networkSource mode applied inside Network/Connect/Source."""
    path: list[str] = []
    node = call.getparent()
    while node is not None and isinstance(node.tag, str):
        q = etree.QName(node)
        if q.namespace == XSL and q.localname == 'template':
            caller = _caller(node, seen)
            if caller is not None:
                return _within(caller, seen | {id(node)}) + list(reversed(path))
            break
        if q.namespace == EVT:
            if q.localname in ('Event', 'Events'):
                break
            path.append(q.localname)
        elif q.namespace == FN and node.get('key'):
            path.append(node.get('key'))
        node = node.getparent()
    return list(reversed(path))


def _caller(template: etree._Element, seen: frozenset) -> etree._Element | None:
    """Where a part's template is run from: the first apply-templates of its mode, or call of its name, that is
    inside an element (the record's own templates, with no element around the call, don't count)."""
    if id(template) in seen:
        return None
    root = template.getroottree().getroot()
    if template.get('name'):
        found = root.iter(f'{{{XSL}}}call-template')
        found = [c for c in found if c.get('name') == template.get('name')]
    elif template.get('mode') and template.get('mode') != '#default':
        found = [a for a in root.iter(f'{{{XSL}}}apply-templates') if a.get('mode') == template.get('mode')]
    else:
        return None
    for candidate in found:
        node = candidate.getparent()
        while node is not None and isinstance(node.tag, str) and not (
                etree.QName(node).namespace == XSL and etree.QName(node).localname == 'template'):
            if etree.QName(node).namespace in (EVT, FN):
                return candidate
            node = node.getparent()
    return None


def calls_of(xslt: str, templates: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Each call in a calling XSLT to one of the shared named templates: {template, within, writes, at}."""
    root = _parse(xslt)
    if root is None:
        return []
    out = []
    for call in root.iter(f'{{{XSL}}}call-template'):
        name = call.get('name')
        if name not in templates:
            continue
        within = _within(call)
        writes = templates[name]
        passed = {w.get('name'): w.get('select') or (w.text or '').strip()
                  for w in call.findall(f'{{{XSL}}}with-param') if w.get('name')}
        out.append({'template': name, 'within': '/'.join(within), 'writes': writes,
                    'at': ['/'.join(within + [w]) for w in writes], **({'with_params': passed} if passed else {})})
    return out


def usage(xslt: str, shared: dict[str, str]) -> list[dict[str, Any]]:
    """How an XSLT uses the shared documents it imports: shared maps each href to its XSLT text (None when it
    could not be read). One entry per call, and one per import whose templates it does not call by name (template
    rules or xsl:apply-imports)."""
    out = []
    for href in imports_of(xslt):
        text = shared.get(href)
        templates = templates_of(text) if text else {}
        calls = calls_of(xslt, templates)
        details = describe(text)['templates'] if text else {}
        out += [{'href': href, **c, **{k: v for k, v in details.get(c['template'], {}).items() if k != 'writes'}}
                for c in calls]
        functions = function_calls(xslt, functions_of(_parse(text))) if text else []
        out += [{'href': href, 'template': None, **f} for f in functions]
        if not calls and not functions:
            out.append({'href': href, 'template': None, 'note': (
                'not found as an XSLT document' if text is None else
                'imported, but none of its named templates or functions is called: it supplies template rules '
                'or xsl:apply-imports')})
    return out
