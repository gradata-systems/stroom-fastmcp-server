import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

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
            patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'))):
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
        if result.get('status') == 'drop_first':
            gates.append(result)
            ids['index_dropped'] = True
            continue
        if not str(result.get('status', '')).startswith('needs_'):
            return gates, result
        gates.append(result)
        key = 'confirmation_id' if result['status'] == 'needs_confirmation' else 'approval_id'
        ids[key] = result[key]


@respx.mock
@pytest.mark.parametrize('filtered, with_output', [([5], []), ([], [5])])
async def test_streams_the_pipeline_already_processed_go_through_reprocessing(ctx, filtered, with_output):
    create = mock_stroom(elastic=False, filtered=filtered, with_output=with_output)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-managed']), build_of=AsyncMock(return_value='b'))):
        with pytest.raises(ToolError, match=r"already processed stream\(s\) \[5\]: use reprocess_streams"):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[5, 6])
    assert not create.called


GUARD = dict(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-managed']),
             build_of=AsyncMock(return_value='b'))
# Not tagged managed: the development batch size is left alone.
ES_GUARD = {**GUARD, 'tags': AsyncMock(return_value=['mcp-generated'])}


@respx.mock
async def test_the_samples_filter_is_created_enabled_once_the_user_approves(ctx):
    # Asked for by the user: an events pipeline's sample filter, left disabled for them to enable in Stroom, wasn't.
    create = mock_stroom(elastic=False)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(**GUARD)), \
            patch('tools.templates._shape', AsyncMock(return_value={'stage': 'translation'})):
        gates, result = await gated_through(ctx, stream_ids=[6])
    assert [g['status'] for g in gates] == ['needs_approval'] and gates[0]['details']['scope'] == 'streams [6]'
    assert result['filter_id'] == 9 and create.call_count == 1 and result['enabled'] is True
    assert json.loads(create.calls.last.request.content)['enabled'] is True and 'filter_id=9' in result['hint']


@respx.mock
async def test_a_feeds_filter_in_a_build_runs_once_the_user_approves(ctx):
    # Seen in testing: the build's events pipeline given the feed, its filter made disabled, and the agent asked the
    # user to enable it between its steps. Approved in a form instead, as a sample is (asked for by the user).
    create = mock_stroom(elastic=False, feed_streams=[{'id': 12, 'createMs': FEED_MADE + 10 ** 8}])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(**GUARD)), \
            patch('tools.templates._shape', AsyncMock(return_value={'stage': 'translation'})):
        gates, result = await gated_through(ctx, feed='ACME', created_after='2026-10-10T00:00:00Z')
    assert [g['status'] for g in gates] == ['needs_approval'] and 'process the feed' in gates[0]['summary']
    assert gates[0]['details']['scope'].startswith('feed ACME (Raw Events) created after')
    assert json.loads(create.calls.last.request.content)['enabled'] is True and result['enabled'] is True


def test_every_pipelines_stage_is_read_by_the_policys_markers():
    # Seen: a translation pipeline the template search found by the policy's markers, classed 'other' when processed,
    # so its sample's filter was made disabled for the user to enable.
    from security.policy import StageMarkers
    from tools.templates import _classify, use_markers
    acme = ({'p': 'JSONParser', 'x': 'XSLTFilter', 's': 'SchemaFilter'}, {('s', 'schemaGroup'): 'ACME-EVENTS'})
    assert _classify(*acme) == ('other', None)
    try:
        use_markers({'translation': StageMarkers(schema_groups=['ACME-EVENTS'], stream_types=['Events'])})
        assert _classify(*acme) == ('translation', None)
    finally:
        use_markers(None)


@respx.mock
@pytest.mark.parametrize('stage, scope', [('forwarding', {'stream_ids': [6]})])
async def test_a_feeds_filter_or_one_sending_elsewhere_is_left_disabled_for_the_user(ctx, stage, scope):
    # A pipeline sending to Kafka: disabled, no form; enabling it in Stroom is the user's approval.
    create = mock_stroom(elastic=False, feed_streams=[{'id': 12, 'createMs': FEED_MADE + 10 ** 8}])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(**GUARD)), \
            patch('tools.templates._shape', AsyncMock(return_value={'stage': stage})):
        gates, result = await gated_through(ctx, **scope)
    assert gates == [] and result['filter_id'] == 9 and create.call_count == 1
    assert json.loads(create.calls.last.request.content)['enabled'] is False and result['enabled'] is False
    assert 'docUuid=p1' in result['pipeline_link']
    assert "enable filter 9 on its Processors tab" in result['hint'] and 'not before' in result['hint']
    # As a button (asked for by the user), with nothing to do first here.
    [enable] = result['user_actions']
    assert enable['action'] == 'enable_processor_filter' and enable['filter_id'] == 9 and 'after' not in enable


@respx.mock
async def test_elasticsearch_indexing_is_approved_once_the_template_is_committed(ctx):
    # The approval also confirms the admin has committed the agreed template.
    create = mock_stroom(elastic=True)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(**ES_GUARD)):
        gates, result = await gated_through(ctx, stream_ids=[6], source_pipeline_uuid='ev')
    assert [g['status'] for g in gates][-1] == 'needs_approval' and 'template is committed' in gates[-1]['summary']
    assert gates[-1]['details']['index template'] == ("the agreed index template 'ecs-acme-v2' for Elasticsearch index "
                                                     "'ecs-acme-v2' is committed to cluster ES_DEV")
    assert json.loads(create.calls.last.request.content)['enabled'] is True and result['enabled'] is True


@respx.mock
@pytest.mark.parametrize('agreed, drop', [
    (AGREED, 'DELETE ecs-acme-v2.'),
    ({**AGREED, 'dev_tools': 'PUT _index_template/ecs-acme-v2\n{"data_stream": {}}'}, 'DELETE _data_stream/ecs-acme-v2.')])
async def test_an_index_the_pipeline_wrote_to_is_dropped_first(ctx, agreed, drop):
    # Seen in testing: the translation amended and the sample processed again, its new Events indexed, the index held
    # the superseded Events' documents beside them. The whole index is dropped and starts fresh (asked for by the
    # user): the pipeline indexed Events stream 4 before.
    create = mock_stroom(elastic=True, filtered=[4], streams={6: ('Events', 'ev')}, agreed=agreed)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(**ES_GUARD)):
        gates, result = await gated_through(ctx, stream_ids=[6], source_pipeline_uuid='ev')
    # Asked for by the user: the drop comes first, as a button, not only as a line in the approval form.
    assert [g['status'] for g in gates] == ['drop_first', 'needs_approval']
    [button] = gates[0]['user_actions']
    assert button['action'] == 'elasticsearch_request' and button['request'] == drop.rstrip('.')
    assert 'index_dropped=true' in gates[0]['hint'] and gates[0]['already_indexed'] == gates[1]['details']['already indexed']
    said = gates[1]['details']['already indexed']
    assert f'the cluster admin drops it first, so it starts fresh: {drop}' in said and '_delete_by_query' not in said
    assert 'once the index is dropped' in gates[1]['summary'] and result['enabled'] is True and create.call_count == 1


