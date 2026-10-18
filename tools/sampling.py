"""Sampling an existing feed to learn which kinds of event it holds."""
import time
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
from utils.progress import current_progress, progress_from
from utils.stroom import StroomGateway, body_text, gateway_from, refused_reason, set_body_text, stroom_chars
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


NOT_DELETED = {'type': 'operator', 'op': 'NOT', 'children': [
    {'type': 'term', 'field': 'Status', 'condition': 'EQUALS', 'value': 'Deleted'}]}


async def pick_spread(stroom: StroomGateway, terms: list[dict[str, Any]], count: int,
                      skip: set[int]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Up to `count` unread streams spread over the feed's lifetime, in bisection order. A stream search a slice of
    time (seen: 28 of them, 2.4 s each, most of a 35 s survey_feed call): the client is told now and then."""
    live = lambda rows: [r['meta'] for r in rows.get('values') or [] if r['meta'].get('status') != 'DELETED']
    # Deleted streams left out by Stroom, not after: seen in Ask Stroom AI, TWITTER's oldest stream was deleted, the
    # one row asked for was dropped, and the survey failed with "list index out of range".
    terms = terms + [NOT_DELETED]
    say = await current_progress()
    await say(f"Finding up to {count} streams spread over the feed's time range")
    newest = live(await stroom.find_meta(terms, 1))
    oldest = live(await stroom.find_meta(terms, 1, newest_first=False))
    if not newest or not oldest:
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
        await say.now_and_then(f"Finding streams spread over the feed's time range: {len(picked)} of up to {count} "
                               f"found")
    if len(picked) < count:  # thin or clustered feeds: fill with any unread streams
        for m in live(await stroom.find_meta(terms, len(skip) + count * 2)):
            if len(picked) >= count:
                break
            if m['id'] not in skip and m['id'] not in {p['id'] for p in picked}:
                picked.append(m)
    return picked, span


SURVEY_STREAMS = 10           # streams a survey call reads unless the user says otherwise
_FEWER = 'survey_fewer_streams'
_FEWER_TTL = 4 * 3600


async def _fewer_streams(ctx: Context, feed: str, count: int, confirmation_id: str | None) -> dict[str, Any] | None:
    """A survey call reading fewer than SURVEY_STREAMS streams is the user's to ask for, confirmed in a form (seen in
    VS Code: an agent surveyed a live feed 3 streams at a time, and the user had to ask for a larger sample). The
    answer holds for that feed and count this session, so later calls go on unasked. None when it may go ahead."""
    from tools.plan import _user
    key = (_user(ctx), feed.lower())
    try:
        store = ctx.lifespan_context.setdefault(_FEWER, {})
    except Exception:
        store = {}
    agreed = store.get(key)
    if agreed and agreed['count'] == count and time.time() - agreed['at'] < _FEWER_TTL:
        return None
    gate = await consent_from(ctx).require(
        ctx, 'confirmation', 'survey_feed',
        f"Survey feed '{feed}' only {count} stream(s) a call (the default is {SURVEY_STREAMS})",
        {'feed': feed, 'streams a call': count, 'default': SURVEY_STREAMS,
         'fewer streams': "rarer kinds of event are less likely to be found in a call; the survey still goes on "
                          "until it is covered"},
        confirmation_id)
    if gate:
        return gate
    store[key] = {'count': count, 'at': time.time()}
    return None


async def _in_a_build(ctx: Context, feed: str) -> bool:
    """Whether a feed is one a build made (its uploaded samples, a test feed); not for an existing feed, or when it
    can't be told."""
    from urllib.parse import quote
    try:
        ref = await gateway_from(ctx).get(f'/feed/v1/getDocRefForName/{quote(feed, safe="")}')
        return bool(ref and ref.get('uuid') and await guard_from(ctx).build_of(
            {'type': 'Feed', 'uuid': ref['uuid'], 'name': ref.get('name') or feed}))
    except Exception:
        return True


