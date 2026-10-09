"""The plan with its state, `next` in results, and the refusals that keep the order."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tools import pipeline_writes, plan
from tools.pipeline_writes import PropertyValue


def test_the_checklist_covers_both_stages_in_order():
    steps = [s['step'] for s in plan.checklist()]
    assert steps[:3] == ['feed', 'samples', 'converter'] and steps[-1] == 'promoted'
    assert steps.index('stepped') < steps.index('processed') < steps.index('documented') < steps.index('index')


async def test_results_carry_the_next_step_until_promotion():
    with patch.object(plan, 'status', AsyncMock(return_value={'next': {'step': 'samples', 'do': 'upload', 'tools': 'upload_sample'}})):
        result = await plan.with_next(None, 'b', {'type': 'Feed'})
    assert result['next']['step'] == 'samples' and result['done'] is False
    with patch.object(plan, 'status', AsyncMock(return_value={'next': {'step': 'promoted', 'do': 'promote', 'tools': 'promote_build'}})):
        assert 'done' not in await plan.with_next(None, 'b', {'type': 'Feed'})
    # A confirmation round, or no build, passes through untouched; a failing status never fails the write.
    assert await plan.with_next(None, 'b', {'status': 'needs_confirmation'}) == {'status': 'needs_confirmation'}
    assert await plan.with_next(None, None, {'x': 1}) == {'x': 1}
    with patch.object(plan, 'status', AsyncMock(side_effect=RuntimeError('stroom down'))):
        assert await plan.with_next(None, 'b', {'x': 1}) == {'x': 1}


async def test_start_onboarding_profiles_every_file_and_returns_the_plan():
    guard = SimpleNamespace(build_folder=AsyncMock(return_value={'_path': 'System/MCP Workspace/onboard-fortios-firewall', 'uuid': 'f'}))
    # The environment's own templates, whatever they're called: no 'Event Data (Text)' is assumed.
    here = {'candidates': [{'name': 'json-in v3', 'parser': 'JSONParser'}, {'name': 'Acme text v2', 'parser': 'DSParser'}]}
    with patch.object(plan, 'guard_from', lambda c: guard), \
            patch('tools.templates.find_pipeline_templates', AsyncMock(return_value=here)), \
            patch('tools.instructions.applicable_instructions', AsyncMock(return_value={'instructions': []})):
        result = await plan.start_onboarding(None, 'FortiOS firewall', {
            'a.log': 'date=2026-10-01 time=10:00:00 srcip=10.0.0.1 action=accept\n' * 3,
            'b.log': 'date=2026-10-02 time=10:00:00 srcip=10.0.0.2 action=deny dstport=443\n' * 3})
    assert result['build'] == 'onboard-fortios-firewall' and result['done'] is False
    assert result['profile']['format'] == 'key=value' and result['parser'] == 'DSParser'
    assert result['template'].startswith('Acme text v2 (its parser, DSParser') and result['text_converter'].startswith('needed: build_data_splitter')
    assert result['next']['step'] == 'feed' and len(result['plan']) == 15
    assert result['next']['call'] == {'tool': 'create_feed', 'arguments': {
        'build': 'onboard-fortios-firewall', 'name': '<the feed name the user confirmed>'}}
    with pytest.raises(ToolError, match='Give the sample files'):
        await plan.start_onboarding(None, 'x', {})


async def test_create_pipeline_refuses_documents_from_outside_the_build():
    guard = SimpleNamespace(tags=AsyncMock(return_value=['mcp-managed', 'mcp-generated', 'mcp-build-other']))
    props = [PropertyValue(element='translationFilter', name='xslt', doc_uuid='x', doc_type='XSLT')]
    with patch.object(pipeline_writes, 'guard_from', lambda c: guard):
        with pytest.raises(ToolError, match="is not a document of build 'fortios'"):
            await pipeline_writes._own_documents(None, 'fortios', props, allowed=False)
        await pipeline_writes._own_documents(None, 'other', props, allowed=False)      # its own build's XSLT
        await pipeline_writes._own_documents(None, 'fortios', props, allowed=True)     # the user said so
    guard.tags = AsyncMock(return_value=[])                                            # a production XSLT
    with patch.object(pipeline_writes, 'guard_from', lambda c: guard):
        with pytest.raises(ToolError, match='reuse_existing_docs=true'):
            await pipeline_writes._own_documents(None, 'fortios', props, allowed=False)


async def test_create_pipeline_refuses_a_parser_that_cannot_read_the_sample():
    merged = {'elements': [{'id': 'jsonParser', 'type': 'JSONParser'}, {'id': 'translationFilter', 'type': 'XSLTFilter'}],
              'links': [{'from': 'jsonParser', 'to': 'translationFilter'}]}
    sample = {'stream_id': 7, 'feed': 'FORTIOS', 'format': 'key=value', 'suggested_parser': 'Data Splitter splitting on spaces then =',
              'needs_text_converter': True}
    with patch('tools.plan.sample_format', AsyncMock(return_value=sample)):
        with pytest.raises(ToolError, match='key=value, which this template.s JSONParser cannot read; it needs DSParser'):
            await pipeline_writes._parser_reads_sample(None, 'b', merged, None, allowed=False)
        await pipeline_writes._parser_reads_sample(None, 'b', merged, 'CombinedParser', allowed=False)   # reads anything
        await pipeline_writes._parser_reads_sample(None, 'b', merged, None, allowed=True)
    with patch('tools.plan.sample_format', AsyncMock(return_value=None)):   # no sample uploaded yet: nothing to check
        await pipeline_writes._parser_reads_sample(None, 'b', merged, None, allowed=False)


def test_next_is_one_call_with_what_the_build_already_knows():
    # A small model given a list of tools for the next step deliberated between them until its context ran out.
    call, then = plan.next_call('stepped', 'acme-v1', ['ACME'], [7, 8], [], 'p-1', None)
    assert call == {'tool': 'step_sample', 'arguments': {'pipeline_uuid': 'p-1', 'stream_ids': [7, 8]}}
    assert 'build_translation_xslt uuid=' in then
    call, _ = plan.next_call('indexed', 'acme-v1', ['ACME'], [7], [9], 'p-1', 'p-2')
    assert call['arguments'] == {'pipeline_uuid': 'p-2', 'stream_ids': [9], 'source_pipeline_uuid': 'p-1'}
    # Every step has one, naming a tool that exists, with arguments it takes.
    import inspect
    import main_tools
    tools = {t.__name__: t for m in main_tools.TOOL_MODULES for t in m.ALL_TOOLS}
    for step in [i['step'] for i in plan.checklist()]:
        call, _ = plan.next_call(step, 'b', ['F'], [1], [2], 'p', 'q')
        assert set(call['arguments']) <= set(inspect.signature(tools[call['tool']]).parameters), step


async def _status_of(indexing_verified: bool, agreed: bool, filters: list[dict]) -> dict:
    """The plan of an Elasticsearch build with an events pipeline, an index doc and an indexing pipeline, both
    stepped clean, its events processed and documented."""
    docs = [{'type': 'Feed', 'uuid': 'f', 'name': 'ACME', 'path': 'b', 'working_copy_of': None},
            {'type': 'TextConverter', 'uuid': 'tc', 'name': 'ACME-CSV', 'path': 'b', 'working_copy_of': None},
            {'type': 'XSLT', 'uuid': 'x', 'name': 'ACME-Translation', 'path': 'b', 'working_copy_of': None},
            {'type': 'Pipeline', 'uuid': 'ev', 'name': 'ACME-Events', 'path': 'b', 'working_copy_of': None},
            {'type': 'Documentation', 'uuid': 'd', 'name': 'ACME-Events', 'path': 'b', 'working_copy_of': None},
            {'type': 'ElasticIndex', 'uuid': 'ix-doc', 'name': 'ACME-V1', 'path': 'b', 'working_copy_of': None},
            {'type': 'Pipeline', 'uuid': 'ix', 'name': 'ACME-Indexing', 'path': 'b', 'working_copy_of': None}]
    stroom = SimpleNamespace(
        get_doc=AsyncMock(side_effect=lambda t, u: {'uuid': u, 'name': u, 'description': ''}),
        pipeline_layers=AsyncMock(return_value=[]),
        find_meta=AsyncMock(return_value={'values': [{'meta': {'id': 9, 'status': 'UNLOCKED'}}]}))
    stages = {'ev': 'translation', 'ix': 'indexing'}
    with patch('tools.builds._build_docs', AsyncMock(return_value=docs)), \
            patch('tools.builds.build_checks', AsyncMock(return_value=[])), \
            patch.object(plan, 'gateway_from', lambda c: stroom), \
            patch.object(plan, 'guard_from', lambda c: None), \
            patch.object(plan, 'sample_streams', AsyncMock(return_value={'Raw Events': [{'id': 7}]})), \
            patch.object(plan, 'sample_format', AsyncMock(return_value={'needs_text_converter': True})), \
            patch('utils.mappingstore.read_mapping', lambda d: ('translation', {})), \
            patch('tools.templates._shape', AsyncMock(side_effect=lambda s, u: {'stage': stages[u], 'parser': None})), \
            patch('tools.pipeline_writes.open_slots', AsyncMock(return_value=[])), \
            patch('tools.pipelines.merge_layers', lambda layers: {}), \
            patch('tools.stepping.stepped_clean', AsyncMock(return_value=True)), \
            patch('tools.stepping.verified', AsyncMock(return_value=indexing_verified)), \
            patch('tools.processing_writes.elastic_destination',
                  AsyncMock(side_effect=lambda s, u: {'index name': 'acme-v1', 'cluster': 'ES'} if u == 'ix' else None)), \
            patch('tools.processing_writes.agreement_problem', AsyncMock(return_value=None if agreed else 'not agreed')), \
            patch('tools.processing.processing_status', AsyncMock(return_value={'filters': filters})):
        return await plan.status(None, 'b')


async def test_an_index_is_indexed_once_verified_not_once_stepped():
    # Seen in VS Code: stepping the indexing pipeline clean counted as indexed, so the plan led on to promotion
    # with nothing agreed, committed or indexed.
    unagreed = await _status_of(indexing_verified=False, agreed=False, filters=[])
    state = {s['step']: s['state'] for s in unagreed['steps']}
    assert state['indexing_pipeline'] == 'done' and state['index_template'] == 'to do' and state['indexed'] == 'to do'
    assert unagreed['next']['step'] == 'index_template'
    assert unagreed['next']['call']['tool'] == 'propose_index_template'
    assert unagreed['next']['call']['arguments']['pipeline_uuid'] == 'ix'
    assert unagreed['next']['call']['arguments']['example_template'].startswith("<the user's example index template")
    # Agreed: the processor filter, then the wait while it runs, then verify_index; done once that passes.
    nxt = [(await _status_of(False, True, filters))['next'] for filters in
           ([], [{'finished': False}], [{'finished': True}])]
    assert [n['step'] for n in nxt] == ['indexed'] * 3
    assert [n['call']['tool'] for n in nxt] == ['create_processor_filter', 'wait_for_processing', 'verify_index']
    assert nxt[2]['call']['arguments']['index_uuid'] == 'ix-doc' and nxt[2]['call']['arguments']['backend'] == 'elasticsearch'
    done = await _status_of(True, True, [{'finished': True}])
    assert {s['step']: s['state'] for s in done['steps']}['indexed'] == 'done'
    assert done['next']['step'] == 'index_documented'


def test_every_indexing_call_takes_its_arguments():
    import inspect
    import main_tools
    tools = {t.__name__: t for m in main_tools.TOOL_MODULES for t in m.ALL_TOOLS}
    for processing in (None, 'running', 'finished'):
        call, _ = plan.next_call('indexed', 'b', ['F'], [1], [2], 'p', 'q', processing, {'uuid': 'i', 'backend': 'lucene'})
        assert set(call['arguments']) <= set(inspect.signature(tools[call['tool']]).parameters), processing


async def test_next_says_what_to_do_when_its_tool_is_hidden(monkeypatch):
    # Seen: create_pipeline hidden in a VS Code tool group; the agent wrote a handoff note and stopped.
    from tools import plan
    async def nxt(ctx, build, made=None):
        return {'step': 'pipeline', 'call': {'tool': 'create_pipeline', 'arguments': {}}}
    monkeypatch.setattr(plan, 'next_step', nxt)
    monkeypatch.setattr(plan, 'remember_build', lambda ctx, build: None)
    result = await plan.with_next(None, 'b', {'ok': True})
    assert result['next']['if_missing'].startswith('create_pipeline not in your tool list? Call the activate_* tool')


async def test_a_sample_cut_part_way_through_a_record_is_profiled_by_its_whole_records():
    # Qwen in VS Code: 50 XML fragments (35 KB) read from their first 20,000 characters, the last fragment cut, were
    # profiled as key=value ("User: x", "Action: [y]" in the messages), and create_pipeline refused the
    # XMLFragmentParser that start_onboarding had named.
    from tools import plan
    record = ('<ns0:Event xmlns:ns0="http://schemas.microsoft.com/win/2004/08/events/event"><ns0:System>'
              '<ns0:EventID>1000</ns0:EventID><ns0:Computer>ss.domain.com</ns0:Computer></ns0:System><ns0:EventData>'
              '<ns0:Data>Sep 27 2026 23:48:10 - User: domain.com\\Bloggs, Joe - [[SecretServer]] Event: [User] '
              'Action: [Login] By User: domain.com\\joe.bloggs (Item Id: 7)</ns0:Data></ns0:EventData></ns0:Event>\n')
    head = (record * 5)[:len(record) * 4 + 200]
    streams = {'Raw Events': [{'id': 7, 'feed': 'SS', 'createMs': 1}]}
    with patch.object(plan, 'sample_streams', AsyncMock(return_value=streams)), \
            patch('tools.sampling.read_head', AsyncMock(return_value=(head, True, 1))), \
            patch.object(plan, 'gateway_from', lambda ctx: None):
        assert (await plan.sample_format(None, 'b'))['format'] == 'xml fragments'
