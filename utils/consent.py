"""Confirmations (key details) and approvals (risky actions) before a write tool acts.

The user is asked directly through MCP elicitation when the client supports it. Otherwise the tool
returns a pending id with a plain-language summary; the client shows it to the user and repeats the
call with the id once they agree. The id is bound to the action, the exact details and the caller,
and expires, so it cannot be reused for something else.
"""
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

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


def _user() -> str | None:
    token = get_access_token()
    return (token.claims or {}).get('preferred_username') if token else None


def _digest(action: str, details: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([action, details], sort_keys=True, default=str).encode()).hexdigest()


class ConsentStore:
    def __init__(self, use_elicitation: bool = True):
        self.use_elicitation = use_elicitation
        self._pending: dict[str, _Pending] = {}

    async def require(self, ctx: Any, kind: Kind, action: str, summary: str, details: dict[str, Any],
                      token: str | None) -> dict[str, Any] | None:
        """None when the user has agreed; otherwise the response the tool should return.

        Raises ToolError when the user declines or the id does not match this exact request.
        """
        digest = _digest(action, details)
        if token:
            pending = self._pending.pop(token, None)
            if pending is None or pending.expires < time.time():
                raise ToolError(f"Unknown or expired {kind} id; request the {kind} again")
            if (pending.kind, pending.action, pending.digest, pending.user) != (kind, action, digest, _user()):
                raise ToolError(f"This {kind} id was issued for a different request; request the {kind} again")
            audit(kind, action=action, details=details, outcome='granted', via='id')
            return None

        if self.use_elicitation and hasattr(ctx, 'elicit'):
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


def _format(details: dict[str, Any]) -> str:
    return '\n'.join(f"- {k}: {v}" for k, v in details.items())


def consent_from(ctx: Any) -> ConsentStore:
    return ctx.lifespan_context['consent']
