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
    with patch.object(guard, '_build_node', AsyncMock(return_value=(folder, node))), \
            patch('security.guard.asyncio.sleep', AsyncMock()):
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
    tree = {'children': [{'type': 'XSLT', 'uuid': 'x', 'name': 'X', 'tags': ['mcp-managed']},
                         {'type': 'Documentation', 'uuid': 'r', 'name': 'Build record', 'tags': ['mcp-managed']}]}

    async def post(path, body):
        if path == '/explorer/v2/find':
            assert body['filter']['tags'] == ['mcp-managed']
            return {'values': [{'docRef': {'type': 'Folder', 'uuid': 'f', 'name': 'b1'}, 'path': 'System / MCP Workspace'},
                               {'docRef': {'type': 'XSLT', 'uuid': 'x', 'name': 'X'}, 'path': 'System / MCP Workspace / b1'},
                               {'docRef': {'type': 'Pipeline', 'uuid': 'p', 'name': 'P'}, 'path': 'System / MCP Workspace / b1'},
                               {'docRef': {'type': 'Pipeline', 'uuid': 'b', 'name': 'B'}, 'path': 'System / MCP Workspace / b2'},
                               {'docRef': {'type': 'Pipeline', 'uuid': 'q', 'name': 'Q'}, 'path': 'System / Elsewhere'}]}
        return {'tags': ['mcp-generated', 'mcp-managed']}       # getFromDocRef
    guard._stroom = SimpleNamespace(post=post)
    with patch.object(guard, '_build_node', AsyncMock(return_value=(folder, tree))):
        docs = await guard.folder_contents('b1')
    assert sorted(d['uuid'] for d in docs) == ['p', 'x'] and next(d for d in docs if d['uuid'] == 'p')['tags'][1] == 'mcp-managed'
    with patch.object(guard, '_build_node', AsyncMock(return_value=(folder, tree))):
        assert sorted(d['uuid'] for d in await guard.folder_contents('b1', with_record=True)) == ['p', 'r', 'x']


async def test_a_documents_build_is_the_folder_it_is_in():
    # Not a tag: the user asked for only mcp-managed and mcp-generated on documents, so the tag list doesn't grow with
    # every build.
    from tests.fake_explorer import Explorer
    stroom = Explorer()
    guard = WriteGuard(stroom, 'MCP Workspace')
    made = await guard.create('XSLT', 'Acme', 'acme-v1')
    assert sorted(stroom.tags(made['uuid'])) == ['mcp-generated', 'mcp-managed']
    assert await guard.build_of(made) == 'acme-v1'
    elsewhere = stroom.ref(stroom.add('XSLT', 'Prod', stroom.folder('System/Feeds')['uuid']))
    assert await guard.build_of(elsewhere) is None
    assert await guard.build_of({'type': 'XSLT', 'uuid': 'gone', 'name': 'Gone'}) is None
    copied = await guard.create('XSLT', 'Prod', 'acme-v1', copy_of=elsewhere['uuid'])
    assert await guard.copy_of(copied) == elsewhere['uuid'] and await guard.copy_of(made) is None
    assert sorted(stroom.tags(copied['uuid'])) == ['mcp-generated', 'mcp-managed']


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


