"""The write guard's refusals are audited."""
from types import SimpleNamespace
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
        self.deleted, self.ignored_deletes = set(), 0     # Stroom can answer a delete without deleting, at first
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
        if path == '/explorer/v2/getFromDocRef' and body.get('uuid') in self.deleted:
            return None
        return {**body, 'tags': []}

    async def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        if method == 'DELETE' and path == '/explorer/v2/delete':
            if self.ignored_deletes:
                self.ignored_deletes -= 1
            else:
                self.deleted |= {r['uuid'] for r in body['docRefs']}
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


async def test_a_folder_the_search_still_lists_after_deletion_is_not_found():
    stroom = SimpleNamespace(
        find_documents=AsyncMock(return_value={'values': [{'docRef': {'type': 'Folder', 'name': 'b1', 'uuid': 'f'},
                                                           'path': 'System / MCP Workspace'}]}),
        post=AsyncMock(return_value=None))           # getFromDocRef: gone
    guard = WriteGuard(stroom, 'MCP Workspace')
    assert await guard.find_child_folder({'_path': 'System/MCP Workspace'}, 'b1') is None


async def test_a_doc_the_explorer_tree_leaves_out_is_still_in_the_build():
    from security.guard import WriteGuard
    guard = WriteGuard(SimpleNamespace(), 'MCP Workspace')
    folder = {'_path': 'System/MCP Workspace/b1'}
    tree = {'children': [{'type': 'XSLT', 'uuid': 'x', 'name': 'X', 'tags': ['mcp-build-b1']}]}

    async def post(path, body):
        if path == '/explorer/v2/find':
            assert body['filter']['tags'] == ['mcp-build-b1']
            return {'values': [{'docRef': {'type': 'Folder', 'uuid': 'f', 'name': 'b1'}, 'path': 'System / MCP Workspace'},
                               {'docRef': {'type': 'XSLT', 'uuid': 'x', 'name': 'X'}, 'path': 'System / MCP Workspace / b1'},
                               {'docRef': {'type': 'Pipeline', 'uuid': 'p', 'name': 'P'}, 'path': 'System / MCP Workspace / b1'},
                               {'docRef': {'type': 'Pipeline', 'uuid': 'q', 'name': 'Q'}, 'path': 'System / Elsewhere'}]}
        return {'tags': ['mcp-build-b1', 'mcp-managed']}       # getFromDocRef
    guard._stroom = SimpleNamespace(post=post)
    with patch.object(guard, '_build_node', AsyncMock(return_value=(folder, tree))):
        docs = await guard.folder_contents('b1')
    assert sorted(d['uuid'] for d in docs) == ['p', 'x'] and next(d for d in docs if d['uuid'] == 'p')['tags'][1] == 'mcp-managed'


@pytest.mark.parametrize('ignored, removed, deletes', [(1, True, 2), (9, False, 5)])
async def test_a_build_folder_is_removed_only_once_it_is_gone(ignored, removed, deletes):
    # Seen in the translation suite: right after a working copy in it was deleted, Stroom answered the folder's
    # delete without deleting it, and promotion said it was removed.
    stroom = FakeExplorer('System/MCP Workspace', 'System/MCP Workspace/acme-v1')
    stroom.ignored_deletes = ignored
    guard = WriteGuard(stroom, 'MCP Workspace')
    folder = {**stroom.folders['System/MCP Workspace/acme-v1'], '_path': 'System/MCP Workspace/acme-v1'}
    with patch.object(guard, '_build_node', AsyncMock(return_value=(folder, {'nodeFlags': ['L'], 'children': None}))),             patch('security.guard.asyncio.sleep', AsyncMock()):
        assert await guard.remove_build_folder_if_empty('acme-v1') is removed
    assert len([r for r in stroom.requests if r[0] == 'DELETE']) == deletes


async def test_two_docs_made_one_after_the_other_share_one_build_folder():
    # Seen in the translation suite: copy_pipeline made a pipeline and its XSLT in a new build; the search index
    # didn't list the new build folder yet, so the second doc made a twin folder of the same name, and promotion
    # removed one, leaving the other behind.
    stroom = FakeExplorer('System/MCP Workspace')
    indexed = dict(stroom.folders)

    async def lagging_search(name, types, limit):    # only what was there before: new folders aren't indexed yet
        return {'values': [{'docRef': n, 'path': p.rsplit('/', 1)[0].replace('/', ' / ')}
                           for p, n in indexed.items() if n['name'] == name and n['type'] in types]}
    stroom.find_documents = lagging_search
    guard = WriteGuard(stroom, 'MCP Workspace')
    with patch.object(guard, 'tag', AsyncMock()):
        await guard.create('Pipeline', 'ACME-Events-WORKING', 'acme-fix')
        await guard.create('XSLT', 'ACME-Events-WORKING', 'acme-fix')
    made = [r for r in stroom.requests if r[1] == '/explorer/v2/create' and r[2]['docType'] == 'Folder']
    assert [r[2]['docName'] for r in made] == ['acme-fix']
