"""The build graph: nodes are small tool-using agents; routing between them is code.

Onboarding runs stage 1 (sample to Events) then stage 2 (indexing) then documentation and promotion.
Loops (draft, step, fix) are bounded by attempt counts; when a loop runs out the graph interrupts and asks
the person for a hint. Confirmations and approvals interrupt inside tool calls (agent.gating).
"""
from typing import Any, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langchain.agents import create_agent
from langgraph.types import interrupt

from agent.gating import gated
from agent.state import BuildState, findings_text, harvest

MAX_ATTEMPTS = 5
RULES = ("Confirmations and approvals are handled for you: call the tool, and if the user declines you get a "
         "'declined' result with their note; follow it. Never invent ids. Keep replies short.")

# name: (instructions, tools the node may use)
NODES: dict[str, tuple[str, list[str]]] = {
    'intake': ("Profile the sample in the request with profile_sample. If the user supplied vendor documentation, "
               "record it with record_source_notes. Start the build with start_build (a short name for the source).",
               ['profile_sample', 'start_build', 'record_source_notes']),
    'onboard_feed': ("Find how sibling feeds are named (find_documents type Feed), propose a feed name following it, "
                     "create_feed, then upload_sample with the sample from the request.",
                     ['find_documents', 'create_feed', 'upload_sample']),
    'draft_translation': ("Choose the translation template (find_pipeline_templates stage=translation, "
                          "list_template_children, describe_template_contract). Draft the text converter if the "
                          "template needs one, and the XSLT (read stroom://guide/xslt and event-logging if needed). "
                          "Use step_sample with draft_code until clean, then save with create_text_converter / "
                          "create_xslt (or update_xslt) and create_pipeline once. Previous findings to fix: {findings}",
                          ['find_pipeline_templates', 'list_template_children', 'describe_template_contract',
                           'find_similar_translations', 'get_document', 'check_xslt', 'step_sample', 'step_pipeline',
                           'create_text_converter', 'update_text_converter', 'create_xslt', 'update_xslt',
                           'create_pipeline', 'read_stream', 'get_stream_attributes']),
    'step_and_validate': ("Run step_sample on the translation pipeline {translation_pipeline} over streams "
                          "{raw_stream_ids} and report the verdict.", ['step_sample', 'step_pipeline']),
    'process_sample': ("Start processing the sample: create_processor_filter on pipeline {translation_pipeline} with "
                       "stream_ids {raw_stream_ids}, then wait_for_processing. Validate one Events record with "
                       "read_stream, validate_events and check_event_quality.",
                       ['create_processor_filter', 'wait_for_processing', 'read_stream', 'validate_events',
                        'check_event_quality', 'summarise_errors', 'processing_status']),
    'plan_indexing': ("Choose the indexing template (find_pipeline_templates stage=indexing gives the backend), the "
                      "field convention (get_field_conventions; ask the user if none is set), and for Elasticsearch "
                      "the cluster (find_elastic_clusters). Draft the mapping with draft_index_mapping over Events "
                      "streams {events_stream_ids}, then create_index_doc and set_index_fields (Lucene) or "
                      "put_index_template (Elasticsearch), create_xslt with the drafted indexing XSLT, and "
                      "create_indexing_pipeline. Previous findings to fix: {findings}",
                      ['find_pipeline_templates', 'get_field_conventions', 'find_elastic_clusters', 'draft_index_mapping',
                       'create_index_doc', 'set_index_fields', 'put_index_template', 'create_xslt', 'update_xslt',
                       'create_indexing_pipeline', 'list_index_templates', 'simulate_index_template']),
    'step_indexing': ("Run step_sample on indexing pipeline {indexing_pipeline} over Events streams {events_stream_ids}.",
                      ['step_sample', 'step_pipeline']),
    'index_sample': ("create_processor_filter on indexing pipeline {indexing_pipeline} with stream_ids "
                     "{events_stream_ids}, wait_for_processing with expect_events=false, then "
                     "create_verification_dashboard and run_test_searches (stream ids, an exact match on key fields "
                     "using values from stepped documents, and a time range).",
                     ['create_processor_filter', 'wait_for_processing', 'create_verification_dashboard',
                      'run_test_searches', 'summarise_errors', 'step_pipeline']),
    'document': ("Write documentation for pipelines {translation_pipeline} and {indexing_pipeline} with "
                 "write_documentation (describe_translation and summarise_events help), then summarise for the user.",
                 ['write_documentation', 'describe_translation', 'summarise_events', 'describe_pipeline']),
    'promote': ("Propose destination folders from where sibling sources live (find_documents), then promote_build "
                "for build {build}.", ['promote_build', 'find_documents', 'list_build']),
}


