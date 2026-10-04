"""Tools that start processing: processor filters, reprocessing, and waiting for the result.

All of them act only on pipelines this server built in the workspace (the write guard), so reprocessing is
part of developing a pipeline: at most max_reprocess_streams per call, one task at a time. Reprocessing
with a production pipeline is left to the user. When a pipeline processes a stream again, Stroom itself marks
that pipeline's earlier outputs for the stream deleted (superseded); wait_for_processing can count only the
outputs of a given filter while that happens.
"""
import asyncio
import json
import time
from datetime import datetime
from urllib.parse import quote
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import MANAGED, guard_from
from tools.pipelines import merge_layers
from tools.processing import processing_status
from tools.stepping import stepped_clean
from utils.consent import consent_from
from utils.mappingstore import digest, normalise_xslt, read_agreed_template
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


# While a pipeline is developed in the workspace, Elasticsearch indexing runs in small batches: a rejected document
# is then reported with Elasticsearch's own reason, whole, rather than cut short in one large bulk response.
DEV_BATCH_SIZE = 10
_BATCH_NOTE = (f"batch size {DEV_BATCH_SIZE} while developing, so each document Elasticsearch rejects is reported with "
               f"its reason; the template's default comes back once indexing completes without errors")


async def development_batch(ctx: Context, pipeline_uuid: str, small: bool) -> bool:
    """Set the workspace Elasticsearch indexing pipeline's own batchSize to DEV_BATCH_SIZE (small), or remove it so
    the template's (or Stroom's) default applies again. Production pipelines are left alone. True if it changed."""
    stroom = gateway_from(ctx)
    merged = merge_layers(await stroom.pipeline_layers(pipeline_uuid))
    element = next((e['id'] for e in merged['elements'] if e['type'] == 'ElasticIndexingFilter'), None)
    if not element:
        return False
    doc = await stroom.get_doc('Pipeline', pipeline_uuid)
    if MANAGED not in await guard_from(ctx).tags({'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': doc.get('name')}):
        return False
    data = doc.get('pipelineData') or {}
    properties = data.get('properties') or {}
    added = properties.get('add') or []
    own = [p for p in added if p.get('element') == element and p.get('name') == 'batchSize']
    if small and own and (own[0].get('value') or {}).get('integer') == DEV_BATCH_SIZE:
        return False
    if not small and not own:
        return False
    rest = [p for p in added if p not in own]
    properties['add'] = rest + ([{'element': element, 'name': 'batchSize', 'value': {'integer': DEV_BATCH_SIZE}}]
                                if small else [])
    data['properties'] = properties
    doc['pipelineData'] = data
    await stroom.put_doc(doc)
    return True


async def indexing_xslt_digest(stroom: StroomGateway, pipeline_uuid: str) -> str:
    """The XSLT code the pipeline runs, digested: an index template is agreed for the documents this code writes."""
    from tools.pipelines import translation_docs
    parts = []
    for entry in translation_docs(pipeline_uuid, await stroom.pipeline_layers(pipeline_uuid)):
        if entry['doc'].get('type') == 'XSLT':
            parts.append(normalise_xslt((await stroom.get_doc('XSLT', entry['doc']['uuid'])).get('data') or ''))
    return digest(*parts)


