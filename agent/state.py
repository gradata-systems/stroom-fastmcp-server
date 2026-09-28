"""Build state, and harvesting the facts routing needs from tool results."""
import json
from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage, ToolMessage

from agent.gating import parse

Mode = Literal['onboard', 'update_events_pipeline', 'update_indexing_pipeline', 'create_discovery_index',
               'evaluate_events_pipeline']


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
        elif name == 'create_pipeline' and data.get('uuid'):
            update['translation_pipeline'] = data['uuid']
        elif name in ('create_indexing_pipeline', 'copy_pipeline') and data.get('uuid'):
            update['indexing_pipeline' if name == 'create_indexing_pipeline' else 'translation_pipeline'] = data['uuid']
        elif name == 'step_sample' and 'verdict' in data:
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
        elif name == 'promote_build' and data.get('promoted'):
            update['promoted'] = True
    return update


def findings_text(state: BuildState) -> str:
    findings = state.get('last_findings') or []
    return json.dumps(findings, indent=1) if findings else 'none'