@respx.mock
async def test_an_index_nothing_was_indexed_into_yet_needs_no_drop(ctx):
    mock_stroom(elastic=True, filtered=[], streams={6: ('Events', 'ev')})
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': []}))
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(**ES_GUARD)):
        gates, _ = await gated_through(ctx, stream_ids=[6], source_pipeline_uuid='ev')
    assert 'already indexed' not in gates[0]['details']


@respx.mock
@pytest.mark.parametrize('agreed, message', [
    (None, "No Elasticsearch index template has been agreed with the user for index 'ecs-acme-v2'"),
    ({**AGREED, 'index': 'ecs-acme-v1'}, 'No Elasticsearch index template has been agreed'),
    ({**AGREED, 'xslt': 'feedfacefeedface'}, "The indexing XSLT changed since index template 'ecs-acme-v2' was agreed"),
])
async def test_indexing_into_elasticsearch_needs_a_template_agreed_for_the_current_pipeline(ctx, agreed, message):
    # Only a template the user confirmed, for this index and the documents the XSLT now writes, is asked about.
    create = mock_stroom(elastic=True, agreed=agreed)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-managed']), build_of=AsyncMock(return_value='b'))):
        with pytest.raises(ToolError, match=message):
            await gated_through(ctx, stream_ids=[6], source_pipeline_uuid='ev')
    assert create.call_count == 0


async def reprocessed(ctx, **kwargs):
    gates, ids = [], {}
    while True:
        result = await processing_writes.reprocess_streams(ctx, 'p1', **kwargs, **ids)
        if result.get('status') == 'drop_first':
            gates.append(result)
            ids['index_dropped'] = True
            continue
        if not str(result.get('status', '')).startswith('needs_'):
            return gates, result
        gates.append(result)
        ids['confirmation_id' if result['status'] == 'needs_confirmation' else 'approval_id'] =             result.get('confirmation_id') or result.get('approval_id')


@respx.mock
async def test_reprocessing_runs_one_task_at_a_time_and_leaves_superseding_to_stroom(ctx):
    # An events pipeline in a build: the user approves, and the filter is created enabled (asked for by the user).
    create = mock_stroom(elastic=False, filtered=[5], with_output=[6])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-managed']), build_of=AsyncMock(return_value='b'))), \
            patch('tools.templates._shape', AsyncMock(return_value={'stage': 'translation'})):
        gates, result = await reprocessed(ctx, stream_ids=[5, 6])
    assert [g['status'] for g in gates] == ['needs_approval'] and 'superseded' in gates[0]['details']['earlier outputs']
    body = json.loads(create.calls.last.request.content)
    assert body['maxProcessingTasks'] == 1 and body['enabled'] is True
    assert result['filter_id'] == 9 and result['enabled'] is True and 'filter_id=9' in result['hint']
    assert not any(r.request.url.path.endswith('update/status') for r in respx.calls)


@respx.mock
@pytest.mark.parametrize('stage', ['forwarding', 'reference'])
async def test_reprocessing_any_other_pipeline_leaves_its_filter_to_the_user(ctx, stage):
    # Only events and indexing pipelines reprocess on the user's approval; a CEF output sends to Kafka.
    create = mock_stroom(elastic=False, filtered=[5])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-managed']), build_of=AsyncMock(return_value='b'))), \
            patch('tools.templates._shape', AsyncMock(return_value={'stage': stage})):
        gates, result = await reprocessed(ctx, stream_ids=[5])
    assert gates == [] and json.loads(create.calls.last.request.content)['enabled'] is False
    assert result['enabled'] is False and 'enable filter 9' in result['hint']


@respx.mock
@pytest.mark.parametrize('ids, message', [(list(range(1, 12)), 'Give 1 to 10 streams'), ([5, 7], r'\[7\] have not been processed')])
async def test_reprocessing_is_bounded_to_ten_streams_it_already_processed(ctx, ids, message):
    mock_stroom(elastic=False, filtered=[5])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-managed']), build_of=AsyncMock(return_value='b'))):
        with pytest.raises(ToolError, match=message):
            await processing_writes.reprocess_streams(ctx, 'p1', ids)


@respx.mock
async def test_reprocessing_into_elasticsearch_confirms_the_template_in_its_approval(ctx):
    create = mock_stroom(elastic=True, filtered=[5])
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-generated']))):
        gates, result = await reprocessed(ctx, stream_ids=[5], source_pipeline_uuid='ev')
    assert [g['status'] for g in gates] == ['drop_first', 'needs_approval']
    assert [a['request'] for a in gates[0]['user_actions']] == ['DELETE ecs-acme-v2']
    assert "index 'ecs-acme-v2'" in gates[1]['details']['index template']
    assert 'drops it first, so it starts fresh: DELETE ecs-acme-v2.' in gates[1]['details']['already indexed']
    assert 'once the index is dropped' in gates[1]['summary']
    assert json.loads(create.calls.last.request.content)['enabled'] is True and result['filter_id'] == 9
    expression = json.loads(create.calls.last.request.content)['queryData']['expression']
    assert expression['op'] == 'AND' and expression['children'][1] == PIPELINE_TERM


LUCENE = {'type': 'Index', 'uuid': 'ix', 'name': 'ACME-INDEX'}


@respx.mock
@pytest.mark.parametrize('build, others, emptied', [
    ('b', [], True),                                        # the build's own index: emptied first, starting fresh
    ('b', [{'type': 'Pipeline', 'uuid': 'p2'}], False),     # another pipeline of the build writes to it
    (None, [], False),                                      # not the build's
])
async def test_reprocessing_into_the_builds_own_lucene_index_empties_it_first(ctx, build, others, emptied):
    # Seen (eval, Haiku): an indexing XSLT fixed and its stream reprocessed left each event in the index twice.
    create = mock_stroom(elastic=False, filtered=[5, 6])
    find = respx.post(f'{API}/index/v2/shard/find').mock(return_value=httpx.Response(200, json={'values': [
        {'id': 434, 'status': 'OPEN', 'documentCount': 5, 'nodeName': 'node1a', 'indexUuid': 'ix'},
        {'id': 400, 'status': 'DELETED', 'documentCount': 3, 'nodeName': 'node1a', 'indexUuid': 'ix'}]}))
    delete = respx.post(f'{API}/index/v2/shard/delete').mock(return_value=httpx.Response(200, json=1))
    guard = SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-managed']),
                            build_of=AsyncMock(return_value=build), folder_contents=AsyncMock(return_value=others))
    with patch('tools.processing_writes.guard_from', return_value=guard), \
            patch('tools.templates._shape', AsyncMock(return_value={'stage': 'indexing'})), \
            patch.object(processing_writes, 'lucene_index', AsyncMock(return_value=LUCENE)):
        gates, result = await reprocessed(ctx, stream_ids=[5])
    details = gates[-1]['details']
    assert result['enabled'] is True and json.loads(create.calls.last.request.content)['enabled'] is True
    # The server empties only its build's own index; any other is the user's, first, from its doc.
    assert [g['status'] for g in gates] == ['needs_approval'] if emptied else ['drop_first', 'needs_approval']
    if not emptied:
        assert [a['action'] for a in gates[0]['user_actions']] == ['open_document']
    if emptied:
        assert 'deleted first' in details['earlier documents'] and 'already indexed' not in details
        assert delete.call_count == 1 and delete.calls.last.request.url.params['nodeName'] == 'node1a'
        body = json.loads(delete.calls.last.request.content)
        assert body['indexUuidSet'] == {'matchAll': False, 'set': ['ix']}
        assert body['indexShardIdSet'] == {'matchAll': False, 'set': [434]}
        assert find.called
    else:
        assert 'delete its shards' in details['already indexed'] and 'earlier documents' not in details
        assert not delete.called


