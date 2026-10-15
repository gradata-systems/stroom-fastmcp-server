"""Tools that start processing: processor filters, reprocessing, and waiting for the result.

All of them act only on pipelines this server built in the workspace (the write guard), so reprocessing is
part of developing a pipeline: at most max_reprocess_streams per call, one task at a time. Reprocessing
with a production pipeline is left to the user. When a pipeline processes a stream again, Stroom itself marks
that pipeline's earlier outputs for the stream deleted (superseded); wait_for_processing can count only the
outputs of a given filter while that happens.
"""
import asyncio
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import MANAGED, guard_from
from tools.pipelines import merge_layers
from tools.processing import processing_status
from tools.streams import feed_created_ms, own_streams, refuse_older_than_feed
from tools.stepping import stepped_clean
from utils.consent import consent_from
from utils import useractions
from utils.mappingstore import digest, normalise_xslt, read_agreed_template
from utils.params import ONE_OR_MORE
from utils.stroom import StroomGateway, doc_link, gateway_from

STREAM_STORE = {'type': 'StreamStore', 'uuid': '0', 'name': 'StreamStore'}
# The pipelines whose reprocessing in a build runs on the user's approval (asked for by the user): events (or records)
# and indexing. Every other filter is created disabled, for the user to enable.
REPROCESS_STAGES = ('translation', 'records', 'indexing', 'discovery')
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
                         priority: int, max_tasks: int, min_create_ms: int | None, enabled: bool) -> dict[str, Any]:
    had_processor = bool(await stroom.processor_filters(pipeline['uuid']))
    created = await stroom.post('/processorFilter/v1', {
        'pipeline': {'type': 'Pipeline', 'uuid': pipeline['uuid'], 'name': pipeline['name']},
        'processorType': 'PIPELINE', 'enabled': enabled, 'priority': priority, 'autoPriority': False,
        'reprocess': False, 'export': False, 'maxProcessingTasks': max_tasks,
        'minMetaCreateTimeMs': min_create_ms,
        'queryData': {'dataSource': STREAM_STORE, 'expression': expression}})
    # Stroom gives a pipeline's new processor its first filter's enabled flag, and a disabled processor runs none of
    # its filters: the user enabling the filter alone saw nothing processed. A processor whose filters are all
    # disabled runs nothing, so the one this filter made is enabled and enabling stays the filter's, the user's
    # choice. One already there keeps its state, unless the user approved this run.
    processor = created.get('processor') or {}
    if processor.get('id') and processor.get('enabled') is False and (enabled or not had_processor):
        await stroom.request('PUT', f"/processor/v1/{processor['id']}/enabled", True)
    return created


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
    selected = set()
    for f in await stroom.processor_filters(pipeline_uuid):
        selected |= _selected_ids((f.get('queryData') or {}).get('expression'))
    return [i for i in stream_ids if i in selected or await _outputs(stroom, i, pipeline_uuid)]


async def _feed_term(stroom: StroomGateway, feed: str) -> dict[str, Any]:
    """The filter's condition on its feed, by reference (Feed IS_DOC_REF), not by name: Stroom then keeps it as a
    dependency of the feed, and a renamed feed is still the one selected (asked for by the user)."""
    try:
        ref = await stroom.get(f'/feed/v1/getDocRefForName/{quote(feed, safe="")}')
    except ToolError as e:
        raise ToolError(f"No feed named '{feed}' in Stroom: give the feed's exact name") from e
    if not (ref or {}).get('uuid'):
        raise ToolError(f"No feed named '{feed}' in Stroom: give the feed's exact name")
    return {'type': 'term', 'field': 'Feed', 'condition': 'IS_DOC_REF',
            'docRef': {'type': 'Feed', 'uuid': ref['uuid'], 'name': ref.get('name') or feed}}


def _pipeline_term(ref: dict[str, Any]) -> dict[str, Any]:
    return {'type': 'term', 'field': 'Pipeline', 'condition': 'IS_DOC_REF',
            'docRef': {'type': 'Pipeline', 'uuid': ref['uuid'], 'name': ref['name']}}


async def feed_stream_type(stroom: StroomGateway, feed: str) -> str:
    """The stream type a feed receives (Raw Events, Raw Reference, ...), from the feed itself."""
    try:
        ref = await stroom.get(f'/feed/v1/getDocRefForName/{quote(feed, safe="")}')
        return (await stroom.get_doc('Feed', ref['uuid'])).get('streamType') or 'Raw Events'
    except Exception:
        return 'Raw Events'


async def output_stream_type(stroom: StroomGateway, pipeline_uuid: str) -> str:
    """The stream type a pipeline writes, from its stream appender (Events, Reference, Records, ...)."""
    try:
        merged = merge_layers(await stroom.pipeline_layers(pipeline_uuid))
    except Exception:
        return 'Events'
    types = {e['id']: e['type'] for e in merged['elements']}
    for prop in merged['properties']:
        if types.get(prop['element']) == 'StreamAppender' and prop['name'] == 'streamType' and prop.get('value'):
            value = prop['value']
            return value if isinstance(value, str) else str((value or {}).get('string') or value)
    return 'Events'


async def _sends_to_kafka(stroom: StroomGateway, pipeline_uuid: str) -> bool:
    try:
        merged = merge_layers(await stroom.pipeline_layers(pipeline_uuid))
    except Exception:
        return False
    return any(e['type'] == 'StandardKafkaProducer' for e in merged['elements'])


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
    await _refuse_superseded(stroom, metas)
    foreign = [m for m in metas if m.get('pipelineUuid') != source_uuid]
    missing = sorted(set(stream_ids or []) - {m['id'] for m in metas})
    if foreign or missing:
        producers = sorted({m.get('pipelineUuid') for m in foreign if m.get('pipelineUuid')})
        names = []
        for uuid in producers[:3]:
            try:
                names.append(f"'{(await stroom.get_doc('Pipeline', uuid)).get('name')}' ({uuid})")
            except Exception:       # the name is extra: the refusal stands without it
                names.append(uuid)
        raise ToolError(f"Stream(s) {sorted(m['id'] for m in foreign) + missing} were not produced by '{source['name']}'"
                        + (f" ({missing} not found)" if missing else '')
                        + (f"; they were produced by {', '.join(names)}: if the events pipeline was updated as a new "
                           f"version, give that version as source_pipeline_uuid" if names else '')
                        + ": only the source pipeline's Events can be indexed here")
    return {'type': 'Pipeline', 'uuid': source['uuid'], 'name': source['name']}


