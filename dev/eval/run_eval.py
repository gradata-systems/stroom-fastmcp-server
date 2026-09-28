"""The Phase 4 evaluation: 10 samples, onboarded end to end, scored the same way whoever does the work.

    uv run python dev/eval/run_eval.py --reference [case ...]      # no model: each case's reference solution
    uv run --extra agent python dev/eval/run_eval.py --agent [case ...]

Pass criterion (docs/DESIGN.md, Delivery phases): at least 8 of 10 samples reach indexed events with at most
one human hint each.

--reference proves the cases and the scoring against the local Stroom stack (dev/stroom): each case's
reference converter and field mapping go through build_translation_xslt, stepping, processing, a Lucene index
and a verification search, exactly as the agent would, with no model involved.

--agent runs the LangGraph agent against a Stroom MCP server (AGENT_MCP_URL, default the local dev server on
http://127.0.0.1:8765/mcp started with STROOM_MCP_DEV_NO_AUTH=true and STROOM_MCP_DEFAULT_CONVENTION=stroom-flat)
with the model in AGENT_MODEL (and AGENT_MODEL_BASE_URL / OPENAI_API_KEY for an OpenAI-compatible server such
as vLLM serving Gemma). A scripted user answers the interrupts: yes to every confirmation and approval, accepts
the proposed template, enables the filter, and answers a request for help with the case's next hint (counted)
or 'no hint'.

Results go to dev/eval/results/<time>-<mode>.json with a summary table.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import yaml
from lxml import etree

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

CASES = Path(__file__).parent / 'cases'
RESULTS = Path(__file__).parent / 'results'
EVT = 'event-logging:3'
DETAIL_META = {'TypeId', 'Description', 'Classification', 'Purpose'}
PASS_MIN, MAX_HINTS = 8, 1


def load_cases(only: list[str] | None = None) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(CASES.glob('*.yaml')):
        case = yaml.safe_load(path.read_text(encoding='utf-8'))
        case['id'] = path.stem
        if not only or any(o in path.stem for o in only):
            cases.append(case)
    return cases


def agent_request(case: dict[str, Any]) -> str:
    return (f"{case['request'].strip()}\n\nUse the stroom-flat field convention for the index.\n\n"
            f"Sample:\n{case['sample']}")


# --- scoring, the same for both modes ---
@dataclass
class Score:
    case: str
    mode: str
    passed: bool = False
    stage1: bool = False
    indexed: bool = False
    hints: int = 0
    events: int = 0
    valid_events: int = 0
    event_types: list[str] = field(default_factory=list)
    missing_types: list[str] = field(default_factory=list)
    missing_paths: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


def event_facts(records: list[str]) -> tuple[list[etree._Element], set[str]]:
    events, types = [], set()
    for xml in records:
        try:
            root = etree.fromstring(xml.encode('utf-8'))
        except etree.XMLSyntaxError:
            continue
        for event in root.iter(f'{{{EVT}}}Event'):
            events.append(event)
            detail = event.find(f'{{{EVT}}}EventDetail')
            for child in detail if detail is not None else []:
                name = etree.QName(child).localname
                if isinstance(child.tag, str) and name not in DETAIL_META:
                    types.add(name)
    return events, types


def has_path(event: etree._Element, path: str) -> bool:
    node = event.find('/'.join(f'{{{EVT}}}{p}' for p in path.split('/')))
    return node is not None and bool((node.text or '').strip() or len(node))


def score_events(score: Score, case: dict[str, Any], records: list[str], validity: list[bool]) -> None:
    expected = case['expected']
    events, types = event_facts(records)
    score.events, score.valid_events, score.event_types = len(events), sum(validity), sorted(types)
    score.missing_types = sorted(set(expected['event_types']) - types)
    score.missing_paths = sorted({p for p in expected['paths'] for e in events if not has_path(e, p)})
    if len(events) != expected['records']:
        score.problems.append(f"{len(events)} events, expected {expected['records']}")
    if score.valid_events != len(validity) or not validity:
        score.problems.append(f"{len(validity) - score.valid_events} of {len(validity)} Events records invalid")
    score.stage1 = (len(events) == expected['records'] and bool(validity) and all(validity)
                    and not score.missing_types and not score.missing_paths)


Call = Callable[..., Awaitable[dict[str, Any]]]


async def check_output(call: Call, score: Score, case: dict[str, Any], events_stream_ids: list[int]) -> None:
    records, validity = [], []
    for stream_id in events_stream_ids:
        read = await call('read_stream', stream_id=stream_id, first_record=0, record_count=100)
        for record in read.get('records') or []:
            records.append(record)
            validity.append(bool((await call('validate_events', events_xml=record)).get('valid')))
    score_events(score, case, records, validity)


# --- the scripted user ---
def respond(payload: dict[str, Any], case: dict[str, Any], score: Score) -> dict[str, Any]:
    kind = payload.get('kind')
    if kind == 'help':
        hints = case.get('hints') or []
        if score.hints < len(hints):
            score.hints += 1
            return {'note': hints[score.hints - 1]}
        score.hints += 1
        return {'note': 'No hint: use the sample, the guides and the tools to work it out.'}
    if kind == 'enable':
        return {'approved': True, 'note': 'enable it for me'}
    return {'approved': True}


# --- reference mode: the case's own solution through the tools, no model ---
async def run_reference(case: dict[str, Any], stamp: str) -> Score:
    import e2e_phase2 as p2
    from config import Settings
    from security.policy import AccessPolicy
    from tools import (feeds, generation, indexing, pipeline_writes, processing_writes, stepping, templates,
                       translation, validation, streams)
    from tools.pipeline_writes import PropertyValue
    from utils.consent import ConsentStore
    from utils.fieldplan import FieldPlan
    from utils.stroom import StroomGateway
    from utils.triage import ErrorRules
    from utils.xsltgen import TranslationMapping

    local = p2.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=p2.VERSION)
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'), 'elastic': None,
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    tools = {'read_stream': streams.read_stream, 'validate_events': validation.validate_events}

    async def call(name: str, **kwargs):
        return await tools[name](ctx, **kwargs)
    score, started = Score(case['id'], 'reference'), time.monotonic()
    tag = case['id'].split('_', 1)[0]
    build, feed = f'eval-{tag}-{stamp}', f'EVAL-{tag}-{stamp}'
    try:
        reference = case['reference']
        generated = await generation.build_translation_xslt(ctx, TranslationMapping.model_validate(reference['mapping']))
        if not generated['ok']:
            raise RuntimeError(f"mapping problems: {generated['problems']}")
        await p2.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
        raw = (await feeds.upload_sample(ctx, feed, case['sample']))['stream_id']
        template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                        if c['name'] == case['template'])
        props = []
        converter = reference.get('converter')
        if converter:
            code = p2.CSV_SPLITTER if converter == 'csv_header' else converter
            tc = await translation.create_text_converter(ctx, build, feed, 'DATA_SPLITTER', code)
            props.append(PropertyValue(element='dsParser', name='textConverter', doc_uuid=tc['uuid'], doc_type='TextConverter'))
        x = await translation.create_xslt(ctx, build, f'{feed}-Events', generated['xslt'])
        props.append(PropertyValue(element='translationFilter', name='xslt', doc_uuid=x['uuid'], doc_type='XSLT'))
        if case['template'] == 'Event Data (JSON)':
            props.append(PropertyValue(element='jsonParser', name='addRootObject', value=False))
        pipeline = await p2.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                   template_uuid=template['uuid'], properties=props)
        sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
        if sample['verdict'] == 'blocking':
            score.problems.append(f"stepping blocking: {[(g['element'], g.get('examples')) for g in sample['groups']][:3]}")
        await p2.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'], stream_ids=[raw])
        gate = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [raw])
        events = [e for s in gate['streams'] for e in s['events']]
        await check_output(call, score, case, events)
        if not score.stage1:
            return score

        draft = await indexing.draft_index_mapping(ctx, 'lucene', f'{feed}-INDEX', 'stroom-flat', events)
        plan = FieldPlan.model_validate(draft['plan'])
        index = await p2.agreed(indexing.create_index_doc, ctx=ctx, build=build, backend='lucene', name=f'{feed}-INDEX',
                                time_field=plan.time_field)
        await indexing.set_index_fields(ctx, index['uuid'], plan)
        ixslt = await translation.create_xslt(ctx, build, f'{feed}-INDEX-XSLT', draft['xslt'])
        lucene = next(c for c in (await templates.find_pipeline_templates(ctx, 'indexing'))['candidates']
                      if c['backend'] == 'lucene')
        ipipe = await p2.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=build, name=f'{feed}-INDEX - Indexing',
                                template_uuid=lucene['uuid'], xslt_uuid=ixslt['uuid'], index_uuid=index['uuid'])
        await p2.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=ipipe['uuid'], stream_ids=events,
                        source_pipeline_uuid=pipeline['uuid'])
        igate = await processing_writes.wait_for_processing(ctx, ipipe['uuid'], events, expect_events=False)
        dash = await indexing.create_verification_dashboard(ctx, build, f'{feed}-VERIFY', index['uuid'], 'lucene',
                                                            ['StreamId', 'EventId', plan.time_field])
        searched = await indexing.run_test_searches(ctx, dash['uuid'], events, score.events)
        score.indexed = igate['gate'] == 'pass' and searched['passed']
        if not score.indexed:
            score.problems.append(f"indexing: gate {igate['gate']}, searches {[c for c in searched['checks'] if not c['pass']]}")
    except Exception as e:  # a case failing must not stop the evaluation
        score.problems.append(f"{type(e).__name__}: {e}")
    finally:
        score.seconds = round(time.monotonic() - started, 1)
        score.passed = score.stage1 and score.indexed and score.hints <= MAX_HINTS
        await stroom.close()
    return score


# --- agent mode ---
async def run_agent(case: dict[str, Any], client, tools, model, timeout: float) -> Score:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command
    from agent.gating import parse
    from agent.graph import build_graph

    by_name = {t.name: t for t in tools}

    async def call(name: str, **kwargs):
        return parse(await by_name[name].ainvoke(kwargs))
    score, started = Score(case['id'], 'agent'), time.monotonic()
    graph = build_graph(model, tools, checkpointer=MemorySaver())
    config = {'configurable': {'thread_id': case['id']}, 'recursion_limit': 250}
    command: Any = {'mode': 'onboard', 'request': agent_request(case)}
    state: dict[str, Any] = {}
    try:
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(f"no result within {timeout:.0f}s")
            state = await asyncio.wait_for(graph.ainvoke(command, config), remaining)
            pending = state.get('__interrupt__')
            if not pending:
                break
            payload = pending[0].value
            score.notes.append(f"interrupt {payload.get('kind')}: {str(payload.get('summary'))[:120]}")
            command = Command(resume=respond(payload, case, score))
        events = state.get('events_stream_ids') or []
        if events:
            await check_output(call, score, case, events)
        score.indexed = bool(state.get('searches_passed')) and state.get('processing_gate') == 'pass'
    except Exception as e:
        score.problems.append(f"{type(e).__name__}: {e}")
    finally:
        score.seconds = round(time.monotonic() - started, 1)
        score.passed = score.stage1 and score.indexed and score.hints <= MAX_HINTS
        score.notes += [n[:200] for n in (state.get('notes') or [])][-12:]
    return score


async def agent_session(cases: list[dict[str, Any]], timeout: float) -> list[Score]:
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth
    from langchain.chat_models import init_chat_model
    from agent.mcp_tools import load_tools

    url = os.environ.get('AGENT_MCP_URL', 'http://127.0.0.1:8765/mcp')
    auth = BearerAuth(os.environ['AGENT_BEARER']) if os.environ.get('AGENT_BEARER') else None
    model = init_chat_model(os.environ['AGENT_MODEL'], base_url=os.environ.get('AGENT_MODEL_BASE_URL'))
    scores = []
    async with Client(url, auth=auth) as client:
        tools = await load_tools(client)
        for case in cases:
            print(f"### {case['id']} (agent)")
            scores.append(await run_agent(case, client, tools, model, timeout))
            print_score(scores[-1])
    return scores


def print_score(s: Score) -> None:
    print(f"  {'PASS' if s.passed else 'FAIL'} stage1={s.stage1} indexed={s.indexed} hints={s.hints} "
          f"events={s.valid_events}/{s.events} types={s.event_types} {s.seconds}s")
    for p in s.problems + ([f"missing types {s.missing_types}"] if s.missing_types else []) + \
            ([f"missing paths {s.missing_paths}"] if s.missing_paths else []):
        print(f"    - {p}")


def summary(scores: list[Score]) -> str:
    rows = ['| Case | Result | Stage 1 | Indexed | Hints | Valid events | Seconds |', '| --- | --- | --- | --- | --- | --- | --- |']
    for s in scores:
        rows.append(f"| {s.case} | {'pass' if s.passed else 'fail'} | {s.stage1} | {s.indexed} | {s.hints} | "
                    f"{s.valid_events}/{s.events} | {s.seconds} |")
    passed = sum(s.passed for s in scores)
    criterion = f"the exit criterion ({PASS_MIN} of 10, at most {MAX_HINTS} hint each)"
    verdict = (f"meets {criterion}" if passed >= PASS_MIN and len(scores) >= 10 else
               f"does not meet {criterion}" if len(scores) >= 10 else f"a partial run, not scored against {criterion}")
    rows.append(f"\n{passed} of {len(scores)} passed; {verdict}.")
    return '\n'.join(rows)


async def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--reference', action='store_true')
    mode.add_argument('--agent', action='store_true')
    parser.add_argument('cases', nargs='*', help="Case ids or parts of them, e.g. 06 json")
    parser.add_argument('--timeout', type=float, default=1800, help="Seconds per case (agent mode).")
    args = parser.parse_args()
    cases = load_cases(args.cases)
    stamp = time.strftime('%H%M%S')
    if args.reference:
        scores = []
        for case in cases:
            print(f"### {case['id']} (reference)")
            scores.append(await run_reference(case, stamp))
            print_score(scores[-1])
    else:
        scores = await agent_session(cases, args.timeout)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{'reference' if args.reference else 'agent'}.json"
    out.write_text(json.dumps([asdict(s) for s in scores], indent=1), encoding='utf-8')
    print('\n' + summary(scores) + f"\n\nResults: {out.relative_to(ROOT)}")


if __name__ == '__main__':
    asyncio.run(main())
