"""The audit log file survives rotation."""
import json
import sys

import pytest

import security.audit as audit_module


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
