"""XPath parsed by elementpath for reading an XSLT back (utils/xpathtree): asked for by the user in place of splitting
expressions with regular expressions. Parsing only: Stroom evaluates the XSLT."""
import pytest
from elementpath.exceptions import ElementPathError
from lxml import etree

from tests.test_rebuild import CASES, IMPORTS, code_of
from utils.xpathtree import XPathParser, expr_slot, items, normal, parser_for, splice, string_slot, unify
from utils.xsltgen import generate
from utils.xsltread import rebuild
from tests.test_xsltgen import LOGON, RECORDS, SCHEMA, mapping, transform

NS = {'stroom': 'stroom', 'mcp': 'urn:mcp', 'local': 'urn:local', 'geo': 'urn:geo'}


def test_every_part_of_every_generated_expression_is_its_exact_text():
    # Each subtree's text, cut from the expression by the token positions elementpath gives, parses back to the same
    # tree: an expression read back is written as it was, not re-serialised.
    checked = 0
    for m, _ in list(CASES.values()) + list(IMPORTS.values()):
        root = etree.fromstring(code_of(m).encode())
        xp = parser_for(root.nsmap)
        for element in root.iter():
            for attribute in ('select', 'test') if isinstance(element.tag, str) else ():
                text = element.get(attribute)
                if not text:
                    continue
                tree = xp.parse(text)
                assert tree.text == text.strip()
                for node in tree.walk():
                    assert xp.parse(node.text).key == node.key, (text, node.text)
                    checked += 1
    assert checked > 2000


@pytest.mark.parametrize('call', ["stroom:lookup('M', 'k')", 'stroom:record-no()', "mcp:data('n', 1)",
                                  "stroom:format-date('x', 'yyyy', '+10:00')", "local:clean(geo:city('10.0.0.1'))"])
def test_functions_it_doesnt_know_are_parsed_by_any_prefix_the_stylesheet_binds_and_never_run(call):
    # Stroom's, the XSLT's own (mcp:, or a prefix added by hand, local: say) and an imported XSLT's (geo:): each a
    # function of any arity whose body is never run; names the standard functions have too (data, format-date) as well.
    tree = XPathParser(NS).parse(call)
    assert tree.symbol == 'call' and tree.text == call


def test_a_prefix_the_stylesheet_doesnt_bind_is_an_error_as_it_is_in_stroom():
    with pytest.raises(ElementPathError, match='XPST0081'):
        XPathParser(NS).parse("nope:thing(1)")


def test_slots_match_what_fills_them_and_brackets_and_guards_dont_count():
    pattern = XPathParser(NS).parse(f"stroom:lookup('{string_slot(1)}', string(({expr_slot(1)})[1]))")
    found = unify(pattern, XPathParser(NS).parse("(stroom:lookup('USERS', string((data[@name='u']/@value[normalize-space(.)])[1])))"))
    assert found.string(1) == 'USERS' and found.expr(1).text == "data[@name='u']/@value"
    assert [n.text for n in items(XPathParser(NS).parse("(a, (b), c)"))] == ['a', 'b', 'c']
    assert unify(pattern, XPathParser(NS).parse("stroom:lookup($m, 'k')")) is None


def test_splicing_replaces_nodes_by_where_they_are():
    text = "concat($a, '$a', $a)"
    tree = XPathParser(NS).parse(text)
    variables = [n for n in tree.walk() if n.symbol == '$']
    assert splice(text, [(v, '(x)') for v in variables]) == "concat((x), '$a', (x))"


LOCAL = """<xsl:function name="local:shout" as="xs:string"><xsl:param name="v"/>
  <xsl:sequence select="upper-case(string($v[1]))"/></xsl:function>"""


def test_functions_the_xslt_defines_by_hand_are_its_own_and_an_imported_prefix_calls_imported_functions():
    # A hand edit adding local:shout(), defined in the XSLT: its own, read through (not an imported XSLT's prefix,
    # so no functions entry for it). An imported prefix (geo:) is reported as calling an imported function.
    code = code_of(mapping())
    edited = (code.replace('<xsl:stylesheet ', '<xsl:stylesheet xmlns:local="urn:local" ', 1)
              .replace('</xsl:stylesheet>', LOCAL + '</xsl:stylesheet>')
              .replace("<Action>Logon</Action>", "<Action><xsl:value-of select=\"local:shout('logon')\"/></Action>", 1))
    rebuilt = rebuild(edited, None, None)
    assert rebuilt.problems == [] and rebuilt.mapping.functions == []
    action = next(f for f in rebuilt.mapping.events[0].fields if f.path.endswith('Authenticate/Action'))
    assert action.xpath and 'upper-case' in action.xpath      # the function read through to its body
    geo = mapping(events=[{'name': 'logon', 'fields': LOGON + [
        {'path': 'EventDetail/Authenticate/Data', 'data_name': 'city', 'xpath': "geo:city(data[@name='host']/@value)"}]}],
        functions=[{'href': 'Geo Functions', 'prefix': 'geo', 'namespace': 'urn:geo'}])
    result = generate(geo, SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    rebuilt = rebuild(result['xslt'], None, None, {})
    assert rebuilt.raw == [] and rebuilt.imported_calls == ['rule logon: EventDetail/Authenticate/Data Data city']
    assert [f.prefix for f in rebuilt.mapping.functions] == ['geo']
