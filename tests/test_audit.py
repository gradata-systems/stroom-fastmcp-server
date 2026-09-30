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
