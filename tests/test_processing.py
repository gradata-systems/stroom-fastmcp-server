import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from config import Settings
from tools import processing_writes
from utils.consent import ConsentStore
from utils.mappingstore import digest, with_agreed_template
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


@pytest.fixture(autouse=True)
def stepped(request):
    """Processing needs a clean step recorded; these tests are about what comes after, except the one that is not."""
    if 'no_step_recorded' in request.keywords:
        yield
        return
    with patch.object(processing_writes, 'stepped_clean', AsyncMock(return_value=True)), \
            patch('tools.plan.with_next', AsyncMock(side_effect=lambda ctx, build, result: result)), \
            patch('tools.plan.build_of', AsyncMock(return_value=None)):
        yield


@pytest.mark.no_step_recorded
@respx.mock
async def test_processing_refuses_a_pipeline_that_was_not_stepped_clean(ctx):
    mock_stroom(elastic=False)
    with patch.object(processing_writes, 'stepped_clean', AsyncMock(return_value=False)), \
            patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
        with pytest.raises(ToolError, match='no clean step of its current code recorded'):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[1])


FEED_MADE = 1791580000000         # 2026-10-09T21:06:40Z
AGREED = {'name': 'ecs-acme-v2', 'index': 'ecs-acme-v2', 'cluster': 'ES_DEV', 'component_templates': ['ecs-base'],
          'xslt': digest(), 'agreed': '2026-10-03T09:00:00Z', 'dev_tools': 'PUT _index_template/ecs-acme-v2\n{}'}


def mock_stroom(elastic: bool, filtered: list[int] = (), with_output: list[int] = (), streams: dict | None = None,
                agreed: dict | None = AGREED, feed_streams: list[dict] = ()):
    """p1 is the pipeline under test (Elasticsearch indexing, or a translation); 'ev' is an events pipeline.

    streams maps a stream id to (type, producing pipeline); by default indexing reads Events from 'ev' and a
    translation reads Raw Events. An Elasticsearch p1 carries the agreed index template (for its code: no XSLT).
    """
    streams = streams or {i: ('Events', 'ev') if elastic else ('Raw Events', None) for i in range(1, 20)}
    p1 = {'uuid': 'p1', 'name': 'Acme', **({'description': with_agreed_template('Indexes Acme.', agreed)}
                                           if elastic and agreed else {})}
    respx.get(f'{API}/pipeline/v1/p1').mock(return_value=httpx.Response(200, json=p1))
    respx.get(f'{API}/pipeline/v1/ev').mock(return_value=httpx.Response(200, json={'uuid': 'ev', 'name': 'Acme-Events'}))
    respx.post(f'{API}/pipeline/v1/fetchPipelineLayers').mock(side_effect=lambda request: httpx.Response(
        200, json=layers(elastic and json.loads(request.content)['uuid'] == 'p1')))
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 3, 'pipelineUuid': 'p1', 'queryData': {'expression': {
            'type': 'operator', 'op': 'OR', 'children': [{'type': 'term', 'field': 'Id', 'condition': 'EQUALS',
                                                          'value': str(i)} for i in filtered]}}}}]}))

    respx.get(f'{API}/feed/v1/getDocRefForName/ACME').mock(
        return_value=httpx.Response(200, json={'type': 'Feed', 'uuid': 'acme', 'name': 'ACME'}))
    respx.get(f'{API}/feed/v1/acme').mock(return_value=httpx.Response(200, json={
        'type': 'Feed', 'uuid': 'acme', 'name': 'ACME', 'createTimeMs': FEED_MADE}))

    def meta(request):
        terms = json.loads(request.content)['expression']['children']
        if terms[0]['field'] == 'Feed':
            return httpx.Response(200, json={'values': [{'meta': m} for m in feed_streams]})
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
async def test_elasticsearch_indexing_starts_once_the_template_is_applied(ctx):
    # One approval, worded so the user confirms the admin applied the index template (propose_index_template built
    # it), then processing starts: the filter is created enabled, for the agent to wait and verify.
    create = mock_stroom(elastic=True)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        gates, result = await gated_through(ctx, stream_ids=[6], source_pipeline_uuid='ev')
    [approve] = [g for g in gates if g['status'] == 'needs_approval']
    assert approve['summary'].startswith("The agreed index template 'ecs-acme-v2' for Elasticsearch index 'ecs-acme-v2' "
                                         "is committed to cluster ES_DEV: start indexing")
    assert approve['details']['index template'].startswith("the agreed index template 'ecs-acme-v2'")
    assert json.loads(create.calls.last.request.content)['enabled'] is True and create.call_count == 1
    assert result['filter_id'] and 'pipeline_link' not in result


