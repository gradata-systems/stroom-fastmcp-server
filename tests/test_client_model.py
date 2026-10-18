"""The model in an XSLT's version history: as the client names it in the request's _meta, believed over what the
agent says it is. Seen: Qwen 3.8 in Ask Stroom AI said it was claude-opus-4-5."""
import json

import httpx
from fastmcp import Context, FastMCP

from utils.xsltversion import MODEL_META_KEY, agent_line, remember_model

PROTOCOL = '2025-11-25'


def _server() -> FastMCP:
    mcp = FastMCP('client-model-test')

    @mcp.tool
    async def save(ctx: Context, agent_model: str | None = None) -> str:
        remember_model(ctx, agent_model)
        return agent_line(ctx)
    return mcp


async def test_the_clients_model_is_believed_over_the_agents():
    app = _server().http_app(stateless_http=True)
    base = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': PROTOCOL}
    async with app.lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                    base_url='http://mcp', timeout=30) as http:
        async def line(arguments: dict, meta: dict | None = None, headers: dict | None = None) -> str:
            params = {'name': 'save', 'arguments': arguments, **({'_meta': meta} if meta else {})}
            response = await http.post('/mcp', headers={**base, **(headers or {})},
                                       json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': params})
            [message] = [json.loads(row[5:]) for row in response.text.splitlines() if row.startswith('data:')]
            return message['result']['structuredContent']['result']
        await http.post('/mcp', headers=base, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': PROTOCOL, 'capabilities': {}, 'clientInfo': {'name': 'stroom-ai', 'version': '1'}}})

        said = await line({'agent_model': 'claude-opus-4-5'})
        assert said.endswith('model claude-opus-4-5 (as the agent said)')
        by_meta = await line({'agent_model': 'claude-opus-4-5'}, meta={MODEL_META_KEY: 'qwen3.8-flash-next'})
        assert by_meta.endswith('model qwen3.8-flash-next') and 'claude' not in by_meta
        by_header = await line({}, headers={'Stroom-AI-Model': 'qwen3.8-flash-next'})
        assert 'qwen' not in by_header      # _meta only, not a header (asked for by the user)
