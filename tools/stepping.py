"""Tools that step pipelines record by record, optionally with draft (unsaved) code."""
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from security.guard import MANAGED, guard_from
from tools.pipelines import merge_layers, own_elements, translation_docs
from utils.params import ONE_OR_MORE
from utils.stroom import StroomGateway, gateway_from
from utils.accepted import accepted_for
from utils.triage import from_stored_error, triage

logger = logging.getLogger(__name__)

PipelineUuid = Annotated[str, Field(description="UUID of the pipeline to step.")]
DraftCode = Annotated[dict[str, str] | None, Field(
    description="Unsaved code to step with instead of the saved documents, keyed by element id, "
                "e.g. {'translationFilter': '<xsl:stylesheet ...>'}. Nothing is saved.")]


async def _check_drafts(ctx: Context, draft_code: dict[str, str] | None) -> None:
    """Refuse draft XSLT that check_xslt rejects, with its reasons. Stepping would otherwise report the symptom
    only: an unknown stroom: function as a compile error, or a missing namespace as empty output."""
    from tools.validation import XSL as XSL_NS, check_xslt
    for element, code in (draft_code or {}).items():
        if XSL_NS not in code:
            continue
        result = await check_xslt(ctx, code)
        if not result['ok']:
            raise ToolError(f"draft_code[{element!r}] is not stepped: " + '; '.join(result['errors']))


# Records stepped from the head of each sample stream unless told otherwise.
PER_STREAM = 50


class _Pipeline:
    """What stepping needs to know about a pipeline, fetched once per tool call."""

    def __init__(self, doc: dict[str, Any], layers: list[dict[str, Any]]):
        self.doc = doc
        merged = merge_layers(layers)
        self.types = {e['id']: e['type'] for e in merged['elements']}
        self.own = own_elements(layers)
        # A JSON parser that wraps everything in one root map makes a JSON array a single record.
        self.json_root_map = any(t == 'JSONParser' and not any(
            p.get('element') == e and p.get('name') == 'addRootObject' and p.get('value') is False
            for p in merged.get('properties') or []) for e, t in self.types.items())

    @classmethod
    async def load(cls, stroom: StroomGateway, uuid: str) -> '_Pipeline':
        return cls(await stroom.get(f'/pipeline/v1/{uuid}'), await stroom.pipeline_layers(uuid))

    def default_outputs(self) -> list[str]:
        """The pipeline's own XSLT steps, which is where its translation happens."""
        own_xslt = [e for e, t in self.types.items() if e in self.own and t == 'XSLTFilter']
        return own_xslt or [e for e, t in self.types.items() if t == 'XSLTFilter'][-1:]


async def code_fingerprint(stroom: StroomGateway, pipeline_uuid: str,
                           draft_code: dict[str, str] | None = None) -> dict[str, str]:
    """Per element the pipeline sets its own code for, a hash of the code that runs: the draft where one is
    given, else the saved XSLT or text converter."""
    prints = {}
    for doc in translation_docs(pipeline_uuid, await stroom.pipeline_layers(pipeline_uuid)):
        if doc['inherited_from_template']:
            continue
        code = (draft_code or {}).get(doc['element'])
        if code is None:
            code = (await stroom.get_doc(doc['doc']['type'], doc['doc']['uuid'])).get('data') or ''
        prints[doc['element']] = hashlib.sha256(code.encode()).hexdigest()
    return prints


# Clean steps are recorded as explorer tags on the pipeline, 'mcp-stepped-<UTC time>-<code digest>', so
# every replica sees them and they survive restarts. Only pipelines the server manages (in a build) are
# tagged: stepping anything else stays read-only. Promotion removes them with mcp-managed.
STEPPED = 'mcp-stepped-'
KEEP_STEPPED = 5


def fingerprint_digest(prints: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(prints, sort_keys=True).encode()).hexdigest()[:16]


def stepped_tags(tags: list[str]) -> list[str]:
    return sorted(t for t in tags if t.startswith(STEPPED))


