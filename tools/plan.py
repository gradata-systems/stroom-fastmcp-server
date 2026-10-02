"""The onboarding plan, with its state read from the build.

A model that treats each request as one tool call stops when the tool returns. So the plan is not only in the
prompts: start_onboarding and start_build return it, build_status derives each step's state from what the build
holds (feed, sample streams, converter, XSLT with its mapping, pipeline, a clean step, Events, documentation,
index), and every write tool's result carries `next`, the first unfinished step and the tool that does it.
"""
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import MANAGED, build_tag, guard_from
from utils.params import ONE_OR_MORE
from utils.profile import profile, profile_many
from utils.stroom import gateway_from

Build = Annotated[str, Field(description="Build name, e.g. 'fortios-v1.0'.")]
# Which parser element reads which profiled format.
PARSER_FOR_FORMAT = {
    'json array': ('JSONParser', 'CombinedParser'), 'json lines': ('JSONParser', 'CombinedParser'),
    'xml': ('XMLParser', 'CombinedParser'), 'xml fragments': ('XMLFragmentParser', 'CombinedParser'),
    'delimited': ('DSParser', 'CombinedParser'), 'syslog rfc5424': ('DSParser', 'CombinedParser'),
    'syslog rfc3164': ('DSParser', 'CombinedParser'), 'key=value': ('DSParser', 'CombinedParser'),
    'unknown text': ('DSParser', 'CombinedParser'),
}
TEXT_FORMATS = {'delimited', 'syslog rfc5424', 'syslog rfc3164', 'key=value', 'unknown text'}

STAGE_1 = [
    ('feed', 'Create the feed in the build', 'create_feed'),
    ('samples', 'Upload every sample file as its own stream', 'upload_sample'),
    ('converter', 'Text converter for the format (Data Splitter from a spec, or the XML fragment wrapper)', 'build_data_splitter, save_text_converter'),
    ('translation', 'Translation XSLT from a mapping, saved with its mapping', 'build_translation_xslt, save_xslt mapping=...'),
    ('pipeline', 'Events pipeline as a child of the right template', 'find_pipeline_templates stage=translation, create_pipeline'),
    ('stepped', 'Every sample record stepped clean', 'step_sample over all sample streams (draft_code first)'),
    ('processed', 'Sample streams processed into Events', 'create_processor_filter, wait_for_processing'),
    ('validated', 'Events validated against the schema and the quality rules', 'check_events'),
    ('documented', 'Events pipeline documented, with its Field mapping generated', 'write_documentation stream_ids=...'),
]
STAGE_2 = [
    ('index', 'Index doc for the agreed backend, convention and name', 'get_field_conventions, draft_index_mapping, create_index_doc'),
    ('indexing_pipeline', 'Indexing pipeline from the plan (XSLT saved with index_plan)', 'save_xslt index_plan=..., create_indexing_pipeline'),
    ('indexed', 'Events indexed and found by the verification searches', 'create_processor_filter, wait_for_processing, verify_index'),
    ('index_documented', 'Indexing pipeline documented', 'write_documentation stream_ids=<Events streams>'),
]
FINISH = [('promoted', 'Promoted beside sibling sources, filters handed over disabled', 'build_status, promote_build')]


def checklist() -> list[dict[str, str]]:
    return [{'step': s, 'what': w, 'tools': t, 'stage': stage}
            for stage, steps in (('1 events', STAGE_1), ('2 indexing', STAGE_2), ('finish', FINISH)) for s, w, t in steps]


async def build_of(ctx: Context, ref: dict[str, Any]) -> str | None:
    """The build a managed document belongs to, from its tags."""
    try:
        tags = await guard_from(ctx).tags(ref)
    except Exception:
        return None
    marker = build_tag('x')[:-1]
    return next((t[len(marker):] for t in tags if t.startswith(marker)), None)


