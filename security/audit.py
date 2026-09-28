"""Structured audit trail: one JSON object per line on the 'audit' logger.

Records every tool call and every Stroom request with the Keycloak identity that caused it,
so content the agent creates or changes can be traced back to a person.
"""
import json
import logging
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

# Correlates the tool_call event with the stroom_request events it triggered.
_call_id: ContextVar[str | None] = ContextVar('audit_call_id', default=None)


def configure_audit_log(path: Path | None) -> None:
    """Send audit events to `path` (JSON lines) or stdout, separately from application logs."""
    handler = logging.FileHandler(path, encoding='utf-8') if path else logging.StreamHandler(sys.stdout)
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
        'username': claims.get('preferred_username'),
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


class AuditMiddleware(Middleware):
    """Records every tool call with its arguments, outcome and duration."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        reset = _call_id.set(uuid.uuid4().hex)
        started = time.perf_counter()
        tool, arguments = context.message.name, context.message.arguments
        try:
            result = await call_next(context)
        except Exception as e:
            audit('tool_call', tool=tool, arguments=arguments, outcome='error', error=str(e),
                  duration_ms=round((time.perf_counter() - started) * 1000))
            raise
        else:
            audit('tool_call', tool=tool, arguments=arguments, outcome='success',
                  duration_ms=round((time.perf_counter() - started) * 1000))
            return result
        finally:
            _call_id.reset(reset)
