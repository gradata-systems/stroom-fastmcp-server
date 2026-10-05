"""Tools for samples, feeds and source notes."""
import asyncio
import time
from urllib.parse import quote
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, model_validator

from security.guard import guard_from
from tools.streams import SampleStreams, read_sample_streams
from utils.consent import consent_from, edited
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
    Create a feed in the build folder, under the name you propose: the user confirms it, or corrects it in the
    confirmation form, before anything is made (call this rather than asking for the name in the chat first). Stroom checks the
    name against its feed-name rule; a rejected name comes back with the rule so a compliant one can be proposed.
    """
    details = {'build': build, 'feed name': name, 'encoding': encoding, 'stream type': stream_type}
    gate = await consent_from(ctx).require(ctx, 'confirmation', 'create_feed', f"Create feed '{name}'", details,
                                           confirmation_id, editable={'name': ('Feed name', name)})
    if gate:
        return gate
    name = edited(ctx, 'name', name)       # the user may have corrected it in the form
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
    result = {'type': 'Feed', 'uuid': doc['uuid'], 'name': doc['name'], 'stream_type': doc.get('streamType'),
              'encoding': doc.get('encoding')}
    # A feed of this name made before and deleted leaves its streams under the name, out of sight in the UI.
    rows = (await stroom.find_meta([{'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': doc['name']}],
                                   50)).get('values') or []
    earlier = [r['meta']['id'] for r in rows if r['meta'].get('status') != 'DELETED'
               and (r['meta'].get('createMs') or 0) < (doc.get('createTimeMs') or 0)]
    if earlier:
        result['note'] = (f"Stroom still holds {len(earlier)}{'+' if len(rows) == 50 else ''} stream(s) from an earlier feed "
                          f"named {doc['name']}, since deleted (e.g. {earlier[:5]}); the UI doesn't show them. They are "
                          f"not this build's samples: the plan and the stepping and processing tools use only the "
                          f"streams uploaded to this feed from now on. Tell the user.")
    from tools.plan import with_next
    return await with_next(ctx, build, result)


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
    field: str = Field(description="The field's name as the sample has it (a column, key or element).")
    meaning: str
    type: str = ''
    example: str = ''
    event_logging_path: str = Field('', description="The event-logging element it belongs in, e.g. EventSource/User/Id; "
                                                    "the draft maps the field there.")
    values: dict[str, str] = Field(default_factory=dict, description="The field's codes and what each means, from the "
                                                                     "documentation, e.g. {'0x0': 'success'}.")


class EventNote(BaseModel):
    event: str = Field(description="Source event id or action, e.g. '4624' or 'CODE_TO_TOKEN'.")
    description: str
    event_detail: str = Field('', description="The EventDetail action element, e.g. Authenticate.")
    type_id: str = ''
    field: str = Field('', description="The field whose value shows this event in a record, e.g. 'evt'; with value, the "
                                       "draft makes a rule for it and build_translation_xslt checks the mapping against it.")
    value: str = Field('', description="That field's value for this event, e.g. '4625'.")
    action: str = Field('', description="The action element's Action, when it has one, e.g. Logon or Logoff.")
    success: bool | None = Field(None, description="The event's outcome, when the documentation says it, e.g. False for "
                                                   "a failed logon.")


class ReferenceDocument(BaseModel):
    title: str = Field(description="The document's title, e.g. 'Acme door controller event reference v3'.")
    text: str = Field('', description="Its text, verbatim, as Markdown or plain text, as the user gave it (attached in "
                                      "their client). A document too long for one call goes in parts, one call each, "
                                      "titled '<title> part 1', '<title> part 2' and so on.")
    uuid: str = Field('', description="Instead of text: a Documentation doc already in Stroom holding it (one the user "
                                      "made in the Stroom UI, say).")
    source: str = Field('', description="Where it came from: a URL or file name.")

    @model_validator(mode='after')
    def text_or_uuid(self):
        if bool(self.text.strip()) == bool(self.uuid):
            raise ValueError("give the document's text, or the uuid of the Documentation doc holding it, not both")
        return self


async def record_source_notes(
        ctx: Context,
        build: Build,
        source: Annotated[str, Field(description="Source name, e.g. 'Acme door controller'.")],
        summary: Annotated[str, Field(description="What the documentation says about the source, in a few lines.")],
        fields: Annotated[list[FieldNote] | str, ONE_OR_MORE, Field(description="Field dictionary condensed from the documentation.")] = [],
        events: Annotated[list[EventNote] | str, ONE_OR_MORE, Field(description="Event catalogue condensed from the documentation.")] = [],
        references: Annotated[list[str] | str, ONE_OR_MORE, Field(description="Titles or links of the documents used.")] = [],
        documents: Annotated[list[ReferenceDocument] | str, ONE_OR_MORE, Field(
            description="The user's documents themselves, kept verbatim in Stroom as Documentation docs "
                        "'<source> reference - <title>', to read and search later (find_documents content=an event "
                        "id).")] = [],
) -> dict[str, Any]:
    """
    Keep the user's vendor or event reference documentation in Stroom, and the notes condensed from it, as
    Documentation docs in the build: each document verbatim ('<source> reference - <title>'), and '<source> source
    notes' with the field dictionary and event catalogue. The notes are read back by the tools: draft_translation_mapping
    (build=) drafts from them (fields where the dictionary puts them, a rule per catalogued event that gives the field
    and value showing it), build_translation_xslt (build=) checks a mapping against the catalogue, and
    write_documentation lists the source fields with their meanings. Promotion puts them beside the feed.
    """
    from utils.sourcenotes import REFERENCE_INFIX, block
    fields = [FieldNote.model_validate(f) if isinstance(f, dict) else f for f in fields]
    events = [EventNote.model_validate(e) if isinstance(e, dict) else e for e in events]
    documents = [ReferenceDocument.model_validate(d) if isinstance(d, dict) else d for d in documents]
    stroom = gateway_from(ctx)
    guard = guard_from(ctx)
    kept_docs = []
    for d in [d for d in documents if d.uuid]:
        # Already in Stroom (uploaded by the user): kept where it is, and named in the notes.
        existing = await stroom.get_doc('Documentation', d.uuid)
        kept_docs.append({'uuid': d.uuid, 'name': existing.get('name'), 'already_in_stroom': True})
    for d in [d for d in documents if not d.uuid]:
        name = f"{source}{REFERENCE_INFIX}{d.title}"[:200]
        text = f"# {d.title}\n\n" + (f"Source: {d.source}\n\n" if d.source else '') + d.text.strip() + '\n'

        async def keep(ref: dict[str, Any], text: str = text) -> dict[str, Any]:
            doc = await stroom.get_doc('Documentation', ref['uuid'])
            set_body_text(doc, text)
            return await stroom.put_doc(doc)
        kept = await guard.create_filled('Documentation', name, build, keep)
        kept_docs.append({'uuid': kept['uuid'], 'name': kept['name']})
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
    if references or kept_docs:
        lines += ['## Sources', ''] + [f'- {r}' for r in references] + [f"- `{d['name']}` (kept in Stroom)" for d in kept_docs]
    lines += ['', block({'source': source, 'fields': [f.model_dump(exclude_defaults=True) for f in fields],
                         'events': [e.model_dump(exclude_defaults=True) for e in events],
                         'documents': [d['name'] for d in kept_docs]})]

    async def write(ref: dict[str, Any]) -> dict[str, Any]:
        doc = await stroom.get_doc('Documentation', ref['uuid'])
        set_body_text(doc, '\n'.join(lines) + '\n')
        return await stroom.put_doc(doc)

    doc = await guard.create_filled('Documentation', f'{source} source notes', build, write)
    from tools.plan import with_next
    usable = [e.event for e in events if e.field and e.value]
    return await with_next(ctx, build, {'type': 'Documentation', 'uuid': doc['uuid'], 'name': doc['name'], 'fields': len(fields),
            'events': len(events), 'documents_kept': kept_docs,
            'hint': ("draft_translation_mapping with build= drafts from these notes" + (
                f"; events without a field and value ({len(events) - len(usable)}) cannot become rules: give them "
                f"where the documentation says how a record shows the event" if len(usable) < len(events) else '') + ".")})


ALL_TOOLS = [profile_sample, create_feed, upload_sample, record_source_notes]