async def _refuse_superseded(stroom: StroomGateway, metas: list[dict[str, Any]]) -> None:
    """Events streams Stroom marked deleted when their raw stream was processed again (an events pipeline updated
    and reprocessed), named with the Events now in their place. Seen in production: after the parent events pipeline
    was updated, indexing was given the earlier Events streams' ids, and the filter selected streams Stroom no longer
    processes."""
    gone = [m for m in metas if str(m.get('status') or '').upper() == 'DELETED']
    if not gone:
        return
    current: dict[int, list[int]] = {}
    for m in gone:
        parent = m.get('parentMetaId')
        if parent is None:
            continue
        rows = (await stroom.find_meta([_term('Parent Id', parent), _term('Type', m.get('typeName') or 'Events')],
                                       20)).get('values') or []
        current[m['id']] = sorted(r['meta']['id'] for r in rows
                                  if str(r['meta'].get('status') or '').upper() != 'DELETED'
                                  and r['meta'].get('pipelineUuid') == m.get('pipelineUuid'))
    told = '; '.join(f"{i} -> {current.get(i) or 'none yet'}" for i in sorted(m['id'] for m in gone))
    raise ToolError(f"Events stream(s) {sorted(m['id'] for m in gone)} were superseded: their raw streams were processed "
                    f"again (the events pipeline was updated), and Stroom marked them deleted. The Events now in their "
                    f"place: {told}. Use those ids; 'none yet' means processing hasn't finished (wait_for_processing on "
                    f"the events pipeline first).")


async def _build_feeds_only(ctx: Context, pipeline: dict[str, Any], stream_ids: list[int] | None,
                            feed: str | None) -> dict[str, Any] | None:
    """Translation pipelines write their Events into the input's feed, so they only process the build's feeds, or the
    existing feeds the build works from: their Events then go to the build's test feed (output_feed), and the output
    feed is returned.

    Indexing pipelines write to an index, not to a feed, so they are not limited this way.
    """
    stroom = gateway_from(ctx)
    if await _is_indexing(stroom, pipeline['uuid']):
        return
    guard = guard_from(ctx)

    async def build_of(ref: dict[str, Any]) -> str | None:
        return await guard.build_of(ref) if MANAGED in await guard.tags(ref) else None
    build = await build_of({'type': 'Pipeline', 'uuid': pipeline['uuid'], 'name': pipeline['name']})
    feeds = {feed} if feed else {r['meta'].get('feedName') for r in (await stroom.find_meta(
        [_term('Id', i) for i in stream_ids or []], len(stream_ids or []), op='OR')).get('values') or []}
    outside = []
    from tools.feeds import feeds_named
    for name in sorted(f for f in feeds if f):
        ref = await stroom.get(f'/feed/v1/getDocRefForName/{quote(name, safe="")}')
        if not build or not ref or await build_of(ref) != build:
            # Stream data names a feed as Stroom first spelt it: the build's feed may be the same name in another case.
            twins = [f for f in await feeds_named(stroom, name) if f['uuid'] != (ref or {}).get('uuid')]
            in_build = False
            for twin in twins:
                in_build = in_build or bool(build and await build_of(twin) == build)
            if not in_build:
                outside.append(name)
    if outside and build:
        from tools.plan import works_from
        sources = await works_from(ctx, build)
        if {o.lower() for o in outside} <= {s.lower() for s in sources}:
            # Asked for by the user: an existing feed's streams are processed where they are, their Events written to
            # a test feed in the build. Seen before (Haiku, asked to onboard an existing feed): refused here and told
            # to copy the records into a test feed, it made one; with sample files in the workspace, those were what
            # it would have uploaded.
            return await output_feed(ctx, pipeline, sources, build)
    if outside:
        raise ToolError(f"Feed(s) {outside} are not in this build. A translation pipeline writes its Events into the "
                        f"input's feed, so it only processes the build's own feeds, or the existing feeds the build "
                        f"works from: start_build build=<this build> feeds=[the feed] makes it work from that feed, "
                        f"and its Events then go to a test feed in the build. Or step the records where they are "
                        f"(step_records). Create no feed and upload nothing for them.")
    return None


async def output_feed(ctx: Context, pipeline: dict[str, Any], sources: list[str], build: str) -> dict[str, Any] | None:
    """The build's test feed for a pipeline processing the existing feeds it works from (asked for by the user): the
    pipeline's StreamAppender writes there (its feed property) instead of into the production feed. Made here, with the
    source feed's settings, holding only output: nothing is uploaded to it. Promotion leaves it in the workspace and
    takes the property off (restore_output_feed). None for a pipeline with no StreamAppender (nothing written to a
    feed)."""
    stroom = gateway_from(ctx)
    guard = guard_from(ctx)
    merged = merge_layers(await stroom.pipeline_layers(pipeline['uuid']))
    appenders = [e['id'] for e in merged['elements'] if e['type'] == 'StreamAppender']
    if not appenders:
        return None
    from tools.builds import TEST_FEED
    name = f"{sources[0]}{TEST_FEED}"
    found = next((d for d in await guard.folder_contents(build)
                  if d['type'] == 'Feed' and d['name'].lower() == name.lower()), None)
    made = False
    if found is None:
        source = await stroom.get(f'/feed/v1/getDocRefForName/{quote(sources[0], safe="")}')
        settings = await stroom.get_doc('Feed', source['uuid']) if source else {}
        ref = await guard.create('Feed', name, build)
        doc = await stroom.get_doc('Feed', ref['uuid'])
        doc.update(encoding=settings.get('encoding') or 'UTF-8', streamType=settings.get('streamType') or 'Raw Events',
                   description=f"Build '{build}': the Events its pipelines make from {', '.join(sources)}'s streams. "
                               f"Nothing is uploaded to it; it stays in the workspace at promotion.")
        found = await stroom.put_doc(doc)
        from security import record
        await record.update(guard, build, {'type': 'Feed', 'uuid': found['uuid'], 'name': found['name']},
                            lambda entry: entry.update(output_for=sources))
        made = True
    feed_ref = {'type': 'Feed', 'uuid': found['uuid'], 'name': found['name']}
    doc = await stroom.get_doc('Pipeline', pipeline['uuid'])
    data = doc.get('pipelineData') or {}
    added = (data.get('properties') or {}).get('add') or []
    have = {p['element'] for p in added if p.get('name') == 'feed' and p['element'] in appenders
            and ((p.get('value') or {}).get('entity') or {}).get('uuid') == found['uuid']}
    if set(appenders) - have:
        from tools.pipeline_writes import _set_property
        for element in appenders:
            _set_property(data, element, 'feed', {'entity': feed_ref})
        doc['pipelineData'] = data
        await stroom.put_doc(doc)
    return {'name': found['name'], 'uuid': found['uuid'], 'made': made, 'works_from': sources}


async def restore_output_feed(ctx: Context, pipeline_uuid: str, build_feeds: set[str]) -> bool:
    """Take a promoted pipeline's StreamAppender feed property off where it names one of the build's feeds: in
    production its Events go into the input's feed, as Stroom does by default. True if it changed."""
    stroom = gateway_from(ctx)
    merged = merge_layers(await stroom.pipeline_layers(pipeline_uuid))
    appenders = {e['id'] for e in merged['elements'] if e['type'] == 'StreamAppender'}
    doc = await stroom.get_doc('Pipeline', pipeline_uuid)
    data = doc.get('pipelineData') or {}
    properties = data.get('properties') or {}
    added = properties.get('add') or []
    kept = [p for p in added if not (p.get('element') in appenders and p.get('name') == 'feed'
                                     and ((p.get('value') or {}).get('entity') or {}).get('uuid') in build_feeds)]
    if len(kept) == len(added):
        return False
    properties['add'] = kept
    data['properties'] = properties
    doc['pipelineData'] = data
    await stroom.put_doc(doc)
    return True


