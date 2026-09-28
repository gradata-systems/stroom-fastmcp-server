"""Tools that start processing: processor filters, reprocessing, and waiting for the result."""
import asyncio
import time
from datetime import datetime
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import guard_from
from tools.processing import processing_status
from utils.consent import consent_from
from utils.stroom import StroomGateway, gateway_from

STREAM_STORE = {'type': 'StreamStore', 'uuid': '0', 'name': 'StreamStore'}


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
        approval_id: Annotated[str | None, Field(description="From an earlier needs_approval reply.")] = None,
) -> dict[str, Any]:
    """
    Create and enable a processor filter so Stroom processes streams through the pipeline. Scope it to the
    sample stream ids; a whole-feed scope needs a created_after bound and is limited to the configured task
    count. Enabling processing always needs the user's approval.
    """
    stroom = gateway_from(ctx)
    pipeline = await _managed_pipeline(ctx, pipeline_uuid)
    if bool(stream_ids) == bool(feed):
        raise ToolError("Give either stream_ids (the sample) or feed, not both")
    min_ms = None
    if stream_ids:
        expression = {'type': 'operator', 'op': 'OR', 'children': [_term('Id', i) for i in stream_ids]}
        scope, max_tasks = f"streams {stream_ids}", 0
    else:
        if not created_after:
            raise ToolError("A feed-wide filter needs created_after, so it does not reprocess the feed's history")
        min_ms = int(datetime.fromisoformat(created_after.replace('Z', '+00:00')).timestamp() * 1000)
        expression = {'type': 'operator', 'op': 'AND', 'children': [_term('Feed', feed), _term('Type', stream_type)]}
        scope, max_tasks = f"feed {feed} ({stream_type}) created after {created_after}", stroom.settings.max_feed_filter_tasks
    details = {'pipeline': pipeline['name'], 'scope': scope, 'priority': priority, 'max tasks': max_tasks or 'unlimited'}
    gate = await consent_from(ctx).require(ctx, 'approval', 'create_processor_filter',
                                           f"Start processing {scope} with pipeline '{pipeline['name']}'", details, approval_id)
    if gate:
        return gate
    created = await _create_filter(stroom, pipeline, expression, priority, max_tasks, min_ms)
    return {'filter_id': created['id'], 'pipeline': pipeline['name'], 'scope': scope, 'enabled': created.get('enabled')}


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


async def _outputs(stroom: StroomGateway, raw_id: int, pipeline_uuid: str) -> list[dict[str, Any]]:
    rows = (await stroom.find_meta([_term('Parent Id', raw_id)], 100)).get('values') or []
    return [r['meta'] for r in rows
            if r['meta'].get('pipelineUuid') == pipeline_uuid and r['meta'].get('status') != 'DELETED']


async def reprocess_streams(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="A pipeline this server created, after its content changed.")],
        stream_ids: Annotated[list[int], Field(description="Raw sample streams to process again.")],
        approval_id: Annotated[str | None, Field(description="From an earlier needs_approval reply.")] = None,
) -> dict[str, Any]:
    """
    Process sample streams again after a fix. The outputs this pipeline produced from them before are
    marked deleted first, so each raw stream ends up with exactly one Events stream. Needs approval.
    """
    stroom = gateway_from(ctx)
    pipeline = await _managed_pipeline(ctx, pipeline_uuid)
    if len(stream_ids) > stroom.settings.max_reprocess_streams:
        raise ToolError(f"At most {stroom.settings.max_reprocess_streams} streams per call")
    superseded = [m['id'] for raw in stream_ids for m in await _outputs(stroom, raw, pipeline_uuid)]
    details = {'pipeline': pipeline['name'], 'streams': stream_ids, 'superseded outputs marked deleted': superseded}
    gate = await consent_from(ctx).require(ctx, 'approval', 'reprocess_streams',
                                           f"Reprocess {len(stream_ids)} stream(s) with '{pipeline['name']}'",
                                           details, approval_id)
    if gate:
        return gate
    if superseded:
        await stroom.request('PUT', '/meta/v1/update/status', {
            'criteria': {'expression': {'type': 'operator', 'op': 'OR', 'children': [_term('Id', i) for i in superseded]}},
            'currentStatus': 'UNLOCKED', 'newStatus': 'DELETED'})
    expression = {'type': 'operator', 'op': 'OR', 'children': [_term('Id', i) for i in stream_ids]}
    created = await _create_filter(stroom, pipeline, expression, 10, 0, None)
    return {'filter_id': created['id'], 'superseded_outputs_deleted': superseded, 'streams': stream_ids}


async def wait_for_processing(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The pipeline that is processing.")],
        stream_ids: Annotated[list[int], Field(description="Input streams to wait for.")],
        timeout_seconds: Annotated[int, Field(ge=5, le=900)] = 180,
        expect_events: Annotated[bool, Field(
            description="True for translation pipelines (one Events stream per input); False for indexing "
                        "pipelines, which write to an index and should produce no Error stream.")] = True,
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
        outputs = {raw: await _outputs(stroom, raw, pipeline_uuid) for raw in stream_ids}
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
            problems.append(f"Stream {raw} produced {len(events)} Events streams: it was processed more than once "
                            "(overlapping filters or a reprocess); ask the user which to keep")
    return {'pipeline': pipeline_uuid, 'finished': finished, 'streams': per_stream,
            'gate': 'pass' if not problems and finished else 'fail', 'problems': problems,
            'hint': None if finished else "Tasks were still running at the timeout; call again."}


ALL_TOOLS = [create_processor_filter, set_processor_filter_enabled, reprocess_streams, wait_for_processing]
