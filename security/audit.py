"""Structured audit trail: one JSON object per line on the 'audit' logger.

Records every tool call and resource read, every Stroom and Elasticsearch request, every confirmation and
approval, and every refusal with the identity that caused it, so content the agent creates or changes can be
traced back to a person. See docs/AUDIT.md.
"""
import json
import logging
import logging.handlers
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mcp.types as mt
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

audit_logger = logging.getLogger('audit')

# Correlates the tool_call or resource_read event with the stroom_request events it triggered.
_call_id: ContextVar[str | None] = ContextVar('audit_call_id', default=None)
# Where a call's time went, summed onto its tool_call event: Stroom's requests, and waiting on the user's forms.
_spent: ContextVar[dict[str, Any] | None] = ContextVar('audit_spent', default=None)


def spent_in_stroom(took_ms: int, method: str, path: str) -> None:
    spent = _spent.get()
    if spent is not None:
        spent['stroom_requests'] += 1
        spent['stroom_ms'] += took_ms
        if took_ms > spent['slowest_ms']:
            spent['slowest_ms'], spent['slowest'] = took_ms, f"{method} {path}"


def spent_waiting_for_user(ms: int) -> None:
    spent = _spent.get()
    if spent is not None:
        spent['user_ms'] += ms


def configure_audit_log(path: Path | None) -> None:
    """Send audit events to `path` (JSON lines) or stdout, separately from application logs.

    The file is reopened when it has been moved or deleted, so logrotate (without copytruncate) can rotate it
    and no event is lost or written to the old file.
    """
    handler = logging.handlers.WatchedFileHandler(path, encoding='utf-8') if path else logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(message)s'))
    audit_logger.handlers[:] = [handler]
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False


def _identity() -> dict[str, Any]:
    token = get_access_token()
    if token is None:
        return {'sub': None}
    claims = token.claims or {}
    return {
        'sub': token.subject,
        # preferred_username from Keycloak, Okta and Entra ID when the profile scope is granted; else Entra's
        # upn, else email.
        'username': claims.get('preferred_username') or claims.get('upn') or claims.get('email'),
        'client_id': claims.get('azp') or token.client_id,
    }


def audit(event: str, **fields: Any) -> None:
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'event': event,
        'call_id': _call_id.get(),
        **_identity(),
        **fields,
    }
    audit_logger.info(json.dumps(record, default=str))


class ArrivalTimer:
    """ASGI middleware: when each HTTP request reached this server, kept in its scope's state, so a tool call's audit
    says how long the request waited between arriving and the tool starting (before_ms). Seen: VS Code calls reaching
    the tool 2 to 48 s after VS Code sent them, the wait growing through a session, with nothing to say whether it
    was before the server or inside it (authentication, the MCP session) ahead of this middleware's own timing."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get('type') == 'http':
            scope.setdefault('state', {})['arrived'] = time.perf_counter()
        await self.app(scope, receive, send)


def _waited_ms(started: float) -> int | None:
    """How long the current HTTP request waited before `started`, from ArrivalTimer's stamp; None outside HTTP."""
    try:
        from fastmcp.server.dependencies import get_http_request
        arrived = get_http_request().scope.get('state', {}).get('arrived')
    except Exception:   # stdio, tests, or no request in this context
        return None
    return round((started - arrived) * 1000) if arrived else None


class AuditMiddleware(Middleware):
    """Records every tool call with its arguments, and every resource read (such as a guide) with its URI, each
    with its outcome and duration."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        return await _audited(context, call_next, 'tool_call',
                              tool=context.message.name, arguments=context.message.arguments)

    async def on_read_resource(self, context: MiddlewareContext[mt.ReadResourceRequestParams],
                               call_next: CallNext[mt.ReadResourceRequestParams, Any]) -> Any:
        return await _audited(context, call_next, 'resource_read', uri=str(context.message.uri))


async def _audited(context: MiddlewareContext[Any], call_next: CallNext[Any, Any], event: str, **fields: Any) -> Any:
    """Run the request, then record `event` with `fields`, the outcome and the duration. Audit events for the
    requests it makes share its call_id."""
    reset = _call_id.set(uuid.uuid4().hex)
    spent = {'stroom_requests': 0, 'stroom_ms': 0, 'slowest_ms': 0, 'slowest': None, 'user_ms': 0}
    reset_spent = _spent.set(spent)
    started = time.perf_counter()

    before = _waited_ms(started)

    def timing() -> dict[str, Any]:
        # The rest of duration_ms (less Stroom and the user) is this server's own work and the network to Stroom.
        took = round((time.perf_counter() - started) * 1000)
        return {'duration_ms': took, **({'before_ms': before} if before is not None else {}),
                **{k: v for k, v in spent.items() if v}}
    try:
        result = await call_next(context)
    except Exception as e:
        audit(event, **fields, outcome='error', error=str(e), **timing())
        raise
    else:
        audit(event, **fields, outcome='success', **timing())
        return result
    finally:
        _spent.reset(reset_spent)
        _call_id.reset(reset)
