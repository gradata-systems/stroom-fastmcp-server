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
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import guard_from
from tools.pipelines import merge_layers
from tools.processing import processing_status
from utils.consent import consent_from
from utils.stroom import StroomGateway, gateway_from

STREAM_STORE = {'type': 'StreamStore', 'uuid': '0', 'name': 'StreamStore'}
INDEXING_ELEMENTS = {'IndexingFilter', 'ElasticIndexingFilter'}
SourcePipeline = Annotated[str | None, Field(
    description="Indexing pipelines reading Events: the events pipeline this server built that produced them. "
                "The filter only selects Events streams from exactly that pipeline.")]


def _term(field: str, value: Any, condition: str = 'EQUALS') -> dict[str, Any]:
    return {'type': 'term', 'field': field, 'condition': condition, 'value': str(value)}


async def _managed_pipeline(ctx: Context, uuid: str) -> dict[str, Any]:
    doc = await gateway_from(ctx).get_doc('Pipeline', uuid)
    await guard_from(ctx).check_managed({'type': 'Pipeline', 'uuid': uuid, 'name': doc.get('name')})
    return doc


async def _create_filter(stroom: StroomGateway, pipeline: dict[str, Any], expression: dict[str, Any],
                         priority: int, max_tasks: int, min_create_ms: int | None) -> dict[str, Any]:
    return await stroom.post('/processorFilter/v1', {
        'pipeline': {'type': 'Pipeline', 'uuid': pipeline['uuid'], 'name': pipeline['name']},
        'processorType': 'PIPELINE', 'enabled': True, 'priority': priority, 'autoPriority': False,
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
                         stream_ids: list[int] | None, stream_type: str) -> dict[str, Any] | None:
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
    source = await _managed_pipeline(ctx, source_uuid)
    if await _is_indexing(stroom, source_uuid):
        raise ToolError(f"'{source['name']}' is an indexing pipeline, not the events pipeline that produced the Events")
    foreign = sorted(m['id'] for m in metas if m.get('pipelineUuid') != source_uuid)
    missing = sorted(set(stream_ids or []) - {m['id'] for m in metas})
    if foreign or missing:
        raise ToolError(f"Stream(s) {foreign + missing} were not produced by '{source['name']}'"
                        + (f" ({missing} not found)" if missing else '') + ": only its Events can be indexed here")
    return {'type': 'Pipeline', 'uuid': source['uuid'], 'name': source['name']}


async def elastic_destination(stroom: StroomGateway, pipeline_uuid: str) -> dict[str, Any] | None:
    """Cluster and index name an Elasticsearch indexing pipeline writes to; None for other pipelines."""
    merged = merge_layers(await stroom.pipeline_layers(pipeline_uuid))
    elements = {e['id'] for e in merged['elements'] if e['type'] == 'ElasticIndexingFilter'}
    if not elements:
        return None
    props = {p['name']: p['value'] for p in merged['properties'] if p['element'] in elements}
    return {'index name': props.get('indexName'), 'cluster': (props.get('cluster') or {}).get('name')}


async def _template_gate(ctx: Context, destination: dict[str, Any], confirmation_id: str | None) -> dict[str, Any] | None:
    """Elasticsearch: the user confirms the index template for the destination index has been written."""
    index = destination['index name']
    if not index:
        raise ToolError("This Elasticsearch indexing pipeline has no indexName set")
    written = {**destination, 'seen from this server': await _template_check(ctx, index)}
    return await consent_from(ctx).require(
        ctx, 'confirmation', 'processing:index_template',
        f"Has the index template for Elasticsearch index '{index}' (cluster {destination['cluster']}) been "
        f"written? Indexing only starts once it has.",
        {k: v for k, v in written.items() if v is not None}, confirmation_id, keep=True)


async def _template_check(ctx: Context, index_name: str) -> str | None:
    """What this server can see of the index template, when it has Elasticsearch access."""
    elastic = ctx.lifespan_context.get('elastic')
    if not (elastic and elastic.configured):
        return None
    try:
        matched = await elastic.simulate(index_name)
    except ToolError as e:
        return f"could not check: {e}"
    return "a template matches this index" if matched else "NO template matches this index yet"


async def create_processor_filter(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="A pipeline this server created.")],
        stream_ids: Annotated[list[int] | None, Field(
            description="Process exactly these streams (the sample). The default and safest scope.")] = None,
        feed: Annotated[str | None, Field(description="Or process a whole feed's streams of stream_type.")] = None,
        stream_type: Annotated[str, Field(description="Stream type to process with a feed scope.")] = 'Raw Events',
        created_after: Annotated[str | None, Field(
            description="With a feed scope: only streams created after this ISO time. Required for a feed scope.")] = None,
        priority: Annotated[int, Field(ge=1, le=100)] = 10,
        source_pipeline_uuid: SourcePipeline = None,
        confirmation_id: Annotated[str | None, Field(
            description="From an earlier needs_confirmation reply (Elasticsearch: the index template is written).")] = None,
        approval_id: Annotated[str | None, Field(description="From an earlier needs_approval reply.")] = None,
) -> dict[str, Any]:
    """
    Create and enable a processor filter so Stroom processes streams through the pipeline. Scope it to the
    sample stream ids; a whole-feed scope needs a created_after bound and is limited to the configured task
    count. Streams the pipeline already processed are refused: use reprocess_streams for those.
    An indexing pipeline reading Events needs source_pipeline_uuid, and its filter only selects Events from
    exactly that events pipeline. For an Elasticsearch indexing pipeline the user first confirms that the
    index template for the destination index has been written. Enabling processing always needs approval.
    """
    stroom = gateway_from(ctx)
    pipeline = await _managed_pipeline(ctx, pipeline_uuid)
    if bool(stream_ids) == bool(feed):
        raise ToolError("Give either stream_ids (the sample) or feed, not both")
    source = await _events_source(ctx, pipeline, source_pipeline_uuid, stream_ids, stream_type)
    min_ms = None
    if stream_ids:
        done = await _already_processed(stroom, pipeline_uuid, stream_ids)
        if done:
            raise ToolError(f"Pipeline '{pipeline['name']}' has already processed stream(s) {done}: use "
                            f"reprocess_streams (at most {stroom.settings.max_reprocess_streams} per call)")
        expression = {'type': 'operator', 'op': 'OR', 'children': [_term('Id', i) for i in stream_ids]}
        scope, max_tasks = f"streams {stream_ids}", 0
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
        details.update(destination)
        gate = await _template_gate(ctx, destination, confirmation_id)
        if gate:
            return gate

    gate = await consent_from(ctx).require(ctx, 'approval', 'create_processor_filter',
                                           f"Start processing {scope} with pipeline '{pipeline['name']}'"
                                           + (f" into index '{destination['index name']}'" if destination else ''),
                                           details, approval_id)
    if gate:
        return gate
    created = await _create_filter(stroom, pipeline, expression, priority, max_tasks, min_ms)
    consent_from(ctx).discard(confirmation_id)
    return {'filter_id': created['id'], 'pipeline': pipeline['name'], 'scope': scope, 'enabled': created.get('enabled'),
            **({'destination': destination} if destination else {}),
            **({'events_from_pipeline': source['name']} if source else {})}


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
        stream_ids: Annotated[list[int], Field(description="Streams it already processed, to process again.")],
        source_pipeline_uuid: SourcePipeline = None,
        confirmation_id: Annotated[str | None, Field(
            description="From an earlier needs_confirmation reply (Elasticsearch: the index template is written).")] = None,
        approval_id: Annotated[str | None, Field(description="From an earlier needs_approval reply.")] = None,
) -> dict[str, Any]:
    """
    Process streams again through a pipeline under development, after changing it. At most
    max_reprocess_streams (default 10) per call, run one task at a time. Stroom marks the pipeline's earlier
    outputs for these streams deleted once the new ones are written; pass the returned filter_id to
    wait_for_processing so only the new outputs count. Needs approval (and, for
    Elasticsearch indexing, the index template confirmation). Promoted pipelines are refused by the write
    guard: reprocessing production data is the user's.
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
    source = await _events_source(ctx, pipeline, source_pipeline_uuid, stream_ids, 'Events')
    max_tasks = stroom.settings.reprocess_max_tasks
    details = {'pipeline': pipeline['name'], 'streams': stream_ids, 'max tasks': max_tasks,
               **({'only Events from pipeline': source['name']} if source else {}),
               'earlier outputs': 'superseded: Stroom marks them deleted once the new ones are written'}
    destination = await elastic_destination(stroom, pipeline_uuid)
    if destination:
        details.update(destination)
        details['note'] = "documents already indexed from these streams may be indexed again"
        gate = await _template_gate(ctx, destination, confirmation_id)
        if gate:
            return gate
    gate = await consent_from(ctx).require(ctx, 'approval', 'reprocess_streams',
                                           f"Reprocess {len(stream_ids)} stream(s) with '{pipeline['name']}'",
                                           details, approval_id)
    if gate:
        return gate
    expression = {'type': 'operator', 'op': 'OR', 'children': [_term('Id', i) for i in stream_ids]}
    if source:
        expression = {'type': 'operator', 'op': 'AND', 'children': [expression, _pipeline_term(source)]}
    created = await _create_filter(stroom, pipeline, expression, 10, max_tasks, None)
    consent_from(ctx).discard(confirmation_id)
    return {'filter_id': created['id'], 'pipeline': pipeline['name'], 'streams': stream_ids, 'max_tasks': max_tasks,
            'hint': f"wait_for_processing with filter_id={created['id']} so only this run's outputs count."}


async def wait_for_processing(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The pipeline that is processing.")],
        stream_ids: Annotated[list[int], Field(description="Input streams to wait for.")],
        timeout_seconds: Annotated[int, Field(ge=5, le=900)] = 180,
        expect_events: Annotated[bool, Field(
            description="True for translation pipelines (one Events stream per input); False for indexing "
                        "pipelines, which write to an index and should produce no Error stream.")] = True,
        filter_id: Annotated[int | None, Field(
            description="Only count outputs from this processor filter, e.g. the one reprocess_streams made.")] = None,
) -> dict[str, Any]:
    """
    Wait until the pipeline's processor tasks finish, then report per input stream the Events and Error
    streams it produced. The gate before the next stage: exactly one Events stream per input stream.
    A missing or duplicate one is flagged with the likely cause.
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
        events = [m['id'] for m in metas if m.get('typeName') == 'Events']
        errors = [m['id'] for m in metas if m.get('typeName') == 'Error']
        per_stream.append({'input': raw, 'events': events, 'errors': errors})
        if not expect_events:
            if errors:
                problems.append(f"Stream {raw} produced Error stream(s) {errors}: summarise_errors {raw}")
        elif len(events) == 0:
            problems.append(f"Stream {raw} produced no Events stream: check processing_status and summarise_errors "
                            f"{raw} (failed task, fatal error, or a filter that missed it)")
        elif len(events) > 1:
            problems.append(f"Stream {raw} has {len(events)} Events streams: it was processed more than once. "
                            "After reprocess_streams, pass its filter_id so only the new output counts; otherwise "
                            "ask the user which to keep")
    return {'pipeline': pipeline_uuid, 'finished': finished, 'streams': per_stream,
            'gate': 'pass' if not problems and finished else 'fail', 'problems': problems,
            'hint': None if finished else "Tasks were still running at the timeout; call again."}


ALL_TOOLS = [create_processor_filter, set_processor_filter_enabled, reprocess_streams, wait_for_processing]
