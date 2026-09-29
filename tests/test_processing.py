import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from config import Settings
from tools import processing_writes
from utils.consent import ConsentStore
from utils.stroom import StroomGateway

SETTINGS = Settings(_env_file=None, stroom_url='https://stroom.example', dev_no_auth=True, stroom_api_key='k')
API = 'https://stroom.example/api'


def layers(elastic: bool) -> list[dict]:
    element = ({'id': 'elasticIndexingFilter', 'type': 'ElasticIndexingFilter'} if elastic
               else {'id': 'translationFilter', 'type': 'XSLTFilter'})
    props = [{'element': 'elasticIndexingFilter', 'name': 'indexName', 'value': {'string': 'ecs-acme-v2'}},
             {'element': 'elasticIndexingFilter', 'name': 'cluster',
              'value': {'entity': {'type': 'ElasticCluster', 'uuid': 'c', 'name': 'ES_DEV'}}}] if elastic else []
    return [{'sourcePipeline': {'type': 'Pipeline', 'uuid': 'p1', 'name': 'Acme'},
             'pipelineData': {'elements': {'add': [element]}, 'properties': {'add': props}}}]


@pytest.fixture
async def ctx():
    stroom = StroomGateway(SETTINGS)
    yield SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': ConsentStore(False)})
    await stroom.close()


def mock_stroom(elastic: bool, filtered: list[int] = (), with_output: list[int] = (), streams: dict | None = None):
    """p1 is the pipeline under test (Elasticsearch indexing, or a translation); 'ev' is an events pipeline.

    streams maps a stream id to (type, producing pipeline); by default indexing reads Events from 'ev' and a
    translation reads Raw Events.
    """
    streams = streams or {i: ('Events', 'ev') if elastic else ('Raw Events', None) for i in range(1, 20)}
    respx.get(f'{API}/pipeline/v1/p1').mock(return_value=httpx.Response(200, json={'uuid': 'p1', 'name': 'Acme'}))
    respx.get(f'{API}/pipeline/v1/ev').mock(return_value=httpx.Response(200, json={'uuid': 'ev', 'name': 'Acme-Events'}))
    respx.post(f'{API}/pipeline/v1/fetchPipelineLayers').mock(side_effect=lambda request: httpx.Response(
        200, json=layers(elastic and json.loads(request.content)['uuid'] == 'p1')))
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 3, 'pipelineUuid': 'p1', 'queryData': {'expression': {
            'type': 'operator', 'op': 'OR', 'children': [{'type': 'term', 'field': 'Id', 'condition': 'EQUALS',
                                                          'value': str(i)} for i in filtered]}}}}]}))

    def meta(request):
        terms = json.loads(request.content)['expression']['children']
        if terms[0]['field'] == 'Id':
            values = [{'meta': {'id': int(term['value']), 'typeName': streams[int(term['value'])][0],
                                'pipelineUuid': streams[int(term['value'])][1]}}
                      for term in terms if int(term['value']) in streams]
        else:
            parent = int(terms[0]['value'])
            values = [{'meta': {'id': 99, 'pipelineUuid': 'p1', 'typeName': 'Events'}}] if parent in with_output else []
        return httpx.Response(200, json={'values': values})
    respx.post(f'{API}/meta/v1/find').mock(side_effect=meta)
    return respx.post(f'{API}/processorFilter/v1').mock(return_value=httpx.Response(200, json={'id': 9, 'enabled': True}))


async def gated_through(ctx, **kwargs) -> tuple[list[dict], dict]:
    """Call as a user agreeing to every gate; return the gates seen and the final result."""
    gates, ids = [], {}
    while True:
        result = await processing_writes.create_processor_filter(ctx, 'p1', **kwargs, **ids)
        if not str(result.get('status', '')).startswith('needs_'):
            return gates, result
        gates.append(result)
        key = 'confirmation_id' if result['status'] == 'needs_confirmation' else 'approval_id'
        ids[key] = result[key]


@respx.mock
@pytest.mark.parametrize('filtered, with_output', [([5], []), ([], [5])])
async def test_streams_the_pipeline_already_processed_go_through_reprocessing(ctx, filtered, with_output):
    create = mock_stroom(elastic=False, filtered=filtered, with_output=with_output)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        with pytest.raises(ToolError, match=r"already processed stream\(s\) \[5\]: use reprocess_streams"):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[5, 6])
    assert not create.called


