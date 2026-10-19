"""The server's Saxon reads nothing of the server's: no file, no URL, no external entity, no environment variable;
and the XSLT it saves for Stroom calls nothing that would read Stroom's."""
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from utils import cef
from utils.saxon import processor, refused, unsafe_in_xslt, value_template_parts
from utils.xpathcheck import check_extractions, check_xpaths
from utils.xsltgen import TranslationMapping, generate
from tests.test_cef import drafted


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / 'secret.txt'
    path.write_text('the-secret')
    return path.as_uri()


def evaluate(expression: str) -> str | None:
    proc = processor()
    xp = proc.new_xpath_processor()
    xp.set_context(xdm_item=proc.parse_xml(xml_text='<a/>'))
    value = xp.evaluate_single(expression)
    return value.string_value if value is not None else None


def test_files_and_urls_are_refused_by_the_processor(secret_file):
    for expression in (f"unparsed-text('{secret_file}')", f"string(doc('{secret_file}'))",
                       f"string(json-doc('{secret_file}'))", "unparsed-text('http://127.0.0.1:9/x')"):
        with pytest.raises(Exception, match='prohibited|disallowed'):
            evaluate(expression)


def test_a_samples_external_entities_are_not_loaded(secret_file):
    # A sample is log content: whoever the logs record wrote it.
    proc = processor()
    assert proc.parse_xml(xml_text=f'<!DOCTYPE r [<!ENTITY e SYSTEM "{secret_file}">]><r>&e;</r>').string_value == ''
    assert proc.parse_xml(xml_text='<!DOCTYPE r [<!ENTITY e "inner">]><r>&e;</r>').string_value == 'inner'


def test_an_xslt_imports_nothing_by_href(secret_file):
    xslt = (f'<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
            f'<xsl:import href="{secret_file}"/></xsl:stylesheet>')
    with pytest.raises(Exception):
        processor().new_xslt30_processor().compile_stylesheet(stylesheet_text=xslt)


@pytest.mark.parametrize('expression', [
    "environment-variable('STROOM_MCP_REQUEST_STATE_KEYS')",
    "fn:environment-variable('X')",
    "Q{http://www.w3.org/2005/xpath-functions}environment-variable('X')",
    "available-environment-variables()",
    "environment-variable#1('X')",
    "environment-variable (: a comment :) ('X')",
    "environment-variable(: (: nested :) :)('X')",
    "'(:', environment-variable (:x:) ('X'), ':)'",
    "function-lookup(xs:QName('fn:environment-variable'), 1)('X')",
    "transform(map{'stylesheet-text': '...'})",
    "EventSource/User/Id)) , doc('file:///etc/passwd') , ((.",
    "unparsed-text-lines('file:///etc/hosts')",
    "fn:document('a')",
])
def test_expressions_reading_the_environment_or_a_resource_are_refused(expression):
    assert refused(expression)


@pytest.mark.parametrize('expression', [
    "EventSource/System/Name", "json-to-xml(*[@key='event'])/*/*[@key='user']", "document-node()",
    "doc/id", "substring-after(data[@name='msg']/@value, 'user=')", "$doc('x')", "my-doc(1)",
    "matches(., 'transform-[a-z]+')", "format-dateTime(current-dateTime(), '[Y]')",
    # Stroom's own functions, network ones included: enrichment pipelines need them (the user's call).
    "stroom:http-call('https://enrich/api', '', 'application/json', ., '')", "stroom:fetch-json('https://x/y', '')",
])
def test_ordinary_expressions_pass(expression):
    assert refused(expression) is None


