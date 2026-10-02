"""Tools for diagnosing a reported problem with an events pipeline and proposing a fix.

A user reports a stream (and optionally an event) that came out wrong. locate_event traces it back to the
raw stream, part and record that produced it, and the translation documents involved. summarise_fix
proves a drafted change against real records and packages it either to apply or to hand over for the user
to apply themselves. Neither tool changes anything.
"""
import difflib
import re
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from tools.pipelines import CODE_PROPERTIES, merge_layers, translation_docs  # noqa: F401
from tools.stepping import _field_values, _markers, _Pipeline, _step, compare_outputs, step_sample
from tools.streams import _meta, _term
from utils.params import ONE_OR_MORE
from utils.stroom import StroomGateway, gateway_from

_EVENT = re.compile(r'<(?:[\w.-]+:)?Event[\s>/]')


async def _children(stroom: StroomGateway, stream_id: int) -> list[dict[str, Any]]:
    rows = (await stroom.find_meta([_term('Parent Id', stream_id)], 100)).get('values') or []
    return [r['meta'] for r in rows if r['meta'].get('status') != 'DELETED']


async def _resolve(stroom: StroomGateway, stream_id: int, pipeline_uuid: str | None) -> dict[str, Any]:
    """Raw stream, pipeline and Events stream for whatever stream the user reported."""
    meta = await _meta(stroom, stream_id)
    kind = meta.get('typeName')
    if meta.get('pipelineUuid'):  # an output (Events, Error, ...): its parent is the input
        raw = meta.get('parentMetaId')
        if not raw:
            raise ToolError(f"Stream {stream_id} ({kind}) has no parent stream to trace back to")
        pipeline = meta['pipelineUuid']
        if kind == 'Events':
            events = stream_id
        else:
            events = next((c['id'] for c in await _children(stroom, raw)
                           if c.get('typeName') == 'Events' and c.get('pipelineUuid') == pipeline), None)
        return {'reported': {'id': stream_id, 'type': kind}, 'raw': raw, 'pipeline': pipeline, 'events': events}
    outputs = [c for c in await _children(stroom, stream_id) if c.get('typeName') == 'Events']
    if pipeline_uuid:
        outputs = [c for c in outputs if c.get('pipelineUuid') == pipeline_uuid]
    pipelines = sorted({c['pipelineUuid'] for c in outputs})
    if len(pipelines) > 1:
        raise ToolError(f"Stream {stream_id} was processed by several pipelines {pipelines}; give pipeline_uuid")
    if not pipelines and not pipeline_uuid:
        raise ToolError(f"Stream {stream_id} ({kind}) has no Events output; give pipeline_uuid to step it")
    return {'reported': {'id': stream_id, 'type': kind}, 'raw': stream_id,
            'pipeline': pipeline_uuid or pipelines[0], 'events': outputs[0]['id'] if outputs else None}


def _nth_event(xml: str, n: int) -> str | None:
    """The n-th (1-based) Event element of an output document, as XML, or None."""
    from lxml import etree
    try:
        root = etree.fromstring(re.sub(r'^\s*<\?xml[^>]*\?>', '', xml).encode('utf-8'))
    except etree.XMLSyntaxError:
        return None
    events = [e for e in root.iter() if isinstance(e.tag, str) and etree.QName(e).localname == 'Event']
    if not 0 < n <= len(events):
        return None
    wrapper = etree.Element(root.tag, nsmap=root.nsmap)
    wrapper.append(events[n - 1])
    return etree.tostring(wrapper, encoding='unicode')


