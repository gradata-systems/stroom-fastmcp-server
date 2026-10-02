"""Tools that start processing: processor filters, reprocessing, and waiting for the result.

All of them act only on pipelines this server built in the workspace (the write guard), so reprocessing is
part of developing a pipeline: at most max_reprocess_streams per call, one task at a time. Reprocessing
with a production pipeline is left to the user. When a pipeline processes a stream again, Stroom itself marks
that pipeline's earlier outputs for the stream deleted (superseded); wait_for_processing can count only the
outputs of a given filter while that happens.
"""
import asyncio
import time
from datetime import datetime
from urllib.parse import quote
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import guard_from
from tools.pipelines import merge_layers
from tools.processing import processing_status
from tools.stepping import stepped_clean
from utils.consent import consent_from
from utils.params import ONE_OR_MORE
from utils.stroom import StroomGateway, doc_link, gateway_from

STREAM_STORE = {'type': 'StreamStore', 'uuid': '0', 'name': 'StreamStore'}
INDEXING_ELEMENTS = {'IndexingFilter', 'ElasticIndexingFilter'}
SourcePipeline = Annotated[str | None, Field(
    description="Indexing pipelines reading Events: the events pipeline that produced them (one this server built, "
                "or another with the user's confirmation). The filter only selects Events from exactly that pipeline.")]
SourceConfirmation = Annotated[str | None, Field(
    description="From an earlier needs_confirmation reply: the user confirmed indexing Events from a pipeline this "
                "server did not build.")]


def _term(field: str, value: Any, condition: str = 'EQUALS') -> dict[str, Any]:
    return {'type': 'term', 'field': field, 'condition': condition, 'value': str(value)}


async def _managed_pipeline(ctx: Context, uuid: str) -> dict[str, Any]:
    doc = await gateway_from(ctx).get_doc('Pipeline', uuid)
    await guard_from(ctx).check_managed({'type': 'Pipeline', 'uuid': uuid, 'name': doc.get('name')})
    return doc


async def _create_filter(stroom: StroomGateway, pipeline: dict[str, Any], expression: dict[str, Any],
                         priority: int, max_tasks: int, min_create_ms: int | None, enabled: bool = True) -> dict[str, Any]:
    return await stroom.post('/processorFilter/v1', {
        'pipeline': {'type': 'Pipeline', 'uuid': pipeline['uuid'], 'name': pipeline['name']},
        'processorType': 'PIPELINE', 'enabled': enabled, 'priority': priority, 'autoPriority': False,
        'reprocess': False, 'export': False, 'maxProcessingTasks': max_tasks,
        'minMetaCreateTimeMs': min_create_ms,
        'queryData': {'dataSource': STREAM_STORE, 'expression': expression}})


def _selected_ids(expression: dict[str, Any] | None) -> set[int]:
    ids = set()
    for child in (expression or {}).get('children') or []:
        if child.get('type') == 'operator':
            ids |= _selected_ids(child)
        elif child.get('field') == 'Id' and child.get('condition') == 'EQUALS' and str(child.get('value')).isdigit():
            ids.add(int(child['value']))
    return ids


async def _already_processed(stroom: StroomGateway, pipeline_uuid: str, stream_ids: list[int]) -> list[int]:
    """Streams this pipeline has output for, or that one of its filters already selects."""
    rows = await stroom.post('/processorFilter/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': []}})
    selected = set()
    for row in rows.get('values') or []:
        f = row.get('processorFilter') or {}
        if f.get('pipelineUuid') == pipeline_uuid and not f.get('deleted'):
            selected |= _selected_ids((f.get('queryData') or {}).get('expression'))
    return [i for i in stream_ids if i in selected or await _outputs(stroom, i, pipeline_uuid)]


def _pipeline_term(ref: dict[str, Any]) -> dict[str, Any]:
    return {'type': 'term', 'field': 'Pipeline', 'condition': 'IS_DOC_REF',
            'docRef': {'type': 'Pipeline', 'uuid': ref['uuid'], 'name': ref['name']}}


async def _is_indexing(stroom: StroomGateway, pipeline_uuid: str) -> bool:
    merged = merge_layers(await stroom.pipeline_layers(pipeline_uuid))
    return any(e['type'] in INDEXING_ELEMENTS for e in merged['elements'])