async def sample_streams(ctx: Context, build: str, docs: list[dict[str, Any]] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Raw streams on the build's feeds, by feed: {'Raw Events': [...], 'Raw Reference': [...]} entries hold meta."""
    stroom = gateway_from(ctx)
    docs = docs if docs is not None else await guard_from(ctx).folder_contents(build)
    found: dict[str, list[dict[str, Any]]] = {}
    for feed in (d for d in docs if d['type'] == 'Feed'):
        for stream_type in ('Raw Events', 'Raw Reference'):
            terms = [{'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': feed['name']},
                     {'type': 'term', 'field': 'Type', 'condition': 'EQUALS', 'value': stream_type}]
            rows = (await stroom.find_meta(terms, 20)).get('values') or []
            metas = [r['meta'] for r in rows if r['meta'].get('status') != 'DELETED']
            if metas:
                found.setdefault(stream_type, []).extend({**m, 'feed': feed['name']} for m in metas)
    return found


async def sample_format(ctx: Context, build: str, docs: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The profiled format of the build's newest raw sample stream (its head), or None without one."""
    from tools.sampling import read_head
    streams = await sample_streams(ctx, build, docs)
    metas = streams.get('Raw Events') or []
    if not metas:
        return None
    newest = max(metas, key=lambda m: m.get('createMs') or 0)
    text, _, _ = await read_head(gateway_from(ctx), newest['id'], 0, 20_000)
    profiled = profile(text)
    return {'stream_id': newest['id'], 'feed': newest['feed'], 'format': profiled['format'],
            'suggested_parser': profiled.get('suggested_parser'), 'needs_text_converter': profiled['format'] in TEXT_FORMATS
            or profiled['format'] == 'xml fragments'}


async def status(ctx: Context, build: str) -> dict[str, Any]:
    """Each plan step's state, read from the build."""
    from tools.builds import _build_docs, build_checks, kept_mapping
    from tools.stepping import stepped_clean
    from tools.templates import _shape
    from utils.mappingstore import read_mapping
    stroom = gateway_from(ctx)
    guard = guard_from(ctx)
    docs = await _build_docs(ctx, build)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for d in docs:
        by_type.setdefault(d['type'], []).append(d)
    streams = await sample_streams(ctx, build, docs)
    raw = streams.get('Raw Events') or []
    fmt = await sample_format(ctx, build, docs) if raw else None
    xslts = []
    for d in by_type.get('XSLT', []):
        doc = await stroom.get_doc('XSLT', d['uuid'])
        kept = read_mapping(doc.get('description'))
        xslts.append({'name': d['name'], 'uuid': d['uuid'], 'mapping': kept[0] if kept else None})
    pipelines = []
    for d in by_type.get('Pipeline', []):
        shape = await _shape(stroom, d['uuid'])
        pipelines.append({**d, 'stage': shape['stage'], 'parser': shape.get('parser'), 'stepped': await stepped_clean(ctx, d)})
    translation = [p for p in pipelines if p['stage'] == 'translation']
    indexing = [p for p in pipelines if p['stage'] == 'indexing']
    documented = {d['name'] for d in by_type.get('Documentation', [])}
    events = []
    for feed in by_type.get('Feed', []):
        rows = (await stroom.find_meta([{'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': feed['name']},
                                        {'type': 'term', 'field': 'Type', 'condition': 'EQUALS', 'value': 'Events'}], 50)).get('values') or []
        events += [r['meta']['id'] for r in rows if r['meta'].get('status') != 'DELETED']
    checks = await build_checks(ctx, docs)
    stale = [c for c in checks if 'Field mapping' in c or 'differs' in c]

    done: dict[str, bool | None] = {
        'feed': bool(by_type.get('Feed')),
        'samples': bool(raw),
        'converter': (bool(by_type.get('TextConverter')) if fmt and fmt['needs_text_converter'] else None),
        'translation': any(x['mapping'] == 'translation' for x in xslts),
        'pipeline': bool(translation),
        'stepped': any(p['stepped'] for p in translation),
        'processed': bool(events),
        'validated': None,   # not recorded: the model validates after processing
        'documented': bool(translation) and all(p['name'] in documented for p in translation) and not stale,
        'index': bool(by_type.get('Index') or by_type.get('ElasticIndex')),
        'indexing_pipeline': bool(indexing),
        'indexed': bool(indexing) and all(p['stepped'] for p in indexing),
        'index_documented': bool(indexing) and all(p['name'] in documented for p in indexing),
        'promoted': False,
    }
    steps = []
    for item in checklist():
        state = done.get(item['step'])
        steps.append({**item, 'state': 'done' if state else 'not needed' if state is None and item['step'] == 'converter'
                      else 'not recorded' if state is None else 'to do'})
    pending = [s for s in steps if s['state'] == 'to do']
    nxt = pending[0] if pending else None
    return {
        'build': build,
        'documents': docs,
        'sample': fmt,
        'feeds': [d['name'] for d in by_type.get('Feed', [])],
        'sample_streams': {t: [m['id'] for m in ms] for t, ms in streams.items()},
        'xslts': xslts,
        'pipelines': [{k: p[k] for k in ('name', 'uuid', 'stage', 'parser', 'stepped')} for p in pipelines],
        'events_streams': events[:20],
        'before_promotion': checks,
        'steps': steps,
        'next': {'step': nxt['step'], 'do': nxt['what'], 'tools': nxt['tools']} if nxt else
                {'step': 'promoted', 'do': 'Everything is in place: build_status, then promote_build with the user\'s approval',
                 'tools': 'promote_build'},
    }


async def build_status(ctx: Context, build: Build) -> dict[str, Any]:
    """
    Where an onboarding stands: every step of the plan (feed, sample streams, converter, translation with its
    mapping, pipeline, clean step, Events, validation, documentation, index, indexing pipeline, promotion) as
    done or to do, read from what the build holds, with the sample's format and what is next. Call it whenever
    unsure what remains; every write tool's result carries the same `next`.
    """
    return await status(ctx, build)


async def next_step(ctx: Context, build: str | None) -> dict[str, Any] | None:
    """The first unfinished step of the build's plan, or None when the build is unknown or unreadable."""
    if not build:
        return None
    try:
        return (await status(ctx, build))['next']
    except Exception:   # the plan is advice: never fail a write over it
        return None


async def with_next(ctx: Context, build: str | None, result: dict[str, Any]) -> dict[str, Any]:
    """The tool result with `next`, the plan's next step, added (and a reminder that the work is not done)."""
    if not isinstance(result, dict) or 'status' in result:   # a confirmation or approval round, not an outcome
        return result
    nxt = await next_step(ctx, build)
    if nxt:
        result['next'] = nxt
        if nxt['step'] != 'promoted':
            result['done'] = False
    return result


async def start_onboarding(
        ctx: Context,
        source_name: Annotated[str, Field(description="The source, e.g. 'FortiOS firewall' (names the build).")],
        samples: Annotated[dict[str, str], Field(description="Every sample file the user has, by file name.")],
        build: Annotated[str | None, Field(description="Build name; defaults to one made from the source name.")] = None,
        folders: Annotated[list[str], ONE_OR_MORE, Field(description="Folders the work will be promoted to, if known.")] = [],
) -> dict[str, Any]:
    """
    Start onboarding a source: profiles every sample file (format, fields, timestamp patterns, what differs
    between files, which parser and template to use, whether a text converter is needed), creates the build,
    and returns the plan with its first step and the standing instructions that apply. The work is not done
    until build_status shows every step done and promote_build has run; each tool's result says what is next.
    """
    from tools.instructions import applicable_instructions
    if not samples:
        raise ToolError("Give the sample files (samples by file name); ask the user for every file they have")
    name = build or ('onboard-' + ''.join(c if c.isalnum() else '-' for c in source_name.lower()).strip('-')[:40])
    folder = await guard_from(ctx).build_folder(name)
    profiled = profile_many(samples) if len(samples) > 1 else profile(next(iter(samples.values())))
    fmt = profiled['format']
    parser = PARSER_FOR_FORMAT.get(fmt, ('DSParser',))[0]
    template = {'JSONParser': 'Event Data (JSON)', 'XMLParser': 'Event Data (XML)',
                'XMLFragmentParser': "Event Data (XML) with replace_parser='XMLFragmentParser'"}.get(parser, 'Event Data (Text)')
    plan = checklist()
    return {
        'build': name, 'folder': folder['_path'], 'source': source_name,
        'profile': profiled,
        'parser': parser, 'template': f"{template} (confirm with find_pipeline_templates stage=translation)",
        'text_converter': ('needed: build_data_splitter from a spec, then save_text_converter' if fmt in TEXT_FORMATS else
                           'needed: the XML fragment wrapper (profile text_converter)' if fmt == 'xml fragments' else
                           'not needed: the template\'s parser reads this format'),
        'plan': plan,
        'next': {'step': 'feed', 'do': plan[0]['what'], 'tools': plan[0]['tools']},
        'done': False,
        'standing_instructions': await applicable_instructions(ctx, folders, []),
        'hint': ("Propose the feed name from sibling feeds and create_feed; upload each file as its own stream; then the "
                 "converter (if needed), build_translation_xslt with the sample, save_xslt with the mapping, the "
                 "pipeline, step_sample over all streams until clean, process, validate, write_documentation, index. "
                 "build_status shows what remains at any point."),
    }


# The plan step each core tool implements, put first in its description so a client that picks tools by
# similarity to the prompt pulls the whole onboarding chain for an "onboard these logs" request.
STEP_OF = {
    'start_onboarding': 'start', 'build_status': 'any step', 'start_build': 'start',
    'create_feed': 'feed', 'upload_sample': 'samples', 'profile_sample': 'start',
    'build_data_splitter': 'converter', 'save_text_converter': 'converter',
    'build_translation_xslt': 'translation', 'save_xslt': 'translation',
    'find_pipeline_templates': 'pipeline', 'describe_template': 'pipeline', 'create_pipeline': 'pipeline',
    'step_sample': 'stepped', 'step_pipeline': 'stepped', 'step_records': 'stepped',
    'create_processor_filter': 'processed', 'wait_for_processing': 'processed',
    'check_events': 'validated', 'write_documentation': 'documented',
    'get_field_conventions': 'index', 'draft_index_mapping': 'index', 'create_index_doc': 'index',
    'create_indexing_pipeline': 'indexing_pipeline', 'verify_index': 'indexed', 'promote_build': 'promoted',
}


def annotate_tools(modules) -> None:
    """Prefix each core tool's description with its onboarding plan step, e.g. 'Onboarding step 5 of 14, the
    events pipeline: ...'. Done once, before registration."""
    steps = [s['step'] for s in checklist()]
    for module in modules:
        for tool in module.ALL_TOOLS:
            step = STEP_OF.get(tool.__name__)
            doc = (tool.__doc__ or '').strip()
            if not step or doc.startswith('Onboarding'):
                continue
            if step in steps:
                label = f"Onboarding step {steps.index(step) + 1} of {len(steps)} ({step}): "
            else:
                label = "Onboarding, any step: " if step == 'any step' else "Onboarding start: "
            tool.__doc__ = label + doc


ALL_TOOLS = [start_onboarding, build_status]
