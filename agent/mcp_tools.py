"""Load the Stroom MCP server's tools as LangChain tools, using FastMCP's own client.

langchain-mcp-adapters pins mcp<2, while the server (FastMCP 4) speaks mcp 2, so the agent talks to the
server with fastmcp.Client and wraps each tool itself. Results come back as JSON text, which is what
agent.gating and agent.state expect.
"""
import json
from typing import Any

from fastmcp import Client
from langchain_core.tools import BaseTool, StructuredTool


def _text(result: Any) -> str:
    if getattr(result, 'structured_content', None) is not None:
        data = result.structured_content
        # FastMCP wraps non-object results as {"result": ...}
        if isinstance(data, dict) and set(data) == {'result'}:
            data = data['result']
        return json.dumps(data, default=str)
    parts = [getattr(block, 'text', '') for block in getattr(result, 'content', []) or []]
    return '\n'.join(p for p in parts if p)


def wrap(client: Client, tool: Any) -> BaseTool:
    async def call(**kwargs: Any) -> str:
        result = await client.call_tool(tool.name, {k: v for k, v in kwargs.items() if v is not None},
                                        raise_on_error=False)
        text = _text(result)
        if getattr(result, 'is_error', False):
            return json.dumps({'status': 'error', 'message': text})
        return text

    return StructuredTool.from_function(coroutine=call, name=tool.name, description=tool.description or '',
                                        args_schema=tool.input_schema)


async def load_tools(client: Client) -> list[BaseTool]:
    """Every tool the server exposes; the client must already be connected (async with client)."""
    return [wrap(client, t) for t in await client.list_tools()]