def test_a_mappings_unsafe_xpath_is_not_evaluated():
    m = TranslationMapping.model_validate({
        'input': 'xml_fragments', 'record': 'Event',
        'common': [{'path': 'EventSource/System/Name', 'value': 'App'},
                   {'path': 'EventSource/User/Id', 'xpath': "environment-variable('PATH')"}],
        'events': [{'name': 'any', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'}]}]})
    [warning] = check_xpaths(m, '<Event><User>alice</User></Event>\n')
    assert 'was not evaluated' in warning and 'environment-variable()' in warning

    m = TranslationMapping.model_validate({
        'input': 'xml_fragments', 'record': 'Event',
        'extract': [{'xpath': "unparsed-text('file:///etc/passwd')", 'regex': '(.*)', 'names': ['all']}],
        'common': [{'path': 'EventSource/User/Id', 'field': 'all'}],
        'events': [{'name': 'any', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'}]}]})
    problems, _ = check_extractions(m, '<Event><User>alice</User></Event>\n', None, [])
    assert any('unparsed-text()' in p for p in problems)


def test_a_cef_plan_with_an_unsafe_expression_is_a_problem_and_writes_no_xslt():
    plan, _ = drafted()
    plan = plan.model_copy(update={'vendor': cef.CefValue(source="environment-variable('STROOM_MCP_REQUEST_STATE_KEYS')"),
                                   'kafka_key': "doc('http://169.254.169.254/')"})
    problems = plan.problems()
    assert any(p.startswith('header Device Vendor') and 'environment-variable()' in p for p in problems)
    assert any(p.startswith('kafka_key') and 'doc()' in p for p in problems)
    with pytest.raises(ValueError, match="isn't written"):
        plan.xslt()


def test_a_name_in_a_string_is_not_a_call_but_a_comment_hides_nothing():
    assert refused("'Printed document (A4)'") is None
    assert refused("concat('doc(', 'x')") is None
    assert refused("(: it's :) environment-variable('X') (: ' :)")
    assert refused("environment-variable('unterminated")     # unreadable: read as written


def test_value_templates_are_read_as_xslt_reads_them():
    assert value_template_parts("a {x} b {{literal}} {map{'k': 1}?k}") == ['x', "map{'k': 1}?k"]
    assert value_template_parts("{concat('}', doc('f'))}") == ["concat('}', doc('f'))"]
    assert value_template_parts("{x") == ['{x']


XSLT = '''<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xpath-default-namespace="records:2" version="3.0">
  <xsl:template match="record">
    <Event><Description>Printed document (A4)</Description>{BODY}</Event>
  </xsl:template>
</xsl:stylesheet>'''


def xslt(body: str) -> str:
    return XSLT.replace('{BODY}', body)


@pytest.mark.parametrize('body, what', [
    ('<xsl:value-of select="environment-variable(\'X\')"/>', 'environment-variable()'),
    ('<xsl:value-of select="doc&#40;\'file:///etc/passwd\')"/>', 'doc()'),     # a character reference is a '('
    ('<xsl:value-of select="map{}, unparsed-text(\'f\')"/>', 'unparsed-text()'),
    ('<Data Name="x" Value="{unparsed-text(\'file:///etc/hosts\')}"/>', 'unparsed-text()'),
    ('<Data Name="x" Value="{concat(\'}\', doc(\'f\'))}"/>', 'doc()'),
    ('<Data xsl:use-when="doc-available(\'f\')" Name="x" Value="y"/>', 'doc-available()'),
    ('<xsl:evaluate xpath="\'1\'"/>', 'xsl:evaluate'),
    ('<xsl:result-document href="file:///tmp/x"><a/></xsl:result-document>', 'xsl:result-document'),
    ('<xsl:source-document href="f"><a/></xsl:source-document>', 'xsl:source-document'),
])
def test_an_xslt_reading_or_writing_a_resource_is_refused(body, what):
    assert what in [w for w, _ in unsafe_in_xslt(xslt(body))]


def test_ordinary_xslt_passes_and_imports_by_name_only():
    assert unsafe_in_xslt(xslt('<Data Name="doc (v1)" Value="{data[@name=\'doc\']/@value}"/>'
                               '<xsl:value-of select="\'see document (2)\'"/><!-- doc(x) in a comment -->')) == []
    by_url = XSLT.replace('version="3.0">', 'version="3.0"><xsl:import href="file:///etc/x.xsl"/>').replace('{BODY}', '')
    assert [w for w, _ in unsafe_in_xslt(by_url)] == ['xsl:import file:///etc/x.xsl']
    by_name = XSLT.replace('version="3.0">', 'version="3.0"><xsl:import href="Stream-Meta-V1"/>').replace('{BODY}', '')
    assert unsafe_in_xslt(by_name) == []


def test_a_mapping_calling_a_refused_function_generates_nothing():
    from tests.test_xsltgen import SCHEMA, mapping as translation_mapping
    m = translation_mapping(common=[{'path': 'EventSource/System/Name', 'value': 'ACME'},
                                    {'path': 'EventSource/User/Id', 'xpath': "environment-variable('PATH')"}])
    result = generate(m, SCHEMA, '4.1.0')
    assert result['xslt'] is None and any('environment-variable()' in p for p in result['problems'])


@pytest.fixture
async def ctx():
    from tests.test_gateway import SETTINGS
    from utils.stroom import StroomGateway
    gw = StroomGateway(SETTINGS)
    yield SimpleNamespace(lifespan_context={'stroom': gw})
    await gw.close()


async def test_check_xslt_refuses_it_and_a_save_keeps_what_the_production_xslt_already_did(ctx):
    from tools import translation, validation
    unsafe = xslt('<xsl:value-of select="document(\'lookup.xml\')/x"/>')
    result = await validation.check_xslt(ctx, unsafe)
    assert not result['ok'] and any('document()' in e for e in result['errors'])
    with pytest.raises(ToolError, match='document()'):
        await translation._checked(ctx, unsafe)
    # A working copy of a production XSLT that already calls document(): saved, but nothing new is let in.
    await translation._checked(ctx, unsafe, previous=unsafe)
    with pytest.raises(ToolError, match='environment-variable'):
        await translation._checked(ctx, unsafe.replace('</Event>', '<xsl:value-of select="environment-variable(\'X\')"/></Event>'),
                                   previous=unsafe)


GEO = 'http://example.com/geo'


def test_a_user_defined_function_of_the_same_name_is_the_xslts_own():
    assert refused("geo:lookup-ip(@ip)", {'geo': GEO}) is None
    assert refused("geo:document(@ip)", {'geo': GEO}) is None
    assert refused(f"Q{{{GEO}}}doc(@ip)") is None
    # fn under another prefix, the prefixes Saxon declares itself, and Saxon's own namespace are refused.
    assert refused("x:doc('f')", {'x': 'http://www.w3.org/2005/xpath-functions'})
    assert refused("fn:doc('f')") and refused("saxon:doc('f')")
    assert refused("s:doc('f')", {'s': 'http://saxon.sf.net/'})
    assert refused("geo:document(@ip), doc('f')", {'geo': GEO})
    # A prefix nothing binds can't compile anywhere: left to the check that says it isn't bound.
    assert refused("geo:document(@ip)") is None


def test_an_xslts_prefixes_are_read_where_the_expression_is():
    imported = XSLT.replace('version="3.0">', f'version="3.0" xmlns:geo="{GEO}"><xsl:import href="GeoIP-V1"/>')
    assert unsafe_in_xslt(imported.replace('{BODY}', '<xsl:value-of select="geo:document(@ip)"/>'
                                                     '<xsl:value-of select="geo:lookup-ip(@ip)"/>')) == []
    aliased = XSLT.replace('version="3.0">', 'version="3.0" xmlns:f="http://www.w3.org/2005/xpath-functions">')
    assert unsafe_in_xslt(aliased.replace('{BODY}', '<xsl:value-of select="f:doc(\'x\')"/>'))
    # A prefix rebound on an element counts in it, not in the text after it.
    rebound = XSLT.replace('version="3.0">', 'version="3.0" xmlns:f="http://www.w3.org/2005/xpath-functions" '
                                             'expand-text="yes">')
    body = f'<x xmlns:f="{GEO}"/>{{f:doc(\'secret\')}}'
    assert [w for w, _ in unsafe_in_xslt(rebound.replace('{BODY}', body))] == ['doc()']


def test_a_mapping_may_call_its_shared_xslts_functions_whatever_their_names():
    from tests.test_xsltgen import BASE, SCHEMA, mapping as translation_mapping
    country = {'path': 'EventSource/Device/Location/Country', 'xpath': "geo:document(data[@name='host']/@value)"}
    bound = translation_mapping(functions=[{'href': 'GeoIP-V1', 'prefix': 'geo', 'namespace': GEO}],
                                common=BASE + [country])
    assert generate(bound, SCHEMA, '4.1.0')['ok']
    # A required import's function called before a functions entry binds its prefix: the problem says so, and
    # nothing calls it a document() read.
    unbound = generate(translation_mapping(imports=['GeoIP-V1'], common=BASE + [country]), SCHEMA, '4.1.0')
    assert any("no functions entry binds 'geo'" in p for p in unbound['problems'])
    assert not any('calls document()' in p for p in unbound['problems'])


def test_an_xslt_is_compiled_with_stroom_and_imported_functions_and_templates_stood_in_for():
    from utils.saxon import compile_errors
    sheet = ('<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0" xmlns:stroom="stroom" '
             f'xmlns:geo="{GEO}">{{imports}}<xsl:template match="/"><a>{{body}}</a></xsl:template></xsl:stylesheet>')
    fine = ("<xsl:value-of select=\"stroom:format-date('x', 'yyyy', 'UTC'), stroom:record-no(), geo:document(.)\"/>"
            "<xsl:call-template name=\"eventSource\"><xsl:with-param name=\"ip\" select=\"1\"/></xsl:call-template>")
    imports = '<xsl:import href="GeoIP-V1"/><xsl:import href="Common-Event-V1"/>'
    assert compile_errors(sheet.format(imports=imports, body=fine)) == ([], [])
    # The XSLT's own mistakes are not stood in for: a variable nothing declares, a function XPath doesn't have.
    errors, notes = compile_errors(sheet.format(imports='', body='<xsl:value-of select="$kv"/><xsl:value-of select="nosuch(1)"/>'))
    assert errors == ['XPST0008 Variable $kv has not been declared (or its declaration is not in scope)',
                      'XPST0017 Cannot find a 1-argument function named '
                      'Q{http://www.w3.org/2005/xpath-functions}nosuch()'] and not notes
    # With imports, the variable may be one of theirs: said, not refused.
    errors, notes = compile_errors(sheet.format(imports=imports, body='<xsl:value-of select="$kv"/>'))
    assert not errors and 'fine only if an import (GeoIP-V1, Common-Event-V1) declares it' in notes[0]
