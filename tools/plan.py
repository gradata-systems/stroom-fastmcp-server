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
from tools.streams import SampleStreams, own_streams, read_sample_streams
from utils.params import ONE_OR_MORE
from utils.profile import profile, profile_many
from utils.samples import SampleTexts, as_named_samples
from utils.stroom import gateway_from

Build = Annotated[str, Field(description="Build name, e.g. 'acme-door-v1.0'.")]
# Which parser element reads which profiled format.
PARSER_FOR_FORMAT = {
    'json array': ('JSONParser', 'CombinedParser'), 'json lines': ('JSONParser', 'CombinedParser'),
    'xml': ('XMLParser', 'CombinedParser'), 'xml fragments': ('XMLFragmentParser', 'CombinedParser'),
    'delimited': ('DSParser', 'CombinedParser'), 'syslog rfc5424': ('DSParser', 'CombinedParser'),
    'syslog rfc3164': ('DSParser', 'CombinedParser'), 'key=value': ('DSParser', 'CombinedParser'),
    'unknown text': ('DSParser', 'CombinedParser'), 'cef': ('DSParser', 'CombinedParser'),
}
TEXT_FORMATS = {'delimited', 'syslog rfc5424', 'syslog rfc3164', 'key=value', 'unknown text', 'cef'}


async def templates_reading(ctx: Context, fmt: str) -> str:
    """The environment's translation templates whose parser reads the format, by name as they are there: no
    template is assumed to exist, or to be called anything (seen: guidance naming 'Event Data (XML)' wherever)."""
    from tools.templates import find_pipeline_templates
    readers = PARSER_FOR_FORMAT.get(fmt, ('DSParser', 'CombinedParser'))
    try:
        candidates = (await find_pipeline_templates(ctx, 'translation'))['candidates']
    except Exception:
        candidates = []
    fitting = [c['name'] for c in candidates if c.get('parser') in readers]
    if fitting:
        return f"{', '.join(fitting[:3])} (its parser, {readers[0]}, reads {fmt}; find_pipeline_templates stage=translation)"
    if fmt == 'xml fragments':
        xml = [c['name'] for c in candidates if c.get('parser') == 'XMLParser']
        if xml:
            return f"{xml[0]} with replace_parser='XMLFragmentParser' (no template here parses XML fragments)"
    return (f"none found that reads {fmt} (a {readers[0]}): ask the user which pipeline to base it on "
            f"(find_pipeline_templates stage=translation lists what there is)")

STAGE_1 = [
    ('feed', 'Create the feed in the build', 'create_feed'),
    ('samples', 'Upload every sample file as its own stream', 'upload_sample files=[their paths] (sample= only for text pasted into the chat)'),
    ('converter', 'Text converter for the format: build_data_splitter infers the Data Splitter from the sample text (or the XML fragment wrapper from profile_sample)', 'build_data_splitter stream_ids=<the sample streams>, save_text_converter'),
    ('translation', 'Translation XSLT from a mapping: draft it from the sample, decide the action elements, generate and save it with the mapping', 'draft_translation_mapping, build_translation_xslt build=... name=... (uuid=... to replace)'),
    ('pipeline', 'Events pipeline as a child of the right template, with its text converter and XSLT set (create_pipeline fills them from the build; update_pipeline sets a missing one)', 'find_pipeline_templates stage=translation, create_pipeline, update_pipeline'),
    ('stepped', 'Every sample record stepped clean', 'step_sample over all sample streams; fix the mapping and build_translation_xslt uuid=... in between'),
    ('processed', 'Sample streams processed into Events', 'create_processor_filter, wait_for_processing'),
    ('validated', 'Events validated against the schema and the quality rules', 'check_events'),
    ('documented', 'Events pipeline documented, with its Field mapping generated', 'write_documentation stream_ids=...'),
]
STAGE_2 = [
    ('index', 'Index doc for the agreed backend, convention and name', 'get_field_conventions, draft_index_mapping, create_index_doc'),
    ('indexing_pipeline', 'Indexing pipeline from the plan (XSLT saved with index_plan)', 'save_xslt index_plan=... (no code), create_indexing_pipeline'),
    ('index_template', "Elasticsearch: the index template agreed with the user, built from the example index template they pasted (or confirmed without one), then committed to the cluster by them", 'propose_index_template example_template=<theirs>, check_index_template'),
    ('indexed', "Sample Events indexed and found by verify_index's searches (stepping clean is not indexing)", 'create_processor_filter, wait_for_processing, verify_index'),
    ('index_documented', 'Indexing pipeline documented', 'write_documentation stream_ids=<Events streams>'),
]
FINISH = [('promoted', 'Promoted beside sibling sources, filters handed over disabled', 'build_status, promote_build')]


