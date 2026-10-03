"""Tools for samples, feeds and source notes."""
import asyncio
import time
from urllib.parse import quote
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from security.guard import guard_from
from tools.streams import SampleStreams, read_sample_streams
from utils.consent import consent_from
from utils.params import ONE_OR_MORE
from utils.profile import profile, profile_many
from utils.samples import SampleTexts, as_named_samples, check_sample
from utils.stroom import gateway_from, set_body_text

Build = Annotated[str, Field(description="Build name; its workspace folder is created if needed.")]


async def profile_sample(
        ctx: Context,
        sample: Annotated[str | None, Field(description="A representative sample of the raw data, several records long.")] = None,
        samples: Annotated[SampleTexts | None, Field(
            description="Several sample files of the same source: their texts, by file name or as a list. Profiled "
                        "each and together: fields and timestamp shapes only some files have are reported, as a mapping "
                        "built from one file breaks on the others. Prefer this whenever the user has more than one file.")] = None,
        stream_ids: SampleStreams = [],
) -> dict[str, Any]:
    """
    Profile raw data locally (nothing is sent to Stroom): its format (XML document or fragments, JSON array or
    lines, delimited with or without a header, RFC 3164/5424 syslog, key=value), record structure, and each
    field's fill rate, inferred type and examples. Timestamps get a stroom:format-date pattern inferred from
    their values; string fields holding JSON are flagged. Says which parser and template to use, whether a text
    converter is needed, and for JSON the parser setting. With several files, also what differs between them.
    """
    notes = []
    if stream_ids and sample is None and samples is None:
        named, notes = await read_sample_streams(ctx, stream_ids)
    else:
        named = as_named_samples(samples, sample)
    if not named:
        raise ToolError("Give sample (the file's text), samples (several files' texts), or stream_ids")
    result = profile_many(named) if len(named) > 1 else profile(next(iter(named.values())))
    return {**result, 'read': notes} if notes else result


