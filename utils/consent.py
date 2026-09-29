"""Confirmations (key details) and approvals (risky actions) before a write tool acts.

The user is asked directly, as a form, when the client supports elicitation, so the model never holds the
answer:
- MCP 2026-07-28 connections have no server-initiated requests: the tool returns an input-required result
  holding the form, the client asks the user and repeats the call with the answer (SEP-2322). The request
  state, sealed by the framework, names the exact request (and any gates already passed in this call), so
  an answer cannot be replayed for something else.
- Earlier connections: the server sends the elicitation request during the call.
Otherwise the tool returns a pending id with a plain-language summary; the client shows it to the user
and repeats the call with the id once they agree. The id is bound to the action, the exact details and
the caller, and expires, so it cannot be reused for something else.
"""
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

import mcp_types
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from security.audit import audit

logger = logging.getLogger(__name__)
Kind = Literal['confirmation', 'approval']
TTL_SECONDS = 3600


@dataclass
class _Pending:
    kind: Kind
    action: str
    digest: str
    user: str | None
    expires: float
    granted: bool = False


def _user() -> str | None:
    token = get_access_token()
    return (token.claims or {}).get('preferred_username') if token else None


def _digest(action: str, details: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([action, details], sort_keys=True, default=str).encode()).hexdigest()


CAPABILITIES_META_KEY = 'io.modelcontextprotocol/clientCapabilities'


def _modern(ctx: Any) -> bool:
    """A 2026-07-28 connection: no server-initiated requests, input is gathered by repeating the call."""
    check = getattr(ctx, '_is_modern_protocol', None)
    try:
        return bool(check()) if check else False
    except Exception:
        return False


def _client_can_answer_forms(ctx: Any) -> bool:
    """Whether this request's client declared form elicitation (sent per request on modern connections)."""
    meta = getattr(getattr(ctx, 'request_context', None), 'meta', None)
    if meta is not None and not isinstance(meta, dict):
        meta = getattr(meta, 'model_extra', None) or (meta.model_dump(by_alias=True) if hasattr(meta, 'model_dump') else {})
    caps = (meta or {}).get(CAPABILITIES_META_KEY) or {}
    if hasattr(caps, 'model_dump'):
        caps = caps.model_dump(by_alias=True, exclude_none=True)
    return isinstance(caps, dict) and caps.get('elicitation') is not None


def _call_state(ctx: Any) -> dict[str, Any]:
    """This call's consent state: gates granted so far, from the sealed request state of earlier rounds."""
    state = getattr(ctx, '_consent_call_state', None)
    if state is None:
        raw = None
        try:
            raw = ctx.request_state
        except Exception:
            pass
        try:
            state = json.loads(raw) if raw else {}
        except ValueError:
            state = {}
        state.setdefault('granted', [])
        try:
            setattr(ctx, '_consent_call_state', state)
        except Exception:
            pass
    return state


class ConsentStore:
    def __init__(self, use_elicitation: bool = True):
        self.use_elicitation = use_elicitation
        self._pending: dict[str, _Pending] = {}

    async def require(self, ctx: Any, kind: Kind, action: str, summary: str, details: dict[str, Any],
                      token: str | None, keep: bool = False) -> dict[str, Any] | None:
        """None when the user has agreed; otherwise the response the tool should return.

        Raises ToolError when the user declines or the id does not match this exact request. Ids are single
        use. When a later gate in the same call may still stop it, pass keep=True so the id survives the
        call being repeated for that gate, and discard() it once the action is done.
        """
        digest = _digest(action, details)
        if token:
            pending = self._pending.get(token) if keep else self._pending.pop(token, None)
            if pending is None or pending.expires < time.time():
                raise ToolError(f"Unknown or expired {kind} id; request the {kind} again")
            if (pending.kind, pending.action, pending.digest, pending.user) != (kind, action, digest, _user()):
                raise ToolError(f"This {kind} id was issued for a different request; request the {kind} again")
            if not keep or pending.granted is False:
                audit(kind, action=action, details=details, outcome='granted', via='id')
            pending.granted = True
            return None

        if self.use_elicitation and _modern(ctx):
            outcome = self._form_round(ctx, kind, action, summary, details, digest)
            if outcome is not False:
                return outcome
        elif self.use_elicitation and hasattr(ctx, 'elicit'):
            try:
                answer = await ctx.elicit(f"{summary}\n\n{_format(details)}", bool)
            except Exception as e:  # client without elicitation support
                logger.info("Elicitation unavailable, falling back to %s id: %s", kind, e)
            else:
                agreed = getattr(answer, 'action', None) == 'accept' and bool(getattr(answer, 'data', False))
                audit(kind, action=action, details=details, outcome='granted' if agreed else 'declined', via='elicitation')
                if not agreed:
                    raise ToolError(f"The user did not agree to: {summary}")
                return None

        pending_id = f'{kind[:4]}-{secrets.token_urlsafe(9)}'
        self._pending[pending_id] = _Pending(kind, action, digest, _user(), time.time() + TTL_SECONDS)
        audit(kind, action=action, details=details, outcome='requested', id=pending_id)
        return {'status': f'needs_{kind}', f'{kind}_id': pending_id, 'summary': summary, 'details': details,
                'hint': f"Show the summary and details to the user. If they agree, call {action} again with the "
                        f"same arguments plus {kind}_id='{pending_id}'. If they change a detail, call again with "
                        f"the new values and no id to get a fresh {kind}."}


    def _form_round(self, ctx: Any, kind: Kind, action: str, summary: str, details: dict[str, Any],
                    digest: str) -> Any:
        """Modern connections: None if agreed, an input-required result to ask, False if the client cannot."""
        state = _call_state(ctx)
        bound = f"{digest}:{_user()}"
        if bound in state['granted']:
            return None
        key = f"{kind}-{digest[:16]}"
        try:
            responses = ctx.input_responses
        except Exception:
            responses = None
        answer = (responses or {}).get(key) if responses else None
        if answer is not None and state.get('asked') == bound:
            action_taken = getattr(answer, 'action', None) or (answer.get('action') if isinstance(answer, dict) else None)
            content = getattr(answer, 'content', None) or (answer.get('content') if isinstance(answer, dict) else None) or {}
            agreed = action_taken == 'accept' and bool(content.get('value'))
            audit(kind, action=action, details=details, outcome='granted' if agreed else 'declined', via='form')
            if not agreed:
                raise ToolError(f"The user did not agree to: {summary}")
            state['granted'].append(bound)
            state.pop('asked', None)
            return None
        if not _client_can_answer_forms(ctx):
            return False
        state['asked'] = bound
        audit(kind, action=action, details=details, outcome='requested', via='form')
        form = mcp_types.ElicitRequest(params=mcp_types.ElicitRequestFormParams(
            message=f"{summary}\n\n{_format(details)}",
            requested_schema={'type': 'object', 'required': ['value'], 'properties': {
                'value': {'type': 'boolean', 'title': 'Approve' if kind == 'approval' else 'Confirm',
                          'description': summary}}}))
        return mcp_types.InputRequiredResult(input_requests={key: form},
                                            request_state=json.dumps(state, sort_keys=True))

    def discard(self, token: str | None) -> None:
        if token:
            self._pending.pop(token, None)


def _format(details: dict[str, Any]) -> str:
    return '\n'.join(f"- {k}: {v}" for k, v in details.items())


def consent_from(ctx: Any) -> ConsentStore:
    return ctx.lifespan_context['consent']
