"""The Saxon processor the server checks XPath and runs XSLT with, locked down; and what the server won't run or save.

What it runs is not the server's own: expressions come from the model (a mapping's xpaths, a CEF plan's sources and
fields), and documents from the samples, written by whoever the logs record. Saxon as it comes reads whatever the
process can: doc() and unparsed-text() opened local files and URLs, an XML sample's external entity pulled a local
file into the record, and environment-variable() read the server's settings, its request-state keys among them.

So the processor refuses every URI (no file, no network, no xsl:import or xsl:include by href) and loads no external
entity or DTD; and an expression calling a function that reads the environment or a resource, or that looks a
function up by a name made at run time, is refused before it reaches Saxon. Saxon has no setting for
environment-variable(), so only the check stands in its way.

The XSLT the server saves runs in Stroom, on its nodes, with Stroom's access: the same functions are refused there,
with the instructions that read or write a resource or evaluate an XPath made at run time, and imports by URL.
"""
import re

from lxml import etree

_FEATURE = 'http://saxon.sf.net/feature/'
_PARSER = _FEATURE + 'parserFeature?uri='
LOCKED = {
    _FEATURE + 'allowedProtocols': '',
    _PARSER + 'http%3A//xml.org/sax/features/external-general-entities': 'false',
    _PARSER + 'http%3A//xml.org/sax/features/external-parameter-entities': 'false',
    _PARSER + 'http%3A//apache.org/xml/features/nonvalidating/load-external-dtd': 'false',
}

# The XPath functions' and Saxon's own. A user-defined function of the same name is the XSLT's business: defined in
# the XSLT, or in one it imports by name (a shared XSLT, as an AGENTS doc's Required imports may name), e.g.
# geo:document() from 'GeoIP-V1'.
FN = 'http://www.w3.org/2005/xpath-functions'
SAXON = 'http://saxon.sf.net/'
REFUSED_NAMESPACES = (FN, SAXON)
# Prefixes Saxon's XPath binds unless they are bound otherwise (the server's own checks run there).
PREDECLARED = {'fn': FN, 'saxon': SAXON}
REFUSED = ('environment-variable', 'available-environment-variables', 'function-lookup', 'transform',
           'load-xquery-module', 'doc', 'doc-available', 'document', 'collection', 'uri-collection', 'unparsed-text',
           'unparsed-text-lines', 'unparsed-text-available', 'json-doc')
# A call, name(...), or a function reference, name#1, with its prefix or Q{uri}. Not $name(...), a variable's function.
_CALL = re.compile(r'(?<![\w.\-$])(?:(?P<prefix>[\w.\-]+):|Q\{(?P<uri>[^}]*)\})?(?P<name>'
                   + '|'.join(re.escape(n) for n in sorted(REFUSED, key=len, reverse=True)) + r')\s*[(#]')
XSL = 'http://www.w3.org/1999/XSL/Transform'
# XSLT instructions that write a file, read a document by URI, evaluate an XPath made at run time, or bring in a
# package from elsewhere. xsl:merge-source only with for-each-source (documents by URI).
REFUSED_INSTRUCTIONS = ('evaluate', 'result-document', 'source-document', 'use-package')
_SCHEME = re.compile(r'^\s*[A-Za-z][A-Za-z0-9+.\-]*:')

_processor = None


def processor():
    """The server's one Saxon processor, locked down (above)."""
    global _processor
    if _processor is None:
        # A runtime dependency: it was a dev one, and build_translation_xslt failed in a deployed image.
        from saxonche import PySaxonProcessor
        proc = PySaxonProcessor(license=False)
        for name, value in LOCKED.items():
            proc.set_configuration_property(name, value)
        _processor = proc
    return _processor


def _comment_end(text: str, at: int) -> int:
    """The index after the XPath comment starting at `at` (comments nest; a quote in one is just text), or -1."""
    depth, i = 0, at
    while i < len(text):
        if text.startswith('(:', i):
            depth, i = depth + 1, i + 2
        elif text.startswith(':)', i):
            depth, i = depth - 1, i + 2
            if not depth:
                return i
        else:
            i += 1
    return -1


def _literal_end(text: str, at: int) -> int:
    """The index after the string literal starting at `at`, or -1. A doubled quote reads as two literals side by
    side, which hides nothing."""
    end = text.find(text[at], at + 1)
    return end + 1 if end >= 0 else -1


def _code(expression: str) -> str | None:
    """The expression with its string literals emptied and its comments made spaces, as XPath reads it; None when a
    literal or comment never ends."""
    out, i = [], 0
    while i < len(expression):
        if expression[i] in '\'"':
            end = _literal_end(expression, i)
            if end < 0:
                return None
            out.append("''")
        elif expression.startswith('(:', i):
            end = _comment_end(expression, i)
            if end < 0:
                return None
            out.append(' ')
        else:
            out.append(expression[i])
            end = i + 1
        i = end
    return ''.join(out)