@respx.mock
async def test_translation_pipeline_needs_only_approval(ctx):
    create = mock_stroom(elastic=False)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        gates, result = await gated_through(ctx, stream_ids=[6])
    assert [g['status'] for g in gates] == ['needs_approval']
    assert result['filter_id'] == 9 and create.call_count == 1


@respx.mock
async def test_elasticsearch_indexing_filter_is_precreated_disabled_once_the_template_is_committed(ctx):
    create = mock_stroom(elastic=True)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        gates, result = await gated_through(ctx, stream_ids=[6], source_pipeline_uuid='ev')
    [confirm] = gates
    assert confirm['status'] == 'needs_confirmation' and "committed the index template for Elasticsearch index " \
        "'ecs-acme-v2' (cluster ES_DEV)" in confirm['summary']
    assert confirm['details'] == {'index name': 'ecs-acme-v2', 'cluster': 'ES_DEV'}
    assert json.loads(create.calls.last.request.content)['enabled'] is False and create.call_count == 1
    assert result['enabled'] is False and result['destination'] == {'index name': 'ecs-acme-v2', 'cluster': 'ES_DEV'}
    assert result['pipeline_link'] == 'https://stroom.example/?action=open-doc&docType=Pipeline&docUuid=p1'
    assert 'ready to enable' in result['next'] and result['pipeline_link'] in result['next']


async def reprocessed(ctx, **kwargs):
    gates, ids = [], {}
    while True:
        result = await processing_writes.reprocess_streams(ctx, 'p1', **kwargs, **ids)
        if not str(result.get('status', '')).startswith('needs_'):
            return gates, result
        gates.append(result)
        ids['confirmation_id' if result['status'] == 'needs_confirmation' else 'approval_id'] =             result.get('confirmation_id') or result.get('approval_id')


@respx.mock
async def test_reprocessing_runs_one_task_at_a_time_and_leaves_superseding_to_stroom(ctx):
    create = mock_stroom(elastic=False, filtered=[5], with_output=[6])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        gates, result = await reprocessed(ctx, stream_ids=[5, 6])
    assert [g['status'] for g in gates] == ['needs_approval'] and 'superseded' in gates[0]['details']['earlier outputs']
    body = json.loads(create.calls.last.request.content)
    assert body['maxProcessingTasks'] == 1 and result['filter_id'] == 9 and 'filter_id=9' in result['hint']
    assert not any(r.request.url.path.endswith('update/status') for r in respx.calls)


@respx.mock
@pytest.mark.parametrize('ids, message', [(list(range(1, 12)), 'Give 1 to 10 streams'), ([5, 7], r'\[7\] have not been processed')])
async def test_reprocessing_is_bounded_to_ten_streams_it_already_processed(ctx, ids, message):
    mock_stroom(elastic=False, filtered=[5])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        with pytest.raises(ToolError, match=message):
            await processing_writes.reprocess_streams(ctx, 'p1', ids)


@respx.mock
async def test_reprocessing_into_elasticsearch_confirms_the_template_first(ctx):
    create = mock_stroom(elastic=True, filtered=[5])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        gates, result = await reprocessed(ctx, stream_ids=[5], source_pipeline_uuid='ev')
    assert [g['status'] for g in gates] == ['needs_confirmation']
    assert "index 'ecs-acme-v2'" in gates[0]['summary'] and 'indexed again' in result['note']
    assert json.loads(create.calls.last.request.content)['enabled'] is False and 'pipeline_link' in result
    expression = json.loads(create.calls.last.request.content)['queryData']['expression']
    assert expression['op'] == 'AND' and expression['children'][1] == PIPELINE_TERM


@respx.mock
async def test_wait_counts_only_the_given_filters_outputs(ctx):
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 9, 'pipelineUuid': 'p1'}}]}))
    respx.post(f'{API}/processorTask/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 9}, 'status': 'COMPLETE'}]}))
    respx.post(f'{API}/meta/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'meta': {'id': 20, 'pipelineUuid': 'p1', 'typeName': 'Events', 'processorFilterId': 3}},
        {'meta': {'id': 30, 'pipelineUuid': 'p1', 'typeName': 'Events', 'processorFilterId': 9}}]}))
    everything = await processing_writes.wait_for_processing(ctx, 'p1', [5], timeout_seconds=5)
    assert everything['gate'] == 'fail' and 'filter_id' in everything['problems'][0]
    latest = await processing_writes.wait_for_processing(ctx, 'p1', [5], timeout_seconds=5, filter_id=9)
    assert latest['gate'] == 'pass' and latest['streams'] == [{'input': 5, 'events': [30], 'errors': []}]