@respx.mock
@pytest.mark.parametrize('agreed, message', [
    (None, "No Elasticsearch index template has been agreed with the user for index 'ecs-acme-v2'"),
    ({**AGREED, 'index': 'ecs-acme-v1'}, 'No Elasticsearch index template has been agreed'),
    ({**AGREED, 'xslt': 'feedfacefeedface'}, "The indexing XSLT changed since index template 'ecs-acme-v2' was agreed"),
])
async def test_indexing_into_elasticsearch_needs_a_template_agreed_for_the_current_pipeline(ctx, agreed, message):
    # Only a template the user confirmed, for this index and the documents the XSLT now writes, is asked about.
    create = mock_stroom(elastic=True, agreed=agreed)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        with pytest.raises(ToolError, match=message):
            await gated_through(ctx, stream_ids=[6], source_pipeline_uuid='ev')
    assert create.call_count == 0


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
async def test_reprocessing_into_elasticsearch_confirms_the_template_in_its_approval(ctx):
    create = mock_stroom(elastic=True, filtered=[5])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-build-b']))):
        gates, result = await reprocessed(ctx, stream_ids=[5], source_pipeline_uuid='ev')
    assert [g['status'] for g in gates] == ['needs_approval']
    assert "index 'ecs-acme-v2'" in gates[0]['details']['index template']
    assert 'POST ecs-acme-v2/_delete_by_query' in gates[0]['details']['already indexed'] and 'StreamId' in gates[0]['details']['already indexed']
    assert json.loads(create.calls.last.request.content)['enabled'] is True and result['filter_id'] == 9
    expression = json.loads(create.calls.last.request.content)['queryData']['expression']
    assert expression['op'] == 'AND' and expression['children'][1] == PIPELINE_TERM


IN_BUILD = SimpleNamespace(tags=AsyncMock(return_value=['mcp-generated', 'mcp-build-b']))

@respx.mock
async def test_wait_counts_only_the_given_filters_outputs(ctx):
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 9, 'pipelineUuid': 'p1'}}]}))
    respx.post(f'{API}/processorTask/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 9}, 'status': 'COMPLETE'}]}))
    respx.post(f'{API}/meta/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'meta': {'id': 20, 'pipelineUuid': 'p1', 'typeName': 'Events', 'processorFilterId': 3}},
        {'meta': {'id': 30, 'pipelineUuid': 'p1', 'typeName': 'Events', 'processorFilterId': 9}}]}))
    with patch('tools.processing_writes.guard_from', return_value=IN_BUILD):
        everything = await processing_writes.wait_for_processing(ctx, 'p1', [5], timeout_seconds=5)
        latest = await processing_writes.wait_for_processing(ctx, 'p1', [5], timeout_seconds=5, filter_id=9)
    assert everything['gate'] == 'fail' and 'filter_id' in everything['problems'][0]
    assert latest['gate'] == 'pass' and latest['streams'] == [{'input': 5, 'events': [30], 'errors': []}]