def called(expression: str | None, namespaces: dict[str | None, str] | None = None) -> str | None:
    """The refused function an XPath expression calls, if any. Read as XPath reads it, so a name in a string is not a
    call and a comment between a name and its '(' doesn't hide one; one that can't be read is read as written.
    namespaces: the prefixes in scope where it runs (an XSLT's, or those a mapping's functions entries bind). A name
    with no prefix is the functions namespace's. One whose prefix nothing binds is not refused: it can't compile, in
    Stroom or here, and the caller says so more usefully (a mapping: "no functions entry binds 'geo'")."""
    text = expression or ''
    namespaces = namespaces or {}
    for found in _CALL.finditer(_code(text) or text):
        prefix = found.group('prefix')
        if found.group('uri') is not None:
            uri = found.group('uri').strip()
        elif prefix:
            uri = namespaces[prefix] if prefix in namespaces else PREDECLARED.get(prefix)
        else:
            uri = FN
        if uri in REFUSED_NAMESPACES:
            return found.group('name')
    return None


def refused(expression: str | None, namespaces: dict[str | None, str] | None = None) -> str | None:
    """Why the server won't run or save an XPath expression, or None."""
    name = called(expression, namespaces)
    return (f"it calls {name}(), which reads the environment, files or network, or looks a function up by name: the "
            f"server doesn't run or save it. Read the record's own values") if name else None


def value_template_parts(text: str) -> list[str]:
    """The expressions in an attribute (or text) value template: the parts in braces, '{{' and '}}' being braces
    themselves. A brace in a string or comment, or a map's braces, don't end one. Unreadable: the whole text."""
    out, i = [], 0
    while i < len(text):
        if text.startswith('{{', i) or text.startswith('}}', i):
            i += 2
            continue
        if text[i] != '{':
            i += 1
            continue
        j, depth = i + 1, 0
        while True:
            if j >= len(text):
                return [text]
            ch = text[j]
            if ch in '\'"' or text.startswith('(:', j):
                j = _literal_end(text, j) if ch in '\'"' else _comment_end(text, j)
                if j < 0:
                    return [text]
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                if not depth:
                    break
                depth -= 1
            j += 1
        out.append(text[i + 1:j])
        i = j + 1
    return out


def unsafe_in_xslt(code: str) -> list[tuple[str, str]]:
    """(what, why) for each thing in an XSLT the server won't save: a refused function in an XSL attribute (an
    expression, or a template whose braces are), in another element's attribute value template or a text value
    template; a refused instruction; an import or include by URL. Not well-formed: nothing (check_xslt says so)."""
    try:
        root = etree.fromstring(code.encode('utf-8'), etree.XMLParser(resolve_entities=False, no_network=True))
    except (etree.XMLSyntaxError, ValueError):
        return []
    found: dict[str, str] = {}

    def scan(where: str, expressions: list[str], scope: etree._Element | None) -> None:
        # The prefixes in scope where the expression is: an element's for its attributes and text, its parent's for
        # the text after it.
        namespaces = dict(scope.nsmap) if scope is not None else {}
        for expression in expressions:
            name = called(expression, namespaces)
            if name and f'{name}()' not in found:
                found[f'{name}()'] = f"{where} calls {name}()"

    for el in root.iter():
        if not isinstance(el.tag, str):     # a comment or processing instruction: only the text after it counts
            scan('text', value_template_parts(el.tail or ''), el.getparent())
            continue
        xsl = el.tag.startswith(f'{{{XSL}}}')
        local = etree.QName(el).localname
        name = f"xsl:{local}" if xsl else local
        if xsl and (local in REFUSED_INSTRUCTIONS or (local == 'merge-source' and el.get('for-each-source'))):
            found.setdefault(name, f"{name} reads or writes a document by URI, or evaluates an XPath made at run time")
        if xsl and local in ('import', 'include') and _SCHEME.match(el.get('href') or ''):
            found.setdefault(f"{name} {el.get('href')}", f"{name} href='{el.get('href')}' is a URL: import Stroom XSLTs "
                                                         f"by name")
        for attribute, value in el.attrib.items():
            # An XSL element's attributes (and xsl:use-when and the like on others) are expressions or value
            # templates: read both ways. Another element's are value templates.
            expression = xsl or attribute.startswith(f'{{{XSL}}}')
            scan(f"{name}/@{etree.QName(attribute).localname}",
                 [value, *value_template_parts(value)] if expression else value_template_parts(value), el)
        scan('text', value_template_parts(el.text or ''), el)
        scan('text', value_template_parts(el.tail or ''), el.getparent())
    return [(what, f"{why}, which reads the environment, files or network, or looks a function up by name: Stroom "
                   f"would run it with its own access, so the server doesn't save it" if what.endswith('()') else why)
            for what, why in found.items()]