PIPELINE_TERM = {'type': 'term', 'field': 'Pipeline', 'condition': 'IS_DOC_REF',
                 'docRef': {'type': 'Pipeline', 'uuid': 'ev', 'name': 'Acme-Events'}}


@respx.mock
async def test_indexing_filter_only_selects_events_from_the_source_events_pipeline(ctx):
    create = mock_stroom(elastic=True)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))) as guard:
        gates, result = await gated_through(ctx, stream_ids=[6, 7], source_pipeline_uuid='ev')
    expression = json.loads(create.calls.last.request.content)['queryData']['expression']
    assert expression == {'type': 'operator', 'op': 'AND', 'children': [
        {'type': 'operator', 'op': 'OR', 'children': [
            {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': '6'},
            {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': '7'}]},
        PIPELINE_TERM]}
    assert "only Events from pipeline 'Acme-Events'" in result['scope']
    assert result['events_from_pipeline'] == 'Acme-Events'
    # the source must be one this server built (in the workspace or promoted)
    assert {c.args[0]['uuid'] for c in guard.return_value.check_managed.call_args_list} == {'p1'}
    assert {c.args[0]['uuid'] for c in guard.return_value.check_built.call_args_list} == {'ev'}


@respx.mock
async def test_feed_wide_indexing_filter_carries_the_pipeline_condition(ctx):
    create = mock_stroom(elastic=True)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        await gated_through(ctx, feed='ACME', stream_type='Events', created_after='2026-09-29T00:00:00Z',
                            source_pipeline_uuid='ev')
    children = json.loads(create.calls.last.request.content)['queryData']['expression']['children']
    assert children[1] == PIPELINE_TERM and [c['field'] for c in children[0]['children']] == ['Feed', 'Type']


@respx.mock
@pytest.mark.parametrize('streams, ids, source, message', [
    (None, [6], None, 'needs source_pipeline_uuid'),
    ({6: ('Events', 'other')}, [6], 'ev', r"Stream\(s\) \[6\] were not produced by 'Acme-Events'"),
    ({6: ('Events', 'ev'), 7: ('Raw Events', None)}, [6, 7], 'ev', 'Mixed stream types'),
])
async def test_indexing_refuses_events_it_cannot_tie_to_the_source(ctx, streams, ids, source, message):
    create = mock_stroom(elastic=True, streams=streams)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        with pytest.raises(ToolError, match=message):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=ids, source_pipeline_uuid=source)
    assert not create.called


@respx.mock
async def test_translation_pipelines_take_no_source_pipeline(ctx):
    mock_stroom(elastic=False)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        with pytest.raises(ToolError, match='only for indexing pipelines'):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6], source_pipeline_uuid='ev')


def guard(pipeline_tags=('mcp-build-b',), feed_tags=('mcp-build-b',), built=True):
    async def tags(ref):
        return list(pipeline_tags if ref.get('type') == 'Pipeline' else feed_tags)

    async def check_built(ref):
        if not built:
            raise ToolError('not built by this server')
    return SimpleNamespace(check_managed=AsyncMock(), check_built=check_built, tags=tags)


@respx.mock
async def test_sample_filters_run_one_task_at_a_time(ctx):
    create = mock_stroom(elastic=False)
    with patch('tools.processing_writes.guard_from', return_value=guard()):
        await gated_through(ctx, stream_ids=[6])
    assert json.loads(create.calls.last.request.content)['maxProcessingTasks'] == 1


@respx.mock
async def test_translation_pipelines_only_process_the_builds_feeds(ctx):
    create = mock_stroom(elastic=False, streams={6: ('Raw Events', None)})
    respx.post(f'{API}/meta/v1/find').mock(side_effect=lambda request: httpx.Response(200, json={'values': [
        {'meta': {'id': 6, 'typeName': 'Raw Events', 'feedName': 'PROD-FEED'}}]}))
    respx.get(f'{API}/feed/v1/getDocRefForName/PROD-FEED').mock(
        return_value=httpx.Response(200, json={'type': 'Feed', 'uuid': 'pf', 'name': 'PROD-FEED'}))
    with patch('tools.processing_writes.guard_from', return_value=guard(feed_tags=['mcp-generated'])):
        with pytest.raises(ToolError, match=r"Feed\(s\) \['PROD-FEED'\] are not in this build"):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6])
        with pytest.raises(ToolError, match='not in this build'):
            await processing_writes.create_processor_filter(ctx, 'p1', feed='PROD-FEED',
                                                            created_after='2026-09-29T00:00:00Z')
    assert not create.called