async def _events_source(ctx: Context, pipeline: dict[str, Any], source_uuid: str | None,
                         stream_ids: list[int] | None, stream_type: str,
                         source_confirmation_id: str | None = None) -> Any:
    """For an indexing pipeline reading Events: the events pipeline they must come from, checked.

    Indexing filters over Events always carry `Pipeline IS_DOC_REF <source>`, so they never pick up Events
    streams from anywhere else. The source must be an events pipeline this server built, and every given
    stream must be an Events stream it produced. Raw input (a discovery index) has no source pipeline.
    """
    stroom = gateway_from(ctx)
    if not await _is_indexing(stroom, pipeline['uuid']):
        if source_uuid:
            raise ToolError("source_pipeline_uuid is only for indexing pipelines")
        return None
    metas = []
    if stream_ids:
        metas = [r['meta'] for r in (await stroom.find_meta([_term('Id', i) for i in stream_ids], len(stream_ids),
                                                            op='OR')).get('values') or []]
        types = {m.get('typeName') for m in metas}
        if 'Events' not in types:
            if source_uuid:
                raise ToolError("These streams are not Events streams; source_pipeline_uuid does not apply")
            return None
        if types != {'Events'}:
            raise ToolError(f"Mixed stream types {sorted(t or '?' for t in types)}: process Events streams on their own")
    elif stream_type != 'Events':
        if source_uuid:
            raise ToolError("source_pipeline_uuid only applies when the feed scope's stream_type is Events")
        return None
    if not source_uuid:
        raise ToolError("Indexing Events needs source_pipeline_uuid: the events pipeline this server built that "
                        "produced them. The filter only selects Events from that pipeline.")
    source = await stroom.get_doc('Pipeline', source_uuid)
    try:
        await guard_from(ctx).check_built({'type': 'Pipeline', 'uuid': source_uuid, 'name': source.get('name')})
    except ToolError:
        # Not one of ours: allowed only when the user confirms this exact source pipeline.
        gate = await consent_from(ctx).require(
            ctx, 'confirmation', 'index_events_from_other_pipeline',
            f"Index Events from pipeline '{source.get('name')}', which this server did not build? Only its Events "
            f"are selected (Pipeline condition).",
            {'source pipeline': source.get('name'), 'uuid': source_uuid, 'indexing pipeline': pipeline['name']},
            source_confirmation_id, keep=True)
        if gate:
            return gate
    if await _is_indexing(stroom, source_uuid):
        raise ToolError(f"'{source['name']}' is an indexing pipeline, not the events pipeline that produced the Events")
    foreign = sorted(m['id'] for m in metas if m.get('pipelineUuid') != source_uuid)
    missing = sorted(set(stream_ids or []) - {m['id'] for m in metas})
    if foreign or missing:
        raise ToolError(f"Stream(s) {foreign + missing} were not produced by '{source['name']}'"
                        + (f" ({missing} not found)" if missing else '') + ": only its Events can be indexed here")
    return {'type': 'Pipeline', 'uuid': source['uuid'], 'name': source['name']}


async def _build_feeds_only(ctx: Context, pipeline: dict[str, Any], stream_ids: list[int] | None,
                            feed: str | None) -> None:
    """Translation pipelines write their Events into the input's feed, so they only process the build's feeds.

    Production records are stepped where they are (step_records) or copied into a test feed in the build.
    Indexing pipelines write to an index, not to a feed, so they are not limited this way.
    """
    stroom = gateway_from(ctx)
    if await _is_indexing(stroom, pipeline['uuid']):
        return
    guard = guard_from(ctx)
    tags = await guard.tags({'type': 'Pipeline', 'uuid': pipeline['uuid'], 'name': pipeline['name']})
    builds = {t for t in tags if t.startswith('mcp-build-')}
    feeds = {feed} if feed else {r['meta'].get('feedName') for r in (await stroom.find_meta(
        [_term('Id', i) for i in stream_ids or []], len(stream_ids or []), op='OR')).get('values') or []}
    outside = []
    for name in sorted(f for f in feeds if f):
        ref = await stroom.get(f'/feed/v1/getDocRefForName/{quote(name, safe="")}')
        feed_tags = await guard.tags(ref) if ref else []
        if not builds & set(feed_tags):
            outside.append(name)
    if outside:
        raise ToolError(f"Feed(s) {outside} are not in this build. A translation pipeline writes its Events into the "
                        f"input's feed, so it only processes the build's own feeds: step production records in place "
                        f"(step_records), or copy them into a test feed in the build (create_feed '<FEED>-MCP-TEST', "
                        f"upload_sample).")


