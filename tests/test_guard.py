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


class FakeExplorer:
    """Folders by path, enough for finding and creating them."""

    def __init__(self, *paths):
        self.folders = {'System': {'type': 'System', 'uuid': '0', 'name': 'System', 'uniqueKey': 'k-0'}}
        self.requests = []
        for path in paths:
            self.add(path)

    def add(self, path):
        n = len(self.folders)
        node = {'type': 'Folder', 'uuid': f'u-{n}', 'name': path.rsplit('/', 1)[1], 'uniqueKey': f'k-{n}'}
        self.folders[path] = node
        return node

    async def find_documents(self, name, types, limit):
        return {'values': [{'docRef': n, 'path': p.rsplit('/', 1)[0].replace('/', ' / ')}
                           for p, n in self.folders.items() if n['name'] == name and n['type'] in types]}

    async def post(self, path, body):
        self.requests.append(('POST', path, body))
        if path == '/explorer/v2/fetchExplorerNodes':
            return {'rootNodes': [self.folders['System']]}
        if path == '/explorer/v2/create':
            parent = next(p for p, n in self.folders.items() if n['uuid'] == body['destinationFolder']['uuid'])
            return self.add(f"{parent}/{body['docName']}") if body['docType'] == 'Folder' else \
                {'type': body['docType'], 'uuid': 'd-1', 'name': body['docName']}
        return {**body, 'tags': []}

    async def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        return {}


async def test_resolve_folder_finds_what_exists_and_names_what_is_missing():
    guard = WriteGuard(FakeExplorer('System/Feeds', 'System/Feeds/Events'), 'MCP Workspace')
    node, missing = await guard.resolve_folder('System / Feeds/Events/Acme/V1/')
    assert node['_path'] == 'System/Feeds/Events' and missing == ['Acme', 'V1']
    node, missing = await guard.resolve_folder('System/Feeds')
    assert node['_path'] == 'System/Feeds' and missing == []
    with pytest.raises(ToolError, match='starting with System/'):
        await guard.resolve_folder('Feeds/Events')


async def test_a_document_whose_filling_fails_is_deleted_not_left_empty():
    stroom = FakeExplorer()
    guard = WriteGuard(stroom, 'MCP Workspace')

    async def fill(ref):
        raise ToolError('Stroom rejected the request (500)')

    with pytest.raises(ToolError, match='500'):
        await guard.create_filled('Documentation', 'ACME-VPN-Events', 'acme-v1', fill)
    assert ('DELETE', '/explorer/v2/delete', {'docRefs': [{'type': 'Documentation', 'uuid': 'd-1',
                                                            'name': 'ACME-VPN-Events'}]}) in stroom.requests

    async def ok(ref):
        return {**ref, 'data': '# ACME'}

    assert (await guard.create_filled('Documentation', 'ACME-VPN-Events', 'acme-v1', ok))['data'] == '# ACME'


@pytest.mark.parametrize('node, removed', [
    ({'nodeFlags': ['FM', 'F', 'L'], 'children': None}, True),                    # empty
    ({'nodeFlags': ['FM', 'F', 'O'], 'children': [{'type': 'Folder'}]}, False),   # a subfolder is left
    ({'nodeFlags': ['FM', 'F'], 'children': None}, False),                        # not known to be a leaf
    ({}, False),                                                                  # not found in the tree
])
async def test_a_build_folder_is_removed_only_when_completely_empty(node, removed):
    stroom = FakeExplorer('System/MCP Workspace', 'System/MCP Workspace/acme-v1')
    guard = WriteGuard(stroom, 'MCP Workspace')
    folder = {**stroom.folders['System/MCP Workspace/acme-v1'], '_path': 'System/MCP Workspace/acme-v1'}
    with patch.object(guard, '_build_node', AsyncMock(return_value=(folder, node))):
        assert await guard.remove_build_folder_if_empty('acme-v1') is removed
    deletes = [r for r in stroom.requests if r[0] == 'DELETE']
    assert deletes == ([('DELETE', '/explorer/v2/delete', {'docRefs': [{'type': 'Folder', 'uuid': folder['uuid'],
                                                                         'name': 'acme-v1'}]})] if removed else [])


async def test_reading_a_build_does_not_create_its_folder():
    stroom = FakeExplorer('System/MCP Workspace')
    guard = WriteGuard(stroom, 'MCP Workspace')
    assert await guard.folder_contents('acme-v1') == []
    assert 'System/MCP Workspace/acme-v1' not in stroom.folders
    assert (await guard.build_folder('acme-v1'))['_path'] == 'System/MCP Workspace/acme-v1'  # writes still do


async def test_managed_docs_pass_without_an_audit_event():
    guard = WriteGuard(AsyncMock(post=AsyncMock(return_value={'tags': ['mcp-managed', 'mcp-generated']})), 'MCP Workspace')
    with patch('security.guard.audit') as audit:
        await guard.check_managed(XSLT)
        await guard.check_built(XSLT)
    audit.assert_not_called()
