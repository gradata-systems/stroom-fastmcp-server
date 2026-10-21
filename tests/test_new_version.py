"""A production pipeline's next version, its translation written anew: a child of the same template, with the build's
XSLT, the previous version's converter and settings, and its reference data if the user keeps them; and the current
XSLT's lookups named while the mapping is drafted and saved.

Seen in testing: V1.3 of a Fortigate feed drafted from the sample lost V1.2's IP lookups, and the plan's pipeline step
said to find the best template, so the agent offered copying V1.2 or a pipeline unlike it, and the user asked why."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tests.test_existing_data_plan import _doc, status
from tools import pipeline_writes, plan
from tools.pipeline_writes import (PropertyValue, _carried_references, _not_carried, _plain_settings, create_pipeline,
                                   fill_open_slots)
from tools.pipelines import merge_layers
from tests.test_slots import TEXT_TEMPLATE
from utils.xsltgen import TranslationMapping

REF = {'element': 'translationFilter', 'name': 'pipelineReference', 'streamType': 'Reference',
       'pipeline': {'type': 'Pipeline', 'uuid': 'loader', 'name': 'Reference Loader'},
       'feed': {'type': 'Feed', 'uuid': 'geo', 'name': 'GEO-IP'}}
PREVIOUS = {'uuid': 'v12', 'name': 'FG-V1.2', 'parentPipeline': {'type': 'Pipeline', 'uuid': 'tpl', 'name': 'Event Data (Text)'},
            'pipelineData': {
                'properties': {'add': [
                    {'element': 'dsParser', 'name': 'textConverter',
                     'value': {'entity': {'type': 'TextConverter', 'uuid': 'tc12', 'name': 'FG-V1.2'}}},
                    {'element': 'translationFilter', 'name': 'xslt',
                     'value': {'entity': {'type': 'XSLT', 'uuid': 'x12', 'name': 'FG-Events-V1.2'}}},
                    {'element': 'translationFilter', 'name': 'warnOnly', 'value': {'boolean': True}},
                    {'element': 'gone', 'name': 'flag', 'value': {'boolean': True}}]},
                'pipelineReferences': {'add': [REF]},
                'elements': {'add': [{'id': 'extraFilter', 'type': 'XSLTFilter'}]}}}


def _ctx(previous=PREVIOUS):
    stroom = SimpleNamespace(get_doc=AsyncMock(return_value=previous))
    return SimpleNamespace(lifespan_context={'stroom': stroom}), stroom


async def test_a_version_of_a_pipeline_with_reference_data_asks_whether_to_keep_them():
    ctx, _ = _ctx()
    with patch('tools.plan.resolve_build', lambda c, b, t: 'b'), patch.object(pipeline_writes, 'gateway_from', lambda c: c.lifespan_context['stroom']):
        with pytest.raises(ToolError, match=r"'FG-V1.2' loads reference data for its lookups: \['GEO-IP via Reference "
                                            r"Loader on translationFilter'\]\. Ask the user .*keep_references=true or false"):
            await create_pipeline(ctx, name='FG-V1.3', version_of='v12')
        with pytest.raises(ToolError, match='keep_references is for a new version'):
            await create_pipeline(ctx, name='FG-V1.3', template='tpl', keep_references=True)
    orphan, _ = _ctx({**PREVIOUS, 'parentPipeline': None})
    with patch('tools.plan.resolve_build', lambda c, b, t: 'b'), patch.object(pipeline_writes, 'gateway_from', lambda c: c.lifespan_context['stroom']):
        with pytest.raises(ToolError, match="inherits from no template.*copy_pipeline source_uuid='v12'"):
            await create_pipeline(orphan, name='FG-V1.3', version_of='v12', keep_references=False)


def test_the_previous_versions_references_come_on_the_same_element_once():
    merged = merge_layers(TEXT_TEMPLATE)
    assert _carried_references(merged, [REF], []) == [REF]
    # Its element gone from the new template: the first XSLT step does the lookups; one references gives is not doubled.
    moved = _carried_references(merged, [{**REF, 'element': 'oldFilter'}], [])
    assert moved[0]['element'] == 'translationFilter'
    assert _carried_references(merged, [REF], [REF]) == []


def test_the_previous_versions_plain_settings_are_kept_and_the_rest_named():
    types = {e['id']: e['type'] for e in merge_layers(TEXT_TEMPLATE)['elements']}
    kept = _plain_settings(PREVIOUS, types, [])
    assert [(p.element, p.name, p.value) for p in kept] == [('translationFilter', 'warnOnly', True)]
    properties = kept + [PropertyValue(element='translationFilter', name='xslt', doc_uuid='x13', doc_type='XSLT'),
                         PropertyValue(element='dsParser', name='textConverter', doc_uuid='tc12', doc_type='TextConverter')]
    assert _not_carried(PREVIOUS, properties) == ['gone.flag', 'element extraFilter (XSLTFilter) added']


async def test_a_version_with_no_converter_of_its_own_reads_the_feed_with_the_previous_ones():
    docs = [{'type': 'XSLT', 'uuid': 'x13', 'name': 'FG-Events-V1.3'}]
    stroom = SimpleNamespace(get_doc=AsyncMock(return_value={'description': ''}))
    guard = SimpleNamespace(folder_contents=AsyncMock(return_value=docs))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    merged = merge_layers(TEXT_TEMPLATE)
    with patch.object(pipeline_writes, 'guard_from', lambda c: guard):
        props, filled, still_open = await fill_open_slots(ctx, 'b', merged, None, [], PREVIOUS)
        assert [(p.name, p.doc_uuid) for p in props] == [('textConverter', 'tc12'), ('xslt', 'x13')]
        assert filled[0] == 'dsParser.textConverter = FG-V1.2 (as FG-V1.2 has it)' and still_open == []
        # Not for a new source: the build needs a converter of its own.
        with pytest.raises(ToolError, match='needs a text converter'):
            await fill_open_slots(ctx, 'b', merged, None, [])


CURRENT = {'pipeline': {'uuid': 'v12', 'name': 'FG-V1.2'}, 'xslt': [{'name': 'FG-Events-V1.2', 'uuid': 'x12'}],
           'reference_data': ['GEO-IP via Reference Loader on translationFilter'], 'lookup_maps': ['GEOIP'],
           'functions_called': [{'href': 'IP Lookup', 'function': 'gs:geo', 'example': 'gs:geo($ip)'}],
           'functions_entries': [{'href': 'IP Lookup', 'prefix': 'gs', 'namespace': 'http://gradata.com.au'}]}


async def test_the_current_translation_is_read_with_what_its_xslt_looks_up():
    xslt = "<xsl:value-of select=\"stroom:lookup('GEOIP', $ip)\"/><xsl:value-of select=\"stroom:lookup( 'DNS', $ip)\"/>"
    docs = {'Pipeline': {**PREVIOUS, 'uuid': 'v12'}, 'XSLT': {'data': xslt}}
    stroom = SimpleNamespace(get_doc=AsyncMock(side_effect=lambda t, u: docs[t]), pipeline_layers=AsyncMock(return_value=[]))
    uses = [{'href': 'IP Lookup', 'function': 'gs:geo', 'prefix': 'gs', 'namespace': 'http://gradata.com.au',
             'example': 'gs:geo($ip)', 'calls': 3, 'used_by': ['FG-V1.2']},
            {'href': 'IP Lookup', 'document_contents': {}}]
    with patch.object(plan, 'gateway_from', lambda c: stroom), \
            patch.object(plan, 'works_from', AsyncMock(return_value=['FG'])), \
            patch.object(plan, '_translating', AsyncMock(return_value=[{'uuid': 'v12', 'name': 'FG-V1.2'}])), \
            patch('tools.pipelines.translation_docs', lambda u, l: [
                {'doc': {'type': 'XSLT', 'uuid': 'x12', 'name': 'FG-Events-V1.2'}, 'inherited_from_template': False}]), \
            patch('tools.templates.shared_xslt_usage', AsyncMock(return_value=uses)):
        current = await plan.current_translation(None, 'b')
    assert current == {**CURRENT, 'lookup_maps': ['DNS', 'GEOIP'], 'functions_called': [
        {'href': 'IP Lookup', 'function': 'gs:geo', 'example': 'gs:geo($ip)'}]}
    note = plan.lookups_note(current)
    assert note.startswith("The feed is translated now by 'FG-V1.2' (v12), whose XSLT looks up reference map(s) "
                           "['DNS', 'GEOIP'] with stroom:lookup(); calls gs:geo() from 'IP Lookup' (as gs:geo($ip))")
    assert "Ask the user whether the new version keeps these lookups" in note
    assert "create_pipeline version_of='v12' keep_references=true" in note
    with patch.object(plan, 'works_from', AsyncMock(return_value=[])), \
            patch.object(plan, '_translating', AsyncMock(return_value=[])), patch.object(plan, 'gateway_from', lambda c: stroom):
        assert await plan.current_translation(None, 'b') is None


def test_a_mapping_keeps_the_lookups_by_a_lookup_entry_or_the_shared_xslt_it_calls():
    base = {'input': 'json', 'common': [{'path': 'EventSource/System/Name', 'value': 'FortiOS'}], 'events': [
        {'name': 'all', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'}]}]}
    assert not plan.lookups_kept(CURRENT, TranslationMapping.model_validate(base))
    calling = {**base, 'functions': CURRENT['functions_entries']}
    assert plan.lookups_kept(CURRENT, TranslationMapping.model_validate(calling))
    looking = {**base, 'common': base['common'] + [{'path': 'EventSource/Device/Location/Country',
                                                    'lookup': {'map': 'GEOIP', 'field': 'srcip'}}]}
    assert plan.lookups_kept(CURRENT, TranslationMapping.model_validate(looking))
    assert plan.lookups_of(CURRENT) and not plan.lookups_of({'pipeline': CURRENT['pipeline']}) and not plan.lookups_of(None)


async def test_an_existing_feeds_next_version_is_made_from_the_pipeline_translating_it():
    docs = [_doc('Documentation', 's', 'FG - Survey'), _doc('XSLT', 'x13', 'FG-Events-V1.3')]
    survey = {'saturated': True, 'locations': None, 'streams': [5], 'sample': None, 'format': 'json array'}
    reading = [{'uuid': 'v12', 'name': 'FG-V1.2', 'reads_events': False}]
    with patch('tools.describe._pipelines_reading', AsyncMock(return_value=reading)):
        result = await status(docs, sources=['FG'], survey=survey, mappings={'x13': ('translation', {})})
    assert result['next']['step'] == 'pipeline'
    assert result['next']['call'] == {'tool': 'create_pipeline', 'arguments': {
        'build': 'b', 'version_of': 'v12', 'name': "<the new version's name ('FG-V1.2' with its new version)>"}}
    assert 'keep_references' in result['next']['then']
    # Not surveyed first: the XSLT saved from its mapping is what starts the events stage.
    with patch('tools.describe._pipelines_reading', AsyncMock(return_value=reading)):
        unsurveyed = await status(docs[1:], sources=['FG'], mappings={'x13': ('translation', {})})
    assert unsurveyed['next']['step'] == 'pipeline' and unsurveyed['next']['call']['tool'] == 'create_pipeline'
    # A feed nothing translates: the best template, as for a new source.
    with patch('tools.describe._pipelines_reading', AsyncMock(return_value=[])):
        result = await status(docs, sources=['FG'], survey=survey, mappings={'x13': ('translation', {})})
    assert result['next']['call']['tool'] == 'find_pipeline_templates'