@respx.mock
async def test_wait_fails_while_the_current_code_has_not_stepped_clean(ctx):
    # Seen: a filter made after a clean step went on processing once the XSLT was replaced by one that never stepped
    # clean (Unknown nobody agreed to), and the agent documented and indexed its output.
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 9, 'pipelineUuid': 'p1'}}]}))
    respx.post(f'{API}/processorTask/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 9}, 'status': 'COMPLETE'}]}))
    respx.post(f'{API}/meta/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'meta': {'id': 30, 'pipelineUuid': 'p1', 'typeName': 'Events', 'processorFilterId': 9}}]}))
    with patch.object(processing_writes, 'stepped_clean', AsyncMock(return_value=False)),             patch('tools.processing_writes.guard_from', return_value=IN_BUILD):
        result = await processing_writes.wait_for_processing(ctx, 'p1', [5], timeout_seconds=5, filter_id=9)
    assert result['gate'] == 'fail' and result['streams'][0]['events'] == [30]
    assert result['problems'] == ["The pipeline's current code has not stepped clean: its output may not be what was "
                                  "checked. step_sample over the sample streams until the verdict is clean, then "
                                  "reprocess_streams"]
    # Promoted, it keeps no record of its steps (seen in e2e: production v1 failed the gate on new data): not gated.
    promoted = SimpleNamespace(tags=AsyncMock(return_value=['mcp-generated']))
    with patch.object(processing_writes, 'stepped_clean', AsyncMock(return_value=False)),             patch('tools.processing_writes.guard_from', return_value=promoted):
        result = await processing_writes.wait_for_processing(ctx, 'p1', [5], timeout_seconds=5, filter_id=9)
    assert result['gate'] == 'pass' and result['problems'] == []

@respx.mock
async def test_with_no_processor_filter_there_is_nothing_to_wait_for(ctx):
    # Seen in VS Code: create_processor_filter was refused, and the agent waited out 200 seconds of "tasks still
    # running" for a pipeline with nothing set to process.
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': []}))
    meta = respx.post(f'{API}/meta/v1/find').mock(return_value=httpx.Response(200, json={'values': []}))
    started = time.monotonic()
    result = await processing_writes.wait_for_processing(ctx, 'p1', [5], timeout_seconds=60, expect_events=False)
    assert time.monotonic() - started < 5 and not meta.called
    assert result['gate'] == 'fail' and 'no processor filter' in result['problems'][0]
    assert result['hint'].startswith('create_processor_filter first') and "don't wait" in result['hint']


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
    respx.post(f'{API}/explorer/v2/find').mock(return_value=httpx.Response(200, json={'values': [
        {'docRef': {'type': 'Feed', 'uuid': 'pf', 'name': 'PROD-FEED'}, 'path': 'System / Feeds'}]}))
    with patch('tools.processing_writes.guard_from', return_value=guard(feed_tags=['mcp-generated'])):
        with pytest.raises(ToolError, match=r"Feed\(s\) \['PROD-FEED'\] are not in this build"):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6])
        with pytest.raises(ToolError, match='not in this build'):
            await processing_writes.create_processor_filter(ctx, 'p1', feed='PROD-FEED',
                                                            created_after='2026-09-29T00:00:00Z')
    assert not create.called


