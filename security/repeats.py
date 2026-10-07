"""Calls that keep failing: the agent is told to change its approach, and an identical failing call isn't run again.

Seen in a test environment: an agent called build_translation_xslt over a hundred times with near-identical
mappings, each a minute and more, adding backslashes to a regex that failed for another reason. Per user, a
tool's failures in a row are counted: from the fifth (and every fifth after) the reply says to step back, with what
to do instead for that tool. The same call (tool and arguments) failing three times is answered from that failure
after, without running it again. A success resets both.
"""
import hashlib
import json
import re
import time
from typing import Any

import mcp_types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

SAME_CALL_LIMIT = 3     # identical failing calls run; after that, answered from the last failure
STREAK = 5              # failed calls of one tool in a row before the reply says to step back (and every 5th after)
TTL = 1800              # seconds a session's failures are remembered
# Answered from the last failure only where the same arguments give the same answer: the mapping and drafting tools.
# Another tool's call may succeed once the user has changed something in Stroom.
SAME_ANSWER = {'build_translation_xslt', 'draft_translation_mapping', 'build_data_splitter', 'profile_sample',
               'check_xslt', 'check_events', 'validate_events', 'create_text_converter', 'save_text_converter'}
# Failures that may pass on their own, and are retried as they are: never counted.
TRANSIENT = re.compile(r'call again|timed? ?out|temporar|unavailable|could not connect|connection|\b50[234]\b|try again',
                       re.IGNORECASE)

ADVICE = {
    'build_translation_xslt': (
        "Start again from a clean mapping instead of patching this one: draft_translation_mapping with the sample "
        "streams gives one. Change one thing at a time and read each problem in full: an extraction regex that "
        "doesn't match says where it stops matching and what the text has there. A regex is written once, as XPath "
        "reads it: only your call's JSON doubles its backslashes, nothing else escapes it again. If the data is not "
        "what you expected, show the user a record and ask."),
    'create_pipeline': (
        "find_pipeline_templates for the sample's parser (profile_sample names it), then create_pipeline from that "
        "template; for XML fragments, replace_parser='XMLFragmentParser' with the wrapper converter from "
        "profile_sample. If no template fits, ask the user which to use."),
    'step_sample': (
        "Read the groups' messages in full and fix their cause in the mapping (build_translation_xslt uuid=...), "
        "not the same thing again. If a cause is not yours to fix (the data, a template element), ask the user."),
}
DEFAULT_ADVICE = ("Step back: read the last error in full and change the approach, not the same arguments again. "
                  "If it is unclear what is wrong, show the user what you have and ask.")


def _failure(result: ToolResult) -> str | None:
    """What a returned (not raised) failure says: a result with ok false, or a mapping the tool couldn't read."""
    content = getattr(result, 'structured_content', None)
    if not isinstance(content, dict):
        return None
    if content.get('ok') is False or content.get('status') == 'needs_mapping':
        problems = content.get('problems') or []
        return ('; '.join(str(p) for p in problems[:2]) or 'not ok')[:300]
    return None


class RepeatGuard(Middleware):
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    def _state(self, context: MiddlewareContext[Any]) -> dict[str, Any]:
        now = time.monotonic()
        for key in [k for k, s in self._sessions.items() if now - s['at'] > TTL]:
            self._sessions.pop(key, None)
        # Keyed by the user: a session id can be new on every request (stateless connections, and seen in-process).
        from utils.consent import _user
        try:
            who = _user()
        except Exception:
            who = None
        state = self._sessions.setdefault(who or 'anonymous', {'calls': {}, 'streak': {}, 'at': now})
        state['at'] = now
        return state

    async def on_call_tool(self, context: MiddlewareContext[mt.CallToolRequestParams],
                           call_next: CallNext[mt.CallToolRequestParams, ToolResult]) -> ToolResult:
        tool = context.message.name
        arguments = context.message.arguments or {}
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest()
        state = self._state(context)
        same = state['calls'].get((tool, digest))
        advice = ADVICE.get(tool, DEFAULT_ADVICE)
        if same and same['failures'] >= SAME_CALL_LIMIT and tool in SAME_ANSWER:
            same['failures'] += 1
            raise ToolError(f"Not run again: this exact call has already failed {same['failures'] - 1} times, last with: "
                            f"{same['message']}. Running it again changes nothing. {advice}")
        try:
            result = await call_next(context)
        except Exception as e:
            note = self._failed(state, tool, digest, str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__)
            if note:
                raise ToolError(f"{e}\n\n{note}") from e
            raise
        message = _failure(result)
        if message is None:
            state['streak'][tool] = 0
            state['calls'].pop((tool, digest), None)
            return result
        note = self._failed(state, tool, digest, message)
        if note:
            try:
                result.content = [*result.content, mt.TextContent(type='text', text=note)]
                if isinstance(result.structured_content, dict):
                    result.structured_content['repeated'] = note
            except Exception:
                pass
        return result

    def _failed(self, state: dict[str, Any], tool: str, digest: str, message: str) -> str | None:
        """Count a failure; the note to add to it, if it is a repeat or one of a run."""
        if TRANSIENT.search(message):
            return None
        same = state['calls'].setdefault((tool, digest), {'failures': 0, 'message': message})
        same['failures'] += 1
        same['message'] = message
        streak = state['streak'][tool] = state['streak'].get(tool, 0) + 1
        advice = ADVICE.get(tool, DEFAULT_ADVICE)
        if streak >= STREAK and (streak - STREAK) % STREAK == 0:
            return f"That is {streak} failed {tool} calls in a row. {advice}"
        if same['failures'] > 1:
            return f"The same arguments as a call that already failed ({same['failures']} times): change them. {advice}"
        return None
