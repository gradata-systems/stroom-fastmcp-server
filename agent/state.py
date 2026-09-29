"""Build state, and harvesting the facts routing needs from tool results."""
import json
from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage, ToolMessage

from agent.gating import parse

Mode = Literal['onboard', 'update_events_pipeline', 'update_indexing_pipeline', 'create_discovery_index',
               'evaluate_events_pipeline', 'fix_pipeline_issue', 'onboard_existing_feed']


class BuildState(TypedDict, total=False):
    mode: Mode
    request: str                      # what the user asked, including any sample and source docs
    build: str
    raw_stream_ids: list[int]
    events_stream_ids: list[int]
    translation_pipeline: str
    indexing_pipeline: str
    step_verdict: str                 # clean | review | blocking, from the last step_sample
    last_findings: list[dict]         # non-benign error groups, capped, carried into the next draft
    processing_gate: str              # pass | fail, from the last wait_for_processing
    searches_passed: bool
    promoted: bool
    issue_location: dict[str, Any]    # fix_pipeline_issue: locate_event's answer (raw stream, part, record, docs)
    fix: dict[str, Any]               # the last summarise_fix result, including the draft
    fix_attempt: int                  # draft_fix attempt that produced `fix`
    fix_choice: str                   # apply | manual
    backend: str                      # lucene | elasticsearch, from create_indexing_pipeline
    field_plan: dict[str, Any]        # the last draft_index_mapping plan
    proposed_template: dict[str, Any] # propose_index_template: name, index, dev_tools, link
    user_template: str                # the template as the user sent it back, if they changed it
    template_check: dict[str, Any]    # the last check_index_template result
    template_choice: str              # accept | changed | change_pipeline | change_template
    filter_ready: dict[str, Any]      # an Elasticsearch indexing filter created disabled: id, link
    survey: dict[str, Any]            # onboard_existing_feed: feed, shapes, where their examples are, how far back
    attempts: dict[str, int]
    notes: list[str]                  # short progress notes shown to the user
    last_node: str
    messages: list[BaseMessage]       # the current node's conversation


def harvest(messages: list[BaseMessage]) -> dict[str, Any]:
    """Pull the facts routing depends on out of a node's tool results."""
    update: dict[str, Any] = {}
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        data = parse(message.content)
        if not isinstance(data, dict):
            continue
        name = message.name
        if name == 'start_build':
            update['build'] = data.get('build')
        elif name == 'upload_sample' and data.get('stream_id'):
            update.setdefault('raw_stream_ids', []).append(data['stream_id'])
        elif name == 'survey_feed' and 'shapes' in data:
            update['survey'] = {'feed': data.get('feed'), 'oldest_stream_read': data.get('oldest_stream_read'),
                                'saturated': data.get('saturated'), 'new_shapes': data.get('new_shapes'),
                                'signatures': [s['signature'] for s in data.get('shapes') or []],
                                'shapes': [{k: s.get(k) for k in ('signature', 'count', 'example')}
                                           for s in data.get('shapes') or []][:30],
                                'locations': data.get('locations') or []}
        elif name == 'draft_index_mapping' and data.get('plan'):
            update['field_plan'] = data['plan']
        elif name == 'propose_index_template' and data.get('dev_tools'):
            update['proposed_template'] = {k: data.get(k) for k in ('template_name', 'index', 'cluster', 'dev_tools',
                                                                    'pipeline_link')}
            update['proposed_template']['self_check_notes'] = (data.get('self_check') or {}).get('notes')
        elif name == 'check_index_template' and 'compatible' in data:
            update['template_check'] = {k: data.get(k) for k in ('compatible', 'blocking', 'pipeline_changes', 'notes',
                                                                 'template_name', 'pipeline_link')}
        elif name in ('create_processor_filter', 'reprocess_streams') and data.get('enabled') is False:
            update['filter_ready'] = {k: data.get(k) for k in ('filter_id', 'pipeline_link', 'destination')}
        elif name == 'set_processor_filter_enabled' and data.get('enabled'):
            update['filter_ready'] = None
        elif name == 'create_pipeline' and data.get('uuid'):
            update['translation_pipeline'] = data['uuid']
        elif name in ('create_indexing_pipeline', 'copy_pipeline') and data.get('uuid'):
            update['indexing_pipeline' if name == 'create_indexing_pipeline' else 'translation_pipeline'] = data['uuid']
            if data.get('backend'):
                update['backend'] = data['backend']
        elif name in ('step_sample', 'step_records') and 'verdict' in data:
            update['step_verdict'] = data['verdict']
            update['last_findings'] = [
                {k: g.get(k) for k in ('class', 'severity', 'element', 'count', 'examples', 'records')}
                for g in data.get('groups', []) if g.get('class') != 'benign'][:20]
        elif name == 'wait_for_processing' and 'gate' in data:
            update['processing_gate'] = data['gate']
            events = [e for s in data.get('streams', []) for e in s.get('events', [])]
            if events:
                update['events_stream_ids'] = events
        elif name == 'run_test_searches' and 'passed' in data:
            update['searches_passed'] = data['passed']
        elif name == 'locate_event' and data.get('raw_stream'):
            update['issue_location'] = {k: data.get(k) for k in (
                'reported', 'raw_stream', 'feed', 'raw_parts', 'events_stream', 'location', 'pipeline',
                'translation_docs', 'same_as_stored')}
            update['translation_pipeline'] = (data.get('pipeline') or {}).get('uuid')
            update['raw_stream_ids'] = [data['raw_stream']]
        elif name == 'summarise_fix' and 'ready' in data:
            update['fix'] = data
        elif name == 'promote_build' and data.get('promoted'):
            update['promoted'] = True
    return update


def findings_text(state: BuildState) -> str:
    findings = state.get('last_findings') or []
    return json.dumps(findings, indent=1) if findings else 'none'
