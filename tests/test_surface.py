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
    assert tools['create_pipeline'].__doc__.startswith('Onboarding step 5 of 15 (pipeline): ')
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
    with pytest.raises(ToolError, match='Give set_properties'):
        await pipeline_writes.update_pipeline(None, 'p')
    with patch.object(pipeline_writes, 'set_pipeline_property', AsyncMock(return_value={'name': 'P', 'set': 'a.b'})), \
            patch.object(pipeline_writes, 'set_pipeline_references', AsyncMock(return_value={'name': 'P', 'reference_data': ['F via L']})):
        result = await pipeline_writes.update_pipeline(None, 'p', set_properties=[pipeline_writes.PropertyValue(element='a', name='b', value='c')],
                                                       references=[pipeline_writes.PipelineReference(feed='F')])
        assert result['set'] == ['a.b'] and result['reference_data'] == ['F via L']


def test_the_verification_dashboard_shows_the_users_fields_newest_first_and_a_stepping_text_pane():
    config = indexing.dashboard_config({'type': 'ElasticIndex', 'uuid': 'i', 'name': 'IDX'},
                                       ['@timestamp', 'user.name', 'StreamId'], '@timestamp', '2026-08-23T00:00:00.000Z')
    query, table, text = (c['settings'] for c in config['components'])
    columns = {c['name']: c for c in table['fields']}
    assert [c['name'] for c in table['fields'] if c.get('visible', True)] == ['@timestamp', 'user.name']
    assert columns['StreamId']['visible'] is False and columns['EventId']['visible'] is False
    assert columns['@timestamp']['sort'] == {'order': 0, 'direction': 'DESCENDING'}
    assert query['expression']['children'] == [{'type': 'term', 'field': '@timestamp', 'condition': 'BETWEEN',
                                                'value': '2026-08-23T00:00:00.000Z,day()+1d'}]
    assert query['automate']['open'] is True and table['extractValues'] is False
    assert text['showStepping'] is True and 'pipeline' not in text and text['tableId'] == 'table-VERIFY'
    assert text['streamIdField'] == {'id': columns['StreamId']['id'], 'name': 'StreamId'}
    assert text['recordNoField'] == {'id': columns['EventId']['id'], 'name': 'EventId'}
    # Laid out as Stroom's UI lays out a dashboard (seen on live: without sizes and the config's own settings, the UI
    # showed the dashboard empty, though searches ran): the query above, the table and text pane side by side.
    def panes(node):
        return [t['id'] for t in node.get('tabs') or []] + [p for c in node.get('children') or [] for p in panes(c)]

    def sized(node):
        return 'preferredSize' in node and all(sized(c) for c in node.get('children') or [])
    assert panes(config['layout']) == ['query-VERIFY', 'table-VERIFY', 'text-VERIFY'] and sized(config['layout'])
    assert config['layout']['children'][1]['dimension'] == 0
    assert config['layoutConstraints'] == {'fitWidth': True, 'fitHeight': True} and config['modelVersion']
    assert config['designMode'] is False and config['preferredSize'] == {'width': 0, 'height': 0}


def test_the_dashboard_window_starts_at_a_30_day_boundary_before_the_earliest_event():
    # 2026-10-01 is day 20727 since the epoch; the boundary before it is day 20700, 2026-09-04.
    assert indexing.window_start(['2026-10-02T08:00:00.000Z', '2026-10-01T09:00:00Z', 'not a time']) == '2026-09-04T00:00:00.000Z'
    assert indexing.window_start([]) is None


async def test_verify_index_asks_before_creating_reuses_its_own_and_saves_nothing_for_another_builds_index():
    from utils.consent import ConsentStore
    index = {'name': 'IDX', 'timeField': '@timestamp'}
    rows = {'rows': [{'@timestamp': '2026-10-01T09:00:00.000Z', 'StreamId': '1', 'EventId': '1'}], 'errors': []}

    async def run(contents, dashboard=None):
        docs = {'i': index, 'd1': dashboard or {}}
        stroom = SimpleNamespace(get_doc=AsyncMock(side_effect=lambda t, u: docs[u]), put_doc=AsyncMock(),
                                 settings=SimpleNamespace(stroom_url='https://stroom.example', stroom_ui_url=None))
        ctx = SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': ConsentStore(False)})
        guard = SimpleNamespace(folder_contents=AsyncMock(return_value=contents))
        with patch.object(indexing, 'guard_from', lambda c: guard), \
                patch.object(indexing, '_search', AsyncMock(return_value=rows)), \
                patch.object(indexing, 'create_verification_dashboard', AsyncMock(return_value={'uuid': 'new'})) as create, \
                patch.object(indexing, 'run_test_searches', AsyncMock(return_value={'passed': True, 'checks': []})) as searched:
            docs['new'] = {'dashboardConfig': {}}
            result = await indexing.verify_index(ctx, 'b', 'i', 'elasticsearch', [1], 1, ['@timestamp', 'user.name'])
        return result, create, searched
    asked, create, _ = await run([{'type': 'ElasticIndex', 'uuid': 'i', 'name': 'IDX'}])
    assert asked['status'] == 'needs_confirmation' and create.await_count == 0
    assert asked['details']['columns (newest first)'] == ['@timestamp', 'user.name']
    assert asked['details']['initial query'] == '@timestamp from 2026-09-04T00:00:00.000Z through today'
    mine = {'dashboardConfig': indexing.dashboard_config({'type': 'ElasticIndex', 'uuid': 'i'}, ['@timestamp', 'user.name'],
                                                         '@timestamp', None), 'uuid': 'd1'}
    reused, create, _ = await run([{'type': 'ElasticIndex', 'uuid': 'i', 'name': 'IDX'},
                                   {'type': 'Dashboard', 'uuid': 'd1', 'name': 'IDX-VERIFY'}], mine)
    assert reused['passed'] and reused['dashboard']['uuid'] == 'd1' and create.await_count == 0
    # The link to give the user (seen in VS Code: they had to ask for it); none for an unsaved dashboard.
    assert reused['dashboard']['link'].endswith('docType=Dashboard&docUuid=d1')
    elsewhere, create, searched = await run([])
    assert elsewhere['dashboard']['saved'] is False and 'link' not in elsewhere['dashboard'] and create.await_count == 0
    assert searched.await_args.kwargs['dashboard_doc']['dashboardConfig']['components'][0]['settings']['dataSource']['uuid'] == 'i'


