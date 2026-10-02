"""Sampling an existing feed to learn which kinds of event it holds."""
from collections import Counter
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import guard_from
from tools.streams import _meta, _term
from utils.consent import consent_from
from utils.params import ONE_OR_MORE
from utils.stroom import StroomGateway, body_text, gateway_from, set_body_text
from utils.survey import Shapes, share, split_records
from utils.surveydoc import doc_name, dropped, merge, read_state, render, resolve, set_handling


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{ms % 1000:03d}Z'


def spread_order(n: int) -> list[int]:
    """0..n-1 in bisection order (last, first, middle, quarters...), so any prefix spans the whole range."""
    if n <= 0:
        return []
    order, seen = [], set()
    pending = [(0, n - 1)]
    for i in (n - 1, 0):
        if i not in seen:
            order.append(i)
            seen.add(i)
    while pending:
        lo, hi = pending.pop(0)
        if hi - lo < 2:
            continue
        mid = (lo + hi) // 2
        if mid not in seen:
            order.append(mid)
            seen.add(mid)
        pending += [(lo, mid), (mid, hi)]
    return order + [i for i in range(n) if i not in seen]


async def pick_spread(stroom: StroomGateway, terms: list[dict[str, Any]], count: int,
                      skip: set[int]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Up to `count` unread streams spread over the feed's lifetime, in bisection order."""
    live = lambda rows: [r['meta'] for r in rows.get('values') or [] if r['meta'].get('status') != 'DELETED']
    newest = live(await stroom.find_meta(terms, 1))
    oldest = live(await stroom.find_meta(terms, 1, newest_first=False))
    if not newest:
        return [], {}
    span = {'oldest': _iso(oldest[0]['createMs']), 'newest': _iso(newest[0]['createMs'])}
    start, end = oldest[0]['createMs'], newest[0]['createMs'] + 1
    picked: list[dict[str, Any]] = []
    width = max(1, (end - start) // count)
    for bucket in spread_order(count):
        lo, hi = start + bucket * width, (end if bucket == count - 1 else start + (bucket + 1) * width)
        rows = live(await stroom.find_meta(terms + [_term('Create Time', f'{_iso(lo)},{_iso(hi)}', 'BETWEEN')],
                                           len(skip) + len(picked) + 1, newest_first=bucket != 0))
        choice = next((m for m in rows if m['id'] not in skip and m['id'] not in {p['id'] for p in picked}), None)
        if choice:
            picked.append(choice)
    if len(picked) < count:  # thin or clustered feeds: fill with any unread streams
        for m in live(await stroom.find_meta(terms, len(skip) + count * 2)):
            if len(picked) >= count:
                break
            if m['id'] not in skip and m['id'] not in {p['id'] for p in picked}:
                picked.append(m)
    return picked, span


async def read_head(stroom: StroomGateway, stream_id: int, part: int, chars: int) -> tuple[str, bool, int]:
    """The first `chars` characters of one part: (text, cut off, number of parts in the stream)."""
    body = await stroom.post('/data/v1/fetch', {
        'sourceLocation': {'metaId': stream_id, 'partIndex': part, 'recordIndex': 0,
                           'dataRange': {'charOffsetFrom': 0, 'length': chars}},
        'displayMode': 'TEXT', 'recordCount': 1, 'expandedSeverities': []})
    if body.get('errors'):
        raise ToolError(f"Stroom could not read stream {stream_id}: {'; '.join(body['errors'])}")
    text = body.get('data') or ''
    total = (body.get('totalCharacterCount') or {}).get('count')
    parts = (body.get('totalItemCount') or {}).get('count') or 1
    # Stroom rounds a range out to whole lines, so a single-line stream (one big JSON array) can come back
    # whole: cut it here too.
    cut = len(text) > chars
    text = text[:chars]
    return text, cut or bool(total and total > len(text)), parts


async def survey_feed(
        ctx: Context,
        feed: Annotated[str | None, Field(description="The feed that already holds the raw data.")] = None,
        stream_type: Annotated[str, Field(description="Stream type to sample.")] = 'Raw Events',
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="Survey exactly these streams instead of picking from the feed: the sample streams an "
                        "onboarding uploaded (one per file), to see every kind of event they hold and where.")] = [],
        max_streams: Annotated[int, Field(ge=1, le=50, description="Streams to read in this call, spread over the "
                                                                  "feed's lifetime.")] = 10,
        skip_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="Streams already read (streams_read from earlier calls): this call picks others.")] = [],
        known_signatures: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Shape signatures from earlier calls; locations then only cover new shapes.")] = [],
        quiet_streams: Annotated[int, Field(ge=1, le=50, description="Stop after this many streams in a row add no "
                                                                     "new shape.")] = 4,
        max_chars_per_stream: Annotated[int, Field(
            ge=1000, le=5_000_000, description="Read at most this many characters from the start of each part; "
                                               "big streams are only sampled at their head.")] = 250_000,
        max_parts_per_stream: Annotated[int, Field(ge=1, le=20)] = 3,
        max_records_per_stream: Annotated[int, Field(ge=10, le=10_000)] = 1000,
        examples_per_shape: Annotated[int, Field(ge=1, le=10)] = 2,
        build: Annotated[str | None, Field(
            description="Keep the survey record in this build: a Documentation doc '<feed> - Survey' with the kinds "
                        "of event, examples and survey state. An existing record is continued: its streams are "
                        "skipped and its shapes are known.")] = None,
) -> dict[str, Any]:
    """
    Sample a feed's streams spread over its lifetime (newest, oldest, then the middle, then the quarters...)
    and group their records into shapes (kinds of event): by fields and event-naming values for structured
    data, by masked message templates for syslog and other text. Only the head of each stream is read
    (max_chars_per_stream per part, max_parts_per_stream parts), so multi-GB streams cost the same as small
    ones. Stops when quiet_streams streams in a row show nothing new (saturated), or when every stream has
    been read. Returns each shape with its count, share and examples, and where each example is (stream,
    part, record) for step_records. Call again with skip_stream_ids (all streams read so far) and
    known_signatures to read further streams for kinds of event not seen yet. With a build, the results are
    kept in (and continued from) the build's survey doc. Reads only, apart from that doc.
    """
    stroom = gateway_from(ctx)
    chosen = [await _meta(stroom, i) for i in stream_ids]
    if not feed:
        if not chosen:
            raise ToolError("Give feed, or stream_ids")
        feed = chosen[0].get('feedName')
    record = await _load_record(ctx, build, feed) if build else None
    state = read_state(body_text(record)) if record else None
    if state:
        skip_stream_ids = sorted(set(skip_stream_ids) | set(state['streams_read']))
        known_signatures = list(dict.fromkeys(known_signatures + list(state['shapes'])))
    terms = [_term('Feed', feed), _term('Type', stream_type)]
    skip = set(skip_stream_ids)
    if chosen:
        streams = [m for m in chosen if m['id'] not in skip]
        times = [m['createMs'] for m in chosen if m.get('createMs')]
        span = {'oldest': _iso(min(times)), 'newest': _iso(max(times))} if times else {}
        quiet_streams = len(streams) + 1   # read every chosen stream, whatever it adds
    else:
        streams, span = await pick_spread(stroom, terms, max_streams, skip)
    if not streams and skip:
        result = {'feed': feed, 'streams_read': [], 'records_read': 0, 'saturated': True, 'shapes': [],
                  'new_shapes': 0, 'locations': [], 'time_range': span,
                  'hint': "Every stream in the feed has been read: the survey is complete."}
        return await _keep(ctx, build, record, state, result, {}, examples_per_shape)
    if not streams:
        raise ToolError(f"No {stream_type} streams in feed '{feed}'")
    shapes = Shapes(known_signatures, examples_per_shape)
    per_stream, quiet, records_read, fmt, first_chunk = [], 0, 0, None, None
    for meta in streams:
        new_here, read_here, cut = 0, 0, False
        parts = 1
        part = 0
        while part < min(parts, max_parts_per_stream) and read_here < max_records_per_stream:
            text, truncated, parts = await read_head(stroom, meta['id'], part, max_chars_per_stream)
            cut = cut or truncated
            try:
                chunk = split_records(text, max_records_per_stream - read_here, truncated)
            except Exception as e:  # a part this detector cannot split
                per_stream.append({'stream': meta['id'], 'part': part, 'error': f"could not split records: {e}"})
                part += 1
                continue
            if fmt and chunk.format != fmt:
                per_stream.append({'stream': meta['id'], 'part': part, 'note': f"reads as {chunk.format}, not {fmt}; skipped"})
                part += 1
                continue
            fmt, first_chunk = fmt or chunk.format, first_chunk or chunk
            for index in range(len(chunk.records)):
                new_here += shapes.add(chunk, index, meta['id'], part)
                read_here += 1
            part += 1
        records_read += read_here
        per_stream.append({'stream': meta['id'], 'created': _iso(meta['createMs']) if meta.get('createMs') else None,
                           'records': read_here, 'parts': parts, 'head_only': cut or parts > max_parts_per_stream,
                           'new_shapes': new_here})
        quiet = 0 if new_here else quiet + 1
        if quiet >= quiet_streams:
            break
    if first_chunk is None:
        raise ToolError(f"Nothing readable in the sampled streams of '{feed}'")

    found = sorted((s for s in shapes.shapes.values() if s['count']), key=lambda s: -s['count'])
    counts = Counter({s['signature']: s['count'] for s in found})
    new_shapes = [s for s in found if not s['known']]
    to_step = new_shapes if known_signatures else found
    drop = dropped(state)
    read_ids = [p['stream'] for p in per_stream if 'records' in p]
    saturated = (quiet >= quiet_streams or (bool(known_signatures) and not new_shapes and len(read_ids) >= max_streams)
                 or bool(chosen))   # chosen streams: every one was read, so the sample is covered as far as it goes
    result = {
        'feed': feed, 'format': fmt, 'time_range': span, 'streams_read': read_ids, 'records_read': records_read,
        'saturated': saturated,
        'coverage': (f'all {len(read_ids)} given stream(s) read: these are the kinds of event the sample holds' if chosen else
                     'covered: no new kinds of event in the last streams read' if saturated else
                     'NOT COVERED YET: keep surveying before treating the translation as complete'),
        'per_stream': per_stream,
        'shapes': [{'signature': s['signature'], 'new': not s['known'], 'count': s['count'],
                    'share_percent': share(counts, s['signature']), 'streams': s['streams'][:10],
                    'example': s['examples'][0]['text'][:500] if s['examples'] else None,
                    'locations': [e['location'] for e in s['examples']],
                    **({'handling': 'drop', 'reason': drop[s['signature']]} if s['signature'] in drop else {})}
                   for s in found][:60],
        'new_shapes': len(new_shapes),
        # Records of a kind left untranslated are stepped too, expecting no Event.
        'locations': [{**e['location'], 'shape': s['signature'], **({'expect': 'none'} if s['signature'] in drop else {})}
                      for s in to_step for e in s['examples']],
        'hint': _hint(saturated, new_shapes, known_signatures),
    }
    return await _keep(ctx, build, record, state, result, shapes.shapes, examples_per_shape)