async def remember_clean(ctx: Context, pipeline: dict[str, Any], draft_code: dict[str, str] | None,
                         result: dict[str, Any]) -> None:
    """Record the code a clean step_sample or step_records ran, for promotion's checks (the last few runs)."""
    if result.get('verdict') != 'clean' or not result.get('records_stepped'):
        return
    guard = guard_from(ctx)
    ref = {k: pipeline[k] for k in ('type', 'uuid', 'name')}
    try:
        tags = await guard.tags(ref)
        if MANAGED not in tags:
            return
        digest = fingerprint_digest(await code_fingerprint(gateway_from(ctx), ref['uuid'], draft_code))
        mine = stepped_tags(tags)
        if any(t.endswith(f'-{digest}') for t in mine):
            return
        await guard.tag([ref], [f"{STEPPED}{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{digest}"])
        if len(mine) >= KEEP_STEPPED:
            await guard.untag([ref], mine[:len(mine) - KEEP_STEPPED + 1])
    except Exception as e:  # the record is a convenience; never fail the step over it
        logger.warning("Couldn't record a clean step on pipeline %s: %s", ref['uuid'], e)


async def stepped_clean(ctx: Context, pipeline: dict[str, Any]) -> bool:
    """Whether the pipeline's saved code is code that has stepped clean (a recent run, on any replica)."""
    ref = {k: pipeline[k] for k in ('type', 'uuid', 'name')}
    mine = stepped_tags(await guard_from(ctx).tags(ref))
    if not mine:
        return False
    digest = fingerprint_digest(await code_fingerprint(gateway_from(ctx), ref['uuid']))
    return any(t.endswith(f'-{digest}') for t in mine)


# An indexing pipeline whose sample was indexed and found by verify_index's searches is recorded the same way,
# 'mcp-verified-<UTC time>-<code digest>': the plan's 'indexed' step is done only then, not when it steps clean.
VERIFIED = 'mcp-verified-'


def verified_tags(tags: list[str]) -> list[str]:
    return sorted(t for t in tags if t.startswith(VERIFIED))


async def remember_verified(ctx: Context, pipeline: dict[str, Any]) -> bool:
    """Record that the indexing pipeline's current code indexed the sample and verify_index found it."""
    guard = guard_from(ctx)
    ref = {k: pipeline[k] for k in ('type', 'uuid', 'name')}
    try:
        tags = await guard.tags(ref)
        if MANAGED not in tags:
            return False
        digest = fingerprint_digest(await code_fingerprint(gateway_from(ctx), ref['uuid']))
        mine = verified_tags(tags)
        if not any(t.endswith(f'-{digest}') for t in mine):
            await guard.tag([ref], [f"{VERIFIED}{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{digest}"])
            if len(mine) >= KEEP_STEPPED:
                await guard.untag([ref], mine[:len(mine) - KEEP_STEPPED + 1])
        return True
    except Exception as e:  # the record is a convenience; never fail the verification over it
        logger.warning("Couldn't record a verified index on pipeline %s: %s", ref['uuid'], e)
        return False


async def verified(ctx: Context, pipeline: dict[str, Any]) -> bool:
    """Whether verify_index passed for the indexing pipeline's current code."""
    ref = {k: pipeline[k] for k in ('type', 'uuid', 'name')}
    mine = verified_tags(await guard_from(ctx).tags(ref))
    if not mine:
        return False
    digest = fingerprint_digest(await code_fingerprint(gateway_from(ctx), ref['uuid']))
    return any(t.endswith(f'-{digest}') for t in mine)


def record_key(stream_id: int, location: dict[str, Any]) -> str:
    """'stream:record', or 'stream:part:record' past the first part (record numbers restart in each part)."""
    part = location.get('partIndex') or 0
    return f"{stream_id}:{part}:{location['recordIndex']}" if part else f"{stream_id}:{location['recordIndex']}"


