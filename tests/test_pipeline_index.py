"""The cached index of pipelines is read again once this server changes a pipeline or the explorer tree."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import respx

from config import Settings
from tools import templates
from utils.stroom import StroomGateway


async def test_a_pipeline_created_after_the_index_was_read_is_in_the_next_one():
    # Seen in e2e: find_pipeline_templates read the index, a sibling pipeline was created, and describe_template
    # then missed it among the template's children for a few minutes.
    stroom = SimpleNamespace(pipelines_changed=0.0, get=AsyncMock(return_value={'name': 'P', 'parentPipeline': None}),
                             find_all_documents=AsyncMock(return_value=[{'docRef': {'type': 'Pipeline', 'uuid': 'a'}}]))
    ctx = SimpleNamespace(lifespan_context={})
    with patch.object(templates, 'gateway_from', lambda ctx: stroom):
        assert list(await templates._pipeline_index(ctx)) == ['a']
        stroom.find_all_documents.return_value = [{'docRef': {'type': 'Pipeline', 'uuid': u}} for u in ('a', 'b')]
        assert list(await templates._pipeline_index(ctx)) == ['a']        # cached
        stroom.pipelines_changed = time.monotonic()
        assert list(await templates._pipeline_index(ctx)) == ['a', 'b']   # changed since: read again


@respx.mock
async def test_the_gateway_notes_writes_that_change_pipelines():
    gateway = StroomGateway(Settings(_env_file=None, stroom_url='http://stroom.test', dev_no_auth=True,
                                     stroom_api_key='k'))
    respx.post('http://stroom.test/api/explorer/v2/find').mock(return_value=httpx.Response(200, json={}))
    respx.post('http://stroom.test/api/explorer/v2/create').mock(return_value=httpx.Response(200, json={}))
    respx.put('http://stroom.test/api/pipeline/v1/u').mock(return_value=httpx.Response(200, json={}))
    try:
        await gateway.request('POST', '/explorer/v2/find', {})
        assert gateway.pipelines_changed == 0.0     # a search changes nothing
        await gateway.request('POST', '/explorer/v2/create', {})
        created = gateway.pipelines_changed
        assert created > 0
        await gateway.request('PUT', '/pipeline/v1/u', {})
        assert gateway.pipelines_changed >= created
    finally:
        await gateway.close()


async def test_a_pipeline_this_server_wrote_is_in_the_index_before_the_search_lists_it():
    # Seen in e2e: Stroom's search listed a sibling pipeline only a while after it was made, so describe_template
    # missed it (and the shared XSLTs it calls) even with the index read again.
    from fastmcp.exceptions import ToolError

    async def get(path):
        if path.endswith('/gone'):
            raise ToolError('404')
        return {'name': path.rsplit('/', 1)[-1], 'parentPipeline': {'uuid': 't'}}
    stroom = SimpleNamespace(pipelines_changed=0.0, pipelines_written={'new', 'gone'}, get=get,
                             find_all_documents=AsyncMock(return_value=[{'docRef': {'type': 'Pipeline', 'uuid': 'a'}}]))
    with patch.object(templates, 'gateway_from', lambda ctx: stroom):
        index = await templates._pipeline_index(SimpleNamespace(lifespan_context={}))
    assert sorted(index) == ['a', 'new'] and index['new']['parent_uuid'] == 't'     # one deleted since: left out


@respx.mock
async def test_the_gateway_remembers_the_pipelines_it_writes():
    gateway = StroomGateway(Settings(_env_file=None, stroom_url='http://stroom.test', dev_no_auth=True,
                                     stroom_api_key='k'))
    uuid = '0b4d86ed-b131-4f1a-9794-f3f328109148'
    respx.put(f'http://stroom.test/api/pipeline/v1/{uuid}').mock(return_value=httpx.Response(200, json={}))
    try:
        await gateway.request('PUT', f'/pipeline/v1/{uuid}', {})
        assert gateway.pipelines_written == {uuid}
    finally:
        await gateway.close()
