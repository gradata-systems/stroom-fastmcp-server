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


def mock_stroom(elastic: bool, filtered: list[int] = (), with_output: list[int] = ()):
    respx.get(f'{API}/pipeline/v1/p1').mock(return_value=httpx.Response(200, json={'uuid': 'p1', 'name': 'Acme'}))
    respx.post(f'{API}/pipeline/v1/fetchPipelineLayers').mock(return_value=httpx.Response(200, json=layers(elastic)))
    respx.post(f'{API}/processorFilter/v1/find').mock(return_value=httpx.Response(200, json={'values': [
        {'processorFilter': {'id': 3, 'pipelineUuid': 'p1', 'queryData': {'expression': {
            'type': 'operator', 'op': 'OR', 'children': [{'type': 'term', 'field': 'Id', 'condition': 'EQUALS',
                                                          'value': str(i)} for i in filtered]}}}}]}))

    def meta(request):
        parent = json.loads(request.content)['expression']['children'][0]['value']
        values = [{'meta': {'id': 99, 'pipelineUuid': 'p1', 'typeName': 'Events'}}] if int(parent) in with_output else []
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
async def test_streams_the_pipeline_already_processed_are_left_to_the_user(ctx, filtered, with_output):
    create = mock_stroom(elastic=False, filtered=filtered, with_output=with_output)
    with patch('tools.processing_writes.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
        with pytest.raises(ToolError, match=r"already processed stream\(s\) \[5\].*left to the user"):
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
        gates, result = await gated_through(ctx, stream_ids=[6])
    confirm, approve = gates
    assert confirm['status'] == 'needs_confirmation' and "index 'ecs-acme-v2'" in confirm['summary']
    assert confirm['details'] == {'index name': 'ecs-acme-v2', 'cluster': 'ES_DEV'}
    assert approve['status'] == 'needs_approval' and "into index 'ecs-acme-v2'" in approve['summary']
    assert result['destination'] == {'index name': 'ecs-acme-v2', 'cluster': 'ES_DEV'} and create.call_count == 1