async def _load_record(ctx: Context, build: str, feed: str) -> dict[str, Any] | None:
    """The build's survey doc for this feed, if there is one."""
    found = next((d for d in await guard_from(ctx).folder_contents(build)
                  if d['type'] == 'Documentation' and d['name'] == doc_name(feed)), None)
    return await gateway_from(ctx).get_doc('Documentation', found['uuid']) if found else None


async def _keep(ctx: Context, build: str | None, record: dict[str, Any] | None, state: dict[str, Any] | None,
                result: dict[str, Any], shapes: dict[str, dict[str, Any]], examples: int) -> dict[str, Any]:
    """Write this survey into the build's survey doc (creating it the first time)."""
    if not build:
        return result
    stroom = gateway_from(ctx)
    merged = merge(state, result, shapes, examples)

    async def write(ref: dict[str, Any]) -> dict[str, Any]:
        doc = record if record is not None else await stroom.get_doc('Documentation', ref['uuid'])
        set_body_text(doc, render(merged))
        return await stroom.put_doc(doc)

    if record is None:
        saved = await guard_from(ctx).create_filled('Documentation', doc_name(result['feed']), build, write)
    else:
        saved = await write(record)
    result['survey_doc'] = {'type': 'Documentation', 'uuid': saved['uuid'], 'name': saved['name'],
                            'kinds_of_event': len(merged['shapes']), 'streams_read_in_total': len(merged['streams_read'])}
    return result


