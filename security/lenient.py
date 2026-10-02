"""Unknown arguments are dropped, not fatal.

Clients check a call against the tool's schema before sending it and refuse one with a property the schema does
not list; a small model that invents a flag ("save_text_converter": "true") is then stuck. The published schemas
therefore allow additional properties, and this middleware removes the unknown ones before validation, telling
the model which were ignored in the result.
"""
from typing import Any, Sequence

import mcp_types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult


def without_additional_properties(schema: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in schema.items() if k != 'additionalProperties'}
    if isinstance(out.get('$defs'), dict):
        out['$defs'] = {name: {k: v for k, v in d.items() if k != 'additionalProperties'} if isinstance(d, dict) else d
                        for name, d in out['$defs'].items()}
    return out


class LenientArguments(Middleware):
    async def on_list_tools(self, context: MiddlewareContext[mt.ListToolsRequest],
                            call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]]) -> Sequence[Tool]:
        tools = await call_next(context)
        return [tool.model_copy(update={'parameters': without_additional_properties(tool.parameters)}) for tool in tools]

    async def on_call_tool(self, context: MiddlewareContext[mt.CallToolRequestParams],
                           call_next: CallNext[mt.CallToolRequestParams, ToolResult]) -> ToolResult:
        ignored: list[str] = []
        arguments = context.message.arguments
        server = getattr(context.fastmcp_context, 'fastmcp', None)
        if isinstance(arguments, dict) and server is not None:
            try:
                tool = await server.get_tool(context.message.name)
                allowed = set((tool.parameters or {}).get('properties') or {})
            except Exception:
                allowed = set()
            if allowed:
                ignored = sorted(k for k in arguments if k not in allowed)
                for key in ignored:
                    arguments.pop(key, None)
        result = await call_next(context)
        if ignored:
            note = mt.TextContent(type='text', text=f"Ignored unknown argument(s) {ignored}: this tool takes "
                                                   f"{sorted(allowed)}.")
            try:
                result.content = [*result.content, note]
            except Exception:
                pass
        return result