def _criteria(stream_id: int) -> dict[str, Any]:
    # Stroom's booleans given, not left null: a null one is an ERROR in Stroom's log on every request.
    return {'expression': {'type': 'operator', 'op': 'AND', 'children': [
        {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': str(stream_id)}]}, 'fetchRelationships': False}


async def _step(stroom: StroomGateway, pipeline: _Pipeline, stream_id: int, step_type: str,
                location: dict[str, Any] | None, code: dict[str, str] | None) -> dict[str, Any]:
    request = {'pipelineDoc': pipeline.doc, 'criteria': _criteria(stream_id), 'stepType': step_type, 'stepSize': 1,
               'timeout': stroom.settings.stroom_stepping_wait_ms, 'code': code or {}}
    if location:
        request['stepLocation'] = location
    return await stroom.step(request)


_ELEMENT = re.compile(r'<[A-Za-z_]')


def _empty_output(result: dict[str, Any], element: str, record: Any) -> list[dict[str, Any]]:
    """A marker when an XSLT produced no elements at all.

    When no template matches the input (typically a wrong xpath-default-namespace), XSLT's built-in
    rules copy the text through, nothing raises an error, and processing silently writes nothing.
    """
    output = (((result.get('stepData') or {}).get('elementMap') or {}).get(element) or {}).get('output') or ''
    body = re.sub(r'^\s*<\?xml[^>]*\?>', '', output)
    if _ELEMENT.search(body):
        return []
    return [{'severity': 'ERROR', 'element': element, 'record': record, 'location': None,
             'message': "Output contains no XML elements: the XSLT's templates did not match the input. "
                        "Check xpath-default-namespace and the match patterns against the element's input."}]


_UNKNOWN = re.compile(r'<(?:[\w.-]+:)?Unknown[\s>/]')     # EventDetail/Unknown: the schema's only Unknown


def _writes_unknown(result: dict[str, Any], element: str) -> bool:
    output = (((result.get('stepData') or {}).get('elementMap') or {}).get(element) or {}).get('output') or ''
    return bool(_UNKNOWN.search(output))


async def _stream_is_array(stroom: StroomGateway, stream_id: int) -> bool:
    try:
        from tools.sampling import read_head
        from utils.profile import profile
        text, _, _ = await read_head(stroom, stream_id, 0, 20_000)
        return profile(text)['format'] == 'json array'
    except Exception:
        return False


_TYPE_ID = re.compile(r'<(?:[\w.-]+:)?TypeId>([^<]*)<')
_DATA = re.compile(r'<(?:[\w.-]+:)?Data\s+Name="([^"]*)"\s+Value="([^"]*)"')


def _what_unknown_holds(outputs: list[str]) -> str:
    """What the Unknown events hold, for a person to judge: their TypeIds and Data values, a few of each."""
    values: dict[str, list[str]] = {}
    for out in outputs:
        for name, value in [('TypeId', t) for t in _TYPE_ID.findall(out)] + _DATA.findall(out):
            seen = values.setdefault(name, [])
            if value and value not in seen and len(seen) < 5:
                seen.append(value)
    return '; '.join(f"{k}: {', '.join(v)}" for k, v in list(values.items())[:6] if v)


async def _unagreed_unknown(ctx: Context, pipeline_uuid: str, name: str | None, element: str, unknown: list[str],
                            total: int, draft_code: dict[str, str] | None, held: str = '') -> dict[str, Any] | None:
    """A blocking group when a build's own pipeline writes EventDetail/Unknown from an XSLT saved without a mapping:
    build_translation_xslt refuses Unknown where the records show an action and puts the rest to the user, and a
    hand-written XSLT got past both (seen: every TRAFFIC record of a firewall sample as an empty Unknown)."""
    if not unknown or draft_code:
        return None
    try:
        tags = await guard_from(ctx).tags({'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': name})
        if MANAGED not in tags or any(t.startswith('mcp-copy-of-') for t in tags):
            return None     # not the build's own new pipeline: someone else's design
        from tools.builds import kept_mapping
        if await kept_mapping(ctx, pipeline_uuid):
            return None     # saved from a mapping, where Unknown was checked and agreed
    except Exception as e:   # the check is a guard on top; never fail the step over it
        logger.warning("Couldn't check pipeline %s for Unknown events: %s", pipeline_uuid, e)
        return None
    message = (f"{len(unknown)} of {total} records come out as EventDetail/Unknown (e.g. {unknown[0]}"
               + (f"; they hold {held}" if held else '') + "), from an XSLT "
               f"saved without a mapping, so nobody agreed to Unknown for them. Records whose values show what happened "
               f"take that action element: a connection allowed or denied Network/Permit or Network/Deny, a logon "
               f"Authenticate, a configuration change Update, an alert Alert. Draft the mapping from the sample "
               f"(draft_translation_mapping) and save the XSLT with build_translation_xslt: it checks each rule against "
               f"the records, and puts Unknown to the user for the records nothing else describes.")
    return {'class': 'blocking', 'reason': 'Events written as EventDetail/Unknown that nobody agreed to',
            'severity': 'ERROR', 'element': element, 'own_element': True, 'count': len(unknown),
            'records_affected': len(unknown), 'examples': [{'message': message, 'location': None}],
            'records': sorted(unknown)[:20]}


def _block(summary: dict[str, Any], group: dict[str, Any] | None) -> None:
    if group:
        summary['groups'].insert(0, group)
        summary['groups_by_class']['blocking'] = summary['groups_by_class'].get('blocking', 0) + 1
        summary['verdict'] = 'blocking'


def _nothing_stepped(result: dict[str, Any], markers: list[dict[str, Any]], where: str) -> None:
    """No record was stepped: never clean, and say why."""
    result['verdict'] = 'blocking'
    result['hint'] = ("No record was stepped: the errors in groups stop the pipeline before its first record; fix them "
                      "and step again." if markers else
                      f"No record was stepped: Stroom found none in {where}. Check the stream ids, and that the "
                      f"pipeline's parser reads this data.")


def _markers(result: dict[str, Any], record: int | str | None = None) -> list[dict[str, Any]]:
    markers = []
    for element, data in ((result.get('stepData') or {}).get('elementMap') or {}).items():
        for error in ((data.get('indicators') or {}).get('uniqueErrorSet') or []):
            m = from_stored_error(error)
            m['element'] = m['element'] if m['element'] != 'unknown' else element
            m['record'] = record
            markers.append(m)
    for message in result.get('generalErrors') or []:
        markers.append({'severity': 'ERROR', 'element': 'pipeline', 'message': message, 'location': None,
                        'record': record})
    return markers


async def step_pipeline(
        ctx: Context,
        pipeline_uuid: PipelineUuid,
        stream_id: Annotated[int, Field(description="Stream to step through, e.g. a Raw Events sample.")],
        record: Annotated[int | Literal['first', 'last'], Field(
            description="Zero-based record index within the part, or 'first' / 'last'.")] = 'first',
        part: Annotated[int, Field(ge=0, description="Zero-based part of a multi-part stream (locate_event gives it).")] = 0,
        draft_code: DraftCode = None,
        show: Annotated[list[str] | str | None, ONE_OR_MORE, Field(
            description="Element ids whose input and output to return. Defaults to the pipeline's own "
                        "XSLT steps.")] = None,
) -> dict[str, Any]:
    """
    Step one record through a pipeline and show what each element does to it: the input and output of
    the chosen elements, and every element's errors and warnings, triaged. Use draft_code to try a
    translation change without saving it.
    """
    await _check_drafts(ctx, draft_code)
    stroom = gateway_from(ctx)
    pipeline = await _Pipeline.load(stroom, pipeline_uuid)
    if isinstance(record, int):
        result = await _step(stroom, pipeline, stream_id, 'REFRESH',
                             {'metaId': stream_id, 'partIndex': part, 'recordIndex': record}, draft_code)
    else:
        result = await _step(stroom, pipeline, stream_id, record.upper(), None, draft_code)
    if not result.get('foundRecord'):
        raise ToolError(f"No record found in stream {stream_id} for {record!r}")

    elements = (result.get('stepData') or {}).get('elementMap') or {}
    wanted = show or pipeline.default_outputs()
    budget = stroom.settings.max_stream_chars // max(1, 2 * len(wanted))
    outputs = {e: {'type': pipeline.types.get(e), 'input': (elements.get(e) or {}).get('input', '')[:budget],
                   'output': (elements.get(e) or {}).get('output', '')[:budget]}
               for e in wanted if e in elements}
    location = result.get('foundLocation') or {}
    return {'pipeline': pipeline.doc.get('name'), 'stream_id': stream_id,
            'part': location.get('partIndex'), 'record': location.get('recordIndex'), 'draft_code_used': sorted(draft_code or {}),
            'elements': outputs, **triage(_markers(result, location.get('recordIndex'))
                                          + _empty_output(result, pipeline.default_outputs()[-1], location.get('recordIndex')),
                                          ctx.lifespan_context['rules'], pipeline.own,
                                          accepted=await accepted_for(stroom, pipeline.doc.get('uuid')))}


async def step_sample(
        ctx: Context,
        pipeline_uuid: PipelineUuid,
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Sample streams to step through, every record.")],
        draft_code: DraftCode = None,
        max_records: Annotated[int | None, Field(
            ge=1, description="Stop after this many records (default: the server's max_sample_records).")] = None,
        records_per_stream: Annotated[int | None, Field(
            ge=1, description=f"Step at most this many records from the start of each stream (default "
                              f"{PER_STREAM}, unless max_records is given: processing then checks every record).")] = None,
) -> dict[str, Any]:
    """
    Step every record of the sample streams to completion and return one verdict for the whole sample:
    error groups triaged (blocking / review / benign) with the records they affect, plus per-record
    status. This is the correctness check before processing; a blocking group means fix and step again.
    Large samples are stepped from the head of each stream (records_per_stream); processing covers the rest.
    """
    await _check_drafts(ctx, draft_code)
    from tools.streams import refuse_older_than_feed
    await refuse_older_than_feed(ctx, stream_ids)
    stroom = gateway_from(ctx)
    pipeline = await _Pipeline.load(stroom, pipeline_uuid)
    cap = min(max_records or stroom.settings.max_sample_records, stroom.settings.max_sample_records)
    # Large sample files (985 records each) were stepped a record per request on a remote Stroom: the head of each
    # stream says whether the translation is right, and processing then reads every record.
    if records_per_stream is None and not max_records:
        records_per_stream = PER_STREAM
    markers: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    unknown: list[str] = []
    unknown_out: list[str] = []
    per_stream: dict[int, int] = {}
    first_output = None
    for stream_id in stream_ids:
        result = await _step(stroom, pipeline, stream_id, 'FIRST', None, draft_code)
        if not result.get('foundRecord'):
            # An element that fails before the first record (an XSLT that doesn't compile, say) reports it here.
            markers += _markers(result, str(stream_id))
        stream_start = len(records)
        while result.get('foundRecord') and len(records) < cap and (
                records_per_stream is None or len(records) - stream_start < records_per_stream):
            per_stream[stream_id] = per_stream.get(stream_id, 0) + 1
            location = result['foundLocation']
            key = record_key(stream_id, location)
            found = _markers(result, key) + _empty_output(result, pipeline.default_outputs()[-1], key)
            markers += found
            records.append({'record': key, 'errors': len(found)})
            if _writes_unknown(result, pipeline.default_outputs()[-1]):
                unknown.append(key)
                unknown_out.append((((result.get('stepData') or {}).get('elementMap') or {})
                                    .get(pipeline.default_outputs()[-1]) or {}).get('output') or '')
            if first_output is None:
                elements = (result.get('stepData') or {}).get('elementMap') or {}
                first_output = {e: (elements.get(e) or {}).get('output', '')[:stroom.settings.max_stream_chars // 4]
                                for e in pipeline.default_outputs()}
            result = await _step(stroom, pipeline, stream_id, 'FORWARD', location, draft_code)

    summary = triage(markers, ctx.lifespan_context['rules'], pipeline.own, record_count=len(records),
                     accepted=await accepted_for(stroom, pipeline.doc.get('uuid')))
    for group in summary['groups']:
        group['records'] = sorted({m['record'] for m in markers
                                   if (m['severity'], m['element']) == (group['severity'], group['element'])})[:20]
    _block(summary, await _unagreed_unknown(ctx, pipeline_uuid, pipeline.doc.get('name'), pipeline.default_outputs()[-1],
                                            unknown, len(records), draft_code, _what_unknown_holds(unknown_out)))
    result: dict[str, Any] = {'pipeline': pipeline.doc.get('name'), 'records_stepped': len(records),
                              'records_with_errors': sum(1 for r in records if r['errors']),
                              'draft_code_used': sorted(draft_code or {}), **summary,
                              'first_record_output': first_output}
    if len(records) >= cap:
        result['hint'] = f"Stopped at {cap} records; the sample has more."
    elif records_per_stream and any(n >= records_per_stream for n in per_stream.values()):
        result['hint'] = (f"Stepped the first {records_per_stream} records of each stream: processing then reads every "
                          f"record, and wait_for_processing reports any that fail.")
    if pipeline.json_root_map and records and all(n == 1 for n in per_stream.values()) \
            and await _stream_is_array(stroom, stream_ids[0]):
        # JSON lines need the root map and step as one record too: only an array is told to change it.
        summary['groups'].insert(0, {
            'class': 'review', 'reason': 'Each JSON array stepped as one record', 'severity': 'WARNING',
            'element': next(e for e, t in pipeline.types.items() if t == 'JSONParser'), 'own_element': True,
            'count': len(per_stream), 'records_affected': len(per_stream), 'records': sorted(map(str, per_stream))[:20],
            'examples': [{'message': "Each stream stepped as one record: a JSON array read with the parser's root map "
                                     "(jsonParser.addRootObject true) is a single record, however many items it holds, "
                                     "so a large one outlasts Stroom's stepping. Set it false (update_pipeline "
                                     "set_properties=[{element: <the JSON parser>, name: addRootObject, value: false}]) "
                                     "so each item is a record, change the XSLT to match /array/map, and step again.",
                          'location': None}]})
        summary['groups_by_class']['review'] = summary['groups_by_class'].get('review', 0) + 1
        if summary['verdict'] == 'clean':
            summary['verdict'] = 'review'
        result.update(verdict=summary['verdict'], groups=summary['groups'], groups_by_class=summary['groups_by_class'])
    if not records:
        _nothing_stepped(result, markers, f"streams {stream_ids}")
    await remember_clean(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': pipeline.doc.get('name')}, draft_code, result)
    if result['verdict'] == 'clean' and not draft_code:
        from tools.plan import build_of, with_next
        result = await with_next(ctx, await build_of(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': pipeline.doc.get('name')}), result)
    elif result['verdict'] == 'clean':
        result['hint'] = (result.get('hint') or '') + " Clean with draft code: save it (build_translation_xslt uuid= for a generated XSLT, else save_xslt uuid=) and step the saved code once more."
    return result


class RecordLocation(BaseModel):
    stream: int
    part: int = 0
    record: int
    shape: str | None = Field(None, description="The survey shape this record is an example of, if any.")
    expect: Literal['event', 'none'] = Field('event', description="none: a kind the user chose to leave "
                                                                  "untranslated (survey_feed sets it), so no Event is right.")


_EVENT = re.compile(r'<(?:[\w.-]+:)?Event[\s>/]')


async def step_records(
        ctx: Context,
        pipeline_uuid: PipelineUuid,
        locations: Annotated[list[RecordLocation] | str, ONE_OR_MORE, Field(
            description="Records to step where they are, e.g. survey_feed's locations: {stream, part, record}.")],
        draft_code: DraftCode = None,
) -> dict[str, Any]:
    """
    Step chosen records of existing streams, in place, and return one verdict for them all, like
    step_sample: error groups triaged, and per record the events it produced and its errors. Use it with
    survey_feed's locations to check a translation against every kind of event a feed holds, without copying
    or processing anything. A record that produces no event is flagged.
    """
    await _check_drafts(ctx, draft_code)
    from tools.streams import refuse_older_than_feed
    locations = [RecordLocation.model_validate(x) for x in locations]
    await refuse_older_than_feed(ctx, sorted({loc.stream for loc in locations}))
    stroom = gateway_from(ctx)
    pipeline = await _Pipeline.load(stroom, pipeline_uuid)
    output_element = pipeline.default_outputs()[-1]
    markers: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    unknown: list[str] = []
    unknown_out: list[str] = []
    for loc in [RecordLocation.model_validate(x) for x in locations[:stroom.settings.max_sample_records]]:
        where = {'metaId': loc.stream, 'partIndex': loc.part, 'recordIndex': loc.record}
        key = record_key(loc.stream, where)
        result = await _step(stroom, pipeline, loc.stream, 'REFRESH', where, draft_code)
        if not result.get('foundRecord'):
            found = _markers(result, key)
            markers += found
            records.append({'record': key, 'shape': loc.shape, 'found': False, **({'errors': len(found)} if found else {})})
            continue
        found = _markers(result, key) + _empty_output(result, output_element, key)
        output = (((result.get('stepData') or {}).get('elementMap') or {}).get(output_element) or {}).get('output', '')
        events = len(_EVENT.findall(output))
        if _UNKNOWN.search(output):
            unknown.append(key)
            unknown_out.append(output)
        if loc.expect == 'none':
            # Left untranslated by choice: the no-elements check doesn't apply, but an Event does.
            found = [f for f in found if not f['message'].startswith('Output contains no XML elements')]
            if events:
                found.append({'severity': 'WARNING', 'element': output_element, 'record': key, 'location': None,
                              'message': f'The record produced {events} Event(s), but its kind is left untranslated'})
        elif events == 0 and not found:
            found.append({'severity': 'WARNING', 'element': output_element, 'record': key, 'location': None,
                          'message': 'The record produced no Event'})
        markers += found
        records.append({'record': key, 'shape': loc.shape, 'events': events, 'errors': len(found),
                        **({'expect': 'none'} if loc.expect == 'none' else {})})

    summary = triage(markers, ctx.lifespan_context['rules'], pipeline.own, record_count=len(records),
                     accepted=await accepted_for(stroom, pipeline.doc.get('uuid')))
    for group in summary['groups']:
        group['records'] = sorted({m['record'] for m in markers
                                   if (m['severity'], m['element']) == (group['severity'], group['element'])})[:20]
    _block(summary, await _unagreed_unknown(ctx, pipeline_uuid, pipeline.doc.get('name'), output_element, unknown,
                                            len(records), draft_code, _what_unknown_holds(unknown_out)))
    missing = [r['record'] for r in records if r.get('found') is False]
    uncovered = sorted({r['shape'] for r in records if r.get('shape') and (
        r.get('errors') or (r.get('events') == 0 and r.get('expect') != 'none'))})
    result = {'pipeline': pipeline.doc.get('name'), 'records_stepped': len(records) - len(missing),
              'records_with_errors': sum(1 for r in records if r.get('errors')), 'records_not_found': missing,
              'shapes_not_clean': uncovered,
              'left_untranslated_as_intended': sum(1 for r in records if r.get('expect') == 'none' and not r.get('errors')),
              'draft_code_used': sorted(draft_code or {}), **summary,
              'records': records[:100]}
    if not result['records_stepped']:
        _nothing_stepped(result, markers, f"the records {missing[:5]}")
    if not uncovered:
        await remember_clean(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': pipeline.doc.get('name')}, draft_code, result)
    return result


def _field_values(xml: str) -> dict[str, list[str]]:
    """Every value in an output document keyed by a readable path, e.g. 'Event/EventSource/User/Id'.

    Elements named by an attribute (event-logging Data/@Name, records:2 data/@name, JSON XML @key) get
    that name in the path, so reordering them does not show as a change.
    """
    from lxml import etree
    values: dict[str, list[str]] = {}
    try:
        root = etree.fromstring(xml.encode('utf-8'))
    except etree.XMLSyntaxError:
        return {'(unparseable output)': [xml[:200]]}

    def walk(node, path):
        name = etree.QName(node).localname
        label = next((node.get(a) for a in ('Name', 'name', 'key') if node.get(a)), None)
        here = f"{path}/{name}[{label}]" if label else f"{path}/{name}"
        for attr, value in node.attrib.items():
            local = etree.QName(attr).localname
            if local not in ('Name', 'name', 'key', 'schemaLocation', 'version', 'Version', 'StreamId', 'EventId'):
                values.setdefault(f"{here}/@{local}", []).append(value)
        text = (node.text or '').strip()
        if text and len(node) == 0:
            values.setdefault(here, []).append(text)
        for child in node:
            if isinstance(child.tag, str):
                walk(child, here)
    for child in root:
        if isinstance(child.tag, str):
            walk(child, '')
    return {k.lstrip('/'): v for k, v in values.items()}


async def _outputs(stroom: StroomGateway, pipeline: _Pipeline, stream_ids: list[int], element: str,
                   code: dict[str, str] | None, cap: int) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for stream_id in stream_ids:
        result = await _step(stroom, pipeline, stream_id, 'FIRST', None, code)
        while result.get('foundRecord') and len(outputs) < cap:
            location = result['foundLocation']
            elements = (result.get('stepData') or {}).get('elementMap') or {}
            outputs[record_key(stream_id, location)] = (elements.get(element) or {}).get('output', '')
            result = await _step(stroom, pipeline, stream_id, 'FORWARD', location, code)
    return outputs


async def compare_outputs(
        ctx: Context,
        pipeline_uuid: PipelineUuid,
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Streams whose records to compare.")],
        draft_code: DraftCode = None,
        other_pipeline_uuid: Annotated[str | None, Field(
            description="Compare against this pipeline instead of draft code, e.g. a v2 copy.")] = None,
        element: Annotated[str | None, Field(
            description="Element whose output to compare. Defaults to the pipeline's own XSLT step.")] = None,
        max_records: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """
    Step the same records through the current pipeline and a candidate (draft code, or another pipeline
    such as a new version) and diff each record's output field by field. Reports, per field path, how many
    records gained, lost or changed a value, with examples. Use it to prove a change touches only the fields
    it should.
    """
    if not draft_code and not other_pipeline_uuid:
        raise ToolError("Give draft_code or other_pipeline_uuid to compare against")
    stroom = gateway_from(ctx)
    base = await _Pipeline.load(stroom, pipeline_uuid)
    other = await _Pipeline.load(stroom, other_pipeline_uuid) if other_pipeline_uuid else base
    element = element or base.default_outputs()[0]
    before = await _outputs(stroom, base, stream_ids, element, None, max_records)
    after = await _outputs(stroom, other, stream_ids, element if element in other.types else other.default_outputs()[0],
                           draft_code if not other_pipeline_uuid else None, max_records)
    by_path: dict[str, dict[str, Any]] = {}
    changed_records = 0
    for record, old_xml in before.items():
        old, new = _field_values(old_xml), _field_values(after.get(record, ''))
        record_changed = False
        for path in sorted(set(old) | set(new)):
            if old.get(path) == new.get(path):
                continue
            kind = 'added' if path not in old else 'removed' if path not in new else 'changed'
            entry = by_path.setdefault(path, {'path': path, 'added': 0, 'removed': 0, 'changed': 0, 'example': None})
            entry[kind] += 1
            entry['example'] = entry['example'] or {'record': record, 'before': old.get(path), 'after': new.get(path)}
            record_changed = True
        changed_records += record_changed
    return {'records_compared': len(before), 'records_changed': changed_records, 'element': element,
            'fields_changed': sorted(by_path.values(), key=lambda e: -(e['added'] + e['removed'] + e['changed'])),
            'unchanged': not by_path}


ALL_TOOLS = [step_pipeline, step_sample, step_records, compare_outputs]