FEED_CHARS = 250_000          # a part's head, for a feed's streams (any size)
SAMPLE_CHARS = 2_000_000      # a sample stream's (stream_ids), read whole when it is a sample's size


async def read_head(stroom: StroomGateway, stream_id: int, part: int, chars: int) -> tuple[str, bool, int]:
    """The first `chars` characters of one part: (text, cut off, number of parts in the stream). Read in pages:
    Stroom returns about 80,000 characters of a big part a fetch, whatever is asked (seen in production: a survey
    allowed 250,000 characters read 108 of a sample's 1,000 records, and said it had read them all)."""
    from tools.streams import NO_DATA
    from utils.charsets import read_as_bytes, undecodable

    class Undecodable(Exception):
        pass

    async def fetch(offset: int, length: int) -> dict[str, Any]:
        body = await stroom.post('/data/v1/fetch', {
            'sourceLocation': {'metaId': stream_id, 'partIndex': part, 'recordIndex': 0,
                               'dataRange': {'charOffsetFrom': offset, 'length': length}},
            'displayMode': 'TEXT', 'showAsHtml': False, 'recordCount': 1, 'expandedSeverities': []})
        if undecodable(body.get('errors')):
            raise Undecodable()
        if body.get('errors'):
            raise ToolError(f"Stroom could not read stream {stream_id}: {'; '.join(body['errors'])}")
        return body
    try:
        return await _read_head_text(fetch, chars, NO_DATA)
    except Undecodable:
        # Bytes the feed's character set can't decode, anywhere in what is read: Stroom won't show the text, but its
        # pipelines read on, replacing them. Read as they do, through the bytes (utils/charsets).
        return await read_as_bytes(stroom, stream_id, part, chars)


def _charset_entry(stroom: StroomGateway, stream_id: int) -> dict[str, Any]:
    """For a stream read through its bytes: how many its character set couldn't decode, and what to do."""
    from utils.charsets import report_for
    report = report_for(stroom, stream_id)
    return {'undecodable_bytes': {k: report[k] for k in ('encoding', 'undecodable_bytes', 'as_windows_1252', 'advice')}} \
        if report else {}


async def _read_head_text(fetch, chars: int, no_data: str) -> tuple[str, bool, int]:
    body = await fetch(0, chars)
    text = body.get('data') or ''
    total = (body.get('totalCharacterCount') or {}).get('count')
    parts = (body.get('totalItemCount') or {}).get('count') or 1
    pages = [text]
    # Where the next page starts, as Stroom counts (UTF-16 units: an emoji is two). Counted in Python's characters, a
    # page after any emoji started that many characters early, read them twice, and the join made records that aren't
    # in the data (seen in production: a survey of tweets found <data ne="..."> and valvalue= in clean streams).
    have = stroom_chars(text)
    while total and have < min(chars, total - 1):
        # The next page starts after what came back (Stroom widens a range to whole characters and lines).
        data = (await fetch(have, chars - have)).get('data') or ''
        if not data or data.strip() == no_data:
            break
        pages.append(data)
        have += stroom_chars(data)
    text = ''.join(pages)
    # Stroom rounds a range out to whole lines, so a single-line stream (one big JSON array) can come back
    # whole: cut it here too.
    cut = len(text) > chars
    text = text[:chars]
    # Stroom's count can be one more than the text it returns (as raw_text finds): a whole document read as cut had its
    # last line trimmed by sample_format, and an XML sample's closing tag with it (e2e: profiled as unknown text).
    return text, cut or bool(total and total > stroom_chars(text) + 1), parts


