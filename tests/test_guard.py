"""The write guard's refusals are audited."""
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from security.guard import WriteGuard

XSLT = {'type': 'XSLT', 'uuid': 'x-1', 'name': 'ACME-VPN-Events-V1.0'}


@pytest.mark.parametrize('check, tags, reason', [('check_managed', ['mcp-generated'], 'not_managed'),
                                                 ('check_built', [], 'not_built')])
async def test_refusals_are_audited(check, tags, reason):
    guard = WriteGuard(AsyncMock(post=AsyncMock(return_value={'tags': tags})), 'MCP Workspace')
    with patch('security.guard.audit') as audit, pytest.raises(ToolError):
        await getattr(guard, check)(XSLT)
    audit.assert_called_once_with('access_denied', reason=reason, doc=XSLT)


async def test_managed_docs_pass_without_an_audit_event():
    guard = WriteGuard(AsyncMock(post=AsyncMock(return_value={'tags': ['mcp-managed', 'mcp-generated']})), 'MCP Workspace')
    with patch('security.guard.audit') as audit:
        await guard.check_managed(XSLT)
        await guard.check_built(XSLT)
    audit.assert_not_called()