async def set_shape_handling(
        ctx: Context,
        build: Annotated[str, Field(description="The build holding the feed's survey doc.")],
        feed: Annotated[str, Field(description="The surveyed feed.")],
        shapes: Annotated[list[str] | str, ONE_OR_MORE, Field(description="Kinds of event: their signatures from survey_feed, or text "
                                                       "found in exactly one signature, e.g. 'loggerName=org.jgroups'.")],
        handling: Annotated[Literal['drop', 'translate'], Field(
            description="drop: the pipeline deliberately writes no Event for these records; translate: undo that.")],
        reason: Annotated[str, Field(description="Why, in the user's words, e.g. 'cluster housekeeping, no "
                                                 "security value'.")] = '',
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Record, in the build's survey doc, that some kinds of event are left untranslated on purpose (or no longer
    are). The user confirms: dropping records means they never reach Events. Afterwards survey_feed marks those
    kinds and gives their locations expect='none', and step_records counts such a record as clean when it
    produces no Event (and flags it if it produces one). Translate them with a drop rule in the mapping
    (build_translation_xslt), so they don't reach the 'no mapping matched' warning.
    """
    record = await _load_record(ctx, build, feed)
    state = read_state(body_text(record)) if record else None
    if not state:
        raise ToolError(f"No survey of '{feed}' in build '{build}': run survey_feed with build first")
    try:
        signatures = resolve(state, shapes)
    except ValueError as e:
        raise ToolError(str(e)) from None
    if handling == 'drop' and not reason.strip():
        raise ToolError("Give the reason for leaving these records untranslated (the user's words)")
    total = sum(s['count'] for s in state['shapes'].values()) or 1
    counts = {s: state['shapes'][s]['count'] for s in signatures}
    verb = 'Leave untranslated (no Events)' if handling == 'drop' else 'Translate again'
    details = {'feed': feed, 'build': build, 'reason': reason,
               'kinds': [f"{s} ({counts[s]} records seen, {round(100 * counts[s] / total, 1)}%)" for s in signatures]}
    gate = await consent_from(ctx).require(ctx, 'confirmation', 'set_shape_handling',
                                           f"{verb}: {len(signatures)} kind(s) of event in feed '{feed}'", details,
                                           confirmation_id)
    if gate:
        return gate
    set_handling(state, signatures, handling, reason.strip(), datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    set_body_text(record, render(state))
    await gateway_from(ctx).put_doc(record)
    left = dropped(state)
    return {'feed': feed, 'handling': handling, 'shapes': signatures,
            'left_untranslated': [{'shape': s, 'reason': r} for s, r in left.items()],
            # The survey's examples of these kinds, to step with step_records.
            'locations': [{**e['location'], 'shape': s, **({'expect': 'none'} if handling == 'drop' else {})}
                          for s in signatures for e in state['shapes'][s]['examples']],
            'hint': ("Add a drop rule for these kinds to the mapping (EventRule with drop=true and conditions that "
                     "match them), regenerate, and step_records over the survey's locations: those records should "
                     "produce no Event." if handling == 'drop' else
                     "Add rules to translate these kinds, regenerate and step_records over their locations.")}


def _hint(saturated: bool, new_shapes: list, known: list[str]) -> str:
    translate = ("Translate every shape and check it with step_records(pipeline, locations, draft_code): the feed's "
                 "own records are stepped where they are.")
    broad = " Then run the broad check (step_sample over a few whole streams with records_per_stream)."
    if known and not new_shapes:
        return "No new kinds of event in these streams." + (
            " The feed looks covered:" + broad[5:] if saturated else
            " Read more with skip_stream_ids=every stream read so far.")
    if saturated:
        return f"{translate} The last streams read added no new kinds of event, so the feed looks covered.{broad}"
    return (f"{translate} Then call again with skip_stream_ids=every stream read so far and known_signatures=every "
            "signature so far, until saturated.")


ALL_TOOLS = [survey_feed, set_shape_handling]