async def test_every_page_of_documents_is_read_when_there_are_more_than_one():
    from utils.stroom import StroomGateway
    pages = [[{'docRef': {'uuid': str(n)}} for n in range(start, min(start + 3, 7))] for start in (0, 3, 6)]
    calls = []

    async def find(name, types, limit, offset=0):
        calls.append(offset)
        return {'values': pages[offset // 3], 'pageResponse': {'total': 7}}
    gateway = StroomGateway.__new__(StroomGateway)
    gateway.find_documents = find
    found = await StroomGateway.find_all_documents(gateway, 'type:Pipeline', ['Pipeline'], page=3)
    assert [v['docRef']['uuid'] for v in found] == [str(n) for n in range(7)] and calls == [0, 3, 6]



async def test_paging_stops_when_stroom_returns_the_same_page_again():
    from utils.stroom import StroomGateway
    page = [{'docRef': {'uuid': str(n)}} for n in range(3)]
    calls = []

    async def find(name, types, limit, offset=0):
        calls.append(offset)
        return {'values': page}               # the offset ignored, and no total
    gateway = StroomGateway.__new__(StroomGateway)
    gateway.find_documents = find
    found = await StroomGateway.find_all_documents(gateway, 'type:Pipeline', ['Pipeline'], page=3)
    assert [v['docRef']['uuid'] for v in found] == ['0', '1', '2'] and calls == [0, 3]



async def test_promotion_looks_again_when_the_build_lists_empty_right_after_a_write():
    from tools import builds
    listings = [[], [], [{'type': 'Documentation', 'uuid': 'd', 'name': 'D', 'path': 'x', 'working_copy_of': None}]]
    lister = AsyncMock(side_effect=lambda ctx, build: listings.pop(0) if listings else [])
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value={'values': []}))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    with patch.object(builds, '_build_docs', lister), patch.object(builds, '_LISTING_WAIT', 0), \
            patch.object(builds, 'guard_from', lambda c: SimpleNamespace()):
        with pytest.raises(ToolError, match='No destination'):     # found the doc, then wants its destination
            await builds.promote_build(ctx, 'b', destinations={})
    assert lister.await_count == 3
    with patch.object(builds, '_build_docs', AsyncMock(return_value=[])), patch.object(builds, '_LISTING_WAIT', 0), \
            patch.object(builds, 'guard_from', lambda c: SimpleNamespace()):
        with pytest.raises(ToolError, match='has no documents'):
            await builds.promote_build(ctx, 'b', destinations={})


async def test_a_pipelines_documentation_follows_the_pipeline_promoted_with_it():
    from tools import builds
    docs = [{'type': 'Pipeline', 'uuid': 'p', 'name': 'ACME-Events', 'path': 'x', 'working_copy_of': None},
            {'type': 'Documentation', 'uuid': 'd', 'name': 'ACME-Events', 'path': 'x', 'working_copy_of': None}]
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value={'values': []}))
    guard = SimpleNamespace(resolve_folder=AsyncMock(side_effect=RuntimeError('planned')))
    with patch.object(builds, '_build_docs', AsyncMock(return_value=docs)), patch.object(builds, 'guard_from', lambda c: guard):
        with pytest.raises(RuntimeError, match='planned'):      # every doc had a destination: on to the folders
            await builds.promote_build(SimpleNamespace(lifespan_context={'stroom': stroom}), 'b',
                                       destinations={'Pipeline': 'System/Feeds/Acme'})



async def test_schema_validation_is_never_changed_only_the_output():
    from tools import pipeline_writes
    stroom = SimpleNamespace(
        get_doc=AsyncMock(return_value={'uuid': 'p', 'name': 'ACME - Indexing', 'pipelineData': {}}),
        pipeline_layers=AsyncMock(return_value=[{'pipelineData': {'elements': {'add': [
            {'id': 'xsltFilter', 'type': 'XSLTFilter'}, {'id': 'schemaFilter', 'type': 'SchemaFilter'}]}}}]),
        put_doc=AsyncMock())
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    guard = SimpleNamespace(check_managed=AsyncMock())
    with patch.object(pipeline_writes, 'guard_from', lambda c: guard):
        with pytest.raises(ToolError, match='Fix the output instead'):
            await pipeline_writes.set_pipeline_property(ctx, 'p', pipeline_writes.PropertyValue(
                element='schemaFilter', name='schemaValidation', value='false'))
    assert stroom.put_doc.await_count == 0
