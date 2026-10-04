"""The evaluation set: nineteen samples, onboarded end to end, scored the same way whoever does the work.

    uv run python dev/eval/run_eval.py --reference [case ...]   # no model: each case's reference solution
    uv run python dev/eval/run_eval.py --request 06             # the request to give an agent for a case

Pass criterion: every case reaches indexed events with no hints, for the reference solutions and for an agent on
the default model (run_agent.py, with --repeat: each case passing in most of its runs). Lighter models are measured
against the same bar, not held to it.

--reference proves the cases and the scoring against the local Stroom stack (dev/stroom): each case's
reference converter and field mapping go through build_translation_xslt, stepping, processing, a Lucene index
and a verification search, the way an agent would, with no model involved.

To evaluate an agent, whatever runs it, give it each case's request (--request) with the server connected,
answer its confirmations as a user would, and give a case's hints (in order, counted) only when it asks for
help. The case's expected event types and paths are what its output is scored against.

Results go to dev/eval/results/<time>-reference.json with a summary table.
"""
import argparse
import asyncio
import json
import re
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
MAX_HINTS = 0
DOC_SKELETON = """## Purpose and data

Evaluation case.

## Processing

Child of the template.

## Output

See Field mapping.
"""


def load_cases(only: list[str] | None = None) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(CASES.glob('*.yaml')):
        case = yaml.safe_load(path.read_text(encoding='utf-8'))
        case['id'] = path.stem
        # A number names a case by its number (16 is 16_..., not 07_syslog3164_...); other text matches the name.
        if not only or any(path.stem.split('_', 1)[0] == o if o.isdigit() else o in path.stem for o in only):
            cases.append(case)
    return cases


def samples_of(case: dict[str, Any]) -> list[str]:
    """A case's sample files: `samples` (several), else the one `sample`."""
    return list(case.get('samples') or [case['sample']])


def sample_text(case: dict[str, Any]) -> str:
    """The case's sample files (and any reference data export), as shown to an agent."""
    samples = samples_of(case)
    shown = (f"Sample:\n{samples[0]}" if len(samples) == 1 else
             '\n\n'.join(f"Sample file {n}:\n{s}" for n, s in enumerate(samples, 1)))
    ref = case.get('reference', {}).get('reference_data')
    if ref:
        shown += f"\n\nUser directory export (reference data):\n{ref['sample']}"
    return shown


def request_text(case: dict[str, Any]) -> str:
    """What to ask an agent for this case."""
    return f"{case['request'].strip()}\n\nUse the stroom-flat field convention for the index.\n\n{sample_text(case)}"


# --- scoring ---
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


def _steps(path: str) -> str:
    """An expected path as an ElementPath; * is any one element, for choices the schema allows equally (a firewall
    decision under Network/Open, Permit or Deny)."""
    return '/'.join(f'{{{EVT}}}{p}' for p in path.split('/'))


def has_path(event: etree._Element, path: str) -> bool:
    """Whether the event has the path, or one of its alternatives (a|b), with a value or children."""
    return any((node.text or '').strip() or len(node) for alt in path.split('|') for node in event.findall(_steps(alt)))


def path_values(events: list[etree._Element], path: str) -> set[str]:
    return {(node.text or '').strip() for e in events for alt in path.split('|') for node in e.findall(_steps(alt))}


def score_events(score: Score, case: dict[str, Any], records: list[str], validity: list[bool]) -> None:
    expected = case['expected']
    events, types = event_facts(records)
    score.events, score.valid_events, score.event_types = len(events), sum(validity), sorted(types)
    # An expected type may name alternatives the source fits equally (Authenticate|Authorise for a badge at a door).
    score.missing_types = sorted(t for t in expected['event_types'] if not set(t.split('|')) & types)
    score.missing_paths = sorted({p for p in expected['paths'] for e in events if not has_path(e, p)})
    if len(events) != expected['records']:
        score.problems.append(f"{len(events)} events, expected {expected['records']}")
    if score.valid_events != len(validity) or not validity:
        score.problems.append(f"{len(validity) - score.valid_events} of {len(validity)} Events records invalid")
    # Values some event must hold exactly, e.g. a free-text message carried whole, or a time from the right field.
    missing_values = {path: [v for v in values if v not in path_values(events, path)]
                      for path, values in (expected.get('values') or {}).items()}
    for path, values in missing_values.items():
        if values:
            score.problems.append(f"no event has {path} = {values}")
    score.stage1 = (len(events) == expected['records'] and bool(validity) and all(validity)
                    and not score.missing_types and not score.missing_paths and not any(missing_values.values()))


