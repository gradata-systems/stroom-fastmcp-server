"""Agent-to-server check over the real MCP transport (no model needed).

Start the server locally first (dev_no_auth, bound to localhost), e.g.
    STROOM_MCP_STROOM_URL=http://127.0.0.1:18080 STROOM_MCP_STROOM_API_KEY=... STROOM_MCP_DEV_NO_AUTH=true \
    STROOM_MCP_HOST=127.0.0.1 STROOM_MCP_PORT=8765 uv run python main.py
then
    uv run --extra agent python dev/e2e_agent_transport.py

Loads the tools with FastMCP's client (agent.mcp_tools), calls a read tool, and runs a gated create_feed
inside a LangGraph graph: the server's needs_confirmation becomes an interrupt, and resuming with the
user's yes completes the call with the id.
"""
import asyncio
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastmcp import Client  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agent.gating import gated, parse  # noqa: E402
from agent.mcp_tools import load_tools  # noqa: E402


class S(TypedDict, total=False):
    result: Any


async def run(client: Client):
    tools = {t.name: t for t in await load_tools(client)}
    print(f'{len(tools)} tools over MCP')
    assert len(tools) == 55, sorted(tools)
    templates = parse(await tools['find_pipeline_templates'].ainvoke({'stage': 'translation'}))
    print('translation templates:', [c['name'] for c in templates['candidates']])
    stamp = time.strftime('%H%M%S')
    create_feed = gated(tools['create_feed'])

    async def node(state: S) -> S:
        return {'result': await create_feed.ainvoke({'build': f'agent-{stamp}', 'name': f'AGENT-TRANSPORT-{stamp}'})}
    graph = StateGraph(S)
    graph.add_node('create', node)
    graph.add_edge(START, 'create')
    graph.add_edge('create', END)
    app = graph.compile(checkpointer=MemorySaver())
    config = {'configurable': {'thread_id': stamp}}
    first = await app.ainvoke({}, config)
    question = first['__interrupt__'][0].value
    print('interrupt:', question['kind'], '-', question['summary'], question['details'])
    final = await app.ainvoke(Command(resume={'approved': True}), config)
    result = parse(final['result'])
    print('result:', result)
    assert result.get('uuid') and result.get('name') == f'AGENT-TRANSPORT-{stamp}'
    print('PASSED: tools load over MCP, and a confirmation interrupts and resumes through the transport')


async def main():
    async with Client('http://127.0.0.1:8765/mcp') as client:
        await run(client)


if __name__ == '__main__':
    asyncio.run(main())