def _prompt(name: str, state: BuildState) -> str:
    text, _ = NODES[name]
    values = {k: state.get(k) for k in ('translation_pipeline', 'indexing_pipeline', 'raw_stream_ids',
                                        'events_stream_ids', 'build')}
    return text.format(findings=findings_text(state), **values)


def _node(name: str, model: BaseChatModel, tools: dict[str, BaseTool]) -> Callable:
    allowed = [gated(tools[t]) for t in NODES[name][1] if t in tools]
    agent = create_agent(model, allowed)

    async def run(state: BuildState) -> dict[str, Any]:
        attempts = dict(state.get('attempts') or {})
        attempts[name] = attempts.get(name, 0) + 1
        result = await agent.ainvoke({'messages': [
            SystemMessage(f"You are building Stroom content, step '{name}'. {RULES}"),
            HumanMessage(f"Request:\n{state.get('request', '')}\n\nThis step: {_prompt(name, state)}")]})
        update = harvest(result['messages'])
        if 'raw_stream_ids' in update:
            update['raw_stream_ids'] = sorted(set((state.get('raw_stream_ids') or []) + update['raw_stream_ids']))
        last = result['messages'][-1].content if result['messages'] else ''
        return {**update, 'attempts': attempts, 'messages': result['messages'], 'last_node': name,
                'notes': (state.get('notes') or []) + [f"{name}: {str(last)[:300]}"]}
    return run


def ask_for_help(state: BuildState) -> dict[str, Any]:
    answer = interrupt({'kind': 'help', 'summary': "The agent is stuck and needs a hint.",
                        'details': {'last_step_verdict': state.get('step_verdict'),
                                    'processing_gate': state.get('processing_gate'),
                                    'findings': state.get('last_findings')}})
    hint = answer.get('note') if isinstance(answer, dict) else str(answer)
    return {'request': f"{state.get('request', '')}\n\nHint from the user: {hint}", 'attempts': {}}


# Routing: code, not model choices.
def after_step(state: BuildState) -> str:
    if state.get('step_verdict') == 'clean':
        return 'process_sample'
    return 'draft_translation' if (state.get('attempts') or {}).get('draft_translation', 0) < MAX_ATTEMPTS else 'ask_for_help'


def after_processing(state: BuildState) -> str:
    return 'plan_indexing' if state.get('processing_gate') == 'pass' else 'ask_for_help'


def after_step_indexing(state: BuildState) -> str:
    if state.get('step_verdict') == 'clean':
        return 'index_sample'
    return 'plan_indexing' if (state.get('attempts') or {}).get('plan_indexing', 0) < MAX_ATTEMPTS else 'ask_for_help'


def after_index_sample(state: BuildState) -> str:
    if state.get('processing_gate') == 'pass' and state.get('searches_passed'):
        return 'document'
    return 'plan_indexing' if (state.get('attempts') or {}).get('plan_indexing', 0) < MAX_ATTEMPTS else 'ask_for_help'


RESUME_AT = {'intake': 'draft_translation', 'onboard_feed': 'draft_translation', 'draft_translation': 'draft_translation',
             'step_and_validate': 'draft_translation', 'process_sample': 'process_sample',
             'plan_indexing': 'plan_indexing', 'step_indexing': 'plan_indexing', 'index_sample': 'plan_indexing'}


def after_help(state: BuildState) -> str:
    """Resume the loop that got stuck, now with the user's hint in the request."""
    return RESUME_AT.get(state.get('last_node', ''), 'draft_translation')


def build_graph(model: BaseChatModel, tools: list[BaseTool], checkpointer: Any = None):
    by_name = {t.name: t for t in tools}
    graph = StateGraph(BuildState)
    for name in NODES:
        graph.add_node(name, _node(name, model, by_name))
    graph.add_node('ask_for_help', ask_for_help)
    graph.add_edge(START, 'intake')
    graph.add_edge('intake', 'onboard_feed')
    graph.add_edge('onboard_feed', 'draft_translation')
    graph.add_edge('draft_translation', 'step_and_validate')
    graph.add_conditional_edges('step_and_validate', after_step, ['process_sample', 'draft_translation', 'ask_for_help'])
    graph.add_conditional_edges('process_sample', after_processing, ['plan_indexing', 'ask_for_help'])
    graph.add_edge('plan_indexing', 'step_indexing')
    graph.add_conditional_edges('step_indexing', after_step_indexing, ['index_sample', 'plan_indexing', 'ask_for_help'])
    graph.add_conditional_edges('index_sample', after_index_sample, ['document', 'plan_indexing', 'ask_for_help'])
    graph.add_conditional_edges('ask_for_help', after_help, ['draft_translation', 'process_sample', 'plan_indexing'])
    graph.add_edge('document', 'promote')
    graph.add_edge('promote', END)
    return graph.compile(checkpointer=checkpointer)