Call = Callable[..., Awaitable[dict[str, Any]]]


async def check_output(call: Call, score: Score, case: dict[str, Any], events_stream_ids: list[int]) -> None:
    records, validity = [], []
    for stream_id in events_stream_ids:
        read = await call('read_stream', stream_id=stream_id, first_record=0, record_count=100)
        for record in read.get('records') or []:
            records.append(record)
            validity.append(bool((await call('validate_events', events_xml=record)).get('valid')))
    score_events(score, case, records, validity)


def local_ctx() -> SimpleNamespace:
    """A tool context on the local stack (dev/stroom), calling the tools directly as the admin key; close
    ctx.lifespan_context['stroom'] when done."""
    import e2e_translation as e2e
    from config import Settings
    from security.policy import AccessPolicy
    from utils.consent import ConsentStore
    from utils.stroom import StroomGateway
    from utils.triage import ErrorRules

    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=e2e.VERSION)
    return SimpleNamespace(lifespan_context={
        'stroom': StroomGateway(settings), 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})


# --- reference mode: the case's own solution through the tools, no model ---
async def run_reference(case: dict[str, Any], stamp: str) -> Score:
    import e2e_translation as e2e
    from tools import (feeds, generation, indexing, pipeline_writes, processing_writes, stepping, templates,
                       translation, validation, streams)
    from tools.pipeline_writes import PipelineReference, PropertyValue
    from utils.dsgen import SplitterSpec
    from utils.refgen import ReferenceMapping
    from utils.fieldplan import FieldPlan
    from utils.xsltgen import TranslationMapping

    ctx = local_ctx()
    stroom = ctx.lifespan_context['stroom']
    tools = {'read_stream': streams.read_stream, 'validate_events': validation.validate_events}

    async def call(name: str, **kwargs):
        return await tools[name](ctx, **kwargs)
    score, started = Score(case['id'], 'reference'), time.monotonic()
    tag = case['id'].split('_', 1)[0]
    build, feed = f'eval-{tag}-{stamp}', f'EVAL-{tag}-{stamp}'
    try:
        reference = case['reference']
        samples = samples_of(case)
        splitter = SplitterSpec.model_validate(reference['splitter']) if reference.get('splitter') else None
        # As an agent should: the samples' text is sent once, to upload_sample; the mapping is then checked against
        # every sample stream, read by the server, and the XSLT saved with it in the build, so its code never comes back.
        await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
        raws = [(await feeds.upload_sample(ctx, feed, sample))['stream_id'] for sample in samples]
        raw = raws[0]
        generated = await e2e.agreed(generation.build_translation_xslt, ctx=ctx,
                                    mapping=TranslationMapping.model_validate(reference['mapping']),
                                    stream_ids=raws, splitter=splitter, build=build, name=f'{feed}-Events')
        if not generated['ok']:
            raise RuntimeError(f"mapping problems: {generated['problems']}")
        if generated.get('sample_check', {}).get('warnings'):
            score.notes.append(f"sample check: {generated['sample_check']['warnings']}")
        template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                        if c['name'] == case['template'])
        references = []
        ref_data = reference.get('reference_data')
        if ref_data:
            # The user directory: a Raw Reference feed, a Reference Data pipeline, processed to Reference streams.
            ref_feed = f'{feed}-USERS'
            await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=ref_feed, stream_type='Raw Reference')
            # Reference data applies from its effective time: before the events' streams, or lookups find nothing.
            ref_raw = (await feeds.upload_sample(ctx, ref_feed, ref_data['sample'], stream_type='Raw Reference',
                                                 effective_time='2000-01-01T00:00:00.000Z'))['stream_id']
            ref_xslt = await generation.build_reference_xslt(ctx, ReferenceMapping.model_validate(ref_data['mapping']))
            if not ref_xslt['ok']:
                raise RuntimeError(f"reference mapping problems: {ref_xslt['problems']}")
            ref_template = next(c for c in (await templates.find_pipeline_templates(ctx, 'reference'))['candidates']
                                if c['name'] == 'Reference Data')
            rprops = []
            rcode = e2e.CSV_SPLITTER if ref_data.get('converter') == 'csv_header' else ref_data.get('converter')
            if rcode:
                rtc = await translation.create_text_converter(ctx, build, ref_feed, 'DATA_SPLITTER', rcode)
                rprops.append(PropertyValue(element='combinedParser', name='textConverter', doc_uuid=rtc['uuid'], doc_type='TextConverter'))
            rx = await translation.create_xslt(ctx, build, f'{ref_feed}-Reference', ref_xslt['xslt'])
            rprops.append(PropertyValue(element='translationFilter', name='xslt', doc_uuid=rx['uuid'], doc_type='XSLT'))
            rpipe = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{ref_feed}-Reference',
                                    template_uuid=ref_template['uuid'], set_properties=rprops)
            rstep = await stepping.step_sample(ctx, rpipe['uuid'], [ref_raw])
            if rstep['verdict'] != 'clean':
                score.problems.append(f"reference pipeline stepping: {rstep['verdict']}")
            await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=rpipe['uuid'], stream_ids=[ref_raw])
            rgate = await processing_writes.wait_for_processing(ctx, rpipe['uuid'], [ref_raw], output_type='Reference')
            if rgate['gate'] != 'pass':
                score.problems.append(f"reference data: {rgate['problems']}")
            references = [PipelineReference(feed=ref_feed)]
        props = []
        converter, replace_parser = reference.get('converter'), reference.get('replace_parser')
        if converter:
            code = e2e.CSV_SPLITTER if converter == 'csv_header' else converter
            tc = await translation.create_text_converter(ctx, build, feed, reference.get('converter_type', 'DATA_SPLITTER'), code)
            parser = pipeline_writes.element_id(replace_parser) if replace_parser else 'dsParser'
            props.append(PropertyValue(element=parser, name='textConverter', doc_uuid=tc['uuid'], doc_type='TextConverter'))
        x = generated['saved']
        props.append(PropertyValue(element='translationFilter', name='xslt', doc_uuid=x['uuid'], doc_type='XSLT'))
        if case['template'] == 'Event Data (JSON)':
            # JSON lines need the parser's root map round the top-level objects; an array reads better without it.
            lines = reference['mapping'].get('json_layout') == 'lines'
            props.append(PropertyValue(element='jsonParser', name='addRootObject', value=lines))
        pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                   template_uuid=template['uuid'], set_properties=props, replace_parser=replace_parser,
                                   references=references)
        sample = await stepping.step_sample(ctx, pipeline['uuid'], raws)
        if sample['verdict'] == 'blocking':
            score.problems.append(f"stepping blocking: {[(g['element'], g.get('examples')) for g in sample['groups']][:3]}")
        await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'], stream_ids=raws)
        gate = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], raws)
        events = [e for s in gate['streams'] for e in s['events']]
        await check_output(call, score, case, events)
        # The documentation: its Field mapping section is generated from the kept mapping over the sample streams.
        from tools import builds
        written = await builds.write_documentation(ctx, build, pipeline['uuid'], DOC_SKELETON, 'Created', stream_ids=raws)
        section = written.get('field_mapping') or ''
        if '### Event types' not in section or 'not written by a rule' in section:
            score.problems.append(f"documentation: field mapping section incomplete ({section[:120]!r})")
        if any(p for p in await builds.build_checks(ctx, await builds._build_docs(ctx, build)) if 'Field mapping' in p or 'differs' in p):
            score.problems.append('documentation: build checks report a stale field mapping or a hand-edited XSLT')
        if not score.stage1:
            return score

        draft = await indexing.draft_index_mapping(ctx, 'lucene', f'{feed}-INDEX', 'stroom-flat', events)
        plan = FieldPlan.model_validate(draft['plan'])
        index = await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=build, backend='lucene', name=f'{feed}-INDEX',
                                time_field=plan.time_field)
        await indexing.set_index_fields(ctx, index['uuid'], plan)
        ixslt = await translation.save_xslt(ctx, build, f'{feed}-INDEX-XSLT', index_plan=plan)   # generated from the plan
        lucene = next(c for c in (await templates.find_pipeline_templates(ctx, 'indexing'))['candidates']
                      if c['backend'] == 'lucene')
        ipipe = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=build, name=f'{feed}-INDEX - Indexing',
                                template_uuid=lucene['uuid'], xslt_uuid=ixslt['uuid'], index_uuid=index['uuid'])
        istep = await stepping.step_sample(ctx, ipipe['uuid'], events)
        if istep['verdict'] != 'clean':
            score.problems.append(f"indexing pipeline stepping: {istep['verdict']}")
        await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=ipipe['uuid'], stream_ids=events,
                        source_pipeline_uuid=pipeline['uuid'])
        igate = await processing_writes.wait_for_processing(ctx, ipipe['uuid'], events, expect_events=False)
        dash = await indexing.create_verification_dashboard(ctx, build, f'{feed}-VERIFY', index['uuid'], 'lucene',
                                                            ['StreamId', 'EventId', plan.time_field])
        searched = await indexing.run_test_searches(ctx, dash['uuid'], events, score.events)
        score.indexed = igate['gate'] == 'pass' and searched['passed']
        idoc = await builds.write_documentation(ctx, build, ipipe['uuid'], DOC_SKELETON, 'Created', stream_ids=events)
        index_section = idoc.get('field_mapping') or ''
        if '| Index field |' not in index_section:
            score.problems.append('documentation: no index field mapping generated')
        elif not re.search(r'Sample values are what the [1-9]\d* documents', index_section):
            score.problems.append('documentation: the index field mapping has no sampled values')
        if not score.indexed:
            score.problems.append(f"indexing: gate {igate['gate']}, searches {[c for c in searched['checks'] if not c['pass']]}")
    except Exception as e:  # a case failing must not stop the evaluation
        score.problems.append(f"{type(e).__name__}: {e}")
    finally:
        score.seconds = round(time.monotonic() - started, 1)
        score.passed = score.stage1 and score.indexed and score.hints <= MAX_HINTS
        await stroom.close()
    return score