async def elastic_destination(stroom: StroomGateway, pipeline_uuid: str) -> dict[str, Any] | None:
    """Cluster and index name an Elasticsearch indexing pipeline writes to; None for other pipelines."""
    merged = merge_layers(await stroom.pipeline_layers(pipeline_uuid))
    elements = {e['id'] for e in merged['elements'] if e['type'] == 'ElasticIndexingFilter'}
    if not elements:
        return None
    props = {p['name']: p['value'] for p in merged['properties'] if p['element'] in elements}
    return {'index name': props.get('indexName'), 'cluster': (props.get('cluster') or {}).get('name')}


def _pipeline_terms(expression: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for child in (expression or {}).get('children') or []:
        if child.get('type') == 'operator':
            out += _pipeline_terms(child)
        elif child.get('field') == 'Pipeline':
            out.append(child)
    return out


async def promotion_processing(ctx: Context, pipelines: list[dict[str, Any]],
                               survey_feeds: list[str]) -> list[dict[str, Any]]:
    """What each promoted pipeline should go on processing: the feeds and stream types of its sample filters.

    A pipeline built by stepping only (from an existing feed) has no sample filters: it processes the
    surveyed feed's Raw Events. Test feeds (-MCP-TEST) are left out: they only held samples.
    """
    stroom = gateway_from(ctx)
    rows = await stroom.post('/processorFilter/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': []}})
    filters = [r['processorFilter'] for r in rows.get('values') or []
               if r.get('processorFilter') and not r['processorFilter'].get('deleted')]
    plan = []
    for pipeline in pipelines:
        own = [f for f in filters if f.get('pipelineUuid') == pipeline['uuid']]
        targets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for f in own:
            expression = (f.get('queryData') or {}).get('expression')
            ids = sorted(_selected_ids(expression))
            metas = [r['meta'] for r in (await stroom.find_meta([_term('Id', i) for i in ids], len(ids), op='OR')
                                         ).get('values') or []] if ids else []
            for meta in metas:
                targets.setdefault((meta.get('feedName'), meta.get('typeName')), _pipeline_terms(expression))
        if not own and not await _is_indexing(stroom, pipeline['uuid']):
            for feed in survey_feeds:
                targets.setdefault((feed, 'Raw Events'), [])
        for (feed, stream_type), extra in sorted(targets.items()):
            if feed and not feed.endswith('-MCP-TEST'):
                plan.append({'pipeline': pipeline, 'feed': feed, 'stream_type': stream_type, 'extra_terms': extra})
    return plan


async def create_promotion_filters(ctx: Context, plan: list[dict[str, Any]], from_ms: int) -> list[dict[str, Any]]:
    """Feed-wide filters for promoted pipelines, from the promotion time, created disabled for the user."""
    stroom = gateway_from(ctx)
    made = []
    for entry in plan:
        expression = {'type': 'operator', 'op': 'AND', 'children': [
            _term('Feed', entry['feed']), _term('Type', entry['stream_type']), *entry['extra_terms']]}
        created = await _create_filter(stroom, entry['pipeline'], expression, 10, stroom.settings.max_feed_filter_tasks,
                                       from_ms, enabled=False)
        made.append({'filter_id': created['id'], 'pipeline': entry['pipeline']['name'], 'feed': entry['feed'],
                     'stream_type': entry['stream_type'], 'enabled': False,
                     'pipeline_link': doc_link(stroom.settings, 'Pipeline', entry['pipeline']['uuid'])})
    return made


async def _template_gate(ctx: Context, destination: dict[str, Any], confirmation_id: str | None) -> dict[str, Any] | None:
    """Elasticsearch: the user confirms the index template for the destination index has been committed."""
    index = destination['index name']
    if not index:
        raise ToolError("This Elasticsearch indexing pipeline has no indexName set")
    return await consent_from(ctx).require(
        ctx, 'confirmation', 'processing:index_template',
        f"Have you committed the index template for Elasticsearch index '{index}' (cluster "
        f"{destination['cluster']})? The indexing filter is then created disabled, for you to enable.",
        {k: v for k, v in destination.items() if v is not None}, confirmation_id)