async def agreement_problem(stroom: StroomGateway, pipeline: dict[str, Any], destination: dict[str, Any]) -> str | None:
    """Why the Elasticsearch indexing pipeline has no index template agreed with the user for the documents its
    current code writes, or None when it has one."""
    index, cluster = destination['index name'], destination['cluster']
    if not index:
        return "This Elasticsearch indexing pipeline has no indexName set"
    agreed = read_agreed_template(pipeline.get('description'))
    if not agreed or agreed.get('index') != index or agreed.get('cluster') != cluster:
        return (f"No Elasticsearch index template has been agreed with the user for index '{index}' (cluster "
                f"{cluster}). Agreeing it is a step of its own, before any indexing: propose_index_template "
                f"pipeline_uuid={pipeline['uuid']} with the field plan, the Events streams and example_template= the "
                f"example index template (or index mapping) the user gave, exactly as they pasted it, with any component "
                f"templates it is composed of; the user confirms the result in a form. Without an example, ask the "
                f"user for one first. A template put on the cluster without that agreement does not count. Until it is "
                f"agreed, committed and the sample indexed and verified, the build is not ready for promotion.")
    if agreed.get('xslt') != await indexing_xslt_digest(stroom, pipeline['uuid']):
        return (f"The indexing XSLT changed since index template '{agreed['name']}' was agreed: "
                f"check_index_template with it (and its component templates {agreed.get('component_templates') or []}) "
                f"over the Events streams, so the user confirms it again for the documents the pipeline now "
                f"writes. The agreed template:\n{(agreed.get('dev_tools') or '')[:4000]}")
    return None


async def _committed(stroom: StroomGateway, pipeline: dict[str, Any], destination: dict[str, Any]) -> str:
    """Elasticsearch: the index template has to be on the cluster before documents arrive, or the index is created
    with dynamic mappings that the template can no longer change. Only a template the user agreed (kept with the
    pipeline by propose_index_template or check_index_template) is asked about, and the user confirms it is
    committed to the cluster in the approval."""
    problem = await agreement_problem(stroom, pipeline, destination)
    if problem:
        raise ToolError(problem)
    agreed = read_agreed_template(pipeline.get('description'))
    return (f"the agreed index template '{agreed['name']}' for Elasticsearch index '{destination['index name']}' is "
            f"committed to cluster {destination['cluster']}")


