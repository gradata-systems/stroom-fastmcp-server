"""What promotion checks: a clean step of the pipeline's current code (recorded in the build's record by stepping),
and docs."""
from types import SimpleNamespace

from security import record
from security.guard import WriteGuard
from tests.fake_explorer import Explorer
from tools import builds, stepping

TEMPLATE = {'type': 'Pipeline', 'uuid': 't', 'name': 'Event Data (Text)'}
OWN = {'type': 'Pipeline', 'uuid': 'p', 'name': 'Acme'}
LAYERS = [
    # The template sets the text converter; the pipeline sets its own XSLT.
    {'sourcePipeline': TEMPLATE, 'pipelineData': {
        'elements': {'add': [{'id': 'dsParser', 'type': 'DSParser'}, {'id': 'translationFilter', 'type': 'XSLTFilter'}]},
        'properties': {'add': [{'element': 'dsParser', 'name': 'textConverter',
                                'value': {'entity': {'type': 'TextConverter', 'uuid': 'tc', 'name': 'shared'}}}]}}},
    {'sourcePipeline': OWN, 'pipelineData': {'properties': {'add': [
        {'element': 'translationFilter', 'name': 'xslt', 'value': {'entity': {'type': 'XSLT', 'uuid': 'x', 'name': 'Acme'}}}]}}},
]
BUILD = 'acme-v1'


class FakeStroom(Explorer):
    """The explorer, plus pipeline layers and the code docs they name."""

    def __init__(self, managed=True):
        super().__init__()
        self.code = {'x': '<xsl:stylesheet>v1</xsl:stylesheet>', 'tc': 'template code'}
        tags = ('mcp-managed', 'mcp-generated') if managed else ()
        for uuid, name in (('p', 'Acme'), ('q', 'Other')):
            if managed:
                self.in_build(BUILD, 'Pipeline', name, uuid, tags)
            else:
                self.add('Pipeline', name, self.folder('System/Feeds')['uuid'], uuid)

    async def pipeline_layers(self, uuid):
        return LAYERS

    async def get_doc(self, doc_type, uuid):
        if uuid in self.code:
            return {'data': self.code[uuid]}
        return await super().get_doc(doc_type, uuid)


def ctx(stroom):
    return SimpleNamespace(lifespan_context={'stroom': stroom})


async def kept(stroom, uuid='p'):
    return await record.entry(WriteGuard(stroom, 'MCP Workspace'), BUILD, uuid)


P = {'type': 'Pipeline', 'uuid': 'p', 'name': 'Acme'}
CLEAN = {'verdict': 'clean', 'records_stepped': 5}


async def test_only_the_pipelines_own_code_is_fingerprinted():
    prints = await stepping.code_fingerprint(FakeStroom(), 'p')
    assert list(prints) == ['translationFilter']


async def test_a_clean_draft_counts_once_it_is_saved_and_a_later_edit_does_not():
    stroom = FakeStroom()
    c = ctx(stroom)
    assert not await stepping.stepped_clean(c, P)
    draft = {'translationFilter': '<xsl:stylesheet>v2</xsl:stylesheet>'}
    await stepping.remember_clean(c, P, draft, CLEAN)
    assert not await stepping.stepped_clean(c, P)  # the saved code is still v1
    stroom.code['x'] = draft['translationFilter']
    assert await stepping.stepped_clean(c, P)
    # Another replica, or a restart: a fresh context reads the same record.
    assert await stepping.stepped_clean(ctx(stroom), P)
    stroom.code['x'] = '<xsl:stylesheet>v3</xsl:stylesheet>'
    assert not await stepping.stepped_clean(c, P)


async def test_records_are_kept_in_the_build_record_not_as_tags():
    # Asked for by the user: tags are what people filter the explorer by, and per-build and per-digest tags grew the
    # list with every build. Only mcp-managed and mcp-generated are on the build's documents.
    stroom = FakeStroom()
    await stepping.remember_clean(ctx(stroom), P, None, CLEAN)
    assert await stepping.remember_verified(ctx(stroom), P) and await stepping.remember_validated(ctx(stroom), P)
    assert sorted(stroom.tags('p')) == ['mcp-generated', 'mcp-managed']
    entry = await kept(stroom)
    assert entry['stepped'] == entry['verified'] == entry['validated'] and entry['name'] == 'Acme'
    folder = stroom.folder(f'System/MCP Workspace/{BUILD}')
    doc = next(c for c in folder['children'] if c['name'] == record.NAME)
    text = stroom.docs[doc['uuid']]['data']
    assert "don't edit it. Don't move or rename it" in text and '| Acme | Pipeline |' in text
    # Not one of the build's documents: build_status and promotion don't list it.
    assert record.NAME not in [d['name'] for d in await WriteGuard(stroom, 'MCP Workspace').folder_contents(BUILD)]


async def test_blocking_or_empty_runs_are_not_recorded():
    stroom = FakeStroom()
    await stepping.remember_clean(ctx(stroom), P, None, {'verdict': 'blocking', 'records_stepped': 5})
    await stepping.remember_clean(ctx(stroom), P, None, {'verdict': 'clean', 'records_stepped': 0})
    assert not (await kept(stroom)).get('stepped')


async def test_pipelines_the_server_does_not_manage_are_never_recorded():
    stroom = FakeStroom(managed=False)
    await stepping.remember_clean(ctx(stroom), P, None, CLEAN)
    assert not await stepping.remember_verified(ctx(stroom), P)
    assert stroom.docs == {} and stroom.tags('p') == []