def _ready_to_enable(stroom: StroomGateway, pipeline: dict[str, Any], created: dict[str, Any],
                     destination: dict[str, Any]) -> dict[str, Any]:
    link = doc_link(stroom.settings, 'Pipeline', pipeline['uuid'])
    return {'filter_id': created['id'], 'enabled': False, 'destination': destination, 'pipeline_link': link,
            'next': (f"Tell the user the indexing filter {created['id']} on pipeline '{pipeline['name']}' is ready to "
                     f"enable, with this link to review the pipeline (its Processors tab holds the filter): {link}. "
                     f"Once they have enabled it (or ask you to, with set_processor_filter_enabled), "
                     f"wait_for_processing and verify.")}


async def create_processor_filter(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="A pipeline this server created.")],
        stream_ids: Annotated[list[int] | None, ONE_OR_MORE, Field(
            description="Process exactly these streams (the sample). The default and safest scope.")] = None,
        feed: Annotated[str | None, Field(description="Or process a whole feed's streams of stream_type.")] = None,
        stream_type: Annotated[str, Field(description="Stream type to process with a feed scope.")] = 'Raw Events',
        created_after: Annotated[str | None, Field(
            description="With a feed scope: only streams created after this ISO time. Required for a feed scope.")] = None,
        priority: Annotated[int, Field(ge=1, le=100)] = 10,
        source_pipeline_uuid: SourcePipeline = None,
        source_confirmation_id: SourceConfirmation = None,
        confirmation_id: Annotated[str | None, Field(
            description="From an earlier needs_confirmation reply (Elasticsearch: the index template is written).")] = None,
        approval_id: Annotated[str | None, Field(description="From an earlier needs_approval reply.")] = None,
) -> dict[str, Any]:
    """
    Create and enable a processor filter so Stroom processes streams through the pipeline. Scope it to the
    sample stream ids; a whole-feed scope needs a created_after bound and is limited to the configured task
    count. Streams the pipeline already processed are refused: use reprocess_streams for those.
    An indexing pipeline reading Events needs source_pipeline_uuid, and its filter only selects Events from
    exactly that events pipeline. For an Elasticsearch indexing pipeline the user confirms they have committed
    the index template for its destination index; the filter is then created disabled, for the user to enable,
    and the result carries a link to the pipeline. Other filters are enabled, after the user's approval.
    A translation pipeline only processes the build's own feeds; sample filters run one task at a time.
    """
    stroom = gateway_from(ctx)
    pipeline = await _managed_pipeline(ctx, pipeline_uuid)
    if bool(stream_ids) == bool(feed):
        raise ToolError("Give either stream_ids (the sample) or feed, not both")
    if not await stepped_clean(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': pipeline.get('name')}):
        raise ToolError(f"Pipeline '{pipeline.get('name')}' has no clean step of its current code recorded: step_sample "
                        f"(or step_records) over the sample streams until the verdict is clean, then process. A change "
                        f"to its XSLT or converter since the last clean step needs stepping again.")
    await _build_feeds_only(ctx, pipeline, stream_ids, feed)
    source = await _events_source(ctx, pipeline, source_pipeline_uuid, stream_ids, stream_type, source_confirmation_id)
    if source and 'status' in source:
        return source
    min_ms = None
    if stream_ids:
        done = await _already_processed(stroom, pipeline_uuid, stream_ids)
        if done:
            raise ToolError(f"Pipeline '{pipeline['name']}' has already processed stream(s) {done}: use "
                            f"reprocess_streams (at most {stroom.settings.max_reprocess_streams} per call)")
        expression = {'type': 'operator', 'op': 'OR', 'children': [_term('Id', i) for i in stream_ids]}
        scope, max_tasks = f"streams {stream_ids}", stroom.settings.sample_max_tasks
    else:
        if not created_after:
            raise ToolError("A feed-wide filter needs created_after, so it does not reprocess the feed's history")
        min_ms = int(datetime.fromisoformat(created_after.replace('Z', '+00:00')).timestamp() * 1000)
        expression = {'type': 'operator', 'op': 'AND', 'children': [_term('Feed', feed), _term('Type', stream_type)]}
        scope, max_tasks = f"feed {feed} ({stream_type}) created after {created_after}", stroom.settings.max_feed_filter_tasks
    if source:
        expression = {'type': 'operator', 'op': 'AND', 'children': [expression, _pipeline_term(source)]}
        scope += f", only Events from pipeline '{source['name']}'"
    details = {'pipeline': pipeline['name'], 'scope': scope, 'priority': priority, 'max tasks': max_tasks or 'unlimited'}

    destination = await elastic_destination(stroom, pipeline_uuid)
    if destination:
        gate = await _template_gate(ctx, destination, confirmation_id)
        if gate:
            return gate
        created = await _create_filter(stroom, pipeline, expression, priority, max_tasks, min_ms, enabled=False)
        return {**_ready_to_enable(stroom, pipeline, created, destination), 'pipeline': pipeline['name'], 'scope': scope,
                **({'events_from_pipeline': source['name']} if source else {})}

    gate = await consent_from(ctx).require(ctx, 'approval', 'create_processor_filter',
                                           f"Start processing {scope} with pipeline '{pipeline['name']}'",
                                           details, approval_id)
    if gate:
        return gate
    created = await _create_filter(stroom, pipeline, expression, priority, max_tasks, min_ms)
    consent_from(ctx).discard(source_confirmation_id)
    from tools.plan import build_of, with_next
    result = {'filter_id': created['id'], 'pipeline': pipeline['name'], 'scope': scope, 'enabled': created.get('enabled'),
              **({'events_from_pipeline': source['name']} if source else {})}
    return await with_next(ctx, await build_of(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': pipeline['name']}), result)


async def set_processor_filter_enabled(
        ctx: Context,
        filter_id: Annotated[int, Field(description="Processor filter id, e.g. from processing_status.")],
        enabled: bool,
        approval_id: Annotated[str | None, Field(description="From an earlier needs_approval reply.")] = None,
) -> dict[str, Any]:
    """Enable (needs approval) or disable a processor filter on a pipeline this server created."""
    stroom = gateway_from(ctx)
    current = await stroom.get(f'/processorFilter/v1/{filter_id}')
    pipeline = await _managed_pipeline(ctx, current['pipelineUuid'])
    if enabled:
        gate = await consent_from(ctx).require(ctx, 'approval', 'set_processor_filter_enabled',
                                               f"Enable processor filter {filter_id} on '{pipeline['name']}'",
                                               {'filter': filter_id, 'pipeline': pipeline['name']}, approval_id)
        if gate:
            return gate
    await stroom.request('PUT', f'/processorFilter/v1/{filter_id}/enabled', enabled)
    return {'filter_id': filter_id, 'enabled': enabled}


async def _outputs(stroom: StroomGateway, raw_id: int, pipeline_uuid: str,
                   filter_id: int | None = None) -> list[dict[str, Any]]:
    rows = (await stroom.find_meta([_term('Parent Id', raw_id)], 100)).get('values') or []
    return [r['meta'] for r in rows
            if r['meta'].get('pipelineUuid') == pipeline_uuid and r['meta'].get('status') != 'DELETED'
            and (filter_id is None or r['meta'].get('processorFilterId') == filter_id)]


async def reprocess_streams(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="A pipeline this server built, after a change.")],
        stream_ids: Annotated[list[int], ONE_OR_MORE, Field(description="Streams it already processed, to process again.")],
        source_pipeline_uuid: SourcePipeline = None,
        source_confirmation_id: SourceConfirmation = None,
        confirmation_id: Annotated[str | None, Field(
            description="From an earlier needs_confirmation reply (Elasticsearch: the index template is written).")] = None,
        approval_id: Annotated[str | None, Field(description="From an earlier needs_approval reply.")] = None,
) -> dict[str, Any]:
    """
    Process streams again through a pipeline under development, after changing it. At most
    max_reprocess_streams (default 10) per call, run one task at a time. Stroom marks the pipeline's earlier
    outputs for these streams deleted once the new ones are written; pass the returned filter_id to
    wait_for_processing so only the new outputs count. Needs approval; for Elasticsearch indexing the user
    instead confirms the index template is committed and the filter is created disabled for them to enable.
    Promoted pipelines are refused by the write guard: reprocessing production data is the user's.
    """
    stroom = gateway_from(ctx)
    pipeline = await _managed_pipeline(ctx, pipeline_uuid)
    limit = stroom.settings.max_reprocess_streams
    if not stream_ids or len(stream_ids) > limit:
        raise ToolError(f"Give 1 to {limit} streams per call; order more once these finish")
    done = set(await _already_processed(stroom, pipeline_uuid, stream_ids))
    fresh = [i for i in stream_ids if i not in done]
    if fresh:
        raise ToolError(f"Stream(s) {fresh} have not been processed by '{pipeline['name']}': use create_processor_filter")
    await _build_feeds_only(ctx, pipeline, stream_ids, None)
    source = await _events_source(ctx, pipeline, source_pipeline_uuid, stream_ids, 'Events', source_confirmation_id)
    if source and 'status' in source:
        return source
    max_tasks = stroom.settings.reprocess_max_tasks
    details = {'pipeline': pipeline['name'], 'streams': stream_ids, 'max tasks': max_tasks,
               **({'only Events from pipeline': source['name']} if source else {}),
               'earlier outputs': 'superseded: Stroom marks them deleted once the new ones are written'}
    expression = {'type': 'operator', 'op': 'OR', 'children': [_term('Id', i) for i in stream_ids]}
    if source:
        expression = {'type': 'operator', 'op': 'AND', 'children': [expression, _pipeline_term(source)]}
    destination = await elastic_destination(stroom, pipeline_uuid)
    if destination:
        gate = await _template_gate(ctx, destination, confirmation_id)
        if gate:
            return gate
        created = await _create_filter(stroom, pipeline, expression, 10, max_tasks, None, enabled=False)
        return {**_ready_to_enable(stroom, pipeline, created, destination), 'streams': stream_ids,
                'note': "documents already indexed from these streams may be indexed again"}
    gate = await consent_from(ctx).require(ctx, 'approval', 'reprocess_streams',
                                           f"Reprocess {len(stream_ids)} stream(s) with '{pipeline['name']}'",
                                           details, approval_id)
    if gate:
        return gate
    created = await _create_filter(stroom, pipeline, expression, 10, max_tasks, None)
    return {'filter_id': created['id'], 'pipeline': pipeline['name'], 'streams': stream_ids, 'max_tasks': max_tasks,
            'hint': f"wait_for_processing with filter_id={created['id']} so only this run's outputs count."}


async def wait_for_processing(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The pipeline that is processing.")],
        stream_ids: Annotated[list[int], ONE_OR_MORE, Field(description="Input streams to wait for.")],
        timeout_seconds: Annotated[int, Field(ge=5, le=900)] = 180,
        expect_events: Annotated[bool, Field(
            description="True for translation pipelines (one Events stream per input); False for indexing "
                        "pipelines, which write to an index and should produce no Error stream.")] = True,
        filter_id: Annotated[int | None, Field(
            description="Only count outputs from this processor filter, e.g. the one reprocess_streams made.")] = None,
        output_type: Annotated[str, Field(description="The stream type expected per input: 'Events', or 'Reference' "
                                                      "for a reference-data pipeline.")] = 'Events',
) -> dict[str, Any]:
    """
    Wait until the pipeline's processor tasks finish, then report per input stream the output (Events, or
    Reference) and Error streams it produced. The gate before the next stage: exactly one output stream per
    input stream. A missing or duplicate one is flagged with the likely cause.
    """
    stroom = gateway_from(ctx)
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = await processing_status(ctx, pipeline_uuid)
        outputs = {raw: await _outputs(stroom, raw, pipeline_uuid, filter_id) for raw in stream_ids}
        all_have_output = all(outputs.values()) or not expect_events
        finished = all(f['finished'] for f in status['filters']) if status['filters'] else False
        if (all_have_output and finished) or time.monotonic() > deadline:
            break
        await asyncio.sleep(3)
    per_stream, problems = [], []
    for raw, metas in outputs.items():
        events = [m['id'] for m in metas if m.get('typeName') == output_type]
        errors = [m['id'] for m in metas if m.get('typeName') == 'Error']
        per_stream.append({'input': raw, 'events': events, 'errors': errors, **({'output_type': output_type} if output_type != 'Events' else {})})
        if not expect_events:
            if errors:
                problems.append(f"Stream {raw} produced Error stream(s) {errors}: summarise_streams (kind=errors) {raw}")
        elif len(events) == 0:
            problems.append(f"Stream {raw} produced no {output_type} stream: check processing_status and summarise_streams (kind=errors) "
                            f"{raw} (failed task, fatal error, or a filter that missed it)")
        elif len(events) > 1:
            problems.append(f"Stream {raw} has {len(events)} {output_type} streams: it was processed more than once. "
                            "After reprocess_streams, pass its filter_id so only the new output counts; otherwise "
                            "ask the user which to keep")
    from tools.plan import build_of, with_next
    result = {'pipeline': pipeline_uuid, 'finished': finished, 'streams': per_stream,
              'gate': 'pass' if not problems and finished else 'fail', 'problems': problems,
              'hint': None if finished else "Tasks were still running at the timeout; call again."}
    return await with_next(ctx, await build_of(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid}), result)


ALL_TOOLS = [create_processor_filter, set_processor_filter_enabled, reprocess_streams, wait_for_processing]