def checklist() -> list[dict[str, str]]:
    return [{'step': s, 'what': w, 'tools': t, 'stage': stage}
            for stage, steps in (('1 events', STAGE_1), ('2 indexing', STAGE_2), ('finish', FINISH)) for s, w, t in steps]


def _user(ctx: Context) -> str:
    try:
        from fastmcp.server.dependencies import get_access_token
        token = get_access_token()
        return (token.claims or {}).get('preferred_username') or (token.claims or {}).get('sub') or '-'
    except Exception:
        return '-'


def remember_build(ctx: Context, build: str | None) -> None:
    """The build the user is working on, so tools called without `build` fall back to it (this replica's memory;
    a call that lands elsewhere is told to name the build)."""
    if build:
        try:
            ctx.lifespan_context.setdefault('current_build', {})[_user(ctx)] = build
        except Exception:
            pass


def resolve_build(ctx: Context, build: str | None, tool: str) -> str:
    if build:
        remember_build(ctx, build)
        return build
    try:
        current = ctx.lifespan_context.get('current_build', {}).get(_user(ctx))
    except Exception:
        current = None
    if current:
        return current
    raise ToolError(f"{tool} needs build: the build this work belongs to, as start_onboarding or start_build named it "
                    f"(e.g. 'onboard-acme-door'). build_status shows a build's state.")


async def build_of(ctx: Context, ref: dict[str, Any]) -> str | None:
    """The build a managed document belongs to, from its tags."""
    try:
        tags = await guard_from(ctx).tags(ref)
    except Exception:
        return None
    marker = build_tag('x')[:-1]
    return next((t[len(marker):] for t in tags if t.startswith(marker)), None)


async def _feed_created(stroom, feed: dict[str, Any]) -> int | None:
    try:
        return (await stroom.get_doc('Feed', feed['uuid'])).get('createTimeMs')
    except ToolError:
        return None


async def sample_streams(ctx: Context, build: str, docs: list[dict[str, Any]] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Raw streams on the build's feeds, by feed: {'Raw Events': [...], 'Raw Reference': [...]} entries hold meta."""
    stroom = gateway_from(ctx)
    docs = docs if docs is not None else await guard_from(ctx).folder_contents(build)
    found: dict[str, list[dict[str, Any]]] = {}
    for feed in (d for d in docs if d['type'] == 'Feed'):
        created = await _feed_created(stroom, feed)
        for stream_type in ('Raw Events', 'Raw Reference'):
            terms = [{'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': feed['name']},
                     {'type': 'term', 'field': 'Type', 'condition': 'EQUALS', 'value': stream_type}]
            rows = (await stroom.find_meta(terms, 20)).get('values') or []
            # Only the feed's own: an earlier, deleted feed of the same name left its streams under the name.
            metas = own_streams([r['meta'] for r in rows if r['meta'].get('status') != 'DELETED'], created)
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
            'namespace': profiled.get('namespace'), 'root': profiled.get('root'),
            'suggested_parser': profiled.get('suggested_parser'), 'needs_text_converter': profiled['format'] in TEXT_FORMATS
            or profiled['format'] == 'xml fragments'}


# Event-logging paths whose index fields make the verification dashboard's columns, in this order (seen in VS Code:
# the user had to ask for the user column).
_COLUMN_PATHS = ('EventTime/TimeCreated', 'EventSource/User/Id', 'EventSource/Device/HostName', 'IPAddress', 'TypeId',
                 'Action', 'Outcome/Success')


