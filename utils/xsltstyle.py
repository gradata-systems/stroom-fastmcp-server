"""The indexing and CEF XSLTs written in the Events translation's style (asked for by the user: "they should follow the
Events XSLT styles generally", for readability and maintainability): each part a template of its own (in its own
mode, or named, or inline, as XsltStyle.layout says), named in the style's naming, with a comment saying what it
writes; and an input a template reads at least style.variable_min_reads times read once into a variable, declared
just before its first use (style.variables). The translation generator's own helpers are used (style_name,
unique_name, just_in_time, the comment wrapper), so the three kinds of XSLT can't drift apart."""
import re

from lxml import etree

from utils.xsltgen import XSL, XsltStyle, _comment, just_in_time, style_name, unique_name

DECLARATION = '<?xml version="1.1" encoding="UTF-8"?>\n'


def parse(text: str) -> etree._Element:
    """The stylesheet, with the whitespace between its elements gone (so it is laid out afresh), but not an
    xsl:text's: <xsl:text>&#10;</xsl:text> is the newline a text output writes."""
    root = etree.fromstring(re.sub(r'^\s*<\?xml[^>]*\?>', '', text).encode('utf-8'))
    for node in root.iter():      # comments too: whitespace left after one stops the layout around it
        if isinstance(node.tag, str) and node.tag != f'{{{XSL}}}text' and node.text is not None \
                and not node.text.strip() and len(node):
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None
    return root


def serialize(sheet: etree._Element) -> str:
    return DECLARATION + etree.tostring(sheet, pretty_print=True, encoding='unicode')


def comment(text: str) -> etree._Comment:
    return etree.Comment(_comment(text))


def part_name(text: str, style: XsltStyle, taken: set[str]) -> str:
    """A template's mode or name in the style's naming, unique in the stylesheet."""
    name = unique_name(style_name(text, style.naming), taken, style.naming)
    taken.add(name)
    return name


def _reading(expression: str) -> re.Pattern:
    """Where an XPath reads expression whole: not part of a longer path (Resource/URL in EventDetail/*/Resource/URL),
    nor followed by a step or predicate."""
    return re.compile(r"(?<![\w/@$.:*'-])" + re.escape(expression) + r"(?![\w/\[(:-])")


def _sites(template: etree._Element, pattern: re.Pattern) -> list[tuple[etree._Element, str]]:
    """The attributes that read an expression: an XSL instruction's select and test, a result element's {...}."""
    out = []
    for el in template.iter(etree.Element):
        in_xsl = etree.QName(el).namespace == XSL
        for attr, value in el.attrib.items():
            if (in_xsl and attr in ('select', 'test')) or (not in_xsl and '{' in value):
                if pattern.search(value):
                    out.append((el, attr))
    return out


def variables(template: etree._Element, reads: dict[str, str], style: XsltStyle, taken: set[str]) -> None:
    """Each expression in reads (-> the name to give it) that the template reads at least style.variable_min_reads
    times read once into a variable, as the Events translation does (an element's guard and its value are two reads;
    an object's guard a third); declared just before its first use, or at the template's start (style.variables)."""
    declared = []
    for expression, base in reads.items():
        pattern = _reading(expression)
        sites = _sites(template, pattern)
        if sum(len(pattern.findall(el.get(attr))) for el, attr in sites) < style.variable_min_reads:
            continue
        name = part_name(base, style, taken)
        for el, attr in sites:
            el.set(attr, pattern.sub(lambda _m: '$' + name, el.get(attr)))
        declared.append(etree.Element(f'{{{XSL}}}variable', name=name, select=expression))
    at = sum(1 for c in template if isinstance(c.tag, str) and c.tag == f'{{{XSL}}}param')
    # just_in_time moves them last first, each before its first reader: given in reverse, they stay in read order.
    for n, variable in enumerate(reversed(declared) if style.variables == 'just_in_time' else declared):
        template.insert(at + n, variable)
    if declared and style.variables == 'just_in_time':
        just_in_time(template)
