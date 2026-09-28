import json
from typing import Any, TypedDict

import pytest

pytest.importorskip('langgraph')

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agent import graph as g  # noqa: E402
from agent.gating import gated  # noqa: E402
from agent.state import harvest  # noqa: E402


def fake_create_feed(calls: list):
    async def create_feed(name: str, confirmation_id: str | None = None) -> str:
        calls.append(confirmation_id)
        if confirmation_id is None:
            return json.dumps({'status': 'needs_confirmation', 'confirmation_id': 'conf-1',
                               'summary': f"Create feed '{name}'", 'details': {'feed name': name}})
        assert confirmation_id == 'conf-1'
        return json.dumps({'type': 'Feed', 'name': name, 'uuid': 'f-1'})
    return StructuredTool.from_function(coroutine=create_feed, name='create_feed', description='Create a feed.')


class S(TypedDict, total=False):
    result: Any


def one_node_graph(tool):
    async def node(state: S) -> S:
        return {'result': await tool.ainvoke({'name': 'ACME-V1.0'})}
    graph = StateGraph(S)
    graph.add_node('n', node)
    graph.add_edge(START, 'n')
    graph.add_edge('n', END)
    return graph.compile(checkpointer=MemorySaver())


@pytest.mark.parametrize('answer, expect_created', [({'approved': True}, True), ({'approved': False, 'note': 'use ACME-VPN'}, False)])
async def test_gated_tool_interrupts_and_passes_the_id_only_when_the_user_agrees(answer, expect_created):
    calls = []
    app = one_node_graph(gated(fake_create_feed(calls)))
    config = {'configurable': {'thread_id': 't'}}
    first = await app.ainvoke({}, config)
    assert first['__interrupt__'][0].value['summary'] == "Create feed 'ACME-V1.0'"
    final = await app.ainvoke(Command(resume=answer), config)
    result = json.loads(final['result'])
    if expect_created:
        assert result['uuid'] == 'f-1' and calls[-1] == 'conf-1'
    else:
        assert result['status'] == 'declined' and result['user_note'] == 'use ACME-VPN'
        assert 'conf-1' not in calls


def tool_message(name, data):
    return ToolMessage(content=json.dumps(data), name=name, tool_call_id=name)


def test_harvest_collects_routing_facts():
    update = harvest([
        tool_message('upload_sample', {'stream_id': 12}),
        tool_message('create_pipeline', {'uuid': 'p-1'}),
        tool_message('step_sample', {'verdict': 'blocking', 'groups': [
            {'class': 'blocking', 'severity': 'ERROR', 'element': 'schemaFilter', 'count': 1},
            {'class': 'benign', 'severity': 'WARNING', 'element': 'decorationFilter', 'count': 9}]}),
        tool_message('wait_for_processing', {'gate': 'pass', 'streams': [{'input': 12, 'events': [13]}]}),
        tool_message('run_test_searches', {'passed': True}),
    ])
    assert update['raw_stream_ids'] == [12] and update['translation_pipeline'] == 'p-1'
    assert update['step_verdict'] == 'blocking' and [f['element'] for f in update['last_findings']] == ['schemaFilter']
    assert update['processing_gate'] == 'pass' and update['events_stream_ids'] == [13]
    assert update['searches_passed'] is True


def test_routing_is_code_with_bounded_loops():
    assert g.after_step({'step_verdict': 'clean'}) == 'process_sample'
    assert g.after_step({'step_verdict': 'blocking', 'attempts': {'draft_translation': 2}}) == 'draft_translation'
    assert g.after_step({'step_verdict': 'blocking', 'attempts': {'draft_translation': 5}}) == 'ask_for_help'
    assert g.after_processing({'processing_gate': 'fail'}) == 'ask_for_help'
    assert g.after_index_sample({'processing_gate': 'pass', 'searches_passed': False, 'attempts': {}}) == 'plan_indexing'
    assert g.after_index_sample({'processing_gate': 'pass', 'searches_passed': True}) == 'document'
    assert g.after_help({'last_node': 'index_sample'}) == 'plan_indexing'


class ToolCallingFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


async def test_node_runs_its_tools_and_harvests_the_verdict():
    async def step_sample(pipeline_uuid: str, stream_ids: list[int]) -> str:
        return json.dumps({'verdict': 'blocking', 'groups': [{'class': 'blocking', 'element': 'schemaFilter', 'count': 2}]})
    tools = {'step_sample': StructuredTool.from_function(coroutine=step_sample, name='step_sample', description='Step.')}
    model = ToolCallingFake(messages=iter([
        AIMessage(content='', tool_calls=[{'name': 'step_sample', 'args': {'pipeline_uuid': 'p-1', 'stream_ids': [12]},
                                           'id': 'c1'}]),
        AIMessage(content='Two schema errors.')]))
    update = await g._node('step_and_validate', model, tools)({'translation_pipeline': 'p-1', 'raw_stream_ids': [12]})
    assert update['step_verdict'] == 'blocking' and update['last_node'] == 'step_and_validate'
    assert g.after_step({**update}) == 'draft_translation'


def test_full_graph_compiles_with_every_node():
    app = g.build_graph(ToolCallingFake(messages=iter([])), [])
    assert set(g.NODES) | {'ask_for_help'} <= set(app.get_graph().nodes)