async def _suggested_columns(ctx: Context, pipeline_uuid: str) -> list[str]:
    """The dashboard columns to suggest, from the index plan kept with the indexing XSLT: the time field, then the
    fields for the user, host, addresses, event type, action and outcome."""
    from tools.builds import kept_mapping
    try:
        kept = await kept_mapping(ctx, pipeline_uuid)
    except Exception:   # a suggestion: never a reason for the plan to fail
        return []
    fields = ((kept or {}).get('payload') or {}).get('fields') or [] if (kept or {}).get('kind') == 'index' else []
    chosen = []
    for path in _COLUMN_PATHS:
        chosen += [f['name'] for f in fields if (f.get('source') or '').endswith(path) and f['name'] not in chosen]
    return chosen[:8]


def next_call(step: str, build: str, feeds: list[str], raw: list[int], events: list[int],
              translation: str | None, indexing: str | None, processing: str | None = None,
              index: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    """The step's first call, with the arguments the build already gives and <...> for those still to decide, and what
    follows it. One call rather than a list of tools: a small model given several options deliberated between them
    until its context ran out."""
    tr, ix = translation or '<the events pipeline uuid>', indexing or '<the indexing pipeline uuid>'
    calls = {
        'feed': (('create_feed', {'build': build, 'name': '<the feed name the user confirmed>'}),
                 'then upload_sample once per sample file'),
        'samples': (('upload_sample', {'feed': feeds[0] if feeds else '<the feed>',
                                       'files': ["<each sample file's path, as the user's terminal sees it>"]}),
                    'run each command it gives in the user\'s terminal (they approve it): each file goes from their '
                    'disk to Stroom whole, and prints its stream id. sample= is only for text the user pasted into '
                    'the chat. From then on give tools stream_ids, not the text'),
        'converter': (('build_data_splitter', {'stream_ids': raw, 'build': build, 'save_as': '<converter name>'}),
                      'it saves the converter once every line parses'),
        'translation': (('draft_translation_mapping', {'stream_ids': raw}),
                        'decide what its notes ask, then build_translation_xslt with the mapping, stream_ids, the '
                        'splitter, build and name: it saves the XSLT with the mapping'),
        'pipeline': (('find_pipeline_templates', {'stage': 'translation'}),
                     "then create_pipeline from the best candidate: it takes the build's converter and XSLT"),
        'stepped': (('step_sample', {'pipeline_uuid': tr, 'stream_ids': raw}),
                    'until the verdict is clean: fix the mapping and build_translation_xslt uuid=... in between'),
        'processed': (('create_processor_filter', {'pipeline_uuid': tr, 'stream_ids': raw}),
                      'then wait_for_processing and check_events'),
        'validated': (('check_events', {'events_xml': '<a record of the Events stream (read_stream)>'}),
                      'one record of each kind of event'),
        'documented': (('write_documentation', {'build': build, 'pipeline_uuid': tr, 'stream_ids': raw,
                                                'markdown': '<the documentation, from the documentation guide>'}),
                       'the Field mapping section is generated'),
        'index': (('get_field_conventions', {}),
                  'go straight on, without asking first: it finds the backend and asks the user, in a form, how the '
                  'index\'s fields are named; then draft_index_mapping as its reply says (index_name, events_stream_ids='
                  f'{events}), and create_index_doc with plan= once the user confirms'),
        'indexing_pipeline': (('save_xslt', {'build': build, 'name': '<index name>-XSLT',
                                             'index_plan': '<the plan from draft_index_mapping>'}),
                              'then create_indexing_pipeline with that XSLT and the index'),
        'index_template': (('propose_index_template', {
            'pipeline_uuid': ix, 'plan': '<the index plan saved with the indexing XSLT>', 'events_stream_ids': events,
            'example_template': "<the user's example index template, exactly as they pasted it>"}),
            "the user confirms it in a form; give them its dev_tools to have it committed to the cluster, and wait for "
            "them to say it is. No example in this conversation: ask them to paste it, and end your turn"),
        'index_template:unstepped': (('step_sample', {'pipeline_uuid': ix, 'stream_ids': events}),
                                     'until the verdict is clean (fix the index plan and save_xslt in between); then '
                                     'propose_index_template with the user\'s example'),
        'indexed': (('create_processor_filter', {'pipeline_uuid': ix, 'stream_ids': events, 'source_pipeline_uuid': tr}),
                    'then wait_for_processing expect_events=false, and verify_index'),
        'indexed:unstepped': (('step_sample', {'pipeline_uuid': ix, 'stream_ids': events}),
                              'until the verdict is clean; then create_processor_filter'),
        'indexed:running': (('wait_for_processing', {'pipeline_uuid': ix, 'stream_ids': events, 'expect_events': False}),
                            'then verify_index'),
        'indexed:no_index': (('create_index_doc', {
            'build': build, 'backend': 'elasticsearch', 'name': '<the index name>', 'index_name': '<the index name>',
            'cluster_uuid': '<the cluster the pipeline writes to>', 'time_field': '@timestamp'}),
            'then verify_index through it'),
        'indexed:finished': (('verify_index', {
            'build': build, 'index_uuid': (index or {}).get('uuid', '<the index doc uuid>'),
            'backend': (index or {}).get('backend', '<lucene or elasticsearch>'), 'stream_ids': events,
            'expected_documents': '<the Events records in those streams>',
            'fields': ("<the columns the user chose; suggest " + ', '.join((index or {}).get('columns') or [])
                       + " and confirm them>" if (index or {}).get('columns') else '<the columns the user chose>'),
            'pipeline_uuid': ix}),
            'the step is done once its searches pass'),
        'index_documented': (('write_documentation', {'build': build, 'pipeline_uuid': ix, 'stream_ids': events,
                                                      'markdown': '<the documentation, from the documentation guide>'}),
                             'the Field mapping section is generated'),
        'promoted': (('promote_build', {'build': build, 'destinations': '<the folders sibling sources use, by type>'}),
                     "with the user's approval"),
    }
    (tool, arguments), then = calls.get(f'{step}:{processing}') or calls[step]
    return {'tool': tool, 'arguments': arguments}, then


async def status(ctx: Context, build: str, made: dict[str, Any] | None = None) -> dict[str, Any]:
    """Each plan step's state, read from the build; made, a doc the calling tool has just created, counts whether or
    not the listing shows it yet."""
    from tools.builds import _build_docs, build_checks, kept_mapping
    from tools.processing import processing_status
    from tools.processing_writes import agreement_problem, elastic_destination
    from tools.stepping import stepped_clean, verified
    from tools.templates import _shape
    from utils.mappingstore import read_mapping
    stroom = gateway_from(ctx)
    guard = guard_from(ctx)
    docs = await _build_docs(ctx, build)
    if made and not any(d['uuid'] == made['uuid'] for d in docs):
        # Just created, the explorer can leave it out for a moment (seen in the plan walk: create_feed's next named the
        # feed step again, inviting a second feed).
        docs = docs + [{'type': made['type'], 'uuid': made['uuid'], 'name': made['name'], 'path': None,
                        'working_copy_of': None}]
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
    from tools.pipeline_writes import open_slots
    from tools.pipelines import merge_layers
    for d in by_type.get('Pipeline', []):
        shape = await _shape(stroom, d['uuid'])
        missing = [f"{s['element']}.{s['property']}" for s in await open_slots(stroom, merge_layers(await stroom.pipeline_layers(d['uuid'])))]
        pipelines.append({**d, 'stage': shape['stage'], 'parser': shape.get('parser'), 'stepped': await stepped_clean(ctx, d),
                          'missing': missing})
    translation = [p for p in pipelines if p['stage'] == 'translation']
    # A discovery build indexes its raw streams as they are: no events pipeline, its discovery pipeline indexes.
    discovery = bool([p for p in pipelines if p['stage'] == 'discovery']) and not translation
    indexing = [p for p in pipelines if p['stage'] in ('indexing', 'discovery')]
    documented = {d['name'] for d in by_type.get('Documentation', [])}
    events = []
    for feed in by_type.get('Feed', []):
        rows = (await stroom.find_meta([{'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': feed['name']},
                                        {'type': 'term', 'field': 'Type', 'condition': 'EQUALS', 'value': 'Events'}], 50)).get('values') or []
        events += [m['id'] for m in own_streams([r['meta'] for r in rows if r['meta'].get('status') != 'DELETED'],
                                                await _feed_created(stroom, feed))]
    # Elasticsearch: the index template agreed with the user for each indexing pipeline's current code.
    # Not needed for Lucene; to do for Elasticsearch until every such pipeline has one.
    problems = []
    for p in indexing:
        destination = await elastic_destination(stroom, p['uuid'])
        if destination:
            problems.append(await agreement_problem(stroom, await stroom.get_doc('Pipeline', p['uuid']), destination))
    # To do once the build is known to be Elasticsearch; otherwise (Lucene, or no index yet) not needed.
    agreed: bool | None = (not any(problems) if problems else
                           False if by_type.get('ElasticIndex') or discovery else None)
    checks = await build_checks(ctx, docs)
    stale = [c for c in checks if 'Field mapping' in c or 'differs' in c]

    done: dict[str, bool | None] = {
        'feed': bool(by_type.get('Feed')),
        'samples': bool(raw),
        'converter': (bool(by_type.get('TextConverter')) if fmt and fmt['needs_text_converter'] else None),
        'translation': any(x['mapping'] == 'translation' for x in xslts),
        'pipeline': bool(translation) and not any(p['missing'] for p in translation),
        'stepped': any(p['stepped'] for p in translation),
        'processed': bool(events),
        'validated': None,   # not recorded: the model validates after processing
        'documented': bool(translation) and all(p['name'] in documented for p in translation) and not stale,
        # A discovery index's doc is made at verification, once documents are in it: its pipeline comes first.
        'index': bool(by_type.get('Index') or by_type.get('ElasticIndex')) or (discovery and bool(indexing)),
        'indexing_pipeline': bool(indexing),
        'index_template': agreed,
        'indexed': bool(indexing) and all([await verified(ctx, p) for p in indexing]),
        'index_documented': bool(indexing) and all(p['name'] in documented for p in indexing),
        'promoted': False,
    }
    # Not needed when their state is None: a converter for a format that needs none, a template for Lucene.
    not_needed = {'converter', 'index_template'}
    # A discovery build indexes its raw streams: no events pipeline at all (a converter only for delimited text).
    skipped = {'translation', 'pipeline', 'stepped', 'processed', 'validated', 'documented'} if discovery else set()
    steps = []
    for item in checklist():
        state = done.get(item['step'])
        steps.append({**item, 'state': 'not needed' if item['step'] in skipped else 'done' if state
                      else 'not needed' if state is None and item['step'] in not_needed
                      else 'not recorded' if state is None else 'to do'})
    pending = [s for s in steps if s['state'] == 'to do']
    nxt = pending[0] if pending else None
    processing, index = None, None
    unstepped = [p for p in indexing if not p['stepped']]
    if nxt and nxt['step'] in ('index_template', 'indexed') and unstepped:
        # The template is checked against the documents a clean step writes, and processing needs one: step first.
        indexing = unstepped + [p for p in indexing if p not in unstepped]
        processing = 'unstepped'
    elif nxt and nxt['step'] == 'indexed':
        # Which call indexing needs next: a processor filter, the wait for it, or the searches that verify it.
        pending_ix = [p for p in indexing if not await verified(ctx, p)][0]
        indexing = [pending_ix] + [p for p in indexing if p is not pending_ix]
        filters = (await processing_status(ctx, pending_ix['uuid']))['filters']
        processing = None if not filters else 'finished' if all(f['finished'] for f in filters) else 'running'
        found = (by_type.get('ElasticIndex') or by_type.get('Index') or [None])[0]
        if found:
            index = {'uuid': found['uuid'], 'backend': 'elasticsearch' if found['type'] == 'ElasticIndex' else 'lucene',
                     'columns': await _suggested_columns(ctx, pending_ix['uuid'])}
        elif processing == 'finished':
            processing = 'no_index'    # searched through an index doc: a discovery build makes it now
    call, then = next_call(nxt['step'] if nxt else 'promoted', build, [d['name'] for d in by_type.get('Feed', [])],
                           [m['id'] for m in raw], [m['id'] for m in raw] if discovery else events[:20],
                           translation[0]['uuid'] if translation else None,
                           indexing[0]['uuid'] if indexing else None, processing, index)
    return {
        'build': build,
        'documents': docs,
        'sample': fmt,
        'feeds': [d['name'] for d in by_type.get('Feed', [])],
        'sample_streams': {t: [m['id'] for m in ms] for t, ms in streams.items()},
        'xslts': xslts,
        'pipelines': [{k: p[k] for k in ('name', 'uuid', 'stage', 'parser', 'stepped', 'missing')} for p in pipelines],
        'events_streams': events[:20],
        'before_promotion': checks,
        'steps': steps,
        'next': {'step': nxt['step'], 'do': nxt['what'], 'call': call, 'then': then} if nxt else
                {'step': 'promoted', 'do': 'Everything is in place: promote_build with the user\'s approval',
                 'call': call, 'then': then},
    }


async def build_status(ctx: Context, build: Build) -> dict[str, Any]:
    """
    Where an onboarding stands: every step of the plan (feed, sample streams, converter, translation with its
    mapping, pipeline, clean step, Events, validation, documentation, index, indexing pipeline, promotion) as
    done or to do, read from what the build holds, with the sample's format and what is next. Call it whenever
    unsure what remains; every write tool's result carries the same `next`.
    """
    return await status(ctx, build)


async def next_step(ctx: Context, build: str | None, made: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The first unfinished step of the build's plan, or None when the build is unknown or unreadable."""
    if not build:
        return None
    try:
        return (await status(ctx, build, made))['next']
    except Exception:   # the plan is advice: never fail a write over it
        return None


async def with_next(ctx: Context, build: str | None, result: dict[str, Any]) -> dict[str, Any]:
    """The tool result with `next`, the plan's next step, added (and a reminder that the work is not done)."""
    if not isinstance(result, dict) or 'status' in result:   # a confirmation or approval round, not an outcome
        return result
    from utils.consent import ctx_changes
    changes = ctx_changes(ctx) if ctx is not None else {}
    if changes:
        # The user corrected the proposal in the form: the agent must not carry on with its own value.
        result['changed_by_user'] = {k: theirs for k, (_, theirs) in changes.items()}
        said = '; '.join(f"the user changed {k.replace('_', ' ')} from '{proposed}' to '{theirs}': use '{theirs}' from "
                         f"here on" for k, (proposed, theirs) in changes.items())
        result['note'] = f"{result['note']} {said[0].upper()}{said[1:]}." if result.get('note') else said
    remember_build(ctx, build)
    made = ({k: result[k] for k in ('type', 'uuid', 'name')}
            if all(isinstance(result.get(k), str) for k in ('type', 'uuid', 'name')) else None)
    nxt = await next_step(ctx, build, made)
    if nxt:
        tool = (nxt.get('call') or {}).get('tool')
        if tool:
            # Seen: create_pipeline hidden in a VS Code tool group; the agent wrote a handoff note and stopped.
            nxt = {**nxt, 'if_missing': f"{tool} not in your tool list? Call the activate_* tool whose description "
                                        f"covers it, then {tool}. Never stop, or work around it, for want of it."}
        result['next'] = nxt
        if nxt['step'] != 'promoted':
            result['done'] = False
    return result


async def start_onboarding(
        ctx: Context,
        source_name: Annotated[str, Field(description="The source, e.g. 'Acme door controller' (names the build).")],
        samples: Annotated[SampleTexts | None, Field(
            description="The text of every sample file the user has, by file name or as a list: text to tell the format and fields from: the start of each file is enough (your reader may cut it: VS Code's read_file cuts a line at 2,000 characters). Never trimmed further, completed or repaired. The files themselves go to Stroom whole with upload_sample files=[their paths], never as this text. Not paths: "
                        "this server cannot read the client's files.")] = None,
        build: Annotated[str | None, Field(description="Build name; defaults to one made from the source name.")] = None,
        folders: Annotated[list[str] | str, ONE_OR_MORE, Field(description="Folders the work will be promoted to, if known.")] = [],
        stream_ids: SampleStreams = [],
) -> dict[str, Any]:
    """
    Start onboarding a source: profiles every sample file (format, fields, timestamp patterns, what differs
    between files, which parser and template to use, whether a text converter is needed), creates the build,
    and returns the plan with its first step and the standing instructions that apply. The work is not done
    until build_status shows every step done and promote_build has run; each tool's result says what is next.
    For a large file, create the feed and upload_sample first, then give stream_ids instead of the text: the
    server reads the streams itself, so the text is sent once.
    """
    from tools.instructions import applicable_instructions
    notes = []
    if stream_ids and samples is None:
        named, notes = await read_sample_streams(ctx, stream_ids)
    else:
        named = as_named_samples(samples)
    if not named:
        raise ToolError("Give the sample files' text (samples by file name), or stream_ids once they are uploaded; ask "
                        "the user for every file they have")
    name = build or ('onboard-' + ''.join(c if c.isalnum() else '-' for c in source_name.lower()).strip('-')[:40])
    folder = await guard_from(ctx).build_folder(name)
    remember_build(ctx, name)
    profiled = profile_many(named) if len(named) > 1 else profile(next(iter(named.values())))
    fmt = profiled['format']
    parser = PARSER_FOR_FORMAT.get(fmt, ('DSParser',))[0]
    template = await templates_reading(ctx, fmt)
    plan = checklist()
    # Given streams, the feed and samples exist already: the build itself says what is next.
    nxt = await next_step(ctx, name) if stream_ids else None
    return {
        **({'read': notes} if notes else {}),
        'build': name, 'folder': folder['_path'], 'source': source_name,
        'profile': profiled,
        'parser': parser, 'template': template,
        'text_converter': ('needed: build_data_splitter from a spec, then save_text_converter' if fmt in TEXT_FORMATS else
                           'needed: the XML fragment wrapper (profile text_converter)' if fmt == 'xml fragments' else
                           'not needed: the template\'s parser reads this format'),
        'plan': plan,
        'next': nxt or dict(zip(('step', 'do', 'call', 'then'),
                                ('feed', plan[0]['what'], *next_call('feed', name, [], [], [], None, None)))),
        'done': False,
        'standing_instructions': await applicable_instructions(ctx, folders, []),
        'hint': ("Propose the feed name from sibling feeds and create_feed; upload each file as its own stream; from then "
                 "on give tools the sample streams (stream_ids), not the text again: the converter (if needed), "
                 "build_translation_xslt with the streams and build and name (it saves the XSLT with the mapping), the "
                 "pipeline, step_sample over all streams until clean, process, validate, write_documentation, index. "
                 "build_status shows what remains at any point."),
        'tools': ("A tool this server names (in `next`, a hint or a refusal) may not be in your tool list yet: some clients"
                  " hide part of a server's tools behind tools that enable a group of them (VS Code: activate_*). Call the "
                  "one whose description covers it, then the named tool. Never work around a hidden tool with others, and "
                  "never stop because one seems to be missing."),
    }


# The plan step each core tool implements, put first in its description so a client that picks tools by
# similarity to the prompt pulls the whole onboarding chain for an "onboard these logs" request.
STEP_OF = {
    'start_onboarding': 'start', 'build_status': 'any step', 'start_build': 'start',
    'create_feed': 'feed', 'upload_sample': 'samples', 'profile_sample': 'start',
    'build_data_splitter': 'converter', 'save_text_converter': 'converter',
    'draft_translation_mapping': 'translation', 'build_translation_xslt': 'translation', 'save_xslt': 'translation',
    'find_pipeline_templates': 'pipeline', 'describe_template': 'pipeline', 'create_pipeline': 'pipeline',
    'step_sample': 'stepped', 'step_pipeline': 'stepped', 'step_records': 'stepped',
    'create_processor_filter': 'processed', 'wait_for_processing': 'processed',
    'check_events': 'validated', 'write_documentation': 'documented',
    'get_field_conventions': 'index', 'draft_index_mapping': 'index', 'create_index_doc': 'index',
    'create_indexing_pipeline': 'indexing_pipeline', 'propose_index_template': 'index_template',
    'check_index_template': 'index_template', 'verify_index': 'indexed', 'promote_build': 'promoted',
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