async def locate_event(
        ctx: Context,
        stream_id: Annotated[int, Field(description="The stream the user reported: Events, Error or Raw Events.")],
        event_id: Annotated[int | None, Field(
            ge=1, description="The event within the Events stream (1-based, as dashboards show EventId).")] = None,
        pipeline_uuid: Annotated[str | None, Field(
            description="Only needed when a raw stream was processed by more than one pipeline.")] = None,
) -> dict[str, Any]:
    """
    Trace a reported stream, and optionally one event in it, back to where it came from: the raw stream,
    the part and record within it, the pipeline, and the XSLTs and text converters it runs (marking any
    inherited from a template). With an event_id, the stored event is shown next to the record's input and
    what stepping that record produces now, ready for step_pipeline(stream, record, part) and a fix.
    """
    stroom = gateway_from(ctx)
    found = await _resolve(stroom, stream_id, pipeline_uuid)
    raw_meta = await _meta(stroom, found['raw'])
    first = await stroom.fetch_data(found['raw'], 0, 1)
    pipeline = await _Pipeline.load(stroom, found['pipeline'])
    docs = translation_docs(found['pipeline'], await stroom.pipeline_layers(found['pipeline']))
    result: dict[str, Any] = {
        'reported': found['reported'], 'raw_stream': found['raw'], 'feed': raw_meta.get('feedName'),
        'raw_parts': (first.get('totalItemCount') or {}).get('count'),
        'pipeline': {'uuid': found['pipeline'], 'name': pipeline.doc.get('name')},
        'events_stream': found['events'], 'translation_docs': docs}
    if event_id is None:
        result['hint'] = ("Give event_id to pin down the record, or find the event type in the Events stream "
                          "(summarise_streams (kind=events)) and step_pipeline the raw stream's records.")
        return result
    if not found['events']:
        raise ToolError(f"Raw stream {found['raw']} has no Events stream from this pipeline to find event {event_id} in")

    stored = await stroom.fetch_data(found['events'], event_id - 1, 1)
    total = (stored.get('totalItemCount') or {}).get('count')
    if total is not None and event_id > total:
        raise ToolError(f"Events stream {found['events']} has {total} events; there is no event {event_id}")
    stored_xml = stored.get('data') or ''

    # Events records run on across the raw stream's parts, so count output events record by record.
    output_element = pipeline.default_outputs()[-1]
    cap, seen, steps = stroom.settings.max_sample_records, 0, 0
    step = await _step(stroom, pipeline, found['raw'], 'FIRST', None, None)
    while step.get('foundRecord') and steps < cap:
        steps += 1
        elements = (step.get('stepData') or {}).get('elementMap') or {}
        output = (elements.get(output_element) or {}).get('output', '')
        produced = len(_EVENT.findall(output))
        if seen + produced >= event_id:
            location = step['foundLocation']
            now = _nth_event(output, event_id - seen)
            parser = next((e for e, kind in pipeline.types.items() if kind.endswith('Parser')), None)
            budget = stroom.settings.max_stream_chars // 4
            result.update({
                'event_id': event_id,
                'location': {'part': location.get('partIndex'), 'record': location.get('recordIndex'),
                             'event_within_record': event_id - seen, 'events_from_record': produced},
                'record_input': ((elements.get(parser) or {}).get('input')
                                 or (elements.get(output_element) or {}).get('input', ''))[:budget],
                'stored_event': stored_xml[:budget],
                'stepped_event_now': (now or output)[:budget],
                'same_as_stored': (_field_values(now) == _field_values(stored_xml)) if now else None,
                'errors_now': _markers(step, location.get('recordIndex')),
                'hint': "Step it with step_pipeline(stream_id=raw_stream, record=location.record, "
                        "part=location.part); try a fix with draft_code, then summarise_fix."})
            if result['same_as_stored'] is False:
                result['note'] = ("Stepping now gives a different event than the one stored: the translation or "
                                  "reference data changed since it was processed.")
            return result
        seen += produced
        step = await _step(stroom, pipeline, found['raw'], 'FORWARD', step['foundLocation'], None)
    raise ToolError(f"Event {event_id} was not found in the first {steps} records of raw stream {found['raw']} "
                    f"({seen} events produced); the pipeline may have changed since it was processed")


def _changed_outside(paths: list[str], expected: list[str]) -> list[str]:
    return [p for p in paths if not any(p == e or p.startswith(e.rstrip('/') + '/') for e in expected)]


