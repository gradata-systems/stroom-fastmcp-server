"""Stateless HTTP, spoken to as Ask Stroom AI's Java client speaks (protocol 2025-11-25, form elicitation declared):
a form can't be asked, as its answer would have nowhere to land, so the call answers at once with a confirmation id;
progress notifications still arrive. dev/e2e_stateless.py does the same against the local Stroom."""
import json
from contextlib import asynccontextmanager

import httpx
from fastmcp import Context, FastMCP

from utils.consent import ConsentStore, consent_from
from utils.progress import progress_from

PROTOCOL = '2025-11-25'


def _server() -> FastMCP:
    @asynccontextmanager
    async def lifespan(_):
        yield {'consent': ConsentStore(use_elicitation=True)}
    mcp = FastMCP('stateless-test', lifespan=lifespan)

    @mcp.tool
    async def change(ctx: Context, confirmation_id: str | None = None) -> dict:
        await (await progress_from(ctx))('Changing it')
        gate = await consent_from(ctx).require(ctx, 'confirmation', 'change', 'Change it', {'what': 'it'},
                                               confirmation_id)
        return gate or {'changed': True}
    return mcp


def _messages(response: httpx.Response) -> list[dict]:
    if 'application/json' in response.headers.get('content-type', ''):
        return [response.json()]
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]


async def test_stateless_a_form_becomes_an_id_at_once_and_progress_still_arrives():
    app = _server().http_app(stateless_http=True)
    headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': PROTOCOL}
    async with app.lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                    base_url='http://mcp', timeout=30) as http:
        async def send(id_: int, method: str, params: dict) -> tuple[list[dict], httpx.Response]:
            response = await http.post('/mcp', headers=headers,
                                       json={'jsonrpc': '2.0', 'id': id_, 'method': method, 'params': params})
            return _messages(response), response
        init, response = await send(1, 'initialize', {'protocolVersion': PROTOCOL,
                                                      'capabilities': {'elicitation': {'form': {}}},
                                                      'clientInfo': {'name': 'java-like', 'version': '0'}})
        assert init[-1]['result']['protocolVersion'] == PROTOCOL and 'mcp-session-id' not in response.headers

        call = {'name': 'change', 'arguments': {}, '_meta': {'progressToken': 'p'}}
        stream, _ = await send(2, 'tools/call', call)
        assert not any(m.get('method') == 'elicitation/create' for m in stream)      # its answer couldn't land
        progress = [m['params'] for m in stream if m.get('method') == 'notifications/progress']
        assert [(p['progressToken'], p['message']) for p in progress] == [('p', 'Changing it')]
        gate = next(m['result'] for m in stream if m.get('id') == 2)['structuredContent']
        assert gate['status'] == 'needs_confirmation' and gate['confirmation_id']

        # Passed back in a new request, with no session: the id is sealed state, not the server's memory.
        stream, _ = await send(3, 'tools/call', {'name': 'change',
                                                 'arguments': {'confirmation_id': gate['confirmation_id']}})
        assert next(m['result'] for m in stream if m.get('id') == 3)['structuredContent'] == {'changed': True}
