"""The model in an XSLT's version history and in the audit log: only as the client names it in the request's _meta,
never the agent's word. Seen: Qwen in Ask Stroom AI said it was claude-opus-4-5, twice."""
import json
import logging
from unittest.mock import patch

import httpx
from fastmcp import Context, FastMCP

import security.audit as audit_module
from security.audit import AuditMiddleware
from security.lenient import LenientArguments
from utils.xsltversion import MODEL_META_KEY, agent_line

PROTOCOL = '2025-11-25'


def _server() -> FastMCP:
    mcp = FastMCP('client-model-test', middleware=[AuditMiddleware(), LenientArguments()])

    @mcp.tool
    async def save(ctx: Context, change: str | None = None) -> dict:
        return {'by': agent_line(ctx)}
    return mcp


async def test_the_model_comes_only_from_the_client_and_an_agents_guess_is_ignored_with_a_note(caplog):
    app = _server().http_app(stateless_http=True)
    base = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': PROTOCOL}
    with patch('security.audit.get_access_token', return_value=None), caplog.at_level(logging.INFO, 'audit'):
        propagate, audit_module.audit_logger.propagate = audit_module.audit_logger.propagate, True
        try:
            async with app.lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                            base_url='http://mcp', timeout=30) as http:
                async def reply(arguments: dict, meta: dict | None = None, headers: dict | None = None) -> dict:
                    params = {'name': 'save', 'arguments': arguments, **({'_meta': meta} if meta else {})}
                    response = await http.post('/mcp', headers={**base, **(headers or {})}, json={
                        'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': params})
                    [message] = [json.loads(row[5:]) for row in response.text.splitlines() if row.startswith('data:')]
                    return json.loads(message['result']['content'][0]['text'])
                await http.post('/mcp', headers=base, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': PROTOCOL, 'capabilities': {}, 'clientInfo': {'name': 'stroom-ai', 'version': '1'}}})

                guessed = await reply({'agent_model': 'claude-opus-4-5'})
                assert 'model' not in guessed['by'] and 'claude' not in guessed['by']
                assert "Ignored unknown argument(s) ['agent_model']" in guessed['argument_notes'][0]
                by_meta = await reply({'agent_model': 'claude-opus-4-5'}, meta={MODEL_META_KEY: 'qwen3.8-flash-next'})
                assert by_meta['by'].endswith('model qwen3.8-flash-next') and 'claude' not in by_meta['by']
                by_header = await reply({}, headers={'Stroom-AI-Model': 'qwen3.8-flash-next'})
                assert 'qwen' not in by_header['by']      # _meta only, not a header (asked for by the user)
        finally:
            audit_module.audit_logger.propagate = propagate
    calls = [json.loads(r.getMessage()) for r in caplog.records if r.name == 'audit']
    # One a call (caught twice when another test left a handler on the way).
    calls = list({c['call_id']: c for c in calls if c['event'] == 'tool_call'}.values())
    # The pod log says which model made each call, as the client named it.
    assert [c.get('model') for c in calls] == [None, 'qwen3.8-flash-next', None]


def test_no_tool_asks_the_agent_which_model_it_is():
    import inspect
    import main_tools
    asking = [tool.__name__ for module in main_tools.TOOL_MODULES for tool in module.ALL_TOOLS
              if 'agent_model' in inspect.signature(tool).parameters]
    assert not asking, asking