IN_BUILD = SimpleNamespace(tags=AsyncMock(return_value=['mcp-generated', 'mcp-managed']), build_of=AsyncMock(return_value='b'))

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
async def test_with_no_events_to_show_the_wait_is_for_the_streams_own_task(ctx):
    # Seen in e2e: a promoted indexing pipeline's earlier tasks were all complete before its filter made one for the
    # new stream, and the gate passed with nothing indexed.
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 9, 'pipelineUuid': 'p1'}}]}))
    asked = []

    def tasks(request):
        if b'Meta Id' not in request.content:
            return httpx.Response(200, json={'values': [{'status': 'COMPLETE', 'metaId': 4}]})
        asked.append(1)
        return httpx.Response(200, json={'values': [{'status': 'COMPLETE', 'metaId': 5}] if len(asked) > 1 else []})
    respx.post(f'{API}/processorTask/v1/find').mock(side_effect=tasks)
    respx.post(f'{API}/meta/v1/find').mock(return_value=httpx.Response(200, json={'values': []}))
    with patch('tools.processing_writes.guard_from', return_value=IN_BUILD), \
            patch('tools.processing_writes.asyncio.sleep', AsyncMock()):
        result = await processing_writes.wait_for_processing(ctx, 'p1', [5], timeout_seconds=60, expect_events=False,
                                                             filter_id=9)
    assert len(asked) == 2 and result['finished'] and result['gate'] == 'pass'
    with patch('tools.processing_writes.guard_from', return_value=IN_BUILD), \
            patch.object(processing_writes, '_tasks_ended', AsyncMock(return_value=set())):
        result = await processing_writes.wait_for_processing(ctx, 'p1', [5], timeout_seconds=0, expect_events=False)
    assert not result['finished'] and result['gate'] == 'fail'


def ended_task(elements: list[str], outputs: list[dict] = ()):
    """A pipeline of these element types, filter 9's task for stream 5 complete, and these outputs."""
    respx.post(f'{API}/pipeline/v1/fetchPipelineLayers').mock(return_value=httpx.Response(200, json=[
        {'pipelineData': {'elements': {'add': [{'id': t[0].lower() + t[1:], 'type': t} for t in elements]}}}]))
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 9, 'pipelineUuid': 'p1'}}]}))
    respx.post(f'{API}/processorTask/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'status': 'COMPLETE', 'metaId': 5}]}))
    respx.post(f'{API}/meta/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'meta': {'pipelineUuid': 'p1', 'processorFilterId': 9, **m}} for m in outputs]}))


@respx.mock
@pytest.mark.parametrize('elements', [['XSLTFilter', 'StandardKafkaProducer'], ['XSLTFilter', 'ElasticIndexingFilter']])
async def test_a_pipeline_that_writes_no_stream_is_done_when_its_task_ends(ctx, elements):
    # Seen in Stroom: an Events pipeline that writes only on error, and an indexing pipeline, each waited out two
    # timeouts at "0 of 3 streams have output; tasks: 6 complete".
    ended_task(elements)
    started = time.monotonic()
    with patch('tools.processing_writes.guard_from', return_value=IN_BUILD):
        result = await processing_writes.wait_for_processing(ctx, 'p1', [5], timeout_seconds=60, expect_events=True)
    assert time.monotonic() - started < 5
    assert result['gate'] == 'pass' and result['finished'] and 'none expected' in result['output']
    assert result['streams'] == [{'input': 5, 'events': [], 'errors': []}]


@respx.mock
async def test_an_ended_task_with_no_events_fails_at_once_rather_than_waiting(ctx):
    ended_task(['XSLTFilter', 'StreamAppender'], [{'id': 7, 'typeName': 'Error'}])
    started = time.monotonic()
    with patch('tools.processing_writes.guard_from', return_value=IN_BUILD):
        result = await processing_writes.wait_for_processing(ctx, 'p1', [5], timeout_seconds=60)
    assert time.monotonic() - started < 5
    assert result['finished'] and result['gate'] == 'fail' and 'output' not in result
    assert result['problems'] == ["Stream 5 produced no Events stream, and its task has ended, so none is coming: "
                                  "summarise_streams (kind=errors) 5 (a failed task, a fatal error, or every record "
                                  "filtered out)"]


@respx.mock
async def test_with_every_task_ended_and_none_for_the_stream_waiting_again_is_not_suggested(ctx):
    ended_task(['XSLTFilter', 'StreamAppender'])
    with patch('tools.processing_writes.guard_from', return_value=IN_BUILD), \
            patch.object(processing_writes, '_unselected', AsyncMock(return_value=[])):
        result = await processing_writes.wait_for_processing(ctx, 'p1', [6], timeout_seconds=5)
    assert not result['finished'] and result['hint'].startswith(
        "Every task of the pipeline's filters has ended, but none for stream(s) [6]: waiting again won't change that.")


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
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-generated']))) as guard:
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
    assert {c.args[0]['uuid'] for c in guard.return_value.check_in_build.call_args_list} == {'p1'}
    assert {c.args[0]['uuid'] for c in guard.return_value.check_built.call_args_list} == {'ev'}


@respx.mock
async def test_feed_wide_indexing_filter_carries_the_pipeline_condition(ctx):
    create = mock_stroom(elastic=True)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-generated']))):
        await gated_through(ctx, feed='ACME', stream_type='Events', created_after='2026-09-29T00:00:00Z',
                            source_pipeline_uuid='ev')
    children = json.loads(create.calls.last.request.content)['queryData']['expression']['children']
    # The source pipeline says which Events: no feed condition beside it (asked for by the user).
    assert children[1] == PIPELINE_TERM and [c['field'] for c in children[0]['children']] == ['Type']


@respx.mock
@pytest.mark.parametrize('streams, ids, source, message', [
    (None, [6], None, 'needs source_pipeline_uuid'),
    ({6: ('Events', 'other')}, [6], 'ev', r"Stream\(s\) \[6\] were not produced by 'Acme-Events'"),
    ({6: ('Events', 'ev'), 7: ('Raw Events', None)}, [6, 7], 'ev', 'Mixed stream types'),
])
async def test_indexing_refuses_events_it_cannot_tie_to_the_source(ctx, streams, ids, source, message):
    create = mock_stroom(elastic=True, streams=streams)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-managed']), build_of=AsyncMock(return_value='b'))):
        with pytest.raises(ToolError, match=message):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=ids, source_pipeline_uuid=source)
    assert not create.called


@respx.mock
async def test_translation_pipelines_take_no_source_pipeline(ctx):
    mock_stroom(elastic=False)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-managed']), build_of=AsyncMock(return_value='b'))):
        with pytest.raises(ToolError, match='only for indexing pipelines'):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6], source_pipeline_uuid='ev')