@respx.mock
async def test_indexing_events_from_another_pipeline_needs_the_users_confirmation(ctx):
    create = mock_stroom(elastic=False, streams={6: ('Events', 'ev')})
    respx.post(f'{API}/pipeline/v1/fetchPipelineLayers').mock(side_effect=lambda request: httpx.Response(200, json=[
        {'sourcePipeline': {'type': 'Pipeline', 'uuid': 'x', 'name': 'x'}, 'pipelineData': {'elements': {'add': [
            {'id': 'indexingFilter', 'type': 'IndexingFilter' if json.loads(request.content)['uuid'] == 'p1' else 'XSLTFilter'}]}}}]))
    with patch('tools.processing_writes.guard_from', return_value=guard(built=False)):
        first = await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6], source_pipeline_uuid='ev')
        assert first['status'] == 'needs_confirmation' and "which this server did not build" in first['summary']
        second = await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6], source_pipeline_uuid='ev',
                                                                 source_confirmation_id=first['confirmation_id'])
        assert second['status'] == 'needs_approval'
        done = await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6], source_pipeline_uuid='ev',
                                                               source_confirmation_id=first['confirmation_id'],
                                                               approval_id=second['approval_id'])
    assert done['events_from_pipeline'] == 'Acme-Events'
    assert json.loads(create.calls.last.request.content)['queryData']['expression']['children'][1] == PIPELINE_TERM


@respx.mock
async def test_promotion_plans_new_data_filters_from_sample_filters_and_surveys(ctx):
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 3, 'pipelineUuid': 'tp', 'queryData': {'expression': {'type': 'operator', 'op': 'OR',
            'children': [{'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': '6'},
                         {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': '7'}]}}}},
        {'processorFilter': {'id': 4, 'pipelineUuid': 'ip', 'queryData': {'expression': {'type': 'operator', 'op': 'AND',
            'children': [{'type': 'operator', 'op': 'OR', 'children': [
                {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': '8'}]}, PIPELINE_TERM]}}}}]}))

    def meta(request):
        ids = {int(t['value']) for t in json.loads(request.content)['expression']['children']}
        rows = {6: ('ACME', 'Raw Events'), 7: ('ACME-MCP-TEST', 'Raw Events'), 8: ('ACME', 'Events')}
        return httpx.Response(200, json={'values': [{'meta': {'id': i, 'feedName': rows[i][0], 'typeName': rows[i][1]}}
                                                    for i in ids]})
    respx.post(f'{API}/meta/v1/find').mock(side_effect=meta)
    respx.post(f'{API}/pipeline/v1/fetchPipelineLayers').mock(return_value=httpx.Response(200, json=layers(False)))
    plan = await processing_writes.promotion_processing(ctx, [
        {'uuid': 'tp', 'name': 'Acme-Events'}, {'uuid': 'ip', 'name': 'Acme - Indexing'},
        {'uuid': 'sp', 'name': 'Old-Events'}], ['OLD-FEED'])
    assert [(e['pipeline']['name'], e['feed'], e['stream_type'], bool(e['extra_terms'])) for e in plan] == [
        ('Acme-Events', 'ACME', 'Raw Events', False),           # the test feed is left out
        ('Acme - Indexing', 'ACME', 'Events', True),            # keeps its Pipeline condition
        ('Old-Events', 'OLD-FEED', 'Raw Events', False)]        # stepping-only build: the surveyed feed
    create = respx.post(f'{API}/processorFilter/v1').mock(return_value=httpx.Response(200, json={'id': 21}))
    made = await processing_writes.create_promotion_filters(ctx, plan[1:2], 1790000000000)
    body = json.loads(create.calls.last.request.content)
    assert body['enabled'] is False and body['minMetaCreateTimeMs'] == 1790000000000 and body['maxProcessingTasks'] == 2
    assert body['queryData']['expression']['children'][2] == PIPELINE_TERM and made[0]['pipeline_link'].endswith('docUuid=ip')