def print_score(s: Score) -> None:
    print(f"  {'PASS' if s.passed else 'FAIL'} stage1={s.stage1} indexed={s.indexed} hints={s.hints} "
          f"events={s.valid_events}/{s.events} types={s.event_types} {s.seconds}s")
    for p in s.problems + ([f"missing types {s.missing_types}"] if s.missing_types else []) + \
            ([f"missing paths {s.missing_paths}"] if s.missing_paths else []):
        print(f"    - {p}")


def criterion(passed: int, scored: int) -> str:
    """The exit criterion, given how many cases passed out of how many were scored: every case, no hints."""
    total = len(load_cases())
    text = f"the exit criterion (all {total} cases, no hints)"
    if scored < total:
        return f"a partial run, not scored against {text}"
    return f"meets {text}" if passed >= total else f"does not meet {text}"


def summary(scores: list[Score]) -> str:
    rows = ['| Case | Result | Stage 1 | Indexed | Hints | Valid events | Seconds |', '| --- | --- | --- | --- | --- | --- | --- |']
    for s in scores:
        rows.append(f"| {s.case} | {'pass' if s.passed else 'fail'} | {s.stage1} | {s.indexed} | {s.hints} | "
                    f"{s.valid_events}/{s.events} | {s.seconds} |")
    passed = sum(s.passed for s in scores)
    rows.append(f"\n{passed} of {len(scores)} passed; {criterion(passed, len(scores))}.")
    return '\n'.join(rows)


async def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--reference', action='store_true', help="Run each case's reference solution.")
    mode.add_argument('--request', action='store_true', help="Print the request to give an agent for each case.")
    parser.add_argument('cases', nargs='*', help="Case ids or parts of them, e.g. 06 json")
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if args.request:
        for case in cases:
            print(f"### {case['id']}\n\n{request_text(case)}\n")
        return
    stamp = time.strftime('%H%M%S')
    scores = []
    for case in cases:
        print(f"### {case['id']} (reference)")
        scores.append(await run_reference(case, stamp))
        print_score(scores[-1])
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}-reference.json"
    out.write_text(json.dumps([asdict(s) for s in scores], indent=1), encoding='utf-8')
    print('\n' + summary(scores) + f"\n\nResults: {out.relative_to(ROOT)}")


if __name__ == '__main__':
    asyncio.run(main())