def guard(pipeline_build='b', feed_build='b', built=True):
    def build(ref):
        return pipeline_build if ref.get('type') == 'Pipeline' else feed_build

    async def tags(ref):
        return ['mcp-managed', 'mcp-generated'] if build(ref) else ['mcp-generated']

    async def build_of(ref):
        return build(ref)

    async def check_built(ref):
        if not built:
            raise ToolError('not built by this server')
    return SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=check_built, tags=tags, build_of=build_of)


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
    with patch('tools.processing_writes.guard_from', return_value=guard(feed_build=None)), \
            patch('tools.plan.works_from', AsyncMock(return_value=[])):
        with pytest.raises(ToolError, match=r"Feed\(s\) \['PROD-FEED'\] are not in this build") as refused:
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6])
        # The way to work on that feed's data is start_build feeds=; never a copy of its records (seen: told to copy
        # them into a test feed, Haiku made one, and a workspace's sample files would have gone in).
        assert 'start_build' in str(refused.value) and 'upload_sample' not in str(refused.value)
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

    async def build_of(ref):
        return 'b' if ref.get('type') == 'Pipeline' or ref.get('uuid') == 'new' else 'a'
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(
            check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), check_built=AsyncMock(), tags=AsyncMock(return_value=['mcp-managed']),
            build_of=build_of)):
        made = await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6])
    assert made['filter_id'] == 9 and create.call_count == 1


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
                             'processor': {'id': 70, 'enabled': filter_.get('processor', True)},
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
async def test_waiting_on_a_disabled_filter_asks_for_the_user_to_enable_it(ctx):
    # Filters are created disabled: nothing runs until the user enables one, so there is nothing to wait out.
    waiting_on({'enabled': False, 'terms': [('Id', '6')]})
    respx.get(f'{API}/pipeline/v1/p1').mock(return_value=httpx.Response(200, json={'uuid': 'p1', 'name': 'Acme-Events'}))
    started = time.monotonic()
    result = await processing_writes.wait_for_processing(ctx, 'p1', [6], timeout_seconds=60)
    assert time.monotonic() - started < 5
    assert result['gate'] == 'waiting' and result['waiting_for'] == 'the user to enable filter(s) [775]'
    assert 'docUuid=p1' in result['pipeline_link'] and 'Processors tab' in result['hint']
    # As a button too (asked for by the user: Stroom shows user_actions on the tool call's card).
    [action] = result['user_actions']
    assert action['action'] == 'enable_processor_filter' and action['filter_id'] == 775
    assert action['doc'] == {'type': 'Pipeline', 'uuid': 'p1', 'name': 'Acme-Events'} and 'docUuid=p1' in action['link']


@respx.mock
async def test_waiting_on_a_disabled_processor_asks_for_the_user_to_enable_it(ctx):
    # A disabled processor runs none of its filters, enabled or not (seen on the local stack: the filter enabled, the
    # processor Stroom made with it still disabled, and nothing processed).
    waiting_on({'enabled': True, 'processor': False, 'terms': [('Id', '6')]})
    respx.get(f'{API}/pipeline/v1/p1').mock(return_value=httpx.Response(200, json={'uuid': 'p1', 'name': 'Acme-Events'}))
    result = await processing_writes.wait_for_processing(ctx, 'p1', [6], timeout_seconds=60)
    assert result['gate'] == 'waiting' and result['waiting_for'].startswith(
        'the user to enable the processor of filter(s) [775]')
    # No button enables a processor: the pipeline is opened for the user to.
    assert [a['action'] for a in result['user_actions']] == ['open_document']


@respx.mock
@pytest.mark.parametrize('existing, enabled, enables_processor', [
    ([], False, True),     # the processor Stroom made with this filter: enabled, so the filter is the one choice
    ([{'processorFilter': {'id': 5, 'pipelineUuid': 'p1'}}], False, False),  # one already there keeps its state
    ([{'processorFilter': {'id': 5, 'pipelineUuid': 'p1'}}], True, True),    # unless the user approved this run
])
async def test_a_new_filter_s_disabled_processor(ctx, existing, enabled, enables_processor):
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': existing}))
    respx.post(f'{API}/processorFilter/v1').mock(return_value=httpx.Response(200, json={
        'id': 9, 'enabled': enabled, 'processor': {'id': 70, 'enabled': False}}))
    enable = respx.put(f'{API}/processor/v1/70/enabled').mock(return_value=httpx.Response(204))
    await processing_writes._create_filter(processing_writes.gateway_from(ctx), {'uuid': 'p1', 'name': 'P'},
                                           {'type': 'operator', 'op': 'AND', 'children': []}, 10, 1, None, enabled)
    assert enable.called is enables_processor
    if enables_processor:
        assert json.loads(enable.calls.last.request.content) is True


@respx.mock
async def test_waiting_goes_on_while_a_filter_selects_the_stream(ctx):
    # Feed names match whatever their case, as in Stroom.
    waiting_on({'min': FEED_MADE, 'enabled': True, 'terms': [('Feed', 'acme'), ('Type', 'Raw Events')]})
    with patch('tools.processing_writes.guard_from', return_value=IN_BUILD):
        result = await processing_writes.wait_for_processing(ctx, 'p1', [6], timeout_seconds=5)
    assert result['hint'] == 'Tasks were still running at the timeout; call again.'


@respx.mock
async def test_waiting_stops_before_the_callers_token_runs_out(ctx):
    # Seen with Stroom's internal identity provider: a token for this server with as little as a minute left, and a
    # wait of three minutes failed part way. It stops in time and says to call again, with a fresh token.
    waiting_on({'min': FEED_MADE, 'enabled': True, 'terms': [('Feed', 'acme'), ('Type', 'Raw Events')]})
    started = time.monotonic()
    with patch.object(StroomGateway, 'token_seconds_left', return_value=6),             patch('tools.processing_writes.guard_from', return_value=IN_BUILD):
        result = await processing_writes.wait_for_processing(ctx, 'p1', [6], timeout_seconds=180)
    assert time.monotonic() - started < 20
    assert result['hint'].startswith('Stopped waiting, with tasks still running, before your sign-in (access token) '
                                     'ran out. Call again')