async def test_only_the_records_that_can_still_matter_are_kept():
    # The new record, and the one for the code saved now: a draft stepped clean doesn't drop the saved code's.
    stroom = FakeStroom()
    await stepping.remember_clean(ctx(stroom), P, None, CLEAN)
    for n in range(8):
        await stepping.remember_clean(ctx(stroom), P, {'translationFilter': f'draft {n}'}, CLEAN)
    assert len((await kept(stroom))['stepped']) == 2 and await stepping.stepped_clean(ctx(stroom), P)
    stroom.code['x'] = 'draft 7'           # the last draft saved: recorded, as stepped before it was saved
    assert await stepping.stepped_clean(ctx(stroom), P)


async def test_tags_of_earlier_versions_are_folded_into_the_record_and_removed():
    # Seen in production: a build in progress when the server was upgraded, its records still tags.
    stroom = FakeStroom()
    digest = stepping.fingerprint_digest(await stepping.code_fingerprint(stroom, 'p'))
    stroom.tags('p').extend([f'mcp-build-{BUILD}', f'mcp-stepped-20261010005129-{digest}', f'mcp-validated-{digest}'])
    stroom.tags('q').extend([f'mcp-build-{BUILD}', 'mcp-copy-of-1a2b-3c4d'])
    assert await stepping.stepped_clean(ctx(stroom), P) and await stepping.validated(ctx(stroom), P)
    assert not await stepping.verified(ctx(stroom), P)
    assert sorted(stroom.tags('p')) == sorted(stroom.tags('q')) == ['mcp-generated', 'mcp-managed']
    assert (await kept(stroom, 'q'))['copy_of'] == '1a2b-3c4d'
    assert await WriteGuard(stroom, 'MCP Workspace').copy_of({'type': 'Pipeline', 'uuid': 'q', 'name': 'Other'}) == '1a2b-3c4d'


async def test_a_record_saved_by_another_replica_meanwhile_is_kept():
    # Two replicas recording at once: Stroom refuses the save of a doc changed since it was read, and the change is
    # made again on the newer one.
    stroom = FakeStroom()
    guard = WriteGuard(stroom, 'MCP Workspace')
    await record.update(guard, BUILD, P, lambda e: e.update(stepped=['a']))
    put = stroom.put_doc
    raced = []

    async def racing_put(doc):
        if not raced:
            raced.append(1)
            current = await Explorer.get_doc(stroom, 'Documentation', doc['uuid'])
            state = record.parse(current['data'])
            state['docs']['q'] = {'name': 'Other', 'verified': ['b']}
            current['data'] = record.render(BUILD, state)
            await put(current)                       # the other replica's save lands first
        return await put(doc)
    stroom.put_doc = racing_put
    await record.update(guard, BUILD, P, lambda e: e.update(validated=['c']))
    state = await record.load(guard, BUILD)
    assert state['docs']['q']['verified'] == ['b'] and state['docs']['p']['validated'] == ['c']


async def test_build_checks_name_what_is_missing():
    stroom = FakeStroom()
    c = ctx(stroom)
    await stepping.remember_clean(c, P, None, {'verdict': 'clean', 'records_stepped': 3})
    docs = [{'type': 'Pipeline', 'uuid': 'p', 'name': 'Acme', 'working_copy_of': None},
            {'type': 'Pipeline', 'uuid': 'q', 'name': 'Other', 'working_copy_of': None},
            {'type': 'Documentation', 'uuid': 'd', 'name': 'Other', 'working_copy_of': None}]
    problems = await builds.build_checks(c, docs)
    assert problems == [
        "Pipeline 'Acme': no documentation (write_documentation)",
        "Pipeline 'Other': no clean step_sample or step_records of its current code is recorded",
    ]


async def test_the_record_goes_once_nothing_it_kept_is_left_in_the_build():
    stroom = FakeStroom()
    guard = WriteGuard(stroom, 'MCP Workspace')
    await stepping.remember_clean(ctx(stroom), P, None, CLEAN)
    await record.update(guard, BUILD, {'type': 'Pipeline', 'uuid': 'q', 'name': 'Other'}, lambda e: e.update(stepped=['x']))
    await record.forget(guard, BUILD, ['p'])                     # q kept in the workspace
    assert list((await record.load(guard, BUILD))['docs']) == ['q']
    await record.forget(guard, BUILD, ['q'])
    assert record.NAME not in [d['name'] for d in await guard.folder_contents(BUILD, with_record=True)]


async def test_the_record_name_is_the_servers_own():
    import pytest
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="server's own record"):
        await WriteGuard(FakeStroom(), 'MCP Workspace').create('Documentation', record.NAME, BUILD)


async def test_a_managed_pipeline_outside_a_build_folder_says_its_step_was_not_recorded():
    stroom = FakeStroom()
    p = stroom.nodes['p']
    stroom.nodes[stroom.parent['p']]['children'].remove(p)
    elsewhere = stroom.folder('System/Feeds')
    elsewhere['children'].append(p)
    stroom.parent['p'] = elsewhere['uuid']
    result = dict(CLEAN)
    await stepping.remember_clean(ctx(stroom), P, None, result)
    assert 'not in a build folder' in result['record'] and 'promotion will report it as not done' in result['record']
