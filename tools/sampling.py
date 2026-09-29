"""Sampling an existing feed to learn which kinds of event it holds."""
from collections import Counter
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from tools.streams import _term, read_records
from utils.stroom import gateway_from
from utils.survey import Chunk, Shapes, share, split_records

CHARS_PER_STREAM = 2_000_000


async def survey_feed(
        ctx: Context,
        feed: Annotated[str, Field(description="The feed that already holds the raw data.")],
        stream_type: Annotated[str, Field(description="Stream type to sample.")] = 'Raw Events',
        max_streams: Annotated[int, Field(ge=1, le=100, description="Most streams to read in this call.")] = 10,
        max_records: Annotated[int, Field(ge=10, le=50_000, description="Most records to read in this call.")] = 5000,
        before_stream_id: Annotated[int | None, Field(
            description="Continue further back: only streams older than this id (oldest_stream_read from the last call).")] = None,
        known_signatures: Annotated[list[str], Field(
            description="Shape signatures from earlier calls; the sample then only carries new shapes.")] = [],
        quiet_streams: Annotated[int, Field(ge=1, le=20, description="Stop after this many streams in a row add no "
                                                                     "new shape.")] = 3,
        examples_per_shape: Annotated[int, Field(ge=1, le=10)] = 2,
) -> dict[str, Any]:
    """
    Sample a feed's streams, newest first, and group their records into shapes (kinds of event): by fields
    and event-naming values for structured data, by masked message templates for syslog and other text.
    Stops when quiet_streams streams in a row show nothing new (saturated) or at the limits. Returns each
    shape with its count, share and examples, and where each example is (stream, part, record), so the
    translation can be stepped on the feed's own records with step_records: nothing is copied or processed.
    Call again with before_stream_id and known_signatures to look further back for kinds of event not seen
    yet. Reads only.
    """
    stroom = gateway_from(ctx)
    terms = [_term('Feed', feed), _term('Type', stream_type)]
    if before_stream_id:
        terms.append(_term('Id', before_stream_id, 'LESS_THAN'))
    streams = [r['meta'] for r in (await stroom.find_meta(terms, max_streams)).get('values') or []
               if r['meta'].get('status') != 'DELETED']
    if not streams and before_stream_id:
        return {'feed': feed, 'streams_read': [], 'records_read': 0, 'oldest_stream_read': None, 'saturated': True,
                'shapes': [], 'new_shapes': 0, 'locations': [],
                'hint': "No older streams: the whole feed has been surveyed."}
    if not streams:
        raise ToolError(f"No {stream_type} streams in feed '{feed}'" + (f" older than {before_stream_id}" if before_stream_id else ''))
    shapes = Shapes(known_signatures, examples_per_shape)
    counts: Counter = Counter()
    per_stream, quiet, records_read, fmt, first_chunk = [], 0, 0, None, None
    for meta in streams:
        parts, _ = await read_records(stroom, meta['id'], 0, 100, None, CHARS_PER_STREAM)
        new_here = 0
        for part, text in enumerate(parts):
            try:
                chunk = split_records(text, max_records - records_read)
            except Exception as e:  # a part this detector cannot split
                per_stream.append({'stream': meta['id'], 'error': f"could not split records: {e}"})
                continue
            if fmt and chunk.format != fmt:
                per_stream.append({'stream': meta['id'], 'note': f"reads as {chunk.format}, not {fmt}; skipped"})
                continue
            fmt, first_chunk = fmt or chunk.format, first_chunk or chunk
            for index in range(len(chunk.records)):
                new = shapes.add(chunk, index, meta['id'], part)
                new_here += new
                records_read += 1
        for shape in shapes.shapes.values():
            counts[shape['signature']] = shape['count']
        per_stream.append({'stream': meta['id'], 'created': meta.get('createMs'), 'new_shapes': new_here})
        quiet = 0 if new_here else quiet + 1
        if quiet >= quiet_streams or records_read >= max_records:
            break
    if first_chunk is None:
        raise ToolError(f"Nothing readable in the sampled streams of '{feed}'")

    found = sorted((s for s in shapes.shapes.values() if s['count']), key=lambda s: -s['count'])
    new_shapes = [s for s in found if not s['known']]
    to_step = new_shapes if known_signatures else found
    locations = [{**e['location'], 'shape': s['signature']} for s in to_step for e in s['examples']]
    read_ids = [p['stream'] for p in per_stream if 'stream' in p]
    saturated = quiet >= quiet_streams
    return {
        'feed': feed, 'format': fmt, 'streams_read': read_ids, 'records_read': records_read,
        'oldest_stream_read': min(read_ids) if read_ids else None, 'saturated': saturated,
        'per_stream': per_stream,
        'shapes': [{'signature': s['signature'], 'new': not s['known'], 'count': s['count'],
                    'share_percent': share(counts, s['signature']), 'streams': s['streams'][:10],
                    'example': s['examples'][0]['text'][:500] if s['examples'] else None,
                    'locations': [e['location'] for e in s['examples']]} for s in found][:60],
        'new_shapes': len(new_shapes),
        'locations': locations,
        'hint': _hint(saturated, new_shapes, known_signatures, read_ids),
    }


def _hint(saturated: bool, new_shapes: list, known: list[str], read_ids: list[int]) -> str:
    if known and not new_shapes:
        return ("No new kinds of event in these streams." + (" The feed looks covered." if saturated else
                " Look further back with before_stream_id=oldest_stream_read if the feed is older."))
    return ("Translate every shape and check it with step_records(pipeline, locations, draft_code): the feed's own "
            "records are stepped where they are. Then call again with before_stream_id=oldest_stream_read and "
            "known_signatures=[every signature so far] to look for kinds of event these streams did not show.")


__all__ = ['survey_feed', 'Chunk']
ALL_TOOLS = [survey_feed]