@respx.mock
@pytest.mark.parametrize('earlier_feed', [True, False])
async def test_a_builds_feed_is_found_whatever_case_the_stream_data_spells_it(ctx, earlier_feed):
    # Qwen in VS Code: DELINEA-SECRETSERVER-V1.0 in the build, an earlier Delinea-SecretServer-V1.0 in another; Stroom
    # filed the new feed's sample under the earlier spelling, and processing was refused as another build's feed.
    # Gemma in VS Code: the earlier feed deleted, so no feed has the stream data's spelling at all.
    create = mock_stroom(elastic=False, streams={6: ('Raw Events', None)})
    respx.post(f'{API}/meta/v1/find').mock(side_effect=lambda request: httpx.Response(200, json={'values': [
        {'meta': {'id': 6, 'typeName': 'Raw Events', 'feedName': 'Delinea-SecretServer-V1.0'}}]}))
    respx.get(f'{API}/feed/v1/getDocRefForName/Delinea-SecretServer-V1.0').mock(
        return_value=httpx.Response(200, json={'type': 'Feed', 'uuid': 'old', 'name': 'Delinea-SecretServer-V1.0'})
        if earlier_feed else httpx.Response(204))
    respx.get(f'{API}/feed/v1/old').mock(return_value=httpx.Response(200, json={
        'type': 'Feed', 'uuid': 'old', 'name': 'Delinea-SecretServer-V1.0', 'createTimeMs': 0}))
    respx.get(f'{API}/feed/v1/new').mock(return_value=httpx.Response(200, json={
        'type': 'Feed', 'uuid': 'new', 'name': 'DELINEA-SECRETSERVER-V1.0', 'createTimeMs': 0}))
    found = [{'docRef': {'type': 'Feed', 'uuid': 'new', 'name': 'DELINEA-SECRETSERVER-V1.0'},
              'path': 'System / MCP Workspace / b'}]
    if earlier_feed:
        found.insert(0, {'docRef': {'type': 'Feed', 'uuid': 'old', 'name': 'Delinea-SecretServer-V1.0'},
                         'path': 'System / MCP Workspace / a'})
    respx.post(f'{API}/explorer/v2/find').mock(return_value=httpx.Response(200, json={'values': found}))

    async def tags(ref):
        return ['mcp-build-b'] if ref.get('type') == 'Pipeline' or ref.get('uuid') == 'new' else ['mcp-build-a']
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(
            check_managed=AsyncMock(), check_built=AsyncMock(), tags=tags)):
        gated = await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6])
    assert gated['status'] == 'needs_approval'


@respx.mock
async def test_a_feed_wide_filter_starts_no_earlier_than_its_feed(ctx):
    # Seen (Qwen, VS Code): created_after a week before the feed was made also selects an earlier, deleted namesake's
    # streams, which Stroom keeps under the same name.
    create = mock_stroom(elastic=False, feed_streams=[{'id': 6, 'createMs': FEED_MADE + 60_000}])
    with patch('tools.processing_writes.guard_from', return_value=guard()):
        gates, result = await gated_through(ctx, feed='ACME', created_after='2026-10-01T00:00:00Z')
    assert json.loads(create.calls.last.request.content)['minMetaCreateTimeMs'] == FEED_MADE
    assert 'created after 2026-10-09T21:06:40.000Z (when the feed was created' in result['scope']


@respx.mock
async def test_a_feed_wide_filter_that_selects_none_of_the_feeds_streams_is_refused(ctx):
    # Seen (Gemma, VS Code): created_after midnight UTC, after the sample arrived: nothing was ever selected.
    create = mock_stroom(elastic=False, feed_streams=[{'id': 6, 'createMs': FEED_MADE + 60_000},
                                                      {'id': 2, 'createMs': FEED_MADE - 60_000}])   # a namesake's
    with patch('tools.processing_writes.guard_from', return_value=guard()):
        with pytest.raises(ToolError, match=r"selects none of the feed's streams.*: 6 \(2026-10-09T21:07:40.000Z\)\. "
                                            r"To process the sample, give stream_ids"):
            await processing_writes.create_processor_filter(ctx, 'p1', feed='ACME', created_after='2026-10-10T00:00:00Z')
    assert not create.called


@respx.mock
@pytest.mark.parametrize('filtered, with_output', [([6], []), ([], [6])])
async def test_a_feed_wide_filter_for_new_data_only_follows_the_processed_sample(ctx, filtered, with_output):
    # The discovery flow: the sample processed by stream id, then the feed's new data from now on.
    create = mock_stroom(elastic=False, filtered=filtered, with_output=with_output,
                         feed_streams=[{'id': 6, 'createMs': FEED_MADE + 60_000}])
    with patch('tools.processing_writes.guard_from', return_value=guard()):
        await gated_through(ctx, feed='ACME', created_after='2026-10-10T00:00:00Z')
    assert create.call_count == 1