@respx.mock
async def test_waiting_says_where_processing_is_now_and_then(ctx):
    # Seen: three minutes with nothing shown until the timeout.
    waiting_on({'min': FEED_MADE, 'enabled': True, 'terms': [('Feed', 'acme'), ('Type', 'Raw Events')]})
    ctx.report_progress = AsyncMock()
    with patch('tools.processing_writes.guard_from', return_value=IN_BUILD):
        await processing_writes.wait_for_processing(ctx, 'p1', [6], timeout_seconds=5)
    said = [c.args[2] for c in ctx.report_progress.call_args_list]
    assert len(said) == 1 and said[0].startswith('Waiting for processing: 0 of 1 streams processed, 0 with output; tasks: ')


@respx.mock
async def test_indexing_events_from_another_pipeline_needs_the_users_confirmation(ctx):
    create = mock_stroom(elastic=False, streams={6: ('Events', 'ev')})
    respx.post(f'{API}/pipeline/v1/fetchPipelineLayers').mock(side_effect=lambda request: httpx.Response(200, json=[
        {'sourcePipeline': {'type': 'Pipeline', 'uuid': 'x', 'name': 'x'}, 'pipelineData': {'elements': {'add': [
            {'id': 'indexingFilter', 'type': 'IndexingFilter' if json.loads(request.content)['uuid'] == 'p1' else 'XSLTFilter'}]}}}]))
    with patch('tools.processing_writes.guard_from', return_value=guard(built=False)):
        first = await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6], source_pipeline_uuid='ev')
        assert first['status'] == 'needs_confirmation' and "which this server did not build" in first['summary']
        approve = await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6], source_pipeline_uuid='ev',
                                                                  source_confirmation_id=first['confirmation_id'])
        assert approve['status'] == 'needs_approval'
        done = await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6], source_pipeline_uuid='ev',
                                                               source_confirmation_id=first['confirmation_id'],
                                                               approval_id=approve['approval_id'])
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
        ('Acme - Indexing', None, 'Events', True),              # its Pipeline condition, no feed
        ('Old-Events', 'OLD-FEED', 'Raw Events', False)]        # stepping-only build: the surveyed feed
    # A build on an existing feed: its test feed, whatever it is called, gives way to the feed it works from (seen in
    # VS Code: the promoted filter selected the test feed).
    existing = await processing_writes.promotion_processing(ctx, [{'uuid': 'tp', 'name': 'Acme-Events'}],
                                                            ['ACME-PROD'], ['ACME'])
    assert [(e['feed'], e['stream_type']) for e in existing] == [('ACME-PROD', 'Raw Events')]
    create = respx.post(f'{API}/processorFilter/v1').mock(return_value=httpx.Response(200, json={'id': 21}))
    made = await processing_writes.create_promotion_filters(ctx, plan[1:2])
    body = json.loads(create.calls.last.request.content)
    # No minimum creation time (asked for by the user): enabled, it processes the feed's existing data too.
    assert body['enabled'] is False and body['minMetaCreateTimeMs'] is None and body['maxProcessingTasks'] == 2
    children = body['queryData']['expression']['children']
    assert [c.get('field') for c in children[:2]] == ['Type', 'Pipeline'] and children[1] == PIPELINE_TERM
    # What it processed in the build too (asked for by the user: no NOT over those streams); the index starts fresh.
    assert len(children) == 2 and 'except_processed_in_build' not in made[0]
    assert made[0]['pipeline_link'].endswith('docUuid=ip')



@respx.mock
async def test_a_promoted_indexing_pipelines_index_starts_fresh_before_its_filter_is_enabled(ctx):
    # Asked for by the user: no NOT over the streams the build indexed; the index is dropped first instead.
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': []}))
    create = respx.post(f'{API}/processorFilter/v1').mock(return_value=httpx.Response(200, json={'id': 23}))
    agreed = {**AGREED, 'dev_tools': 'PUT _index_template/ecs-acme-v2\n{"data_stream": {}}'}
    respx.get(f'{API}/pipeline/v1/ip').mock(return_value=httpx.Response(200, json={
        'uuid': 'ip', 'name': 'Acme - Indexing', 'description': with_agreed_template('Indexes Acme.', agreed)}))
    with patch.object(processing_writes, 'elastic_destination',
                      AsyncMock(return_value={'index name': 'ecs-acme-v2', 'cluster': 'ES_DEV'})):
        made = await processing_writes.create_promotion_filters(ctx, [{
            'pipeline': {'uuid': 'ip', 'name': 'Acme - Indexing'}, 'feed': None, 'stream_type': 'Events',
            'extra_terms': [PIPELINE_TERM]}])
    assert "drops 'ecs-acme-v2' first" in made[0]['before_enabling']
    assert 'DELETE _data_stream/ecs-acme-v2.' in made[0]['before_enabling']
    # As buttons (asked for by the user): the drop, for the cluster admin to copy, then the enable, after it.
    drop, enable = made[0]['user_actions']
    assert drop == {'id': 'drop-ecs-acme-v2', 'action': 'elasticsearch_request', 'by': 'cluster admin',
                    'request': 'DELETE _data_stream/ecs-acme-v2', 'summary': "Drop 'ecs-acme-v2' so it starts fresh"}
    assert enable['action'] == 'enable_processor_filter' and enable['filter_id'] == 23
    assert enable['after'] == ['drop-ecs-acme-v2']
    assert json.loads(create.calls.last.request.content)['enabled'] is False

@respx.mock
async def test_a_feed_is_named_by_reference_so_a_renamed_feed_is_still_selected(ctx):
    # Asked for by the user: Feed IS_DOC_REF, not EQUALS a name, so Stroom keeps the filter as a dependency of the feed.
    respx.get(f'{API}/feed/v1/getDocRefForName/ACME').mock(return_value=httpx.Response(
        200, json={'type': 'Feed', 'uuid': 'feed-uuid', 'name': 'ACME'}))
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': []}))
    create = respx.post(f'{API}/processorFilter/v1').mock(return_value=httpx.Response(200, json={'id': 22}))
    respx.post(f'{API}/pipeline/v1/fetchPipelineLayers').mock(return_value=httpx.Response(200, json=layers(False)))
    pipeline = {'uuid': 'tp', 'name': 'Acme-Events'}
    await processing_writes.create_promotion_filters(
        ctx, [{'pipeline': pipeline, 'feed': 'ACME', 'stream_type': 'Raw Events', 'extra_terms': []}])
    feed = json.loads(create.calls.last.request.content)['queryData']['expression']['children'][0]
    assert feed == {'type': 'term', 'field': 'Feed', 'condition': 'IS_DOC_REF',
                    'docRef': {'type': 'Feed', 'uuid': 'feed-uuid', 'name': 'ACME'}}
    # Read back: shown by name, matched against a stream's feed, and the same filter as one naming the feed.
    from tools.processing import _terms
    expression = {'type': 'operator', 'op': 'AND', 'children': [feed]}
    assert _terms(expression) == ['Feed IS_DOC_REF ACME']
    assert processing_writes._selects(expression, {'feedName': 'acme'})
    assert not processing_writes._selects(expression, {'feedName': 'OTHER'})
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 9, 'pipelineUuid': 'tp', 'queryData': {'expression': {'type': 'operator', 'op': 'AND',
            'children': [{'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': 'ACME'}]}}}}]}))
    with pytest.raises(ToolError, match='already selects these streams'):
        await processing_writes._refuse_duplicate(processing_writes.gateway_from(ctx), 'tp', expression, None)