async def elastic_destination(stroom: StroomGateway, pipeline_uuid: str) -> dict[str, Any] | None:
    """Cluster and index name an Elasticsearch indexing pipeline writes to; None for other pipelines."""
    merged = merge_layers(await stroom.pipeline_layers(pipeline_uuid))
    elements = {e['id'] for e in merged['elements'] if e['type'] == 'ElasticIndexingFilter'}
    if not elements:
        return None
    props = {p['name']: p['value'] for p in merged['properties'] if p['element'] in elements}
    return {'index name': props.get('indexName'), 'cluster': (props.get('cluster') or {}).get('name')}


async def lucene_index(stroom: StroomGateway, pipeline_uuid: str) -> dict[str, Any] | None:
    """The Lucene index doc an indexing pipeline writes to; None for other pipelines."""
    merged = merge_layers(await stroom.pipeline_layers(pipeline_uuid))
    elements = {e['id'] for e in merged['elements'] if e['type'] == 'IndexingFilter'}
    ref = next((p['value'] for p in merged['properties'] if p['element'] in elements and p['name'] == 'index'), None)
    return ref if isinstance(ref, dict) and ref.get('uuid') else None


async def _own_index(ctx: Context, pipeline_uuid: str, index: dict[str, Any]) -> bool:
    """Whether a Lucene index is the build's own and no other pipeline of the build writes to it."""
    stroom, guard = gateway_from(ctx), guard_from(ctx)
    build = await guard.build_of({'type': index.get('type'), 'uuid': index['uuid'], 'name': index.get('name')})
    if not build:
        return False
    for doc in await guard.folder_contents(build):
        if doc['type'] == 'Pipeline' and doc['uuid'] != pipeline_uuid:
            other = await lucene_index(stroom, doc['uuid'])
            if other and other['uuid'] == index['uuid']:
                return False
    return True


def drop_request(pipeline: dict[str, Any], index_name: str) -> str:
    """The Dev Tools request dropping an Elasticsearch index: its data stream, as the agreed template makes it, or the
    index."""
    agreed = read_agreed_template(pipeline.get('description')) or {}
    try:
        body = json.loads((agreed.get('dev_tools') or '').split('\n', 1)[1])
    except (IndexError, ValueError):
        return f"DELETE _data_stream/{index_name} (a data stream), or DELETE {index_name} (an index)"
    return f"DELETE _data_stream/{index_name}" if 'data_stream' in body else f"DELETE {index_name}"


async def _fresh_index(ctx: Context, pipeline: dict[str, Any], destination: dict[str, Any] | None,
                       lucene: dict[str, Any] | None) -> tuple[dict[str, str], bool]:
    """(details, empty): what an index this pipeline already wrote to needs before it indexes again, and whether the
    server empties it once the user approves. Seen in testing: the Events translation amended and its sample processed
    again, indexing the new Events left the superseded Events' documents beside them. The whole index is dropped and
    starts fresh (asked for by the user: simpler than deleting by stream, and no tracking of superseded streams, which
    may have aged off anyway)."""
    every = ("every document goes with it, so this processes all the Events streams the index should hold "
             "(reprocess_streams takes those this pipeline indexed before)")
    if destination:
        return {'already indexed': (f"'{destination['index name']}' holds what this pipeline indexed before: the "
                                    f"cluster admin drops it first, so it starts fresh: "
                                    f"{drop_request(pipeline, destination['index name'])}. The agreed template stays "
                                    f"on the cluster and makes the index again as documents arrive; {every}")}, False
    if lucene and await _own_index(ctx, pipeline['uuid'], lucene):
        return {'earlier documents': (f"deleted first: '{lucene.get('name')}' is this build's index, emptied so it starts "
                                      f"fresh (its shards are deleted); {every}")}, True
    if lucene:
        return {'already indexed': (f"'{lucene.get('name')}' holds what this pipeline indexed before, and isn't this "
                                    f"build's own (or another pipeline writes to it), so it isn't emptied: delete its "
                                    f"shards (the index's Shards tab) first, or it holds those events twice")}, False
    return {}, False


async def _delete_index_shards(stroom: StroomGateway, index_uuid: str) -> int:
    """Delete a Lucene index's shards, and with them every document in it; returns how many documents they held."""
    only = {'indexUuidSet': {'matchAll': False, 'set': [index_uuid]}}
    found = await stroom.post('/index/v2/shard/find', {'pageRequest': {'offset': 0, 'length': 1000}, **only})
    shards = [s for s in found.get('values') or [] if s.get('indexUuid') == index_uuid and s.get('status') != 'DELETED']
    nodes: dict[str, list[int]] = {}
    for s in shards:
        nodes.setdefault(s.get('nodeName') or '', []).append(s['id'])
    for node, ids in nodes.items():
        await stroom.post(f"/index/v2/shard/delete?nodeName={quote(node, safe='')}",
                          {'pageRequest': {'offset': 0, 'length': 1000}, **only,
                           'indexShardIdSet': {'matchAll': False, 'set': ids}})
    return sum(s.get('documentCount') or 0 for s in shards)


def filter_feeds(expression: dict[str, Any] | None) -> list[str]:
    """The feeds a filter's expression names (Feed IS_DOC_REF, or Feed EQUALS as earlier filters had it)."""
    out = []
    for child in (expression or {}).get('children') or []:
        if child.get('type') == 'operator':
            out += filter_feeds(child)
        elif child.get('field') == 'Feed':
            name = (child.get('docRef') or {}).get('name') or child.get('value')
            if name:
                out.append(str(name))
    return out