async def test_an_emptied_build_folder_is_removed_once_the_tree_catches_up():
    # Seen in the translation suite: right after promotion moved its docs out, the tree still showed the build
    # folder with children, and the folder was left behind.
    stroom = FakeExplorer('System/MCP Workspace', 'System/MCP Workspace/acme-v1')
    guard = WriteGuard(stroom, 'MCP Workspace')
    folder = {**stroom.folders['System/MCP Workspace/acme-v1'], '_path': 'System/MCP Workspace/acme-v1'}
    behind = {'nodeFlags': ['FM', 'F', 'O'], 'children': [{'type': 'XSLT', 'name': 'moved'}]}
    caught_up = {'nodeFlags': ['FM', 'F', 'L'], 'children': None}
    nodes = AsyncMock(side_effect=[(folder, behind), (folder, caught_up)])
    with patch.object(guard, '_build_node', nodes), patch.object(guard, 'folder_contents', AsyncMock(return_value=[])), \
            patch('security.guard.asyncio.sleep', AsyncMock()):
        assert await guard.remove_build_folder_if_empty('acme-v1') is True
    # Docs really left in it: no waiting, no delete.
    with patch.object(guard, '_build_node', AsyncMock(return_value=(folder, behind))), \
            patch.object(guard, 'folder_contents', AsyncMock(return_value=[{'type': 'XSLT'}])), \
            patch('security.guard.asyncio.sleep', AsyncMock()) as slept:
        assert await guard.remove_build_folder_if_empty('acme-v1') is False
    assert not slept.called


async def test_a_name_the_build_already_has_is_not_created_again():
    # Seen: after Copilot summarised a long conversation, the agent created the same indexing pipeline twice.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from fastmcp.exceptions import ToolError
    from security.guard import WriteGuard
    stroom = SimpleNamespace(post=AsyncMock(return_value={'type': 'Pipeline', 'uuid': 'new', 'name': 'Other'}))
    guard = WriteGuard(stroom, 'MCP Workspace')
    there = [{'type': 'Pipeline', 'uuid': 'p1', 'name': 'Firewall Indexing', 'tags': [], 'path': 'x'}]
    with patch.object(WriteGuard, 'folder_contents', AsyncMock(return_value=there)), \
            patch.object(WriteGuard, 'build_folder', AsyncMock(return_value={'uuid': 'f', '_path': 'x'})), \
            patch.object(WriteGuard, 'tag', AsyncMock()):
        with pytest.raises(ToolError, match=r"A Pipeline named 'Firewall Indexing' is already in build b \(uuid p1\)"):
            await guard.create('Pipeline', 'Firewall Indexing', 'b')
        assert stroom.post.await_count == 0
        made = await guard.create('XSLT', 'Firewall Indexing', 'b')       # another type may share the name
        assert made['uuid'] == 'new'


async def test_tags_are_asked_for_with_the_doc_ref_alone():
    # Seen (Gemma, VS Code): a feed found by search carried its path, and Stroom refused the request as "Unable to
    # process JSON", so every sample filter for the build failed.
    stroom = SimpleNamespace(post=AsyncMock(return_value={'tags': ['mcp-build-b']}))
    guard = WriteGuard(stroom, 'MCP Workspace')
    found = {'type': 'Feed', 'uuid': 'f1', 'name': 'ACME-V1.0', 'path': 'System / MCP Workspace / b'}
    assert await guard.tags(found) == ['mcp-build-b']
    stroom.post.assert_awaited_once_with('/explorer/v2/getFromDocRef', {'type': 'Feed', 'uuid': 'f1', 'name': 'ACME-V1.0'})


async def test_a_documents_build_is_found_while_the_tree_still_leaves_it_out():
    # e2e: a pipeline copied moments before it stepped clean wasn't in the explorer tree yet, so its step went
    # unrecorded and processing was refused.
    from tests.fake_explorer import Explorer
    stroom = Explorer()
    guard = WriteGuard(stroom, 'MCP Workspace')
    made = await guard.create('Pipeline', 'Acme', 'acme-v1')
    tree = stroom.post
    lagging = {'tree': 2}

    async def post(path, body):
        if path == '/explorer/v2/fetchExplorerNodes' and body.get('ensureVisible') and lagging['tree']:
            lagging['tree'] -= 1
            return {'rootNodes': []}
        return await tree(path, body)
    stroom.post = post
    with patch('security.guard.asyncio.sleep', AsyncMock()):
        assert await guard.build_of(made) == 'acme-v1'                 # from the search index
        stroom.find_documents = AsyncMock(return_value={'values': []})  # not indexed yet either
        lagging['tree'] = 2
        assert await guard.build_of(made) == 'acme-v1'                 # the tree, looked at again