async def test_superseded_events_streams_are_refused_naming_the_events_now_in_their_place():
    # Seen in production: after the parent events pipeline was updated and reprocessed, indexing was given the
    # earlier Events streams' ids, which Stroom had marked deleted, and the filter selected nothing it would process.
    gone = {'id': 3808, 'typeName': 'Events', 'status': 'DELETED', 'parentMetaId': 3807, 'pipelineUuid': 'ev'}
    live = {'values': [{'meta': {'id': 3815, 'status': 'UNLOCKED', 'pipelineUuid': 'ev'}},
                       {'meta': {'id': 3808, 'status': 'DELETED', 'pipelineUuid': 'ev'}},
                       {'meta': {'id': 3820, 'status': 'UNLOCKED', 'pipelineUuid': 'other'}}]}
    stroom = SimpleNamespace(find_meta=AsyncMock(return_value=live))
    with pytest.raises(ToolError, match=r'\[3808\] were superseded.*3808 -> \[3815\]'):
        await processing_writes._refuse_superseded(stroom, [gone])
    terms = stroom.find_meta.call_args.args[0]
    assert {'field': 'Parent Id', 'value': '3807'}.items() <= terms[0].items()
    await processing_writes._refuse_superseded(stroom, [{**gone, 'status': 'UNLOCKED'}])     # a live stream: fine


@respx.mock
async def test_an_existing_feeds_streams_are_processed_into_a_test_feed_in_the_build(ctx):
    # Asked for by the user: a build on an existing feed's data processes its streams where they are, the Events
    # written to a test feed the server makes in the build through the StreamAppender's feed property; nothing is
    # uploaded and no feed is made for raw samples.
    output = {'name': 'PROD-FEED-MCP-TEST', 'uuid': 'tf', 'made': True, 'works_from': ['PROD-FEED']}
    create = mock_stroom(elastic=False, streams={6: ('Raw Events', None)})
    respx.post(f'{API}/meta/v1/find').mock(side_effect=lambda request: httpx.Response(200, json={'values': [
        {'meta': {'id': 6, 'typeName': 'Raw Events', 'feedName': 'PROD-FEED'}}]
        if json.loads(request.content)['expression']['children'][0]['field'] == 'Id' else []}))
    respx.get(f'{API}/feed/v1/getDocRefForName/PROD-FEED').mock(
        return_value=httpx.Response(200, json={'type': 'Feed', 'uuid': 'pf', 'name': 'PROD-FEED'}))
    respx.post(f'{API}/explorer/v2/find').mock(return_value=httpx.Response(200, json={'values': [
        {'docRef': {'type': 'Feed', 'uuid': 'pf', 'name': 'PROD-FEED'}, 'path': 'System / Feeds'}]}))
    redirect = AsyncMock(return_value=output)
    with patch('tools.processing_writes.guard_from', return_value=guard(feed_build=None)), \
            patch('tools.plan.works_from', AsyncMock(return_value=['PROD-FEED'])), \
            patch.object(processing_writes, 'output_feed', redirect), \
            patch.object(processing_writes, 'refuse_older_than_feed', AsyncMock()):
        result = await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6])
    assert redirect.await_args.args[2] == ['PROD-FEED'] and create.called
    assert result['output_feed'] == 'PROD-FEED-MCP-TEST' and result['enabled'] is False
    assert 'not into the production feed' in result['output_note'] and 'Nothing is uploaded' in result['output_note']


def _layers(own_properties):
    """A pipeline inheriting a template whose StreamAppender is 'streamAppender', with its own properties."""
    return [{'pipelineData': {'elements': {'add': [{'id': 'streamAppender', 'type': 'StreamAppender'}]}}},
            {'pipelineData': {'properties': {'add': own_properties}}}]


def _feeds_stroom(taken: set[str]) -> SimpleNamespace:
    """A Stroom holding PROD-FEED and the feeds named in taken, elsewhere."""
    async def get(path):
        name = unquote(path.rsplit('/', 1)[1])
        if name == 'PROD-FEED' or name in taken:
            return {'type': 'Feed', 'uuid': 'pf' if name == 'PROD-FEED' else 'other', 'name': name}
        raise ToolError('404')
    return SimpleNamespace(
        pipeline_layers=AsyncMock(return_value=_layers([])), get=get,
        find_documents=AsyncMock(return_value={'values': []}),
        get_doc=AsyncMock(side_effect=lambda t, u: {
            'pf': {'uuid': 'pf', 'encoding': 'UTF-16LE', 'streamType': 'Raw Events'},
            'tf': {'uuid': 'tf', 'name': 'PROD-FEED-MCP-TEST'},
            'p1': {'uuid': 'p1', 'name': 'P', 'pipelineData': {}}}[u]),
        put_doc=AsyncMock(side_effect=lambda d: d))


async def test_the_test_feed_is_made_once_and_set_on_the_stream_appender():
    stroom = _feeds_stroom(set())
    guard = SimpleNamespace(folder_contents=AsyncMock(return_value=[]),
                            create=AsyncMock(return_value={'type': 'Feed', 'uuid': 'tf', 'name': 'PROD-FEED-MCP-TEST'}))
    recorded = AsyncMock()
    with patch.object(processing_writes, 'gateway_from', lambda c: stroom), \
            patch.object(processing_writes, 'guard_from', lambda c: guard), \
            patch('security.record.load', AsyncMock(return_value={'docs': {}})), \
            patch('security.record.update', recorded):
        made = await processing_writes.output_feed(None, {'uuid': 'p1', 'name': 'P'}, ['PROD-FEED'], 'b')
    assert made == {'name': 'PROD-FEED-MCP-TEST', 'uuid': 'tf', 'made': True, 'works_from': ['PROD-FEED']}
    assert guard.create.await_args.args == ('Feed', 'PROD-FEED-MCP-TEST', 'b')
    feed_doc, pipeline_doc = (c.args[0] for c in stroom.put_doc.await_args_list)
    assert feed_doc['encoding'] == 'UTF-16LE'                     # the source feed's settings
    assert pipeline_doc['pipelineData']['properties']['add'] == [
        {'element': 'streamAppender', 'name': 'feed',
         'value': {'entity': {'type': 'Feed', 'uuid': 'tf', 'name': 'PROD-FEED-MCP-TEST'}}}]
    # Kept in the build's record as an output feed: upload_sample refuses it (no user_samples).
    entry = {}
    recorded.await_args.args[3](entry)
    assert entry == {'output_for': ['PROD-FEED']}