async def create_feed(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Feed name following the environment's convention, e.g. 'ACME-VPN-V1.0'.")],
        encoding: Annotated[str, Field(description="Character encoding of the data.")] = 'UTF-8',
        stream_type: Annotated[str, Field(description="Stream type received data is stored as.")] = 'Raw Events',
        description: Annotated[str, Field(description="What the feed carries and where it comes from.")] = '',
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Create a feed in the build folder. The user confirms the name and encoding first. Stroom checks the
    name against its feed-name rule; a rejected name comes back with the rule so a compliant one can be proposed.
    """
    details = {'build': build, 'feed name': name, 'encoding': encoding, 'stream type': stream_type}
    gate = await consent_from(ctx).require(ctx, 'confirmation', 'create_feed', f"Create feed '{name}'", details,
                                           confirmation_id)
    if gate:
        return gate
    stroom = gateway_from(ctx)
    try:
        ref = await guard_from(ctx).create('Feed', name, build)
    except ToolError as e:
        if 'Invalid name' in str(e):
            raise ToolError(f"Stroom rejected the feed name: {e}. Propose a name that matches the pattern shown.") from e
        raise
    doc = await stroom.get_doc('Feed', ref['uuid'])
    doc.update(encoding=encoding, streamType=stream_type, description=description)
    doc = await stroom.put_doc(doc)
    from tools.plan import with_next
    return await with_next(ctx, build, {'type': 'Feed', 'uuid': doc['uuid'], 'name': doc['name'],
                                        'stream_type': doc.get('streamType'), 'encoding': doc.get('encoding')})


async def upload_sample(
        ctx: Context,
        feed: Annotated[str, Field(description="Feed name, e.g. one created with create_feed.")],
        sample: Annotated[str, Field(description="The raw data to send, exactly as the source produces it.")],
        headers: Annotated[dict[str, str] | None, Field(
            description="Extra receipt headers, e.g. {'MyHost': 'ws01'}; readable in XSLT with stroom:meta().")] = None,
        stream_type: Annotated[str, Field(description="'Raw Events', or 'Raw Reference' for a reference feed.")] = 'Raw Events',
        effective_time: Annotated[str | None, Field(
            description="Reference data only: from when it applies (ISO 8601 UTC). A lookup uses the reference data in "
                        "effect at the event stream's time, so give a time before the events, e.g. "
                        "'2000-01-01T00:00:00.000Z' for a table that always applied. Default: now, which is after "
                        "any sample already uploaded.")] = None,
) -> dict[str, Any]:
    """
    Send sample data to a feed through Stroom's datafeed receiver, as the real source would, and return
    the receipt id and the raw stream it created. Upload each sample file as its own call, so each becomes
    a stream and every file is stepped. Only upload to feeds in a build (test feeds for updates), never to a
    production feed whose processor filters would pick the data up.
    """
    check_sample(sample)
    stroom = gateway_from(ctx)
    # A direct lookup: the explorer search index lags new documents by a moment.
    match = await stroom.get(f'/feed/v1/getDocRefForName/{quote(feed, safe="")}')
    if not match:
        raise ToolError(f"No feed named '{feed}'")
    await guard_from(ctx).check_managed(match)
    started = int(time.time() * 1000) - 1000
    receipt = {'Type': stream_type, **({'EffectiveTime': effective_time} if effective_time else {}), **(headers or {})}
    response = await stroom.datafeed(feed, sample.encode('utf-8'), receipt)
    terms = [{'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': feed},
             {'type': 'term', 'field': 'Type', 'condition': 'EQUALS', 'value': stream_type}]
    for _ in range(20):
        rows = (await stroom.find_meta(terms, 5)).get('values') or []
        fresh = [r['meta'] for r in rows if (r['meta'].get('createMs') or 0) >= started]
        if fresh:
            from tools.plan import build_of, with_next
            return await with_next(ctx, await build_of(ctx, match), {
                'feed': feed, 'receipt_id': response.text.strip(), 'stream_id': fresh[0]['id'],
                'bytes': len(sample.encode('utf-8')),
                'hint': "One stream per sample file: upload the next file, or go on with the plan (next)."})
        await asyncio.sleep(0.5)
    return {'feed': feed, 'receipt_id': response.text.strip(), 'stream_id': None,
            'hint': "Stroom accepted the data but the stream is not visible yet; check find_streams shortly."}


class FieldNote(BaseModel):
    field: str
    meaning: str
    type: str = ''
    example: str = ''
    event_logging_path: str = Field('', description="Suggested event-logging element, e.g. EventSource/User/Id.")


class EventNote(BaseModel):
    event: str = Field(description="Source event id or action, e.g. '4624' or 'CODE_TO_TOKEN'.")
    description: str
    event_detail: str = Field('', description="Suggested EventDetail action element, e.g. Authenticate.")
    type_id: str = ''


async def record_source_notes(
        ctx: Context,
        build: Build,
        source: Annotated[str, Field(description="Source name, e.g. 'Acme door controller'.")],
        summary: Annotated[str, Field(description="What the documentation says about the source, in a few lines.")],
        fields: Annotated[list[FieldNote] | str, ONE_OR_MORE, Field(description="Field dictionary condensed from the documentation.")] = [],
        events: Annotated[list[EventNote] | str, ONE_OR_MORE, Field(description="Event catalogue condensed from the documentation.")] = [],
        references: Annotated[list[str] | str, ONE_OR_MORE, Field(description="Titles or links of the documents used.")] = [],
) -> dict[str, Any]:
    """
    Save the field dictionary and event catalogue condensed from user-supplied vendor documentation or
    annotated samples, as a Documentation doc '<source> source notes' in the build. Drafting uses these for
    field meanings and event types; later updates and evaluations can read them back with describe_document.
    """
    lines = [f'# {source} source notes', '', summary.strip(), '']
    if fields:
        lines += ['## Field dictionary', '', '| Field | Meaning | Type | Example | Event-logging path |',
                  '| --- | --- | --- | --- | --- |']
        lines += [f'| {f.field} | {f.meaning} | {f.type} | {f.example} | {f.event_logging_path} |' for f in fields]
        lines.append('')
    if events:
        lines += ['## Event catalogue', '', '| Event | Description | EventDetail | TypeId |', '| --- | --- | --- | --- |']
        lines += [f'| {e.event} | {e.description} | {e.event_detail} | {e.type_id} |' for e in events]
        lines.append('')
    if references:
        lines += ['## Sources', ''] + [f'- {r}' for r in references]
    stroom = gateway_from(ctx)

    async def write(ref: dict[str, Any]) -> dict[str, Any]:
        doc = await stroom.get_doc('Documentation', ref['uuid'])
        set_body_text(doc, '\n'.join(lines) + '\n')
        return await stroom.put_doc(doc)

    doc = await guard_from(ctx).create_filled('Documentation', f'{source} source notes', build, write)
    from tools.plan import with_next
    return await with_next(ctx, build, {'type': 'Documentation', 'uuid': doc['uuid'], 'name': doc['name'], 'fields': len(fields),
            'events': len(events)})


ALL_TOOLS = [profile_sample, create_feed, upload_sample, record_source_notes]
