"""Tools for finding and reading streams, and triaging their errors."""
from collections import Counter
from datetime import datetime, timezone
from typing import Annotated, Any

from fastmcp import Context
from lxml import etree
from fastmcp.exceptions import ToolError
from pydantic import Field

from tools.pipelines import own_elements
from utils.params import ONE_OR_MORE
from utils.stroom import StroomGateway, gateway_from
from utils.triage import from_stored_error, triage

StreamId = Annotated[int, Field(description="Stream (meta) id, e.g. from find_streams.")]


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None


def _stream(meta: dict[str, Any]) -> dict[str, Any]:
    return {'id': meta['id'], 'feed': meta.get('feedName'), 'type': meta.get('typeName'),
            'status': meta.get('status'), 'created': _iso(meta.get('createMs')),
            'parent_id': meta.get('parentMetaId'), 'pipeline_uuid': meta.get('pipelineUuid')}


def _term(field: str, value: Any, condition: str = 'EQUALS') -> dict[str, Any]:
    return {'type': 'term', 'field': field, 'condition': condition, 'value': str(value)}


async def _meta(stroom: StroomGateway, stream_id: int) -> dict[str, Any]:
    rows = (await stroom.find_meta([_term('Id', stream_id)], 1)).get('values') or []
    if not rows:
        raise ToolError(f"No stream with id {stream_id}")
    return rows[0]['meta']


