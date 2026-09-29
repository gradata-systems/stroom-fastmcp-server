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


async def test_a_call_can_pass_two_gates_in_turn():
    calls = []

    async def index(name: str, confirmation_id: str | None = None, approval_id: str | None = None) -> str:
        calls.append((confirmation_id, approval_id))
        if not confirmation_id:
            return json.dumps({'status': 'needs_confirmation', 'confirmation_id': 'c', 'summary': 'Template written?'})
        if not approval_id:
            return json.dumps({'status': 'needs_approval', 'approval_id': 'a', 'summary': 'Start indexing?'})
        return json.dumps({'filter_id': 9})
    app = one_node_graph(gated(StructuredTool.from_function(coroutine=index, name='index', description='Index.')))
    config = {'configurable': {'thread_id': 'two'}}
    first = await app.ainvoke({}, config)
    assert first['__interrupt__'][0].value['kind'] == 'confirmation'
    second = await app.ainvoke(Command(resume={'approved': True}), config)
    assert second['__interrupt__'][0].value['kind'] == 'approval'
    final = await app.ainvoke(Command(resume={'approved': True}), config)
    assert json.loads(final['result']) == {'filter_id': 9} and calls[-1] == ('c', 'a')


async def test_user_session_refreshes_the_token_before_it_expires():
    import httpx
    import respx
    from agent.auth import UserSession
    session = UserSession('https://kc/realms/r', 'agent', {'access_token': 'old', 'refresh_token': 'r1', 'expires_in': 10})
    with respx.mock:
        route = respx.post('https://kc/realms/r/protocol/openid-connect/token').mock(
            return_value=httpx.Response(200, json={'access_token': 'new', 'refresh_token': 'r2', 'expires_in': 300}))
        assert await session._fresh() == 'new'
        assert await session._fresh() == 'new' and route.call_count == 1
    assert b'refresh_token=r1' in route.calls.last.request.content


def test_fix_flow_routing():
    assert g.start({'mode': 'fix_pipeline_issue'}) == 'locate_issue' and g.start({'mode': 'onboard'}) == 'intake'
    assert g.after_locate({}) == 'ask_for_help' and g.after_locate({'issue_location': {'raw_stream': 38}}) == 'draft_fix'
    ready = {'fix': {'ready': True}, 'fix_attempt': 1, 'attempts': {'draft_fix': 1}}
    assert g.after_draft_fix(ready) == 'offer_fix'
    assert g.after_draft_fix({**ready, 'fix': {'ready': False}}) == 'draft_fix'
    assert g.after_draft_fix({**ready, 'fix': {'ready': False}, 'fix_attempt': 5, 'attempts': {'draft_fix': 5}}) == 'ask_for_help'
    # no summarise_fix in the latest attempt: could not reproduce, so ask rather than loop
    assert g.after_draft_fix({**ready, 'attempts': {'draft_fix': 2}}) == 'ask_for_help'
    assert g.after_offer({'fix_choice': 'apply'}) == 'apply_fix' and g.after_offer({'fix_choice': 'manual'}) == 'explain_fix'


def test_offer_fix_asks_and_manual_path_gives_the_steps():
    fix = {'ready': True, 'pipeline': {'name': 'Acme'}, 'doc': {'name': 'Acme XSLT'}, 'diff': '-a\n+b\n',
           'fields_changed': [{'path': 'Event/EventDetail/Description'}], 'records_changed': 4, 'records_compared': 4,
           'manual_steps': ['Open it.', 'Save it.']}
    graph = StateGraph(g.BuildState)
    graph.add_node('offer_fix', g.offer_fix)
    graph.add_node('explain_fix', g.explain_fix)
    graph.add_node('apply_fix', lambda s: {'notes': ['applied']})
    graph.add_edge(START, 'offer_fix')
    graph.add_conditional_edges('offer_fix', g.after_offer, ['apply_fix', 'explain_fix'])
    graph.add_edge('explain_fix', END)
    graph.add_edge('apply_fix', END)
    app = graph.compile(checkpointer=MemorySaver())
    config = {'configurable': {'thread_id': 'fix'}}
    first = app.invoke({'fix': fix, 'request': 'r'}, config)
    question = first['__interrupt__'][0].value
    assert question['kind'] == 'choice' and question['details']['diff'] == '-a\n+b\n'
    final = app.invoke(Command(resume={'approved': False}), config)
    assert final['fix_choice'] == 'manual' and '1. Open it.' in final['notes'][-1] and '+b' in final['notes'][-1]


def test_template_review_routing():
    es = {'backend': 'elasticsearch', 'step_verdict': 'clean'}
    assert g.after_step_indexing(es) == 'propose_template'
    assert g.after_step_indexing({**es, 'user_template': '{...}'}) == 'check_template'
    assert g.after_step_indexing({'backend': 'lucene', 'step_verdict': 'clean'}) == 'index_sample'
    assert g.after_review({'template_choice': 'changed'}) == 'check_template'
    assert g.after_review({'template_choice': 'accept'}) == 'index_sample'
    assert g.after_check_template({'template_check': {'compatible': True}}) == 'index_sample'
    assert g.after_check_template({'template_check': {'compatible': False}}) == 'flag_pipeline_changes'
    assert g.after_flag({'template_choice': 'change_pipeline'}) == 'plan_indexing'
    assert g.after_flag({'template_choice': 'change_template'}) == 'review_template'
    # a disabled Elasticsearch filter waits for the user before verification
    assert g.after_index_sample({'filter_ready': {'filter_id': 9}}) == 'await_enable'
    assert g.after_index_sample({'processing_gate': 'pass', 'searches_passed': True}) == 'document'