async def test_the_test_feed_takes_a_free_name_or_the_one_the_user_gave():
    # Seen in Ask Stroom AI: an earlier build's PROD-FEED-MCP-TEST held the name, and every filter failed on Stroom's
    # 500 ("A feed named ... already exists"); the user's own name for it was refused.
    stroom = _feeds_stroom({'PROD-FEED-MCP-TEST', 'PROD-FEED-MCP-TEST-2'})
    guard = SimpleNamespace(folder_contents=AsyncMock(return_value=[]),
                            create=AsyncMock(return_value={'type': 'Feed', 'uuid': 'tf', 'name': 'PROD-FEED-MCP-TEST-3'}))
    with patch.object(processing_writes, 'gateway_from', lambda c: stroom), \
            patch.object(processing_writes, 'guard_from', lambda c: guard), \
            patch('security.record.load', AsyncMock(return_value={'docs': {}})), \
            patch('security.record.update', AsyncMock()):
        made = await processing_writes.output_feed(None, {'uuid': 'p1', 'name': 'P'}, ['PROD-FEED'], 'b')
    assert guard.create.await_args.args == ('Feed', 'PROD-FEED-MCP-TEST-3', 'b') and made['made']

    # The user's, made by create_feed test_feed=true after the server's own: the newest is written to.
    stroom = _feeds_stroom(set())
    guard = SimpleNamespace(folder_contents=AsyncMock(return_value=[
        {'type': 'Feed', 'uuid': 'old', 'name': 'PROD-FEED-MCP-TEST'}, {'type': 'Feed', 'uuid': 'u', 'name': 'MINE'}]),
        create=AsyncMock())
    state = {'docs': {'old': {'output_for': ['PROD-FEED']}, 'u': {'output_for': ['PROD-FEED']}}}
    with patch.object(processing_writes, 'gateway_from', lambda c: stroom), \
            patch.object(processing_writes, 'guard_from', lambda c: guard), \
            patch('security.record.load', AsyncMock(return_value=state)):
        made = await processing_writes.output_feed(None, {'uuid': 'p1', 'name': 'P'}, ['PROD-FEED'], 'b')
    assert made == {'name': 'MINE', 'uuid': 'u', 'made': False, 'works_from': ['PROD-FEED']}
    assert not guard.create.called
    [pipeline_doc] = [c.args[0] for c in stroom.put_doc.await_args_list]
    assert pipeline_doc['pipelineData']['properties']['add'][0]['value']['entity']['uuid'] == 'u'


async def test_promotion_takes_the_test_feed_off_the_stream_appender():
    own = [{'element': 'streamAppender', 'name': 'feed', 'value': {'entity': {'type': 'Feed', 'uuid': 'tf'}}},
           {'element': 'translationFilter', 'name': 'xslt', 'value': {'entity': {'type': 'XSLT', 'uuid': 'x'}}}]
    stroom = SimpleNamespace(pipeline_layers=AsyncMock(return_value=_layers(own)),
                             get_doc=AsyncMock(return_value={'uuid': 'p1', 'pipelineData': {'properties': {'add': list(own)}}}),
                             put_doc=AsyncMock(side_effect=lambda d: d))
    with patch.object(processing_writes, 'gateway_from', lambda c: stroom):
        assert await processing_writes.restore_output_feed(None, 'p1', {'tf'})
        assert stroom.put_doc.await_args.args[0]['pipelineData']['properties']['add'] == own[1:]
        # A feed property naming a feed outside the build (set by the user) is left alone.
        stroom.put_doc.reset_mock()
        assert not await processing_writes.restore_output_feed(None, 'p1', {'other'})
        assert not stroom.put_doc.called


@respx.mock
async def test_streams_processed_into_a_test_feed_are_processed_again_once_promoted(ctx):
    # Their Events stayed in the build's test feed, in the workspace: production needs its own.
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 9, 'pipelineUuid': 'tp', 'queryData': {'expression': {
            'type': 'operator', 'op': 'OR', 'children': [{'type': 'term', 'field': 'Id', 'condition': 'EQUALS',
                                                          'value': '8'}]}}}}]}))
    respx.post(f'{API}/meta/v1/find').mock(side_effect=lambda request: httpx.Response(200, json={'values': [
        {'meta': {'id': 8, 'feedName': 'ACME', 'typeName': 'Raw Events'}}]
        if json.loads(request.content)['expression']['children'][0]['field'] == 'Id' else [
        {'meta': {'id': 80, 'feedName': 'ACME-MCP-TEST', 'typeName': 'Events', 'pipelineUuid': 'tp'}}]}))
    with patch.object(processing_writes, '_is_indexing', AsyncMock(return_value=False)):
        plan = await processing_writes.promotion_processing(ctx, [{'uuid': 'tp', 'name': 'Acme-Events'}], ['ACME'],
                                                            ['ACME-MCP-TEST'])
    assert [p['feed'] for p in plan] == ['ACME']


@respx.mock
@pytest.mark.parametrize('stage', ['translation', 'indexing'])
async def test_a_promoted_pipeline_gets_no_sample_filter_enabled_or_otherwise(ctx, stage):
    # Asked by the user: sample filters are created enabled only in a build. Promotion takes mcp-managed off (only
    # mcp-generated stays), and the write guard then refuses the pipeline before any form or filter.
    from security.guard import WriteGuard
    create = mock_stroom(elastic=stage == 'indexing')
    promoted = WriteGuard.__new__(WriteGuard)
    promoted.tags = AsyncMock(return_value=['mcp-generated'])
    with patch('tools.processing_writes.guard_from', return_value=promoted), \
            patch('tools.templates._shape', AsyncMock(return_value={'stage': stage})):
        with pytest.raises(ToolError, match='was not created by this server'):
            await processing_writes.create_processor_filter(ctx, 'p1', stream_ids=[6],
                                                            source_pipeline_uuid='ev' if stage == 'indexing' else None)
        with pytest.raises(ToolError, match='was not created by this server'):
            await processing_writes.reprocess_streams(ctx, 'p1', [6])
    assert not create.called


