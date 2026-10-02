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
    with patch.object(plan, 'guard_from', lambda c: guard), \
            patch('tools.instructions.applicable_instructions', AsyncMock(return_value={'instructions': []})):
        result = await plan.start_onboarding(None, 'FortiOS firewall', {
            'a.log': 'date=2026-10-01 time=10:00:00 srcip=10.0.0.1 action=accept\n' * 3,
            'b.log': 'date=2026-10-02 time=10:00:00 srcip=10.0.0.2 action=deny dstport=443\n' * 3})
    assert result['build'] == 'onboard-fortios-firewall' and result['done'] is False
    assert result['profile']['format'] == 'key=value' and result['parser'] == 'DSParser'
    assert result['template'].startswith('Event Data (Text)') and result['text_converter'].startswith('needed: build_data_splitter')
    assert result['next']['step'] == 'feed' and len(result['plan']) == 14
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