async def summarise_fix(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The pipeline being fixed.")],
        element: Annotated[str, Field(description="Element whose XSLT or text converter the fix changes.")],
        draft: Annotated[str, Field(description="The complete fixed XSLT or text converter.")],
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="Raw streams to prove it on: the reported one plus recent production streams.")],
        expected_paths: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Output field paths the fix should change, e.g. ['Event/EventSource/User/Id'].")],
        issue: Annotated[str, Field(description="The problem in the user's words, for the write-up.")] = '',
) -> dict[str, Any]:
    """
    Prove a drafted fix and package it for the user. Steps every record of the streams with the draft,
    diffs outputs against the saved code (only expected_paths should change), and returns the code diff,
    whether the fix is ready, and step-by-step instructions for applying it by hand. Saves nothing: ask the
    user whether to apply it (update_events_pipeline's copy, update and promote) or do it themselves.
    """
    stroom = gateway_from(ctx)
    docs = {d['element']: d for d in translation_docs(pipeline_uuid, await stroom.pipeline_layers(pipeline_uuid))}
    if element not in docs:
        raise ToolError(f"Element '{element}' has no XSLT or text converter; elements with code: {sorted(docs)}")
    target = docs[element]
    saved = (await stroom.get_doc(target['doc']['type'], target['doc']['uuid'])).get('data') or ''
    diff = ''.join(difflib.unified_diff(saved.splitlines(keepends=True), draft.splitlines(keepends=True),
                                        fromfile=f"{target['doc']['name']} (saved)",
                                        tofile=f"{target['doc']['name']} (fixed)"))
    if not diff:
        raise ToolError("The draft is the same as the saved code")
    code = {element: draft}
    comparison = await compare_outputs(ctx, pipeline_uuid, stream_ids, draft_code=code, element=element
                                       if target['doc']['type'] == 'XSLT' else None)
    stepped = await step_sample(ctx, pipeline_uuid, stream_ids, draft_code=code)
    changed = [f['path'] for f in comparison['fields_changed']]
    unexpected = _changed_outside(changed, expected_paths)
    problems = []
    if not changed:
        problems.append("The draft changes no output on these records: it does not reach the reported case")
    if unexpected:
        problems.append(f"Fields outside expected_paths change too: {unexpected}")
    if stepped['verdict'] == 'blocking':
        problems.append("Stepping with the draft has blocking errors")
    name = (await stroom.get(f'/pipeline/v1/{pipeline_uuid}')).get('name')
    kind = 'XSLT' if target['doc']['type'] == 'XSLT' else 'Text Converter'
    manual = [
        f"Open the {kind} '{target['doc']['name']}' (uuid {target['doc']['uuid']}) in Stroom.",
        "Apply the change in 'diff' below (or paste the complete fixed code), then save.",
        f"Open pipeline '{name}', enter stepping on raw stream(s) {stream_ids} and check the reported record "
        f"now gives the expected output with no errors.",
        "Reprocess the affected streams yourself if you want existing events corrected; new data picks up "
        "the fix automatically.",
    ]
    if target['inherited_from_template']:
        manual.insert(0, f"Note: this {kind} is set by template pipeline '{target['set_by']}', so the change "
                         f"affects every pipeline built from that template.")
    return {
        'issue': issue, 'pipeline': {'uuid': pipeline_uuid, 'name': name}, 'element': element,
        'doc': target['doc'], 'inherited_from_template': target['inherited_from_template'],
        'set_by': target['set_by'], 'ready': not problems, 'problems': problems,
        'records_compared': comparison['records_compared'], 'records_changed': comparison['records_changed'],
        'fields_changed': comparison['fields_changed'][:20], 'step_verdict': stepped['verdict'],
        'step_groups': [g for g in stepped['groups'] if g['class'] != 'benign'][:10],
        'diff': diff[:stroom.settings.max_stream_chars], 'draft': draft, 'manual_steps': manual,
        'hint': ("Show the user the diff and the fields changed, then ask whether to apply the fix to the "
                 "pipeline or give them the manual steps." if not problems else
                 "Revise the draft and call summarise_fix again."),
    }


ALL_TOOLS = [locate_event, summarise_fix]
