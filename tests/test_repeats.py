"""Calls that keep failing: the agent is told to step back, and an identical failing call isn't run again."""
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from security.repeats import RepeatGuard

calls = []


def server() -> FastMCP:
    calls.clear()
    mcp = FastMCP('test', middleware=[RepeatGuard()])

    @mcp.tool
    def build_translation_xslt(mapping: dict) -> dict:
        calls.append(mapping)
        if mapping.get('raise'):
            raise ToolError('mapping is not a translation mapping: events: Input should be a valid list')
        if mapping.get('flaky'):
            raise ToolError('Stroom timed out: call again')
        return {'ok': mapping.get('good', False), 'problems': [] if mapping.get('good') else ['extract[0]: no match']}

    @mcp.tool
    def step_sample(pipeline_uuid: str) -> dict:
        calls.append(pipeline_uuid)
        return {'ok': False, 'problems': ['blocking']}
    return mcp


async def test_a_run_of_failures_says_to_step_back_and_a_success_resets_it():
    async with Client(server()) as client:
        notes = []
        for n in range(5):
            result = await client.call_tool('build_translation_xslt', {'mapping': {'try': n}})
            notes.append(result.structured_content.get('repeated'))
        assert notes[:4] == [None] * 4
        assert notes[4].startswith('That is 5 failed build_translation_xslt calls in a row. Start again from a clean '
                                   'mapping')
        ok = await client.call_tool('build_translation_xslt', {'mapping': {'good': True}})
        assert 'repeated' not in ok.structured_content
        again = await client.call_tool('build_translation_xslt', {'mapping': {'try': 9}})
        assert 'repeated' not in again.structured_content      # the count started again


async def test_the_same_failing_call_is_answered_from_its_failure_after_three():
    async with Client(server()) as client:
        for n in range(3):
            with pytest.raises(ToolError) as raised:
                await client.call_tool('build_translation_xslt', {'mapping': {'raise': True}})
            if n:
                assert 'The same arguments as a call that already failed' in str(raised.value)
        with pytest.raises(ToolError, match='Not run again: this exact call has already failed 3 times'):
            await client.call_tool('build_translation_xslt', {'mapping': {'raise': True}})
        assert len(calls) == 3          # the fourth was not run


async def test_transient_failures_and_tools_whose_answer_can_change_are_run_again():
    async with Client(server()) as client:
        for _ in range(4):
            with pytest.raises(ToolError, match='timed out') as raised:
                await client.call_tool('build_translation_xslt', {'mapping': {'flaky': True}})
            assert 'already failed' not in str(raised.value)
        for _ in range(4):
            await client.call_tool('step_sample', {'pipeline_uuid': 'p'})
    assert calls.count('p') == 4        # step_sample's answer changes with the pipeline: always run
