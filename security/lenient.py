"""Unknown arguments are dropped, not fatal.

Clients check a call against the tool's schema before sending it and refuse one with a property the schema does
not list; a small model that invents a flag ("save_text_converter": "true") is then stuck. The published schemas
therefore allow additional properties, and this middleware removes the unknown ones before validation, telling
the model which were ignored in the result.

A choice given with stray punctuation or in another case (",XML_FRAGMENT", "xml_fragment") is read as the choice it
plainly is, and the result says so: seen, an agent sending ",XML_FRAGMENT" again and again, refused each time.
"""
from typing import Any, Sequence

import mcp_types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult


_STRAY = ' \t\r\n,;:.\'"`()[]{}<>'


def _choices(prop: dict[str, Any]) -> list[str]:
    """The string values a property allows, if it is a choice (an enum, also inside anyOf for an optional one)."""
    found = [v for v in prop.get('enum') or [] if isinstance(v, str)]
    for option in prop.get('anyOf') or []:
        if isinstance(option, dict):
            found += [v for v in option.get('enum') or [] if isinstance(v, str)]
            if isinstance(option.get('const'), str):
                found.append(option['const'])
    if isinstance(prop.get('const'), str):
        found.append(prop['const'])
    return found


def plain_choice(value: Any, choices: list[str]) -> str | None:
    """The choice a value plainly means, when it isn't one as given: stray punctuation round it, or another case."""
    if not isinstance(value, str) or not choices or value in choices:
        return None
    bare = value.strip(_STRAY)
    return next((c for c in choices if c == bare), None) or next((c for c in choices if c.lower() == bare.lower()), None)


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
        read_as: list[str] = []
        arguments = context.message.arguments
        server = getattr(context.fastmcp_context, 'fastmcp', None)
        if isinstance(arguments, dict) and server is not None:
            try:
                tool = await server.get_tool(context.message.name)
                properties = (tool.parameters or {}).get('properties') or {}
                allowed = set(properties)
            except Exception:
                properties, allowed = {}, set()
            for key, prop in properties.items():
                fixed = plain_choice(arguments.get(key), _choices(prop) if isinstance(prop, dict) else [])
                if fixed is not None:
                    read_as.append(f"{key} {arguments[key]!r} as {fixed!r}")
                    arguments[key] = fixed
            if allowed:
                ignored = sorted(k for k in arguments if k not in allowed)
                for key in ignored:
                    arguments.pop(key, None)
        result = await call_next(context)
        notes = ([f"Ignored unknown argument(s) {ignored}: this tool takes {sorted(allowed)}."] if ignored else []) \
            + ([f"Read {', '.join(read_as)}: give the choice exactly as listed."] if read_as else [])
        if notes:
            try:
                result.content = [*result.content, mt.TextContent(type='text', text=' '.join(notes))]
            except Exception:
                pass
        return result