async def create_processor_filter(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="A pipeline this server created.")],
        stream_ids: Annotated[list[int] | int | str | None, ONE_OR_MORE, Field(
            description="Process exactly these streams (the sample). The default and safest scope.")] = None,
        feed: Annotated[str | None, Field(description="Or process a whole feed's streams of stream_type.")] = None,
        stream_type: Annotated[str, Field(description="Stream type to process with a feed scope.")] = 'Raw Events',
        created_after: Annotated[str | None, Field(
            description="With a feed scope: only streams created after this ISO time. Required for a feed scope.")] = None,
        priority: Annotated[int, Field(ge=1, le=100)] = 10,
        source_pipeline_uuid: SourcePipeline = None,
        source_confirmation_id: SourceConfirmation = None,
        approval_id: Annotated[str | None, Field(description="From an earlier needs_approval reply.")] = None,
) -> dict[str, Any]:
    """
    Create and enable a processor filter so Stroom processes streams through the pipeline. Scope it to the
    sample stream ids; a whole-feed scope needs a created_after bound and is limited to the configured task
    count. Streams the pipeline already processed are refused: use reprocess_streams for those.
    An indexing pipeline reading Events needs source_pipeline_uuid, and its filter only selects Events from
    exactly that events pipeline. For an Elasticsearch indexing pipeline, the index template for its destination
    index must have been agreed with the user (propose_index_template, check_index_template), and the approval
    asks the user to confirm it is committed to the cluster, so the index is created with it; processing then
    starts. Filters are enabled after the user's approval.
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
    summary = f"Start processing {scope} with pipeline '{pipeline['name']}'"
    if destination:
        committed = await _committed(stroom, pipeline, destination)
        details['index template'] = committed
        summary = f"{committed[0].upper()}{committed[1:]}: start indexing {scope} with pipeline '{pipeline['name']}'"
    gate = await consent_from(ctx).require(ctx, 'approval', 'create_processor_filter', summary, details, approval_id)
    if gate:
        return gate
    small = bool(destination) and await development_batch(ctx, pipeline_uuid, True)
    created = await _create_filter(stroom, pipeline, expression, priority, max_tasks, min_ms)
    consent_from(ctx).discard(source_confirmation_id)
    from tools.plan import build_of, with_next
    result = {'filter_id': created['id'], 'pipeline': pipeline['name'], 'scope': scope, 'enabled': created.get('enabled'),
              **({'events_from_pipeline': source['name']} if source else {}),
              **({'batch_size': _BATCH_NOTE} if small else {})}
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
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Streams it already processed, to process again.")],
        source_pipeline_uuid: SourcePipeline = None,
        source_confirmation_id: SourceConfirmation = None,
        approval_id: Annotated[str | None, Field(description="From an earlier needs_approval reply.")] = None,
) -> dict[str, Any]:
    """
    Process streams again through a pipeline under development, after changing it. At most
    max_reprocess_streams (default 10) per call, run one task at a time. Stroom marks the pipeline's earlier
    outputs for these streams deleted once the new ones are written; pass the returned filter_id to
    wait_for_processing so only the new outputs count. Needs approval; for Elasticsearch indexing the user
    approval also confirms the index template is applied on the cluster.
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
        details['index template'] = await _committed(stroom, pipeline, destination)
        # Stroom's purgeOnReprocess does not apply to a second filter on the same streams, and this server has no
        # Elasticsearch access: the earlier documents stay unless the cluster admin deletes them first (after
        # reprocessing, the same request would delete the new ones too).
        delete = json.dumps({'query': {'terms': {'StreamId': [int(i) for i in stream_ids]}}})
        details['already indexed'] = (f"Stroom does not remove the documents these streams already put in "
                                      f"'{destination['index name']}': have the cluster admin delete them first, or "
                                      f"they are indexed twice: POST {destination['index name']}/_delete_by_query "
                                      f"{delete}")
    gate = await consent_from(ctx).require(ctx, 'approval', 'reprocess_streams',
                                           f"Reprocess {len(stream_ids)} stream(s) with '{pipeline['name']}'",
                                           details, approval_id)
    if gate:
        return gate
    small = bool(destination) and await development_batch(ctx, pipeline_uuid, True)
    created = await _create_filter(stroom, pipeline, expression, 10, max_tasks, None)
    return {'filter_id': created['id'], 'pipeline': pipeline['name'], 'streams': stream_ids, 'max_tasks': max_tasks,
            **({'batch_size': _BATCH_NOTE} if small else {}),
            'hint': f"wait_for_processing with filter_id={created['id']} so only this run's outputs count."}


async def wait_for_processing(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The pipeline that is processing.")],
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Input streams to wait for.")],
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
    from tools.plan import build_of, with_next
    while True:
        status = await processing_status(ctx, pipeline_uuid)
        if not status['filters']:
            # Nothing was set to process: waiting would only run out the timeout, as if tasks were running.
            return await with_next(ctx, await build_of(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid}), {
                'pipeline': pipeline_uuid, 'finished': False, 'streams': [], 'gate': 'fail',
                'problems': ["This pipeline has no processor filter, so nothing is processing and there is nothing to "
                             "wait for."],
                'hint': "create_processor_filter first. If it was refused, do what its message says; don't wait."})
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
    result = {'pipeline': pipeline_uuid, 'finished': finished, 'streams': per_stream,
              'gate': 'pass' if not problems and finished else 'fail', 'problems': problems,
              'hint': None if finished else "Tasks were still running at the timeout; call again."}
    try:
        restored = result['gate'] == 'pass' and await development_batch(ctx, pipeline_uuid, False)
    except Exception:       # housekeeping: never a reason for the wait to fail
        restored = False
    if restored:
        # Indexing completed without errors: the small development batch was only to see Elasticsearch's responses.
        result['batch_size'] = "restored to the template's default, now that indexing completed without errors"
    return await with_next(ctx, await build_of(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid}), result)


ALL_TOOLS = [create_processor_filter, set_processor_filter_enabled, reprocess_streams, wait_for_processing]