async def find_streams(
        ctx: Context,
        feed: Annotated[str | None, Field(description="Feed name, e.g. 'KEYCLOAK-V1.2'.")] = None,
        stream_type: Annotated[str | None, Field(
            description="Stream type: 'Raw Events', 'Events', 'Error', 'Reference', ...")] = None,
        parent_id: Annotated[int | None, Field(description="Only streams produced from this stream.")] = None,
        pipeline_uuid: Annotated[str | None, Field(description="Only streams produced by this pipeline.")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """
    Find streams by feed, type, parent stream or producing pipeline, newest first.
    Use it to locate a feed's recent Raw Events, or the Events and Error streams a pipeline produced.
    """
    terms = []
    if feed:
        terms.append(_term('Feed', feed))
    if stream_type:
        terms.append(_term('Type', stream_type))
    if parent_id is not None:
        terms.append(_term('Parent Id', parent_id))
    if pipeline_uuid:
        terms.append({'type': 'term', 'field': 'Pipeline', 'condition': 'IS_DOC_REF', 'value': pipeline_uuid,
                      'docRef': {'type': 'Pipeline', 'uuid': pipeline_uuid}})
    if not terms:
        raise ToolError("Give at least one of feed, stream_type, parent_id or pipeline_uuid")
    body = await gateway_from(ctx).find_meta(terms, limit)
    streams = [_stream(row['meta']) for row in body.get('values') or []]
    total = (body.get('pageResponse') or {}).get('total')
    return {'total': total, 'returned': len(streams), 'streams': streams}


async def get_stream_children(ctx: Context, stream_id: StreamId) -> dict[str, Any]:
    """
    Streams produced from a stream (e.g. the Events and Error streams from a Raw Events stream),
    with a count per type. Exactly one Events child per processed raw stream is expected; none or
    several means processing failed or ran twice.
    """
    body = await gateway_from(ctx).find_meta([_term('Parent Id', stream_id)], 100)
    children = [_stream(row['meta']) for row in body.get('values') or []]
    by_type: dict[str, int] = {}
    for child in children:
        by_type[child['type']] = by_type.get(child['type'], 0) + 1
    return {'stream_id': stream_id, 'by_type': by_type, 'children': children}


async def read_stream(
        ctx: Context,
        stream_id: StreamId,
        first_record: Annotated[int, Field(ge=0, description="Zero-based index of the first record.")] = 0,
        record_count: Annotated[int, Field(ge=1, le=100)] = 5,
        child_type: Annotated[str | None, Field(
            description="Read a child part instead, e.g. 'Meta Data' (receipt headers) or 'Context'.")] = None,
) -> dict[str, Any]:
    """
    Read records from a stream: raw input, Events XML, or a child part such as the receipt headers.
    Output is trimmed to a size limit; page with first_record.
    """
    stroom = gateway_from(ctx)
    limit = stroom.settings.max_stream_chars
    records, first_body = await read_records(stroom, stream_id, first_record, record_count, child_type, limit)
    result: dict[str, Any] = {
        'stream_id': stream_id, 'feed': first_body.get('feedName'), 'type': first_body.get('streamTypeName'),
        'first_record': first_record, 'total_records': (first_body.get('totalItemCount') or {}).get('count'),
        'available_child_types': first_body.get('availableChildStreamTypes'), 'records': records}
    if sum(len(r) for r in records) >= limit:
        result['truncated'] = True
        result['hint'] = "Record data was trimmed; read fewer records at a time."
    return result


async def read_records(stroom: StroomGateway, stream_id: int, first: int, count: int, child_type: str | None,
                       char_budget: int) -> tuple[list[str], dict[str, Any]]:
    """Up to `count` records from `first`, within a character budget.

    Stroom returns one record per fetch from a segmented stream (e.g. Events), whatever count is
    asked for, so segmented streams are read record by record. A raw stream is one record.
    """
    body = await stroom.fetch_data(stream_id, first, 1, 'TEXT', child_type)
    if body.get('errors'):
        raise ToolError(f"Stroom could not read stream {stream_id}: {'; '.join(body['errors'])}")
    first_body = body
    total = (body.get('totalItemCount') or {}).get('count') or 1
    records, used = [], 0
    index = first
    while True:
        data = (body.get('data') or '')[:char_budget - used]
        records.append(data)
        used += len(data)
        index += 1
        if len(records) >= count or index >= total or used >= char_budget or body.get('dataType') != 'SEGMENTED':
            return records, first_body
        body = await stroom.fetch_data(stream_id, index, 1, 'TEXT', child_type)


async def get_stream_attributes(ctx: Context, stream_id: StreamId) -> dict[str, Any]:
    """
    Attributes a raw stream carries: its receipt headers (e.g. Feed, RemoteAddress, ReceivedTime,
    or custom ones such as MyHost) with values, usable in XSLT as stroom:meta('Name').
    """
    stroom = gateway_from(ctx)
    headers: dict[str, str] = {}
    body = await stroom.fetch_data(stream_id, 0, 1, 'TEXT', 'Meta Data')
    for line in (body.get('data') or '').splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            headers[key.strip()] = value.strip()
    info = await stroom.get(f'/data/v1/{stream_id}/info')
    details = {e['key']: e['value'] for section in info or [] if section.get('title') in ('Stream', 'Attributes')
               for e in section.get('entries') or [] if e.get('key')}
    return {'stream_id': stream_id, 'attributes': headers, 'stream': details}


async def summarise_errors(
        ctx: Context,
        stream_id: Annotated[int, Field(
            description="An Error stream, or a Raw Events/Events stream whose Error children to summarise.")],
) -> dict[str, Any]:
    """
    Summarise error markers: grouped by severity, element and message, each group classified as
    blocking (must fix), review (may be caused by the pipeline's own content) or benign (report only).
    Elements the pipeline sets itself are distinguished from ones inherited from its template.
    """
    stroom = gateway_from(ctx)
    meta = await _meta(stroom, stream_id)
    if meta.get('typeName') == 'Error':
        error_streams = [meta]
    else:
        body = await stroom.find_meta([_term('Parent Id', stream_id), _term('Type', 'Error')], 20)
        error_streams = [row['meta'] for row in body.get('values') or []]
    if not error_streams:
        return {'stream_id': stream_id, 'error_streams': [], **triage([], ctx.lifespan_context['rules'], set())}

    markers, own = [], set()
    for error_stream in error_streams:
        body = await stroom.fetch_data(error_stream['id'], 0, 1000, 'MARKER')
        markers += [from_stored_error(m) for m in body.get('markers') or [] if m.get('type') == 'storedError']
        if error_stream.get('pipelineUuid'):
            own |= own_elements(await stroom.pipeline_layers(error_stream['pipelineUuid']))
    result = triage(markers, ctx.lifespan_context['rules'], own)
    return {'stream_id': stream_id, 'error_streams': [e['id'] for e in error_streams], **result}


EVT = 'event-logging:3'
_DETAIL_META = {'TypeId', 'Description', 'Classification', 'Purpose'}


def _leaf_paths(event: etree._Element) -> set[str]:
    """Paths (from Event) of elements that carry a value or attributes, ignoring repeats."""
    paths = set()
    for node in event.iter():
        if not isinstance(node.tag, str) or node is event:
            continue
        if (node.text or '').strip() or node.attrib:
            parts, current = [], node
            while current is not None and current is not event:
                parts.append(etree.QName(current).localname)
                current = current.getparent()
            paths.add('/'.join(reversed(parts)))
    return paths


async def summarise_events(
        ctx: Context,
        stream_ids: Annotated[list[int], ONE_OR_MORE, Field(description="Events streams to profile.")],
        max_events: Annotated[int, Field(ge=1, le=2000)] = 200,
) -> dict[str, Any]:
    """
    Profile Events streams: how many events of each EventDetail type, TypeId and Action there are
    (with an example of each), and how often each event-logging path is populated. Use it to document
    what a pipeline produces or to spot fields that are rarely or never filled.
    """
    stroom = gateway_from(ctx)
    types, type_ids, actions = Counter(), Counter(), Counter()
    examples: dict[str, int] = {}
    populated: Counter = Counter()
    count = 0
    for stream_id in stream_ids:
        records, _ = await read_records(stroom, stream_id, 0, max_events - count, None, 50_000_000)
        for record in records:
            root = etree.fromstring(record.encode('utf-8'))
            for event in root.iter(f'{{{EVT}}}Event'):
                count += 1
                detail = event.find(f'{{{EVT}}}EventDetail')
                children = [c for c in (detail if detail is not None else []) if isinstance(c.tag, str)]
                action_el = next((c for c in children if etree.QName(c).localname not in _DETAIL_META), None)
                kind = etree.QName(action_el).localname if action_el is not None else '(none)'
                types[kind] += 1
                examples.setdefault(kind, count - 1)
                type_ids[(event.findtext(f'{{{EVT}}}EventDetail/{{{EVT}}}TypeId') or '(none)').strip()] += 1
                if action_el is not None:
                    actions[f"{kind}/{(action_el.findtext(f'{{{EVT}}}Action') or '(none)').strip()}"] += 1
                populated.update(_leaf_paths(event))
        if count >= max_events:
            break
    return {'events': count, 'event_types': dict(types.most_common()), 'type_ids': dict(type_ids.most_common(50)),
            'actions': dict(actions.most_common(50)),
            'path_population': {p: round(100 * n / count, 1) for p, n in sorted(populated.items())} if count else {},
            'hint': "Percentages are of events sampled; raise max_events for a fuller picture."
            if count >= max_events else None}


ALL_TOOLS = [find_streams, get_stream_children, read_stream, get_stream_attributes, summarise_errors,
             summarise_events]