@respx.mock
async def test_the_same_feed_wide_filter_is_not_made_twice(ctx):
    create = mock_stroom(elastic=False)
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 775, 'pipelineUuid': 'p1', 'minMetaCreateTimeMs': FEED_MADE, 'queryData': {
            'expression': {'type': 'operator', 'children': [
                {'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': 'ACME'},
                {'type': 'term', 'field': 'Type', 'condition': 'EQUALS', 'value': 'Raw Events'}]}}}}]}))
    with patch('tools.processing_writes.guard_from', return_value=guard()):
        with pytest.raises(ToolError, match='Processor filter 775 on this pipeline already selects these streams'):
            await processing_writes.create_processor_filter(ctx, 'p1', feed='ACME', created_after='2026-10-09T22:00:00Z')
    assert not create.called


def waiting_on(filter_: dict) -> None:
    """Stream 6 of feed ACME, made a minute after the feed, with no output yet; p1 has the one filter given."""
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 775, 'pipelineUuid': 'p1', 'enabled': filter_['enabled'],
                             'minMetaCreateTimeMs': filter_.get('min'), 'queryData': {'expression': {
                                 'type': 'operator', 'children': [
                                     {'type': 'term', 'field': f, 'condition': 'EQUALS', 'value': v}
                                     for f, v in filter_['terms']]}}}}]}))
    respx.post(f'{API}/processorTask/v1/find').mock(return_value=httpx.Response(200, json={'values': []}))
    respx.post(f'{API}/meta/v1/find').mock(side_effect=lambda request: httpx.Response(200, json={'values': [
        {'meta': {'id': 6, 'feedName': 'ACME', 'typeName': 'Raw Events', 'createMs': FEED_MADE + 60_000}}]
        if json.loads(request.content)['expression']['children'][0]['field'] == 'Id' else []}))


@respx.mock
@pytest.mark.parametrize('filter_, reason', [
    ({'min': FEED_MADE + 3_600_000, 'enabled': True, 'terms': [('Feed', 'acme'), ('Type', 'Raw Events')]},
     'filter 775 selects only streams created after 2026-10-09T22:06:40.000Z'),
    ({'enabled': True, 'terms': [('Feed', 'OTHER'), ('Type', 'Raw Events')]},
     'filter 775 selects Feed EQUALS OTHER, Type EQUALS Raw Events'),
    ({'enabled': False, 'terms': [('Id', '6')]}, 'filter 775 is disabled'),
])
async def test_waiting_ends_at_once_when_no_filter_will_process_the_stream(ctx, filter_, reason):
    # Seen (Gemma, VS Code): three timeouts of "tasks still running" for a filter whose tracker matched nothing.
    waiting_on(filter_)
    started = time.monotonic()
    result = await processing_writes.wait_for_processing(ctx, 'p1', [6], timeout_seconds=60)
    assert time.monotonic() - started < 5
    assert result['gate'] == 'fail' and result['problems'] == [
        f"No processor filter of this pipeline will process stream 6 (ACME, Raw Events, created "
        f"2026-10-09T21:07:40.000Z): {reason}"]
    assert result['hint'].startswith('Nothing will process these streams')


@respx.mock
async def test_waiting_goes_on_while_a_filter_selects_the_stream(ctx):
    # Feed names match whatever their case, as in Stroom.
    waiting_on({'min': FEED_MADE, 'enabled': True, 'terms': [('Feed', 'acme'), ('Type', 'Raw Events')]})
    with patch('tools.processing_writes.guard_from', return_value=IN_BUILD):
        result = await processing_writes.wait_for_processing(ctx, 'p1', [6], timeout_seconds=5)
    assert result['hint'] == 'Tasks were still running at the timeout; call again.'


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