def test_harvest_picks_up_the_template_and_the_disabled_filter():
    update = harvest([
        tool_message('create_indexing_pipeline', {'uuid': 'ip', 'backend': 'elasticsearch'}),
        tool_message('propose_index_template', {'template_name': 't', 'index': 'ecs-acme-v2', 'dev_tools': 'PUT ...',
                                                'pipeline_link': 'L', 'self_check': {'notes': ['n']}}),
        tool_message('check_index_template', {'compatible': False, 'pipeline_changes': [{'field': 'user.name'}]}),
        tool_message('create_processor_filter', {'filter_id': 9, 'enabled': False, 'pipeline_link': 'L'}),
    ])
    assert update['backend'] == 'elasticsearch' and update['indexing_pipeline'] == 'ip'
    assert update['proposed_template']['dev_tools'] == 'PUT ...' and update['proposed_template']['self_check_notes'] == ['n']
    assert update['template_check']['compatible'] is False and update['filter_ready']['filter_id'] == 9


def review_graph():
    graph = StateGraph(g.BuildState)
    graph.add_node('review_template', g.review_template)
    graph.add_edge(START, 'review_template')
    graph.add_edge('review_template', END)
    return graph.compile(checkpointer=MemorySaver())


@pytest.mark.parametrize('answer, choice', [({'approved': True}, 'accept'),
                                            ({'approved': False, 'template': 'PUT _index_template/x\n{"index_patterns": []}'},
                                             'changed')])
def test_review_shows_the_template_and_takes_back_changes(answer, choice):
    app = review_graph()
    config = {'configurable': {'thread_id': choice}}
    first = app.invoke({'proposed_template': {'index': 'ecs-acme-v2', 'dev_tools': 'PUT _index_template/ecs-acme-v2\n{}'}},
                       config)
    question = first['__interrupt__'][0].value
    assert question['kind'] == 'template' and question['details']['dev_tools'].startswith('PUT _index_template/ecs-acme-v2')
    final = app.invoke(Command(resume=answer), config)
    assert final['template_choice'] == choice
    assert final.get('user_template') == answer.get('template')


def test_await_enable_shows_the_pipeline_link():
    graph = StateGraph(g.BuildState)
    graph.add_node('await_enable', g.await_enable)
    graph.add_edge(START, 'await_enable')
    graph.add_edge('await_enable', END)
    app = graph.compile(checkpointer=MemorySaver())
    config = {'configurable': {'thread_id': 'enable'}}
    first = app.invoke({'filter_ready': {'filter_id': 9, 'pipeline_link': 'https://s/?action=open-doc&docType=Pipeline&docUuid=p'},
                        'request': 'r'}, config)
    question = first['__interrupt__'][0].value
    assert question['kind'] == 'enable' and 'filter 9 is ready to enable' in question['summary']
    assert question['details']['pipeline'].endswith('docUuid=p')
    final = app.invoke(Command(resume={'approved': True, 'note': 'enable it for me'}), config)
    assert 'User on the indexing filter: enable it for me' in final['request']


def test_existing_feed_steps_in_place_until_nothing_new_then_hands_over():
    assert g.start({'mode': 'onboard_existing_feed'}) == 'survey'
    assert g.after_survey({'survey': {'locations': [{'stream': 5, 'part': 0, 'record': 0}]}}) == 'draft_translation'
    assert g.after_survey({}) == 'ask_for_help'
    existing = {'mode': 'onboard_existing_feed', 'step_verdict': 'clean'}
    assert g.after_step({**existing, 'survey': {'saturated': False}}) == 'resurvey'
    # no processing in this mode: once covered, document and promote
    assert g.after_step({**existing, 'survey': {'saturated': True}}) == 'document'
    assert g.after_resurvey({'survey': {'new_shapes': 2}}) == 'draft_translation'
    assert g.after_resurvey({'survey': {'new_shapes': 0, 'saturated': True}}) == 'document'
    assert g.after_resurvey({'survey': {'new_shapes': 0}, 'attempts': {'resurvey': 1}}) == 'resurvey'
    assert g.after_resurvey({'survey': {'new_shapes': 0}, 'attempts': {'resurvey': g.MAX_SURVEYS}}) == 'document'
    assert g.after_step({'mode': 'onboard', 'step_verdict': 'clean'}) == 'process_sample'


def test_harvest_picks_up_the_survey_and_its_locations():
    update = harvest([
        tool_message('survey_feed', {'feed': 'SRC', 'oldest_stream_read': 102, 'saturated': False, 'new_shapes': 1,
                                     'shapes': [{'signature': 'fields:a | action=login', 'count': 5, 'example': '{}'}],
                                     'locations': [{'stream': 105, 'part': 0, 'record': 0, 'shape': 'fields:a | action=login'}]}),
        tool_message('step_records', {'verdict': 'clean', 'groups': []}),
    ])
    assert update['survey']['signatures'] == ['fields:a | action=login'] and update['survey']['oldest_stream_read'] == 102
    assert update['survey']['locations'][0]['stream'] == 105 and update['step_verdict'] == 'clean'
    assert 'test_feed' not in update


def test_the_handover_is_part_of_documenting_an_existing_feed():
    prompt = g._prompt('document', {'mode': 'onboard_existing_feed', 'survey': {'feed': 'SRC'}})
    assert "processing that feed is the user's to start" in prompt and 'index_event_data' in prompt
    assert 'index_event_data' not in g._prompt('document', {'mode': 'onboard'})
