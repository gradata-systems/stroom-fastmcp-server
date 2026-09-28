"""Human-in-the-loop for MCP tools: needs_confirmation / needs_approval become LangGraph interrupts.

The Stroom MCP server answers a gated call with a pending id and a summary. The wrapper pauses the graph
with interrupt(), shows the summary to the person, and only if they agree calls the tool again with the id.
The model never sees or passes the id itself, so it cannot approve its own actions.
"""
import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import interrupt

GATES = {'needs_confirmation': 'confirmation_id', 'needs_approval': 'approval_id'}


def parse(result: Any) -> Any:
    """Tool results arrive as JSON text (or content blocks holding it); decode when possible."""
    if isinstance(result, tuple):  # (content, artifact) from adapters with response_format content_and_artifact
        result = result[0]
    if isinstance(result, list) and result and isinstance(result[0], dict) and 'text' in result[0]:
        result = result[0]['text']
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return result
    return result


def agreed(answer: Any) -> bool:
    if isinstance(answer, dict):
        answer = answer.get('approved', answer.get('answer'))
    return answer is True or (isinstance(answer, str) and answer.strip().lower() in ('y', 'yes', 'approve', 'approved', 'ok'))


def gated(tool: BaseTool) -> BaseTool:
    async def call(**kwargs: Any) -> Any:
        ids: dict[str, str] = {}
        # A call can pass more than one gate, e.g. confirm the index template is written, then approve processing.
        for _ in range(len(GATES) + 1):
            result = await tool.ainvoke({**kwargs, **ids})
            data = parse(result)
            if not (isinstance(data, dict) and data.get('status') in GATES):
                return result
            key = GATES[data['status']]
            answer = interrupt({'tool': tool.name, 'kind': data['status'].removeprefix('needs_'),
                                'summary': data.get('summary'), 'details': data.get('details')})
            if not agreed(answer):
                note = answer.get('note') if isinstance(answer, dict) else None
                return json.dumps({'status': 'declined', 'summary': data.get('summary'),
                                   'user_note': note or 'The user did not agree. Ask what they want instead.'})
            ids[key] = data[key]
        return result

    return StructuredTool.from_function(coroutine=call, name=tool.name, description=tool.description,
                                        args_schema=tool.args_schema)