async def survey_feed(
        ctx: Context,
        feed: Annotated[str | None, Field(description="The feed that already holds the raw data.")] = None,
        stream_type: Annotated[str, Field(description="Stream type to sample.")] = 'Raw Events',
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="Survey exactly these streams instead of picking from the feed: the sample streams an "
                        "onboarding uploaded (one per file), to see every kind of event they hold and where. Not to "
                        "pick an existing feed's streams: leave that to max_streams (fewer than 10 of them, by id, "
                        "is confirmed by the user in a form).")] = [],
        max_streams: Annotated[int, Field(ge=1, le=100, description=(
            "Streams to read in this call, spread over the feed's lifetime: leave the default (10) unless the user "
            "gives a number. More, up to 100, as they ask; fewer is confirmed by the user in a form."))] = SURVEY_STREAMS,
        skip_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="Streams already read (streams_read from earlier calls): this call picks others.")] = [],
        known_signatures: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Shape signatures from earlier calls; locations then only cover new shapes.")] = [],
        max_chars_per_stream: Annotated[int | None, Field(
            ge=1000, le=5_000_000, description="Read at most this many characters from the start of each part; "
                                               "big streams are only sampled at their head. Default 250,000, or "
                                               "SAMPLE_CHARS for stream_ids (a sample is read whole).")] = None,
        max_parts_per_stream: Annotated[int, Field(ge=1, le=20)] = 3,
        max_records_per_stream: Annotated[int | None, Field(
            ge=10, le=10_000, description="Default 1,000, or 10,000 for stream_ids.")] = None,
        examples_per_shape: Annotated[int, Field(ge=1, le=10)] = 2,
        build: Annotated[str | None, Field(
            description="Keep the survey record in this build: a Documentation doc '<feed> - Survey' with the kinds "
                        "of event, examples and survey state. An existing record is continued: its streams are "
                        "skipped and its shapes are known.")] = None,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply (fewer "
                                                                 "streams than the default).")] = None,
) -> dict[str, Any]:
    """
    Sample a feed's streams spread over its lifetime (newest, oldest, then the middle, then the quarters...)
    and group their records into shapes (kinds of event): by fields and event-naming values for structured
    data, by masked message templates for syslog and other text. Only the head of each stream is read
    (max_chars_per_stream per part, max_parts_per_stream parts), so multi-GB streams cost the same as small
    ones; a sample's streams (stream_ids) are read whole up to 2,000,000 characters, and coverage says when one
    wasn't. Every call reads all max_streams streams it picks (a quiet feed's rare kinds of event can sit behind
    several streams with nothing new: asked for by the user). The feed counts as covered (saturated) when a call
    with known_signatures reads a full max_streams with nothing new, or when every stream has been read. Returns each shape with its count, share and examples, and where each example is (stream,
    part, record) for step_records. Call again with skip_stream_ids (all streams read so far) and
    known_signatures to read further streams for kinds of event not seen yet. With a build, the results are
    kept in (and continued from) the build's survey doc. Reads only, apart from that doc.
    """
    stroom = gateway_from(ctx)
    chosen = [await _meta(stroom, i) for i in stream_ids]
    # A sample is read whole where it can be: seen in production, a 750 KB sample read only to its 108th record of
    # 1,000 hid most of its kinds of event.
    max_chars_per_stream = max_chars_per_stream or (SAMPLE_CHARS if chosen else FEED_CHARS)
    max_records_per_stream = max_records_per_stream or (10_000 if chosen else 1000)
    if not feed:
        if not chosen:
            raise ToolError("Give feed, or stream_ids")
        feed = chosen[0].get('feedName')
    else:
        from tools.feeds import resolve_feed
        feed = await resolve_feed(ctx, feed)
        if not isinstance(feed, str):
            return feed     # the user picks the feed they meant in a form
    if not chosen and max_streams < SURVEY_STREAMS:
        gate = await _fewer_streams(ctx, feed, max_streams, confirmation_id)
        if gate:
            return gate
    if chosen and len(chosen) < SURVEY_STREAMS and not await _in_a_build(ctx, feed):
        # A few of an existing feed's streams, by id: as few as a smaller max_streams, so the user's to confirm too
        # (seen in testing on 0.18.13: an existing feed onboarded from 5 streams). A sample uploaded to a build's
        # feed is read as it is.
        gate = await _fewer_streams(ctx, feed, len(chosen), confirmation_id)
        if gate:
            return gate
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
    per_stream, records_read, fmt, first_chunk = [], 0, None, None
    say = await progress_from(ctx)
    await say(f"Reading the head of {len(streams)} stream{'s' if len(streams) != 1 else ''} of {feed}")
    for n, meta in enumerate(streams, 1):
        await say.now_and_then(f"Reading stream {meta['id']} ({n} of {len(streams)}): {records_read} records read so "
                               f"far")
        new_here, read_here, cut = 0, 0, False
        parts = 1
        part = 0
        while part < min(parts, max_parts_per_stream) and read_here < max_records_per_stream:
            try:
                text, truncated, parts = await read_head(stroom, meta['id'], part, max_chars_per_stream)
            except ToolError as e:
                # A feed's oldest streams are read too, and the likeliest to be on a volume Stroom can no longer
                # read: said for that stream, the rest of the survey still made.
                per_stream.append({'stream': meta['id'], 'part': part, 'error': f"Stroom couldn't read it: {refused_reason(e)}"})
                break
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
                           'records': read_here,
                           'parts': parts, 'head_only': cut or parts > max_parts_per_stream
                           or read_here >= max_records_per_stream,
                           'new_shapes': new_here,
                           **_charset_entry(stroom, meta['id'])})
    if first_chunk is None:
        raise ToolError(f"Nothing readable in the sampled streams of '{feed}'")

    found = sorted((s for s in shapes.shapes.values() if s['count']), key=lambda s: -s['count'])
    counts = Counter({s['signature']: s['count'] for s in found})
    new_shapes = [s for s in found if not s['known']]
    to_step = new_shapes if known_signatures else found
    drop = dropped(state)
    read_ids = [p['stream'] for p in per_stream if 'records' in p]
    partly = [p['stream'] for p in per_stream if p.get('head_only')]
    # Covered: a full batch after the first added nothing new, or the feed has no unread streams left (fewer than
    # asked for were there to pick); chosen streams: every one was read, so the sample is covered as far as it goes.
    exhausted = not chosen and len(streams) < max_streams
    saturated = ((bool(known_signatures) and not new_shapes and len(read_ids) >= max_streams) or exhausted
                 or bool(chosen))
    # Not when a given stream was read only in part: the kinds of event further in weren't seen.
    saturated = saturated and not (chosen and partly)
    result = {
        'feed': feed, 'format': fmt, 'time_range': span, 'streams_read': read_ids, 'records_read': records_read,
        'saturated': saturated,
        'coverage': (f"NOT COVERED: only the start of stream(s) {partly} was read ({records_read} records); there may be "
                     f"other kinds of event further in. Survey them again with max_chars_per_stream="
                     f"{min(5_000_000, max_chars_per_stream * 4)} (up to 5,000,000)" if chosen and partly else
                     f'all {len(read_ids)} given stream(s) read whole: these are the kinds of event the sample holds'
                     if chosen else
                     'covered: every stream in the feed has been read' if exhausted else
                     f'covered: no new kinds of event in the last {len(read_ids)} streams read' if saturated else
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
    if record is None:
        # Made by a call just before this one, which Stroom's explorer can leave out for a moment: looked for again
        # now, and merged into, not refused as a twin at its creation (seen: two survey calls back to back).
        record = await _load_record(ctx, build, result['feed'])
        if record is not None:
            state = read_state(body_text(record)) or state
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
    from tools.plan import with_next
    return await with_next(ctx, build, {'feed': feed, 'handling': handling, 'shapes': signatures,
            'left_untranslated': [{'shape': s, 'reason': r} for s, r in left.items()],
            # The survey's examples of these kinds, to step with step_records.
            'locations': [{**e['location'], 'shape': s, **({'expect': 'none'} if handling == 'drop' else {})}
                          for s in signatures for e in state['shapes'][s]['examples']],
            'hint': ("Add a drop rule for these kinds to the mapping (EventRule with drop=true and conditions that "
                     "match them), regenerate, and step_records over the survey's locations: those records should "
                     "produce no Event." if handling == 'drop' else
                     "Add rules to translate these kinds, regenerate and step_records over their locations.")})


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