def _pipeline_terms(expression: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for child in (expression or {}).get('children') or []:
        if child.get('type') == 'operator':
            out += _pipeline_terms(child)
        elif child.get('field') == 'Pipeline':
            out.append(child)
    return out


async def promotion_processing(ctx: Context, pipelines: list[dict[str, Any]], sources: list[str],
                               test_feeds: list[str] = ()) -> list[dict[str, Any]]:
    """What each promoted pipeline should go on processing: the feeds and stream types of its sample filters.

    A build on data already in Stroom works from its source feeds (the feed it surveyed, the one a production
    pipeline it copies processes, those start_build was given): a translation pipeline processes those, never the
    test feeds that held its copies of their records (seen in VS Code: the promoted filter selected the test feed,
    named like any other). A pipeline built by stepping only has no sample filters: it processes the source feeds.
    """
    stroom = gateway_from(ctx)
    tests = {f.lower() for f in test_feeds}
    plan = []
    for pipeline in pipelines:
        own = await stroom.processor_filters(pipeline['uuid'])
        targets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for f in own:
            expression = (f.get('queryData') or {}).get('expression')
            ids = sorted(_selected_ids(expression))
            metas = [r['meta'] for r in (await stroom.find_meta([_term('Id', i) for i in ids], len(ids), op='OR')
                                         ).get('values') or []] if ids else []
            for meta in metas:
                targets.setdefault((meta.get('feedName'), meta.get('typeName')), _pipeline_terms(expression))
        indexing = await _is_indexing(stroom, pipeline['uuid'])
        if not indexing:
            tested = [key for key, extra in targets.items() if not extra and _test_feed(key[0], tests)]
            for key in tested:
                del targets[key]
            if not targets:
                for feed in sources:
                    # The feed's own stream type (a reference feed's is Raw Reference), not Raw Events assumed.
                    targets.setdefault((feed, await feed_stream_type(stroom, feed)), [])
        by_source: set[tuple] = set()
        for (feed, stream_type), extra in sorted(targets.items(), key=lambda t: (t[0][0] or '', t[0][1] or '')):
            sources_of = tuple(sorted((t.get('docRef') or {}).get('uuid') or '' for t in extra if t.get('field') == 'Pipeline'))
            if sources_of:
                # Named by its source pipeline, not its feed: one filter whatever feeds the sample was in (a test
                # feed among them), as the pipeline's Events are those meant.
                if (stream_type, sources_of) not in by_source:
                    by_source.add((stream_type, sources_of))
                    plan.append({'pipeline': pipeline, 'feed': None, 'stream_type': stream_type, 'extra_terms': extra})
            elif feed and not _test_feed(feed, tests):
                plan.append({'pipeline': pipeline, 'feed': feed, 'stream_type': stream_type, 'extra_terms': extra})
    return plan


def _test_feed(feed: str | None, tests: set[str]) -> bool:
    return bool(feed) and (feed.lower() in tests or feed.endswith('-MCP-TEST'))


async def create_promotion_filters(ctx: Context, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Feed-wide filters for promoted pipelines, created disabled for the user. No minimum stream creation time (asked
    for by the user): enabled, a filter processes the feed's data, existing data included, and what the pipeline
    processed in its build too: Stroom does those again for a new filter. An index starts fresh: dropped (or emptied)
    before its filter is enabled, it holds each event once (asked for by the user, in place of leaving out the streams
    processed in the build, which an index would otherwise hold twice). A pipeline with an enabled filter already
    selecting what arrives is given none (asked for by the user): a second would process everything twice."""
    stroom = gateway_from(ctx)
    made: list[dict[str, Any]] = []
    for entry in plan:
        link = doc_link(stroom.settings, 'Pipeline', entry['pipeline']['uuid'])
        covering = await _covering_filter(stroom, entry)
        if covering:
            made.append({'filter_id': covering['id'], 'pipeline': entry['pipeline']['name'], 'feed': entry['feed'],
                         'stream_type': entry['stream_type'], 'enabled': True, 'existing': True,
                         'note': f"no new filter: enabled filter {covering['id']} already selects what arrives",
                         'pipeline_link': link})
            continue
        expression = {'type': 'operator', 'op': 'AND', 'children': (
            [await _feed_term(stroom, entry['feed'])] if entry['feed'] else []) + [
            _term('Type', entry['stream_type']), *entry['extra_terms']]}
        created = await _create_filter(stroom, entry['pipeline'], expression, 10, stroom.settings.max_feed_filter_tasks,
                                       None, enabled=False)
        before, first = await _before_promoted_indexing(stroom, entry['pipeline'])
        selects = (f"processes feed {entry['feed']} ({entry['stream_type']}), existing data included" if entry['feed']
                   else f"processes {entry['stream_type']} from its source pipeline, existing data included")
        made.append({'filter_id': created['id'], 'pipeline': entry['pipeline']['name'], 'feed': entry['feed'],
                     'stream_type': entry['stream_type'], 'enabled': False,
                     **({'before_enabling': before} if before else {}), 'pipeline_link': link,
                     'user_actions': [a for a in (first, useractions.enable_filter(
                         stroom.settings, created['id'], entry['pipeline'], selects,
                         [first['id']] if first else [])) if a]})
    # An index reading the Events of a pipeline promoted with it: the events pipeline's new filter goes first. Seen in
    # e2e: both enabled together, the indexing filter took the build's Events while they were still current, then
    # their re-translation too, and the index held the sample twice.
    new = {entry['pipeline']['uuid']: f for entry, f in zip(plan, made) if not f.get('existing')}
    for entry, f in zip(plan, made):
        sources = [(t.get('docRef') or {}).get('uuid') for t in entry['extra_terms'] if t.get('field') == 'Pipeline']
        first = next((new[u] for u in sources if u in new), None)
        if f.get('before_enabling') and first:
            f['before_enabling'] += (f"; and only once filter {first['filter_id']} (pipeline '{first['pipeline']}') "
                                     f"has processed what the build processed again: enabled before that, it indexes "
                                     f"those Events too, then their replacements")
            f['user_actions'][-1].setdefault('after', []).extend(
                [f"enable-{first['filter_id']}",
                 f"filter {first['filter_id']} has processed what the build processed again"])
    return made


async def _covering_filter(stroom: StroomGateway, entry: dict[str, Any]) -> dict[str, Any] | None:
    """An enabled filter of the pipeline (its processor on) that selects the streams arriving from now on for this
    entry: its feed and type, Events from its source pipeline. A filter by stream id (a build's sample) doesn't."""
    sources = [(t.get('docRef') or {}).get('uuid') for t in entry['extra_terms'] if t.get('field') == 'Pipeline']
    arriving = {'id': -1, 'feedName': entry['feed'] or '', 'typeName': entry['stream_type'],
                'pipelineUuid': sources[0] if sources else None, 'createMs': int(time.time() * 1000)}
    for f in await stroom.processor_filters(entry['pipeline']['uuid']):
        if (f.get('processor') or {}).get('enabled') is False:
            continue
        expression = (f.get('queryData') or {}).get('expression')
        if not entry['feed'] and not _names_pipeline(expression):
            continue    # any feed's Events: only a filter naming the source pipeline is the same selection
        if _will_process(f, arriving):
            return f
    return None


def _names_pipeline(expression: dict[str, Any] | None) -> bool:
    return any(c.get('field') == 'Pipeline' or (c.get('type') == 'operator' and _names_pipeline(c))
               for c in (expression or {}).get('children') or [])


async def _before_promoted_indexing(stroom: StroomGateway,
                                    pipeline: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """(what, action): what a promoted indexing pipeline's index needs before its filter is enabled, dropped or
    emptied so it starts fresh, and the button for it; (None, None) for any other pipeline."""
    destination = await elastic_destination(stroom, pipeline['uuid'])
    if destination:
        doc = await stroom.get_doc('Pipeline', pipeline['uuid'])
        request = drop_request(doc, destination['index name'])
        return ((f"the cluster admin drops '{destination['index name']}' first, so it starts fresh with what this "
                 f"pipeline indexes, the Events indexed in the build among them: {request}. The agreed template stays "
                 f"on the cluster and makes the index again as documents arrive"),
                useractions.es_request(f"drop-{destination['index name']}", request,
                                       f"Drop '{destination['index name']}' so it starts fresh"))
    lucene = await lucene_index(stroom, pipeline['uuid'])
    if lucene:
        return ((f"delete the shards of index '{lucene.get('name')}' first (its Shards tab), so it starts fresh with "
                 f"what this pipeline indexes, the Events indexed in the build among them"),
                useractions.open_document(stroom.settings, {**lucene, 'type': lucene.get('type') or 'Index'},
                                          'delete its shards on the Shards tab, so it starts fresh'))
    return None, None


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
                f"templates it is composed of; the user confirms the result in a form. If you showed them a template "
                f"for review (needs_review) and they've replied, that is the same call with reviewed=true, nothing "
                f"left out: the form is still to come. Without an example, ask the user for one first. A template put "
                f"on the cluster without that agreement does not count (if they've committed it already, it needs "
                f"agreeing all the same, and recommitting only if they change it). Until it is "
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


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


async def _feed_bounds(stroom: StroomGateway, pipeline_uuid: str, feed: str, stream_type: str,
                       min_ms: int) -> tuple[int, str]:
    """A feed-wide filter's lower bound, kept to the feed's own streams.

    Seen (VS Code), the same day: Gemma guessed created_after at midnight after its sample arrived, so the filter
    selected nothing and the agent waited out ten minutes; Qwen chose a date before the feed was made, which also
    selects the streams of an earlier, deleted feed of that name. Raised to the feed's creation; a translation
    pipeline's filter is refused when the feed has streams of its own that nothing has processed or will, and it
    selects none of them. A filter for new data only, once the feed's streams are processed, is left alone.
    """
    created = await feed_created_ms(stroom, feed)
    said = _iso(min_ms)
    if created and min_ms < created:
        min_ms, said = created, f"{_iso(created)} (when the feed was created: streams before then belong to an earlier feed of that name)"
    rows = (await stroom.find_meta([_term('Feed', feed), _term('Type', stream_type)], 100)).get('values') or []
    own = [m for m in own_streams([r['meta'] for r in rows if r['meta'].get('status') != 'DELETED'], created)
           if m.get('createMs')]
    if not own or any(m['createMs'] >= min_ms for m in own) or stream_type == 'Events'             or await _is_indexing(stroom, pipeline_uuid):
        # A pipeline reading Events (indexing, CEF output) may well take new data only: a new index version, the older
        # Events in the old one; ArcSight from now on.
        return min_ms, said
    filters = await stroom.processor_filters(pipeline_uuid)
    handled = set(await _already_processed(stroom, pipeline_uuid, [m['id'] for m in own]))
    waiting = [m for m in own if m['id'] not in handled and not any(_will_process(f, m) for f in filters)]
    if waiting:
        listed = ', '.join(f"{m['id']} ({_iso(m['createMs'])})" for m in sorted(waiting, key=lambda m: m['createMs']))
        raise ToolError(f"A filter on feed {feed} for {stream_type} created after {_iso(min_ms)} selects none of the "
                        f"feed's streams still to process, which were all created before then: {listed}. To process "
                        f"the sample, give stream_ids; for the whole feed, created_after at or before the earliest of "
                        f"them.")
    return min_ms, said


async def _refuse_duplicate(stroom: StroomGateway, pipeline_uuid: str, expression: dict[str, Any], min_ms: int | None) -> None:
    """Seen (Gemma, VS Code): the same feed-wide filter made twice, neither selecting anything."""
    from tools.processing import _terms
    # A feed by name (filters made before) or by reference is the same feed.
    def key(e):
        return sorted(re.sub(r'^Feed (EQUALS|IS_DOC_REF) (.*)$', lambda m: f'Feed {m.group(2).lower()}', t)
                      for t in _terms(e))
    wanted = key(expression)
    for f in await stroom.processor_filters(pipeline_uuid):
        theirs = (f.get('queryData') or {}).get('expression')
        earliest = f.get('minMetaCreateTimeMs')
        if key(theirs) == wanted and (earliest or 0) <= (min_ms or 0):
            raise ToolError(f"Processor filter {f['id']} on this pipeline already selects these streams"
                            + (f" (created after {_iso(earliest)})" if earliest else '') + ": processing_status shows "
                            f"what it matched. If it matched nothing, the streams are not what it selects: a new filter "
                            f"that selects them is needed, not the same one again.")


async def create_processor_filter(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="A pipeline this server created.")],
        stream_ids: Annotated[list[int] | int | str | None, ONE_OR_MORE, Field(
            description="Process exactly these streams (the sample). The default and safest scope.")] = None,
        feed: Annotated[str | None, Field(description="Or process a whole feed's streams of stream_type.")] = None,
        stream_type: Annotated[str | None, Field(description=(
            "Stream type to process with a feed scope. Left out: Events for a pipeline that indexes Events, else "
            "the feed's own (Raw Events, Raw Reference, ...)."))] = None,
        created_after: Annotated[str | None, Field(
            description="With a feed scope: only streams created after this ISO time. Required for a feed scope. "
                        "Raised to the feed's creation time if earlier; refused if it selects none of the feed's "
                        "streams still to process. To process the sample, give stream_ids instead.")] = None,
        priority: Annotated[int, Field(ge=1, le=100)] = 10,
        source_pipeline_uuid: SourcePipeline = None,
        source_confirmation_id: SourceConfirmation = None,
        approval_id: Annotated[str | None, Field(description="From an earlier needs_approval reply.")] = None,
) -> dict[str, Any]:
    """
    Create a processor filter for Stroom to process streams through the pipeline. Scope it to the sample stream
    ids: for an events (or records) or indexing pipeline the user approves it in a form, then it is created enabled
    and processing starts. A whole-feed scope, or any other pipeline (CEF output, reference data), is created
    disabled, for the user to enable in Stroom (the pipeline's Processors tab); a feed scope needs a created_after
    bound and is limited to the configured task count. Streams the pipeline already processed are refused: use
    reprocess_streams for those. An indexing pipeline whose earlier Events have since been superseded (the
    translation processed their raw streams again) still has their documents in its index: the approval says what
    the cluster admin deletes first (a build's own Lucene index holding only those is emptied, its shards deleted).
    An indexing pipeline reading Events needs source_pipeline_uuid, and its filter only selects Events from
    exactly that events pipeline. For an Elasticsearch indexing pipeline, the index template for its destination
    index must have been agreed with the user (propose_index_template, check_index_template); the user enables the
    filter only once it is committed to the cluster, so the index is created with it.
    A translation pipeline processes the build's own feeds, or the existing feeds the build works from (start_build
    feeds=): those streams are processed where they are, and their Events go to a test feed the server makes in the
    build (output_feed in the reply; promotion leaves it in the workspace). Sample filters run one task at a time.
    A disabled filter: give the user the reply's pipeline_link and what enabling does; once they say it is enabled,
    wait_for_processing. An enabled one: wait_for_processing with its filter_id.
    """
    stroom = gateway_from(ctx)
    pipeline = await _managed_pipeline(ctx, pipeline_uuid)
    if bool(stream_ids) == bool(feed):
        raise ToolError("Give either stream_ids (the sample) or feed, not both")
    if not await stepped_clean(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': pipeline.get('name')}):
        raise ToolError(f"Pipeline '{pipeline.get('name')}' has no clean step of its current code recorded: step_sample "
                        f"(or step_records) over the sample streams until the verdict is clean, then process. A change "
                        f"to its XSLT or converter since the last clean step needs stepping again.")
    output = await _build_feeds_only(ctx, pipeline, stream_ids, feed)
    if stream_ids:
        await refuse_older_than_feed(ctx, stream_ids)
    if feed and not created_after:
        raise ToolError("A feed-wide filter needs created_after, so it does not reprocess the feed's history")
    if feed and not stream_type:
        from tools.templates import _shape
        try:
            indexes_events = (await _shape(stroom, pipeline_uuid)).get('stage') == 'indexing'
        except Exception:
            indexes_events = await _is_indexing(stroom, pipeline_uuid)
        stream_type = 'Events' if indexes_events else await feed_stream_type(stroom, feed)
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
        try:
            min_ms = int(datetime.fromisoformat(created_after.replace('Z', '+00:00')).timestamp() * 1000)
        except ValueError:
            raise ToolError(f"created_after '{created_after}' is not an ISO time, e.g. 2026-10-09T21:30:00Z")
        min_ms, since = await _feed_bounds(stroom, pipeline_uuid, feed, stream_type, min_ms)
        # With the source pipeline named (below), its Events are those meant, whatever feed they are in: no feed
        # condition (asked for by the user).
        expression = {'type': 'operator', 'op': 'AND', 'children': (
            [] if source else [await _feed_term(stroom, feed)]) + [_term('Type', stream_type)]}
        scope = (f"{stream_type} streams created after {since}" if source
                 else f"feed {feed} ({stream_type}) created after {since}")
        max_tasks = stroom.settings.max_feed_filter_tasks
    if source:
        expression = {'type': 'operator', 'op': 'AND', 'children': [expression, _pipeline_term(source)]}
        scope += f", only Events from pipeline '{source['name']}'"
    if feed:
        await _refuse_duplicate(stroom, pipeline_uuid, expression, min_ms)
    details = {'pipeline': pipeline['name'], 'scope': scope, 'priority': priority, 'max tasks': max_tasks or 'unlimited'}
    if await _sends_to_kafka(stroom, pipeline_uuid):
        # Stepping sends nothing; processing does: the destination (ArcSight, say) receives these records.
        details['sends'] = ("each processed Event as a Kafka record, through the pipeline's KafkaConfig: the destination "
                            "(ArcSight, say) receives them")

    destination = await elastic_destination(stroom, pipeline_uuid)
    if destination:
        details['index template'] = await _committed(stroom, pipeline, destination)
    # A sample indexed into an index this pipeline already wrote to: the index starts fresh, dropped first (asked for
    # by the user). Not a feed's filter beside an earlier one (a backfill, then new data): those add to the index.
    lucene = await lucene_index(stroom, pipeline_uuid)
    clear = False
    if stream_ids and (destination or lucene) and await stroom.processor_filters(pipeline_uuid):
        fresh, clear = await _fresh_index(ctx, pipeline, destination, lucene)
        details.update(fresh)
    from tools.plan import build_of, with_next
    common = {'pipeline': pipeline['name'], 'scope': scope, **({'events_from_pipeline': source['name']} if source else {}),
              **({'output_feed': output['name'], 'output_note': _output_note(output)} if output else {})}
    if stream_ids and await _reprocess_runs(stroom, pipeline_uuid):
        # The sample, through an events or indexing pipeline: processed once the user approves it (asked for by the
        # user: sample filters made disabled were left for them to enable in Stroom).
        summary = (f"Create processor filter and process the sample\nProcess {len(stream_ids)} sample stream(s) with "
                   f"'{pipeline['name']}'.")
        if 'already indexed' in details:
            summary += " Approve only once the index is dropped, as said below."
        if 'index template' in details:
            summary += " Approve only once the agreed index template is committed to the cluster."
        gate = await consent_from(ctx).require(ctx, 'approval', 'create_processor_filter', summary, details, approval_id)
        if gate:
            return gate
        if clear:
            await _delete_index_shards(stroom, lucene['uuid'])
        small = bool(destination) and await development_batch(ctx, pipeline_uuid, True)
        created = await _create_filter(stroom, pipeline, expression, priority, max_tasks, min_ms, enabled=True)
        consent_from(ctx).discard(source_confirmation_id)
        result = {'filter_id': created['id'], **common, **({'batch_size': _BATCH_NOTE} if small else {}), 'enabled': True,
                  'hint': f"Processing has started: wait_for_processing with filter_id={created['id']}."}
        return await with_next(ctx, await build_of(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': pipeline['name']}), result)
    # A feed's streams, or a pipeline that sends elsewhere (CEF to Kafka) or loads reference data: disabled (asked for
    # by the user), so there is no approval form: enabling it in Stroom is the user's approval.
    small = bool(destination) and await development_batch(ctx, pipeline_uuid, True)
    created = await _create_filter(stroom, pipeline, expression, priority, max_tasks, min_ms, enabled=False)
    consent_from(ctx).discard(source_confirmation_id)
    result = {'filter_id': created['id'], **common, **({'batch_size': _BATCH_NOTE} if small else {}), 'enabled': False,
              **_for_the_user(stroom, pipeline, created['id'], details,
                              _before_actions(stroom, pipeline, destination, lucene, details))}
    return await with_next(ctx, await build_of(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': pipeline['name']}), result)


_FIRST = ('index template', 'already indexed')


def _output_note(output: dict[str, Any]) -> str:
    return (f"The streams are processed where they are, in {', '.join(output['works_from'])}; their Events go to "
            f"the build's test feed {output['name']}{' (made now)' if output['made'] else ''}, through the pipeline's "
            f"StreamAppender (its feed property), not into the production feed. Nothing is uploaded to it. Promotion "
            f"takes the property off and leaves the test feed in the workspace.")


def _for_the_user(stroom: StroomGateway, pipeline: dict[str, Any], filter_id: int,
                  details: dict[str, Any], before: list[dict[str, Any]] = ()) -> dict[str, Any]:
    """What the user needs to enable a disabled filter: where, what it will do, and what must be done first; with
    user_actions, the same as buttons (before: the actions that come first)."""
    first = [f"{details[k]}: enable it only once that is so" if k == 'index template' else details[k]
             for k in _FIRST if k in details]
    before = [b for b in before if b]
    enable = useractions.enable_filter(stroom.settings, filter_id, pipeline, details.get('scope') or '',
                                       [b['id'] for b in before])
    return {
        'pipeline_link': doc_link(stroom.settings, 'Pipeline', pipeline['uuid']),
        'enabling_it': {k: v for k, v in details.items() if k not in _FIRST},
        **({'before_enabling': first} if first else {}),
        **useractions.actions(*before, enable),
        'hint': (f"Filter {filter_id} is disabled: enabling it is the user's. Give them the pipeline_link and ask them "
                 f"to enable filter {filter_id} on its Processors tab"
                 + (", once what before_enabling says is done" if first else '')
                 + f", and to tell you when they have. Then wait_for_processing (filter_id={filter_id}); not before.")}


def _before_actions(stroom: StroomGateway, pipeline: dict[str, Any], destination: dict[str, Any] | None,
                    lucene: dict[str, Any] | None, details: dict[str, Any]) -> list[dict[str, Any] | None]:
    """Buttons for what comes before enabling a filter: the agreed index template committed, an index this pipeline
    wrote to before dropped (Elasticsearch: copied for the cluster admin), a Lucene index's shards deleted (its doc
    opened)."""
    out: list[dict[str, Any] | None] = []
    if destination:
        agreed = read_agreed_template(pipeline.get('description')) or {}
        if agreed.get('dev_tools'):
            out.append(useractions.es_request('commit-template', agreed['dev_tools'],
                                              f"Commit the agreed index template '{agreed.get('name')}' to cluster "
                                              f"{destination.get('cluster')}, if it isn't already"))
        if 'already indexed' in details:
            out.append(useractions.es_request('drop-index', drop_request(pipeline, destination['index name']),
                                              f"Drop '{destination['index name']}' so it starts fresh"))
    elif lucene and 'already indexed' in details:
        out.append(useractions.open_document(stroom.settings, {**lucene, 'type': lucene.get('type') or 'Index'},
                                             'delete its shards on the Shards tab, so it starts fresh'))
    return out


async def set_processor_filter_enabled(
        ctx: Context,
        filter_id: Annotated[int, Field(description="Processor filter id, e.g. from processing_status.")],
        enabled: Annotated[bool, Field(description="false: disable it. true is refused: enabling is the user's.")] = False,
) -> dict[str, Any]:
    """Disable a processor filter on a pipeline this server created. Enabling one is the user's, in Stroom: this
    refuses it."""
    stroom = gateway_from(ctx)
    current = await stroom.get(f'/processorFilter/v1/{filter_id}')
    pipeline = await _managed_pipeline(ctx, current['pipelineUuid'])
    if enabled:
        # Asked for by the user: the server enables no filter, bar reprocessing in a build that the user approves.
        raise ToolError(f"Enabling a processor filter is the user's: ask them to enable filter {filter_id} on the "
                        f"Processors tab of pipeline '{pipeline['name']}' "
                        f"({doc_link(stroom.settings, 'Pipeline', pipeline['uuid'])}), then wait_for_processing.")
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
    wait_for_processing so only the new outputs count. An events (or records) or indexing pipeline: the user
    approves it in a form, then its filter is created enabled and processing starts (for Elasticsearch indexing
    the approval also confirms the index template is applied on the cluster; a build's own Lucene index holding
    only these streams is emptied first, its shards deleted). Any other pipeline (CEF output,
    reference data): the filter is created disabled, for the user to enable.
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
    await refuse_older_than_feed(ctx, stream_ids)
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
    # Stroom's purgeOnReprocess does not apply to a second filter on the same streams, nor does it remove a Lucene
    # index's documents (seen: an eval build's index held each event twice after its indexing XSLT was fixed and the
    # stream reprocessed), and this server has no Elasticsearch access: the index is dropped first and starts fresh.
    lucene = await lucene_index(stroom, pipeline_uuid)
    fresh, clear = await _fresh_index(ctx, pipeline, destination, lucene)
    clear = clear and await _reprocess_runs(stroom, pipeline_uuid)
    details.update(fresh)
    # Reprocessing an events or indexing pipeline while it is developed in a build runs once the user approves it
    # (asked for by the user); every other filter is made disabled, for the user to enable.
    if not await _reprocess_runs(stroom, pipeline_uuid):
        small = bool(destination) and await development_batch(ctx, pipeline_uuid, True)
        created = await _create_filter(stroom, pipeline, expression, 10, max_tasks, None, enabled=False)
        from tools.plan import next_for
        return await next_for(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': pipeline['name']}, {
            'filter_id': created['id'], 'pipeline': pipeline['name'], 'streams': stream_ids, 'max_tasks': max_tasks,
            'enabled': False, **({'batch_size': _BATCH_NOTE} if small else {}),
            **_for_the_user(stroom, pipeline, created['id'], details,
                            _before_actions(stroom, pipeline, destination, lucene, details))})
    summary = f"Create processor filter and reprocess streams\nReprocess {len(stream_ids)} stream(s) with '{pipeline['name']}'."
    if 'already indexed' in details:
        summary += " Approve only once the index is dropped, as said below."
    gate = await consent_from(ctx).require(ctx, 'approval', 'reprocess_streams', summary, details, approval_id)
    if gate:
        return gate
    if clear:
        await _delete_index_shards(stroom, lucene['uuid'])
    small = bool(destination) and await development_batch(ctx, pipeline_uuid, True)
    created = await _create_filter(stroom, pipeline, expression, 10, max_tasks, None, enabled=True)
    from tools.plan import next_for
    return await next_for(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': pipeline['name']}, {
        'filter_id': created['id'], 'pipeline': pipeline['name'], 'streams': stream_ids, 'max_tasks': max_tasks,
        'enabled': True, **({'batch_size': _BATCH_NOTE} if small else {}),
        'hint': f"wait_for_processing with filter_id={created['id']} so only this run's outputs count."})


async def _reprocess_runs(stroom: StroomGateway, pipeline_uuid: str) -> bool:
    """Whether reprocessing runs on the user's approval: an events (or records) or indexing pipeline."""
    from tools.templates import _shape
    try:
        return (await _shape(stroom, pipeline_uuid)).get('stage') in REPROCESS_STAGES
    except Exception:
        return await _is_indexing(stroom, pipeline_uuid)


def _selects(expression: dict[str, Any] | None, meta: dict[str, Any]) -> bool:
    """Whether a filter's expression selects the stream, for the terms this server writes; any other term counts as
    selecting it, so a stream is only ever reported unselected when it plainly is."""
    if not expression:
        return True
    if expression.get('type') == 'operator':
        children = [c for c in expression.get('children') or [] if c.get('enabled', True)]
        results = [_selects(c, meta) for c in children]
        op = expression.get('op') or 'AND'
        if op == 'NOT' or not results:
            return True
        return any(results) if op == 'OR' else all(results)
    field, condition, value = expression.get('field'), expression.get('condition'), str(expression.get('value'))
    if condition == 'EQUALS' and field == 'Id':
        return str(meta.get('id')) == value
    if condition == 'EQUALS' and field == 'Feed':
        return (meta.get('feedName') or '').lower() == value.lower()   # Stroom matches feed names whatever their case
    if condition == 'IS_DOC_REF' and field == 'Feed':
        return (meta.get('feedName') or '').lower() == str((expression.get('docRef') or {}).get('name')).lower()
    if condition == 'EQUALS' and field == 'Type':
        return meta.get('typeName') == value
    if condition == 'IS_DOC_REF' and field == 'Pipeline':
        return meta.get('pipelineUuid') == (expression.get('docRef') or {}).get('uuid')
    return True


def _will_process(f: dict[str, Any], meta: dict[str, Any]) -> bool:
    """Whether the processor filter, enabled, selects the stream, its creation time included."""
    created, earliest, latest = meta.get('createMs'), f.get('minMetaCreateTimeMs'), f.get('maxMetaCreateTimeMs')
    return (bool(f.get('enabled')) and _selects((f.get('queryData') or {}).get('expression'), meta)
            and not (created and earliest and created < earliest) and not (created and latest and created > latest))


async def _waiting_on(stroom: StroomGateway, pipeline_uuid: str, stream_ids: list[int]) -> tuple[list[int], list[int]]:
    """(disabled, stopped): the filters that would process these streams but are disabled, and those whose processor
    is disabled; both empty when something enabled will process them."""
    if not stream_ids:
        return [], []
    metas = [r['meta'] for r in (await stroom.find_meta([_term('Id', i) for i in stream_ids], len(stream_ids),
                                                        op='OR')).get('values') or []]
    filters = await stroom.processor_filters(pipeline_uuid)
    waiting: set[int] = set()
    for meta in metas:
        selecting = [f for f in filters if _will_process({**f, 'enabled': True}, meta)]
        if selecting and not any(f.get('enabled') and (f.get('processor') or {}).get('enabled') is not False
                                 for f in selecting):
            waiting |= {f['id'] for f in selecting}
    stopped = sorted(i for i in waiting if (next(f for f in filters if f['id'] == i).get('processor') or {})
                     .get('enabled') is False)
    disabled = sorted(i for i in waiting if not next(f for f in filters if f['id'] == i).get('enabled'))
    return disabled, stopped


async def _unselected(stroom: StroomGateway, pipeline_uuid: str, stream_ids: list[int]) -> list[str]:
    """Why no enabled filter of the pipeline will process these streams; [] when one will (or might).

    Seen (Gemma, VS Code): a feed-wide filter created after its sample arrived; the agent waited out three timeouts
    of "tasks still running" for a filter whose tracker had matched nothing.
    """
    if not stream_ids:
        return []
    metas = [r['meta'] for r in (await stroom.find_meta([_term('Id', i) for i in stream_ids], len(stream_ids),
                                                        op='OR')).get('values') or []]
    filters = await stroom.processor_filters(pipeline_uuid)
    from tools.processing import _terms
    problems = []
    for meta in sorted(metas, key=lambda m: m['id']):
        created = meta.get('createMs')
        reasons = []
        for f in filters:
            earliest, latest = f.get('minMetaCreateTimeMs'), f.get('maxMetaCreateTimeMs')
            selects = ', '.join(_terms((f.get('queryData') or {}).get('expression'))) or 'everything'
            if not _selects((f.get('queryData') or {}).get('expression'), meta):
                reasons.append(f"filter {f['id']} selects {selects}")
            elif created and earliest and created < earliest:
                reasons.append(f"filter {f['id']} selects only streams created after {_iso(earliest)}")
            elif created and latest and created > latest:
                reasons.append(f"filter {f['id']} selects only streams created before {_iso(latest)}")
            elif not f.get('enabled'):
                reasons.append(f"filter {f['id']} is disabled")
            else:
                reasons = []
                break
        if reasons:
            problems.append(f"No processor filter of this pipeline will process stream {meta['id']} ({meta.get('feedName')}, "
                            f"{meta.get('typeName')}, created {_iso(created) if created else 'at an unknown time'}): "
                            + '; '.join(reasons))
    return problems


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
        output_type: Annotated[str | None, Field(description=(
            "The stream type expected per input (Events, Reference, Records, ...). Left out: what the pipeline "
            "writes, from its stream appender."))] = None,
) -> dict[str, Any]:
    """
    Wait until the pipeline's processor tasks finish, then report per input stream the output (Events, or
    Reference) and Error streams it produced. The gate before the next stage: exactly one output stream per
    input stream. A missing or duplicate one is flagged with the likely cause.
    """
    stroom = gateway_from(ctx)
    deadline = time.monotonic() + timeout_seconds
    from tools.plan import build_of, with_next
    inferred = output_type is None
    checked = None
    while True:
        status = await processing_status(ctx, pipeline_uuid)
        if not status['filters']:
            # Nothing was set to process: waiting would only run out the timeout, as if tasks were running.
            return await with_next(ctx, await build_of(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid}), {
                'pipeline': pipeline_uuid, 'finished': False, 'streams': [], 'gate': 'fail',
                'problems': ["This pipeline has no processor filter, so nothing is processing and there is nothing to "
                             "wait for."],
                'hint': "create_processor_filter first. If it was refused, do what its message says; don't wait."})
        if inferred and output_type is None:
            output_type = await output_stream_type(stroom, pipeline_uuid)
        outputs = {raw: await _outputs(stroom, raw, pipeline_uuid, filter_id) for raw in stream_ids}
        if checked is None:
            waiting_ids, stopped_ids = await _waiting_on(stroom, pipeline_uuid,
                                                         [raw for raw, metas in outputs.items() if not metas])
            disabled = ' and '.join(([f"filter(s) {waiting_ids}"] if waiting_ids else [])
                                    + ([f"the processor of filter(s) {stopped_ids} (the pipeline's processor row, "
                                        f"above its filters)"] if stopped_ids else []))
            if disabled:
                pipeline_ref = {'type': 'Pipeline', 'uuid': pipeline_uuid,
                                'name': (await stroom.get_doc('Pipeline', pipeline_uuid)).get('name')}
                waiting_actions = useractions.actions(
                    *[useractions.enable_filter(stroom.settings, i, pipeline_ref) for i in waiting_ids],
                    useractions.open_document(stroom.settings, pipeline_ref, "enable its processor (the row above its "
                                                                             "filters) on the Processors tab")
                    if stopped_ids else None)
                # Created disabled (asked for by the user): nothing runs until the user enables it, so waiting would
                # only run out the timeout.
                return await with_next(ctx, await build_of(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid}), {
                    'pipeline': pipeline_uuid, 'finished': False, 'streams': [], 'gate': 'waiting',
                    'waiting_for': f"the user to enable {disabled}",
                    'pipeline_link': doc_link(stroom.settings, 'Pipeline', pipeline_uuid), **waiting_actions,
                    'hint': f"Processing hasn't started: {disabled} disabled, and enabling is the user's. Ask them to "
                            f"enable it on the pipeline's Processors tab (pipeline_link) and to tell you when they "
                            f"have; then wait_for_processing again."})
            checked = await _unselected(stroom, pipeline_uuid, [raw for raw, metas in outputs.items() if not metas])
            if checked:
                return await with_next(ctx, await build_of(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid}), {
                    'pipeline': pipeline_uuid, 'finished': False, 'streams': [], 'gate': 'fail', 'problems': checked,
                    'hint': "Nothing will process these streams, so there is nothing to wait for: "
                            "create_processor_filter with stream_ids= them, then wait."})
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
    ref = {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': pipeline_uuid}
    # Only while the pipeline is in a build, where its code changes: a promoted one keeps no record of its steps.
    in_build = MANAGED in await guard_from(ctx).tags(ref)
    if in_build and not await stepped_clean(ctx, ref):
        # Seen: a filter made after a clean step kept processing once the XSLT was replaced by one that never stepped
        # clean, and the agent went on to document and index its output.
        problems.append("The pipeline's current code has not stepped clean: its output may not be what was checked. "
                        "step_sample over the sample streams until the verdict is clean, then reprocess_streams")
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
