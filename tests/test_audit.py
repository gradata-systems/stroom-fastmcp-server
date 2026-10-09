"""Resource reads are audited, and the audit log file survives rotation."""
import json
import logging
import sys
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP

import security.audit as audit_module
from security.audit import AuditMiddleware, audit


async def test_resource_reads_are_audited_with_the_call_id_of_their_requests(caplog):
    mcp = FastMCP('t', middleware=[AuditMiddleware()])

    @mcp.resource('data://{name}')
    def data(name: str) -> str:
        audit('stroom_request', method='GET', path='/x')
        return name

    with patch('security.audit.get_access_token', return_value=None), caplog.at_level(logging.INFO, 'audit'):
        propagate, audit_module.audit_logger.propagate = audit_module.audit_logger.propagate, True
        try:
            async with Client(mcp) as client:
                await client.read_resource('data://x')
        finally:
            audit_module.audit_logger.propagate = propagate
    request, read = (json.loads(r.getMessage()) for r in caplog.records if r.name == 'audit')
    assert request['call_id'] == read['call_id'] is not None
    assert read['event'] == 'resource_read' and read['uri'] == 'data://x' and read['outcome'] == 'success'


@pytest.mark.skipif(sys.platform == 'win32', reason="an open file can't be renamed on Windows")
def test_audit_file_is_reopened_after_rotation(tmp_path):
    log = tmp_path / 'audit.jsonl'
    audit_module.configure_audit_log(log)
    try:
        audit_module.audit('tool_call', tool='before')
        log.rename(tmp_path / 'audit.jsonl.1')   # what logrotate does
        audit_module.audit('tool_call', tool='after')
    finally:
        for handler in audit_module.audit_logger.handlers:
            handler.close()
        audit_module.audit_logger.handlers.clear()
    assert json.loads((tmp_path / 'audit.jsonl.1').read_text())['tool'] == 'before'
    assert json.loads(log.read_text())['tool'] == 'after'


async def test_a_tool_call_says_where_its_time_went(caplog):
    # Calls took a median of 26 s in a VS Code run; Stroom's own log couldn't say why. Each tool_call now says how
    # much was Stroom (and its slowest request) and how much was the user filling a form.
    mcp = FastMCP('t', middleware=[AuditMiddleware()])

    @mcp.tool
    def build_status() -> str:
        audit_module.spent_in_stroom(120, 'POST', '/explorer/v2/find')
        audit_module.spent_in_stroom(900, 'POST', '/processorFilter/v1/find')
        audit_module.spent_waiting_for_user(5000)
        return 'ok'

    with patch('security.audit.get_access_token', return_value=None), caplog.at_level(logging.INFO, 'audit'):
        propagate, audit_module.audit_logger.propagate = audit_module.audit_logger.propagate, True
        try:
            async with Client(mcp) as client:
                await client.call_tool('build_status', {})
        finally:
            audit_module.audit_logger.propagate = propagate
    call = next(json.loads(r.getMessage()) for r in caplog.records if r.name == 'audit')
    assert (call['stroom_requests'], call['stroom_ms'], call['user_ms']) == (2, 1020, 5000)
    assert call['slowest'] == 'POST /processorFilter/v1/find' and call['slowest_ms'] == 900
    assert call['duration_ms'] >= 0 and call['outcome'] == 'success'


async def test_a_requests_wait_before_the_tool_is_measured_from_its_arrival():
    # VS Code calls reached the tool 2 to 48 s after being sent, growing through a session, with nothing to say
    # whether the wait was before the server or inside it ahead of the audit's own timing.
    import time
    from types import SimpleNamespace
    from unittest.mock import patch
    from security.audit import ArrivalTimer, _waited_ms
    seen = {}

    async def app(scope, receive, send):
        seen.update(scope)
    await ArrivalTimer(app)({'type': 'http'}, None, None)
    arrived = seen['state']['arrived']
    request = SimpleNamespace(scope={'state': {'arrived': arrived}})
    with patch('fastmcp.server.dependencies.get_http_request', lambda: request):
        assert _waited_ms(arrived + 1.5) == 1500
    with patch('fastmcp.server.dependencies.get_http_request', side_effect=RuntimeError('no request')):
        assert _waited_ms(time.perf_counter()) is None
