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
    yield SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': ConsentStore(False), 'elastic': None})
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
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
        with pytest.raises(ToolError, match=r"already processed stream\(s\) \[5\]: use reprocess_streams"):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[5, 6])
    assert not create.called


@respx.mock
async def test_translation_pipeline_needs_only_approval(ctx):
    create = mock_stroom(elastic=False)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
        gates, result = await gated_through(ctx, stream_ids=[6])
    assert [g['status'] for g in gates] == ['needs_approval']
    assert result['filter_id'] == 9 and create.call_count == 1


@respx.mock
async def test_elasticsearch_indexing_waits_for_the_user_to_confirm_the_template_for_the_index(ctx):
    create = mock_stroom(elastic=True)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
        gates, result = await gated_through(ctx, stream_ids=[6], source_pipeline_uuid='ev')
    confirm, approve = gates
    assert confirm['status'] == 'needs_confirmation' and "index 'ecs-acme-v2'" in confirm['summary']
    assert confirm['details'] == {'index name': 'ecs-acme-v2', 'cluster': 'ES_DEV'}
    assert approve['status'] == 'needs_approval' and "into index 'ecs-acme-v2'" in approve['summary']
    assert result['destination'] == {'index name': 'ecs-acme-v2', 'cluster': 'ES_DEV'} and create.call_count == 1


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
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
        gates, result = await reprocessed(ctx, stream_ids=[5, 6])
    assert [g['status'] for g in gates] == ['needs_approval'] and 'superseded' in gates[0]['details']['earlier outputs']
    body = json.loads(create.calls.last.request.content)
    assert body['maxProcessingTasks'] == 1 and result['filter_id'] == 9 and 'filter_id=9' in result['hint']
    assert not any(r.request.url.path.endswith('update/status') for r in respx.calls)


@respx.mock
@pytest.mark.parametrize('ids, message', [(list(range(1, 12)), 'Give 1 to 10 streams'), ([5, 7], r'\[7\] have not been processed')])
async def test_reprocessing_is_bounded_to_ten_streams_it_already_processed(ctx, ids, message):
    mock_stroom(elastic=False, filtered=[5])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
        with pytest.raises(ToolError, match=message):
            await processing_writes.reprocess_streams(ctx, 'p1', ids)


@respx.mock
async def test_reprocessing_into_elasticsearch_confirms_the_template_first(ctx):
    create = mock_stroom(elastic=True, filtered=[5])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
        gates, result = await reprocessed(ctx, stream_ids=[5], source_pipeline_uuid='ev')
    assert [g['status'] for g in gates] == ['needs_confirmation', 'needs_approval']
    assert "index 'ecs-acme-v2'" in gates[0]['summary'] and 'indexed again' in gates[1]['details']['note']
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
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())) as guard:
        gates, result = await gated_through(ctx, stream_ids=[6, 7], source_pipeline_uuid='ev')
    expression = json.loads(create.calls.last.request.content)['queryData']['expression']
    assert expression == {'type': 'operator', 'op': 'AND', 'children': [
        {'type': 'operator', 'op': 'OR', 'children': [
            {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': '6'},
            {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': '7'}]},
        PIPELINE_TERM]}
    assert "only Events from pipeline 'Acme-Events'" in gates[-1]['details']['scope']
    assert result['events_from_pipeline'] == 'Acme-Events'
    # the source is checked to be one this server built, like the indexing pipeline itself
    assert {c.args[0]['uuid'] for c in guard.return_value.check_managed.call_args_list} == {'p1', 'ev'}


@respx.mock
async def test_feed_wide_indexing_filter_carries_the_pipeline_condition(ctx):
    create = mock_stroom(elastic=True)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
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
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
        with pytest.raises(ToolError, match=message):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=ids, source_pipeline_uuid=source)
    assert not create.called


@respx.mock
async def test_translation_pipelines_take_no_source_pipeline(ctx):
    mock_stroom(elastic=False)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
        with pytest.raises(ToolError, match='only for indexing pipelines'):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6], source_pipeline_uuid='ev')
