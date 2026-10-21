"""Shared XSLTs standing instructions require (asked for by the user: every Raw Events to Events translation imports a
Stream-Meta-V1 translation): an AGENTS doc says so per stage, the generator adds the import, and an XSLT written by
hand without it is refused."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tools.instructions import required_imports
from utils.eventschema import EventSchema
from utils.xsltgen import TranslationMapping, generate

SCHEMA = EventSchema.parse((Path(__file__).parent / 'fixtures' / 'event-logging-v4.1.0.xsd').read_bytes())
AGENTS = """# Standing instructions

## Fields
- Host names are lower case.

## Required imports
- Raw Events to Events: `Stream-Meta-V1`
- indexing: Common-Index-V1, Lookups-V2
- something else: Ignored-V1

## Naming
- translation: not an import
"""


def test_the_required_imports_section_is_read_per_stage():
    found = required_imports([{'folder': 'System', 'instructions': AGENTS},
                              {'folder': 'System/Feeds/Acme', 'instructions': '## Required imports\n- translation: Stream-Meta-V1, Acme-V1'}])
    assert found == {'translation': [{'href': 'Stream-Meta-V1', 'from': 'System'}, {'href': 'Acme-V1', 'from': 'System/Feeds/Acme'}],
                     'indexing': [{'href': 'Common-Index-V1', 'from': 'System'}, {'href': 'Lookups-V2', 'from': 'System'}]}
    assert required_imports([{'folder': 'System', 'instructions': '## Fields\n- translation: X'}]) == {}


def test_a_mappings_imports_are_written_first_in_the_stylesheet():
    mapping = TranslationMapping.model_validate({
        'input': 'json', 'json_layout': 'lines', 'imports': ['Stream-Meta-V1'],
        'common': [{'path': 'EventTime/TimeCreated', 'field': 'ts', 'time_format': "yyyy-MM-dd'T'HH:mm:ssX"},
                   {'path': 'EventSource/System/Name', 'value': 'X'}, {'path': 'EventSource/System/Environment', 'value': 'T'},
                   {'path': 'EventSource/Generator', 'value': 'g'}, {'path': 'EventSource/Device/HostName', 'value': 'h'},
                   {'path': 'EventDetail/TypeId', 'value': 'x'}],
        'events': [{'name': 'a', 'fields': [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                                            {'path': 'EventDetail/Authenticate/User/Id', 'field': 'u'}]}]})
    xslt = generate(mapping, SCHEMA, '4.1.0')['xslt']
    first = xslt.split('<xsl:import', 1)
    assert len(first) == 2 and 'href="Stream-Meta-V1"' in first[1].split('>', 1)[0]
    assert '<xsl:template' not in first[0]          # before any template


async def test_an_xslt_written_by_hand_without_a_required_import_is_refused():
    from tools import translation
    hand = ('<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns="event-logging:3" version="3.0">'
            '<xsl:template match="/"><Events/></xsl:template></xsl:stylesheet>')
    required = AsyncMock(return_value=[{'href': 'Stream-Meta-V1', 'from': 'System'}])
    with patch('tools.generation._required_for', required):
        with pytest.raises(ToolError, match=r"require every translation XSLT to import 'Stream-Meta-V1'.*"
                                            r'<xsl:import href="Stream-Meta-V1"/>'):
            await translation._refuse_without_required_imports(None, hand, 'b')
        with_it = hand.replace('version="3.0">', 'version="3.0"><xsl:import href="Stream-Meta-V1"/>')
        await translation._refuse_without_required_imports(None, with_it, 'b')       # has it: fine
    assert required.await_args.args[1] == 'translation'


async def test_each_stage_is_held_to_its_own_required_imports():
    from tools import translation
    # The AGENTS doc requires an import of indexing XSLTs only.
    required = AsyncMock(side_effect=lambda ctx, stage, build: [{'href': 'Common-Index-V1', 'from': 'System'}]
                         if stage == 'indexing' else [])
    with patch('tools.generation._required_for', required):
        with pytest.raises(ToolError, match="every indexing XSLT to import 'Common-Index-V1'"):
            await translation._refuse_without_required_imports(None, '<records xmlns="records:2"/>', 'b', 'Indexing')
        await translation._refuse_without_required_imports(None, '<records/>', 'b', 'Records')
    assert [c.args[1] for c in required.await_args_list] == ['indexing', 'records']


async def test_an_indexing_xslt_saved_from_its_plan_gets_the_required_imports():
    from tools import translation
    from utils.fieldplan import FieldPlan, PlannedField
    plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v1', time_field='@timestamp',
                     fields=[PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated')])
    generated = {}

    async def code_of(p):
        generated['plan'] = p
        return '<xsl:stylesheet/>'
    with patch('tools.generation._required_for', AsyncMock(return_value=[{'href': 'Common-Index-V1', 'from': 'System'}])), \
            patch.object(translation, '_generated', code_of), \
            patch.object(translation, 'create_xslt', AsyncMock(return_value={'uuid': 'x'})):
        await translation.save_xslt(SimpleNamespace(lifespan_context={}), 'b', 'ACME - Indexing', index_plan=plan)
    assert generated['plan'].imports == ['Common-Index-V1']
    assert '<xsl:import href="Common-Index-V1" />' in generated['plan'].model_copy()._imports()


async def test_saving_over_an_xslt_is_held_to_the_required_imports_too():
    from tools import translation
    stroom = SimpleNamespace(get_doc=AsyncMock(return_value={'uuid': 'x', 'name': 'ACME-Events', 'data': ''}))
    guard = SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), build_of=AsyncMock(return_value='b'))
    hand = '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0"><Events/></xsl:stylesheet>'
    with patch.object(translation, '_checked', AsyncMock()), patch.object(translation, 'gateway_from', lambda c: stroom), \
            patch.object(translation, 'guard_from', lambda c: guard), \
            patch('tools.generation._required_for', AsyncMock(return_value=[{'href': 'Stream-Meta-V1', 'from': 'System'}])):
        with pytest.raises(ToolError, match="import 'Stream-Meta-V1'"):
            await translation.update_xslt(None, 'x', hand)


def test_cef_and_reference_xslts_write_their_imports_first():
    from utils import cef
    from utils.refgen import ReferenceMapping, generate_reference
    for output in ('kafka', 'text'):
        xslt = cef.CefPlan(output=output, topic='t', imports=['Cef-Common-V1'], vendor=cef.CefValue(value='Acme'),
                           product=cef.CefValue(value='P'), version=cef.CefValue(value='1'),
                           signature=cef.CefValue(source='EventDetail/TypeId'), name=cef.CefValue(value='n'),
                           common=[cef.CefField(path='EventSource/User/Id', key='suser')]).xslt()
        head = xslt.split('<xsl:template', 1)[0]
        assert '<xsl:import href="Cef-Common-V1"/>' in head, output
    ref = generate_reference(ReferenceMapping.model_validate({
        'input': 'json', 'imports': ['Ref-Common-V1'],
        'maps': [{'name': 'HOSTS', 'key': 'host', 'values': [{'field': 'ip'}]}]}))
    assert ref['ok'] and '<xsl:import href="Ref-Common-V1"/>' in ref['xslt'].split('<xsl:template', 1)[0], ref.get('problems')


async def test_a_reference_xslt_built_for_a_build_gets_the_required_imports():
    from tools import generation
    required = AsyncMock(return_value=[{'href': 'Ref-Common-V1', 'from': 'System'}])
    mapping = {'input': 'json', 'maps': [{'name': 'HOSTS', 'key': 'host', 'values': [{'field': 'ip'}]}]}
    with patch.object(generation, '_required_for', required), \
            patch.object(generation, '_import_notes', AsyncMock(return_value=[{'import': 'Ref-Common-V1'}])):
        built = await generation.build_reference_xslt(SimpleNamespace(lifespan_context={}),
                                                      generation.ReferenceMapping.model_validate(mapping),
                                                      schema_version='2.0.1', build='b')
    assert '<xsl:import href="Ref-Common-V1"/>' in built['xslt'] and built['required_imports']
    assert required.await_args.args[1:] == ('reference', 'b')


GEOIP = '''<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:geo="http://example.com/geo"
  xmlns:xs="http://www.w3.org/2001/XMLSchema" version="3.0">
  <xsl:function name="geo:lookup-ip" as="xs:string"><xsl:param name="ip" as="xs:string"/><xsl:sequence select="$ip"/></xsl:function>
  <xsl:function name="geo:document"><xsl:param name="ip"/><xsl:sequence select="$ip"/></xsl:function>
</xsl:stylesheet>'''


async def test_a_required_imports_functions_are_offered_with_the_entry_that_binds_them():
    # An AGENTS doc can require a shared XSLT for its functions, not only its templates.
    from tools.generation import _import_notes
    stroom = SimpleNamespace(
        find_documents=AsyncMock(return_value={'values': [{'docRef': {'type': 'XSLT', 'uuid': 'g', 'name': 'GeoIP-V1'}}]}),
        get_doc=AsyncMock(return_value={'data': GEOIP}))
    required = [{'href': 'GeoIP-V1', 'from': 'System/Feeds'}]
    with patch('utils.stroom.gateway_from', lambda c: stroom):
        [note] = await _import_notes(None, required, functions=True)
        [reference] = await _import_notes(None, required)
    assert note['functions'] == {'geo:lookup-ip': {'params': ['ip as xs:string'], 'returns': 'xs:string'},
                                 'geo:document': {'params': ['ip']}}
    assert note['functions_entries'] == [{'href': 'GeoIP-V1', 'prefix': 'geo', 'namespace': 'http://example.com/geo'}]
    assert 'functions' not in reference      # a reference mapping has no functions entries to call them with

    # Bound so, its functions are called whatever their names; the import is written once.
    from tests.test_xsltgen import BASE, mapping
    m = mapping(imports=['GeoIP-V1'], functions=note['functions_entries'],
                common=BASE + [{'path': 'EventSource/Device/Location/Country',
                                'xpath': "geo:document(geo:lookup-ip(data[@name='host']/@value))"}])
    result = generate(m, SCHEMA, '4.1.0')
    assert result['ok'] and result['xslt'].count('href="GeoIP-V1"') == 1
    from utils.saxon import unsafe_in_xslt
    assert unsafe_in_xslt(result['xslt']) == []     # saved as it is: geo is the XSLT's, not fn