@respx.mock
async def test_promotion_makes_no_second_filter_where_an_enabled_one_selects_what_arrives(ctx):
    # Asked for by the user: an enabled filter already selecting the feed's new streams gets no twin; a build's sample
    # filter, by stream id, selects nothing new, so a filter is made beside it.
    filters = {'tp': [{'id': 5, 'pipelineUuid': 'tp', 'enabled': True, 'queryData': {'expression': {
                   'type': 'operator', 'op': 'AND', 'children': [
                       {'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': 'ACME'},
                       {'type': 'term', 'field': 'Type', 'condition': 'EQUALS', 'value': 'Raw Events'}]}}}],
               'ip': [{'id': 6, 'pipelineUuid': 'ip', 'enabled': True, 'queryData': {'expression': {
                   'type': 'operator', 'op': 'AND', 'children': [{'type': 'operator', 'op': 'OR', 'children': [
                       {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': '8'}]}, PIPELINE_TERM]}}}]}
    respx.post(f'{API}/processorFilter/v1/find').mock(side_effect=lambda request: httpx.Response(200, json={'values': [
        {'processorFilter': f} for f in filters[json.loads(request.content)['expression']['children'][0]['docRef']['uuid']]]}))
    create = respx.post(f'{API}/processorFilter/v1').mock(return_value=httpx.Response(200, json={'id': 30}))
    respx.post(f'{API}/pipeline/v1/fetchPipelineLayers').mock(return_value=httpx.Response(200, json=layers(False)))
    respx.get(f'{API}/feed/v1/getDocRefForName/ACME').mock(
        return_value=httpx.Response(200, json={'type': 'Feed', 'uuid': 'acme', 'name': 'ACME'}))
    made = await processing_writes.create_promotion_filters(ctx, [
        {'pipeline': {'uuid': 'tp', 'name': 'Acme-Events'}, 'feed': 'ACME', 'stream_type': 'Raw Events', 'extra_terms': []},
        {'pipeline': {'uuid': 'ip', 'name': 'Acme - Indexing'}, 'feed': None, 'stream_type': 'Events',
         'extra_terms': [PIPELINE_TERM]}])
    assert made[0]['existing'] and made[0]['filter_id'] == 5 and 'no new filter' in made[0]['note']
    assert made[1]['filter_id'] == 30 and not made[1].get('existing') and create.call_count == 1


@respx.mock
async def test_an_index_reading_a_pipeline_promoted_with_it_is_enabled_after_that_pipelines_filter(ctx):
    # Seen in e2e: both enabled together, the index took the build's Events and then their re-translation too.
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': []}))
    ids = iter([31, 32])
    respx.post(f'{API}/processorFilter/v1').mock(side_effect=lambda request: httpx.Response(200, json={'id': next(ids)}))
    respx.get(f'{API}/feed/v1/getDocRefForName/ACME').mock(
        return_value=httpx.Response(200, json={'type': 'Feed', 'uuid': 'acme', 'name': 'ACME'}))
    respx.get(f'{API}/pipeline/v1/ip').mock(return_value=httpx.Response(200, json={
        'uuid': 'ip', 'name': 'Acme - Indexing', 'description': with_agreed_template('Indexes Acme.', AGREED)}))
    destination = AsyncMock(side_effect=lambda stroom, uuid: {'index name': 'ecs-acme-v2'} if uuid == 'ip' else None)
    with patch.object(processing_writes, 'elastic_destination', destination), \
            patch.object(processing_writes, 'lucene_index', AsyncMock(return_value=None)):
        made = await processing_writes.create_promotion_filters(ctx, [
            {'pipeline': {'uuid': 'ev', 'name': 'Acme-Events'}, 'feed': 'ACME', 'stream_type': 'Raw Events',
             'extra_terms': []},
            {'pipeline': {'uuid': 'ip', 'name': 'Acme - Indexing'}, 'feed': None, 'stream_type': 'Events',
             'extra_terms': [PIPELINE_TERM]}])
    assert "only once filter 31 (pipeline 'Acme-Events') has processed what the build processed again" in \
        made[1]['before_enabling'] and 'before_enabling' not in made[0]
    assert [a['id'] for a in made[0]['user_actions']] == ['enable-31']
    assert made[1]['user_actions'][-1]['after'] == [
        'drop-ecs-acme-v2', 'enable-31', "filter 31 has processed what the build processed again"]


async def test_the_user_can_go_on_with_an_index_template_of_their_own(monkeypatch):
    # Seen in testing: the user had committed their template, and the agreement still stopped indexing, however they
    # said so. They confirm it in a form (the agent's word for it isn't theirs), and it is kept with the pipeline.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastmcp.exceptions import ToolError
    from tools import processing_writes
    from utils.consent import ConsentStore
    from utils.mappingstore import read_agreed_template
    saved = {}
    doc = {'uuid': 'p1', 'name': 'IX', 'description': 'Indexes Acme.'}
    stroom = SimpleNamespace(get_doc=AsyncMock(return_value=dict(doc)),
                             put_doc=AsyncMock(side_effect=lambda d: saved.update(d)))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': ConsentStore(False)})
    monkeypatch.setattr(processing_writes, 'indexing_xslt_digest', AsyncMock(return_value='abc'))
    destination = {'index name': 'ecs-acme-v1', 'cluster': 'ES_PROD'}
    with pytest.raises(ToolError, match='template_committed_by_user=true'):
        await processing_writes._committed(ctx, dict(doc), destination, 'create_processor_filter')
    asked = await processing_writes._committed(ctx, dict(doc), destination, 'create_processor_filter', True)
    assert asked['status'] == 'needs_confirmation' and 'not checked' in asked['details'] and not saved
    pipeline = dict(doc)
    said = await processing_writes._committed(ctx, pipeline, destination, 'create_processor_filter', True,
                                              asked['confirmation_id'])
    kept = read_agreed_template(saved['description'])
    assert kept['by_user'] and kept['index'] == 'ecs-acme-v1' and kept['xslt'] == 'abc'
    assert "the user's own index template" in said and saved['description'].startswith('Indexes Acme.')
    # Kept: the approval that follows, and later calls, aren't stopped again.
    assert "the user's own" in await processing_writes._committed(ctx, pipeline, destination, 'reprocess_streams')


def _refusing_stroom(markers, filters=({'queryData': {'expression': {'type': 'operator', 'op': 'OR', 'children': [
        {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': '6'}]}}},)):
    return SimpleNamespace(
        processor_filters=AsyncMock(return_value=list(filters)),
        find_meta=AsyncMock(return_value={'values': [{'meta': {'id': 60, 'typeName': 'Error', 'pipelineUuid': 'ix'}}]}),
        fetch_data=AsyncMock(return_value={'markers': [{'type': 'storedError', 'message': m} for m in markers]}))


@pytest.mark.parametrize('markers, dropped', [
    (["Elasticsearch rejected document 1 of 1 in a bulk request (security_exception): action [indices:admin/"
      "auto_create] is unauthorized for user [stroom]"], False),
    (["Elasticsearch rejected document 1 of 1 in a bulk request (index_not_found_exception): no such index"], False),
    (["Elasticsearch rejected document 2 of 3 in a bulk request (document_parsing_exception): failed to parse"], True),
])
async def test_an_index_elasticsearch_refused_every_write_to_is_not_dropped(markers, dropped):
    # Seen in testing: a 403, the index dropped first all the same, and the account, unable to create it, refused again.
    from tools.processing_writes import _fresh_index
    stroom = _refusing_stroom(markers)
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    details, _ = await _fresh_index(ctx, {'uuid': 'ix', 'description': None},
                                    {'index name': 'ecs-acme-v1', 'cluster': 'ES'}, None)
    assert ('already indexed' in details) is dropped
    if not dropped:
        assert 'nothing is dropped' in details['earlier runs'] and 'Approve only once' in details['earlier runs']
    else:
        assert 'auto_configure or create_index' in details['already indexed']
