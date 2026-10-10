"""What promotion checks: a clean step of the pipeline's current code (recorded as tags by stepping), and docs."""
from types import SimpleNamespace

from fastmcp.exceptions import ToolError

from tools import builds, stepping

OWN = {'type': 'Pipeline', 'uuid': 'p', 'name': 'Acme'}
TEMPLATE = {'type': 'Pipeline', 'uuid': 't', 'name': 'Event Data (Text)'}
LAYERS = [
    # The template sets the text converter; the pipeline sets its own XSLT.
    {'sourcePipeline': TEMPLATE, 'pipelineData': {
        'elements': {'add': [{'id': 'dsParser', 'type': 'DSParser'}, {'id': 'translationFilter', 'type': 'XSLTFilter'}]},
        'properties': {'add': [{'element': 'dsParser', 'name': 'textConverter',
                                'value': {'entity': {'type': 'TextConverter', 'uuid': 'tc', 'name': 'shared'}}}]}}},
    {'sourcePipeline': OWN, 'pipelineData': {'properties': {'add': [
        {'element': 'translationFilter', 'name': 'xslt', 'value': {'entity': {'type': 'XSLT', 'uuid': 'x', 'name': 'Acme'}}}]}}},
]


class FakeStroom:
    """Pipeline layers, code docs, and explorer tags (what the write guard reads and writes)."""
    settings = SimpleNamespace(workspace_folder='MCP Workspace')

    def __init__(self, managed=('p', 'q')):
        self.code = {'x': '<xsl:stylesheet>v1</xsl:stylesheet>', 'tc': 'template code'}
        self.node_tags = {u: ['mcp-managed', 'mcp-generated'] for u in managed}

    async def pipeline_layers(self, uuid):
        return LAYERS

    async def get_doc(self, doc_type, uuid):
        return {'data': self.code[uuid]}

    async def post(self, path, body):
        assert path == '/explorer/v2/getFromDocRef'
        return {**body, 'tags': list(self.node_tags.get(body['uuid'], []))}

    async def request(self, method, path, body):
        for ref in body['docRefs']:
            tags = self.node_tags.setdefault(ref['uuid'], [])
            if path.endswith('addTags'):
                added = tags + [t for t in body['tags'] if t not in tags]
                if len(' '.join(added)) > 255:
                    # Stroom keeps a node's tags in one varchar(255) column.
                    raise ToolError("Stroom rejected the request (500): Data too long for column 'tags' at row 1")
                tags[:] = added
            else:
                tags[:] = [t for t in tags if t not in body['tags']]


def ctx(stroom):
    return SimpleNamespace(lifespan_context={'stroom': stroom})


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
    # Another replica, or a restart: a fresh context reads the same tags.
    assert await stepping.stepped_clean(ctx(stroom), P)
    stroom.code['x'] = '<xsl:stylesheet>v3</xsl:stylesheet>'
    assert not await stepping.stepped_clean(c, P)


async def test_blocking_or_empty_runs_are_not_recorded():
    stroom = FakeStroom()
    await stepping.remember_clean(ctx(stroom), P, None, {'verdict': 'blocking', 'records_stepped': 5})
    await stepping.remember_clean(ctx(stroom), P, None, {'verdict': 'clean', 'records_stepped': 0})
    assert not stepping.stepped_tags(stroom.node_tags['p'])


async def test_pipelines_the_server_does_not_manage_are_never_tagged():
    stroom = FakeStroom(managed=())
    await stepping.remember_clean(ctx(stroom), P, None, CLEAN)
    assert stroom.node_tags == {}


async def test_only_the_records_that_can_still_matter_are_kept():
    # The new record, and the one for the code saved now: a draft stepped clean doesn't drop the saved code's.
    stroom = FakeStroom()
    await stepping.remember_clean(ctx(stroom), P, None, CLEAN)
    for n in range(8):
        await stepping.remember_clean(ctx(stroom), P, {'translationFilter': f'draft {n}'}, CLEAN)
    assert len(stepping.stepped_tags(stroom.node_tags['p'])) == 2 and await stepping.stepped_clean(ctx(stroom), P)
    stroom.code['x'] = 'draft 7'           # the last draft saved: recorded, as stepped before it was saved
    assert await stepping.stepped_clean(ctx(stroom), P)


async def test_records_fit_the_column_stroom_keeps_tags_in_and_old_ones_are_cleared():
    # Seen in production: five timestamped records of each kind outgrew Stroom's 255 characters, every new record
    # was refused, and promotion said the pipeline had never stepped clean.
    stroom = FakeStroom()
    stroom.node_tags['p'] += ['mcp-build-onboard-fortigate-firewall'] + [
        f'mcp-stepped-2026101009{n:04d}-{n:016x}' for n in range(4)]
    assert len(' '.join(stroom.node_tags['p'])) > 200
    result = dict(CLEAN)
    await stepping.remember_clean(ctx(stroom), P, None, result)
    assert 'record' not in result and await stepping.stepped_clean(ctx(stroom), P)
    assert not any(t.startswith('mcp-stepped-2026') for t in stroom.node_tags['p'])      # the old form cleared
    assert await stepping.remember_verified(ctx(stroom), P) and await stepping.remember_validated(ctx(stroom), P)
    await stepping.remember_clean(ctx(stroom), P, {'translationFilter': 'a draft'}, dict(CLEAN))
    assert len(' '.join(stroom.node_tags['p'])) <= 255 and await stepping.stepped_clean(ctx(stroom), P)


async def test_a_record_that_cannot_be_saved_is_said_in_the_reply():
    stroom = FakeStroom()
    stroom.node_tags['p'] += ['mcp-build-' + 'x' * 220]           # no room left at all
    result = dict(CLEAN)
    await stepping.remember_clean(ctx(stroom), P, None, result)
    assert 'promotion will report it as not done' in result['record']
    assert not await stepping.remember_verified(ctx(FakeStroom(managed=())), P)     # outside a build: not recorded


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
