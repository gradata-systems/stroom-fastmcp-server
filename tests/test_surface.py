"""The merged tools dispatch to the functions they replace, and the core tools carry their plan step."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

import main_tools
from tools import explorer, indexing, pipeline_writes, plan, streams, translation, validation


def test_the_surface_is_smaller_and_every_core_tool_names_its_plan_step():
    tools = {t.__name__: t for m in main_tools.TOOL_MODULES for t in m.ALL_TOOLS}
    assert len(tools) <= 52, sorted(tools)
    for gone in ('list_build', 'get_document', 'describe_pipeline', 'validate_events', 'create_xslt', 'update_xslt',
                 'set_pipeline_property', 'run_test_searches', 'summarise_events'):
        assert gone not in tools
    plan.annotate_tools(main_tools.TOOL_MODULES)
    assert tools['create_pipeline'].__doc__.startswith('Onboarding step 5 of 14 (pipeline): ')
    assert tools['start_onboarding'].__doc__.startswith('Onboarding start: ')
    assert tools['build_status'].__doc__.startswith('Onboarding, any step: ')
    assert not tools['locate_event'].__doc__.lstrip().startswith('Onboarding')
    plan.annotate_tools(main_tools.TOOL_MODULES)   # once only
    assert tools['create_pipeline'].__doc__.count('Onboarding') == 1


async def test_merged_read_tools_combine_their_parts():
    with patch.object(explorer, 'get_document', AsyncMock(side_effect=lambda *a: {'data': '<x/>', 'name': 'T'})), \
            patch('tools.pipelines.describe_pipeline', AsyncMock(return_value={'chain': ['a']})), \
            patch('tools.validation.describe_translation', AsyncMock(return_value={'mappings': []})):
        assert (await explorer.describe_document(None, 'Pipeline', 'p'))['pipeline'] == {'chain': ['a']}
        assert (await explorer.describe_document(None, 'XSLT', 'x'))['translation'] == {'mappings': []}
        assert 'pipeline' not in await explorer.describe_document(None, 'Feed', 'f')
    with patch.object(streams, 'get_stream_children', AsyncMock(return_value={'children': [1], 'by_type': {'Events': 1}})), \
            patch.object(streams, 'get_stream_attributes', AsyncMock(side_effect=ToolError('no headers'))):
        result = await streams.describe_stream(None, 7)
        assert result['children_by_type'] == {'Events': 1} and result['attributes_note'] == 'no headers'
    with patch.object(streams, 'summarise_errors', AsyncMock(return_value={'groups': []})), \
            patch.object(streams, 'summarise_events', AsyncMock(return_value={'events': 3})):
        assert (await streams.summarise_streams(None, [1, 2], 'errors'))['streams'] == {1: {'groups': []}, 2: {'groups': []}}
        assert await streams.summarise_streams(None, [1], 'events') == {'events': 3}
    with patch.object(validation, 'validate_events', AsyncMock(return_value={'valid': True})), \
            patch.object(validation, 'check_event_quality', AsyncMock(return_value={'ok': False, 'rules': {'device': {}}})):
        result = await validation.check_events(None, '<Events/>')
        assert result['ok'] is False and result['quality']['rules'] == {'device': {}}


async def test_save_tools_create_or_update_by_uuid():
    with patch.object(translation, 'create_xslt', AsyncMock(return_value={'uuid': 'new'})) as create, \
            patch.object(translation, 'update_xslt', AsyncMock(return_value={'uuid': 'old'})) as update:
        assert (await translation.save_xslt(None, 'b', 'n', '<x/>'))['uuid'] == 'new'
        assert (await translation.save_xslt(None, 'b', 'n', '<x/>', uuid='old', version='3'))['uuid'] == 'old'
        assert create.await_count == 1 and update.await_args.args[1:4] == ('old', '<x/>', '3')
    with patch.object(translation, 'create_dictionary', AsyncMock(return_value={'uuid': 'd'})), \
            patch.object(translation, 'update_dictionary', AsyncMock(return_value={'uuid': 'd2'})):
        assert (await translation.save_dictionary(None, 'b', 'VIP', 'a\nb'))['uuid'] == 'd'
        assert (await translation.save_dictionary(None, 'b', 'VIP', 'a', uuid='d2'))['uuid'] == 'd2'
    with pytest.raises(ToolError, match='Give properties to set'):
        await pipeline_writes.update_pipeline(None, 'p')
    with patch.object(pipeline_writes, 'set_pipeline_property', AsyncMock(return_value={'name': 'P', 'set': 'a.b'})), \
            patch.object(pipeline_writes, 'set_pipeline_references', AsyncMock(return_value={'name': 'P', 'reference_data': ['F via L']})):
        result = await pipeline_writes.update_pipeline(None, 'p', properties=[pipeline_writes.PropertyValue(element='a', name='b', value='c')],
                                                       references=[pipeline_writes.PipelineReference(feed='F')])
        assert result['set'] == ['a.b'] and result['reference_data'] == ['F via L']


async def test_verify_index_reuses_the_builds_dashboard():
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(get_doc=AsyncMock(return_value={'name': 'IDX'}))})
    guard = SimpleNamespace(folder_contents=AsyncMock(return_value=[{'type': 'Dashboard', 'uuid': 'd1', 'name': 'IDX-VERIFY'}]))
    with patch.object(indexing, 'guard_from', lambda c: guard), \
            patch.object(indexing, 'create_verification_dashboard', AsyncMock()) as create, \
            patch.object(indexing, 'run_test_searches', AsyncMock(return_value={'passed': True, 'checks': []})):
        result = await indexing.verify_index(ctx, 'b', 'i', 'lucene', [1], 3, ['StreamId'])
    assert result['dashboard'] == {'uuid': 'd1', 'name': 'IDX-VERIFY'} and result['passed'] and create.await_count == 0
