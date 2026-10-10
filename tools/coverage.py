"""Coverage of an events pipeline over a whole feed, and the pipelines that follow it.

A sample can miss rare kinds of event that only appear once a whole feed has been processed (and indexed). The
generated translation logs each record no rule takes ("No event mapping matched record N (action=view | ...)", WARN)
and each a kept-Unknown rule takes ("Kept as Unknown by rule 'other': record N (...)", INFO), with the values its
rules test, into the Error stream of the raw stream it came from. So the pipeline's Error streams name which raw
streams and records were missed, and of what kind, without reading the raw data.

A feed may hold hundreds of thousands of streams: the Error streams are read in bounded batches by Id
(continue_from carries on), only clean ones skipped where Stroom counts them, and what is kept is bounded (counts per
kind, a few examples, the affected streams of this batch and their range). Many affected streams are reprocessed by
criteria on the feed, not by naming each. Nothing here processes anything: reprocessing is approved by the user
(reprocess_streams, create_processor_filter), or done by them in production.

A change to the events pipeline reaches the pipelines that read its Events (indexing, CEF output): their processor
filters name it (Pipeline IS_DOC_REF) or its Events feed. Each is checked against the Events the changed pipeline
writes for the records it missed, with what to change, and, for Elasticsearch, what the cluster's admin has to do
before those streams are indexed again.
"""
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import Field

from utils import cef
from utils.params import ONE_OR_MORE
from utils.stroom import StroomGateway, gateway_from

_LOGGED = re.compile(r"(?:No event mapping matched record |Kept as Unknown by rule '(.*)': record )(\d+)(?: \((.*)\))?\s*$")
_PAGE = 200
EXACT_LIMIT = 200       # affected streams named one by one; more are reprocessed by criteria on the feed


def _term(field: str, value: Any, condition: str = 'EQUALS') -> dict[str, Any]:
    return {'type': 'term', 'field': field, 'condition': condition, 'value': str(value)}


def _pipeline_term(uuid: str, name: str | None) -> dict[str, Any]:
    return {'type': 'term', 'field': 'Pipeline', 'condition': 'IS_DOC_REF',
            'docRef': {'type': 'Pipeline', 'uuid': uuid, 'name': name or uuid}}


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if ms else None


def parse_logged(message: str) -> tuple[int, str | None, dict[str, str]] | None:
    """(record, rule kept as Unknown or None for no rule matched, the values the rules test) from a log line."""
    m = _LOGGED.search(message or '')
    if not m:
        return None
    values = {}
    for part in (m.group(3) or '').split(' | '):
        if '=' in part:
            name, value = part.split('=', 1)
            values[name] = value
    return int(m.group(2)), m.group(1), values


async def _error_batch(stroom: StroomGateway, pipeline: dict[str, Any], after: int, budget: int
                       ) -> tuple[list[dict[str, Any]], int | None]:
    """Up to budget of the pipeline's Error streams with an Id above after, oldest first; and the Id to continue
    after, or None when there are no more."""
    out: list[dict[str, Any]] = []
    while len(out) < budget:
        want = min(_PAGE, budget - len(out))
        rows = (await stroom.find_meta([_pipeline_term(pipeline['uuid'], pipeline.get('name')), _term('Type', 'Error'),
                                        _term('Id', after, 'GREATER_THAN')], want, newest_first=False)).get('values') or []
        if not rows:
            return out, None
        out += [{**r['meta'], 'attributes': r.get('attributes') or {}} for r in rows
                if r['meta'].get('status') != 'DELETED']
        after = rows[-1]['meta']['id']
        if len(rows) < want:
            return out, None
    return out, after


async def _logged(stroom: StroomGateway, errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each record no rule matched or a rule kept as Unknown, from these Error streams: {raw, created, record, rule,
    values}."""
    found = []
    for meta in errors:
        counts = {k: v for k, v in (meta.get('attributes') or {}).items()
                  if k in ('Warning Count', 'Error Count', 'Fatal Error Count', 'Info Count')}
        if counts and not any(int(v or 0) for v in counts.values()):
            continue        # counted, and clean (a find doesn't always give the counts: then it is read)
        if not meta.get('parentMetaId'):
            continue
        body = await stroom.fetch_data(meta['id'], 0, 5000, 'MARKER')
        for marker in body.get('markers') or []:
            parsed = parse_logged(marker.get('message') or '')
            if parsed:
                record, rule, values = parsed
                found.append({'raw': int(meta['parentMetaId']), 'created': meta.get('createMs'), 'record': record,
                              'rule': rule, 'values': values})
    return found


async def _example(ctx: Context, mapping, raw: int, record: int) -> str | None:
    """One raw record, as the mapping reads it (record-no counts from 0), for an example."""
    from tools.streams import read_sample_streams
    from utils.localcheck import sample_records
    try:
        texts, _ = await read_sample_streams(ctx, [raw])
    except ToolError:
        return None
    text = next(iter(texts.values()), '')
    splitter = None
    if mapping.input == 'data_splitter':
        from utils.dsgen import infer_spec
        splitter, _ = infer_spec(text)
    records, _ = sample_records(mapping, text, splitter)
    return _shown(records[record]) if record < len(records) else None


def _shown(record: Any) -> str:
    if isinstance(record, dict):
        return ', '.join(f"{k}={v}" for k, v in list(record.items())[:12])[:400]
    try:
        return etree.tostring(record, encoding='unicode')[:400]
    except TypeError:
        return str(record)[:400]


async def _sampled_unknown(stroom: StroomGateway, pipeline: dict[str, Any], streams: int, per_stream: int
                           ) -> tuple[Counter, dict[tuple, dict[str, Any]], set[int]]:
    """For an XSLT that logs no Unknown records (saved before they were logged, or by hand): events kept as Unknown
    in its newest Events streams, grouped by TypeId and the Data they carry."""
    from tools.streams import _root_closed
    rows = (await stroom.find_meta([_pipeline_term(pipeline['uuid'], pipeline.get('name')), _term('Type', 'Events')],
                                   streams)).get('values') or []
    kinds: Counter = Counter()
    examples: dict[tuple, dict[str, Any]] = {}
    raws: set[int] = set()
    for meta in [r['meta'] for r in rows if r['meta'].get('status') != 'DELETED']:
        first = await stroom.fetch_data(meta['id'], 0, 1)
        total = min((first.get('totalItemCount') or {}).get('count') or 1, per_stream)
        for body in [first] + [await stroom.fetch_data(meta['id'], i, 1) for i in range(1, total)]:
            data = body.get('data') or ''
            if 'Unknown' not in data:
                continue
            try:
                root = etree.fromstring(_root_closed(data).encode(), etree.XMLParser(huge_tree=True, recover=True))
            except etree.XMLSyntaxError:
                continue
            for event in root.iter(f'{{{cef.EL}}}Event'):
                if cef.event_kind(event) != 'Unknown':
                    continue
                values = cef.event_values(event)
                names = tuple(sorted(re.findall(r"Data\[@Name='([^']+)'\]", ' '.join(values)))[:6])
                key = (values.get('EventDetail/TypeId', ''), names)
                kinds[key] += 1
                examples.setdefault(key, {'events_stream': meta['id'], 'raw_stream': meta.get('parentMetaId'),
                                          'values': dict(list(values.items())[:14])})
                if meta.get('parentMetaId'):
                    raws.add(int(meta['parentMetaId']))
    return kinds, examples, raws


async def follow_on_pipelines(ctx: Context, pipeline: dict[str, Any], events_feeds: set[str]) -> list[dict[str, Any]]:
    """Pipelines reading this pipeline's Events: their filters name it (Pipeline IS_DOC_REF) or its Events feed."""
    stroom = gateway_from(ctx)
    body = await stroom.post('/processorFilter/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': []}})
    feeds = {f.lower() for f in events_feeds}
    found: dict[str, dict[str, Any]] = {}

    def terms(expression: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for child in (expression or {}).get('children') or []:
            out += terms(child) if child.get('type') == 'operator' else [child]
        return out
    for row in (body or {}).get('values') or []:
        f = row.get('processorFilter') or {}
        if f.get('deleted') or not f.get('pipelineUuid') or f['pipelineUuid'] == pipeline['uuid']:
            continue
        ts = terms((f.get('queryData') or {}).get('expression'))
        names_it = any(t.get('field') == 'Pipeline' and (t.get('docRef') or {}).get('uuid') == pipeline['uuid'] for t in ts)
        reads_feed = any(t.get('field') == 'Feed' and str(t.get('value', '')).lower() in feeds for t in ts) and \
            any(t.get('field') == 'Type' and t.get('value') == 'Events' for t in ts)
        if names_it or reads_feed:
            found.setdefault(f['pipelineUuid'], {'uuid': f['pipelineUuid'], 'name': f.get('pipelineName'),
                                                 'filters': []})['filters'].append(f.get('id'))
    return list(found.values())


def _index_gaps(plan, events: list[etree._Element]) -> list[str]:
    from utils.fieldplan import source_matches
    sources = [f.source for f in plan.fields]
    counts: Counter = Counter()
    for event in events:
        for path in cef.event_values(event):
            if not any(source_matches(s.lstrip('/'), path) for s in sources):
                counts[path] += 1
    return [f"{path} ({n} of {len(events)} events)" for path, n in counts.most_common(25)]


async def _old_events(stroom: StroomGateway, pipeline: dict[str, Any], raws: list[int]) -> list[int]:
    """The Events streams the pipeline wrote from these raw streams (their documents are in the index)."""
    out = []
    for n in range(0, len(raws), 50):
        rows = (await stroom.find_meta([{'type': 'operator', 'op': 'OR', 'children': [
            _term('Parent Id', raw) for raw in raws[n:n + 50]]}, _term('Type', 'Events'),
            _pipeline_term(pipeline['uuid'], pipeline.get('name'))], 500)).get('values') or []
        out += [r['meta']['id'] for r in rows if r['meta'].get('status') != 'DELETED']
    return sorted(out)


async def _review_follow_on(ctx: Context, entry: dict[str, Any], events: list[etree._Element],
                            affected: dict[str, Any], old_events: list[int] | None) -> dict[str, Any]:
    from tools.builds import kept_mapping
    from tools.processing_writes import elastic_destination
    from tools.templates import _shape
    from utils.fieldplan import FieldPlan
    stroom = gateway_from(ctx)
    shape = await _shape(stroom, entry['uuid'])
    kept = await kept_mapping(ctx, entry['uuid'])
    out = {**entry, 'stage': shape['stage'], 'backend': shape.get('backend')}
    if kept and kept['kind'] == 'cef':
        plan = cef.CefPlan.model_validate(kept['payload'])
        added, notes = cef.extend(plan, events) if events else ([], [])
        out.update(kind='cef', missing=notes, changes=added,
                   to_do=("draft_cef_mapping uuid=<its XSLT> overrides=<changes> stream_ids=<the new Events>, then "
                          "step_sample, draft_cef_mapping pipeline_uuid=... to review, write_documentation"
                          if added else "nothing to change for these Events"),
                   reprocessing=("Reprocessing the raw streams writes new Events streams, which this pipeline sends "
                                 "again whole: ArcSight receives the events those streams had already sent a second "
                                 "time (dedupe there, or accept it)."))
    elif kept and kept['kind'] == 'index':
        plan = FieldPlan.model_validate(kept['payload'])
        gaps = _index_gaps(plan, events) if events else []
        destination = await elastic_destination(stroom, entry['uuid'])
        out.update(kind='index', not_indexed=gaps,
                   to_do=("draft_index_mapping with these as extra_fields (the index's agreed template as the "
                          "example), save_xslt index_plan=... uuid=<its XSLT>, step_sample, write_documentation"
                          if gaps else "nothing to change for these Events"))
        if destination:
            field = next((f.name for f in plan.fields if 'StreamId' in f.source or 'stream-id' in f.source), 'StreamId')
            if old_events is not None:
                delete = (f"POST {destination['index name']}/_delete_by_query "
                          f'{{"query": {{"terms": {{"{field}": {old_events}}}}}}}' if old_events else None)
            else:
                # Too many to name: the streams are reprocessed by a time window, and so is what the index holds.
                delete = (f"POST {destination['index name']}/_delete_by_query with a query for the documents of the "
                          f"feed's Events streams created from {affected.get('from')} to {affected.get('to')} (the window "
                          f"the raw streams are reprocessed in): their {field} values, or the event time field if the "
                          f"index has no feed field")
            out['elasticsearch'] = {
                'index': destination['index name'], 'cluster': destination['cluster'],
                'template': ("New fields need the index template changed first: check_index_template with them, which "
                             "the user confirms, and the cluster's admin commits." if gaps else
                             "The index template needs no change for these Events."),
                'admin': (f"Before the affected streams are processed again, the cluster's admin deletes what their old "
                          f"Events streams put in the index, or those events are indexed twice: {delete}"
                          if delete else "No Events streams of the affected raw streams were indexed yet.")}
    else:
        out.update(kind='hand-written' if shape['stage'] in ('indexing', 'forwarding') else shape['stage'],
                   to_do=("Its XSLT keeps no plan: step it over the new Events (step_sample) and check what it "
                          "writes for the new kinds (draft_cef_mapping pipeline_uuid=... for a CEF pipeline)"))
    out['documentation'] = "write_documentation for it again once changed (the change is recorded when the build is promoted)"
    return out


async def review_coverage(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The events pipeline (Raw Events to Events), once it has "
                                                        "processed its feed, or much of it.")],
        continue_from: Annotated[int | None, Field(description=(
            "From an earlier call's progress.continue_from: read the Error streams after it. A large feed is read "
            "over several calls."))] = None,
        max_streams: Annotated[int, Field(ge=1, le=5000, description="Error streams to read in this call.")] = 500,
        events_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description=(
            "Events streams the changed pipeline wrote (e.g. after reprocess_streams in a build), to check the "
            "follow-on pipelines against; left out, the Events it writes for some missed records are stepped."))] = [],
        follow_on: Annotated[bool, Field(description="Also review the pipelines that read its Events.")] = True,
) -> dict[str, Any]:
    """
    Whole-feed coverage of an events pipeline: kinds of event the sample missed, which only appear once the feed
    is processed. The generated translation logs, into each raw stream's Error stream, every record no rule
    matched and every record a rule keeps as Unknown, with the values its rules test; this reads them, a bounded
    batch of Error streams a call (continue_from carries on through a large feed), groups them by kind with
    examples, and says so for the user. The affected raw streams alone need processing again: named when few, by
    criteria on the feed when many; the user approves any reprocessing. Then reviews the pipelines that read its
    Events (indexing, CEF output) against the Events the pipeline writes for those records: what each lacks, what
    to change, its documentation, and for Elasticsearch what the cluster's admin must do before the streams are
    indexed again. Reads only.
    """
    from tools.builds import kept_mapping
    from tools.stepping import _outputs, _Pipeline
    from utils.xsltgen import KEPT_UNKNOWN, TranslationMapping
    stroom = gateway_from(ctx)
    pipeline = await stroom.get_doc('Pipeline', pipeline_uuid)
    kept = await kept_mapping(ctx, pipeline_uuid)
    mapping = TranslationMapping.model_validate(kept['payload']['mapping']) if kept and kept['kind'] == 'translation' else None
    errors, more = await _error_batch(stroom, pipeline, continue_from or 0, max_streams)
    any_events = (await stroom.find_meta([_pipeline_term(pipeline['uuid'], pipeline.get('name')), _term('Type', 'Events')],
                                         1)).get('values') or []
    if not errors and not any_events and not continue_from:
        raise ToolError(f"Pipeline '{pipeline.get('name')}' has processed nothing yet: its Error and Events streams are "
                        f"what this reads. create_processor_filter on its feed first.")
    logged = await _logged(stroom, errors)
    by_key: dict[tuple, dict[str, Any]] = {}
    raws: dict[int, int | None] = {}
    for entry in logged:
        raws.setdefault(entry['raw'], entry['created'])
        key = (entry['rule'], tuple(sorted(entry['values'].items())))
        group = by_key.setdefault(key, {
            'found_as': f"kept as Unknown by rule '{entry['rule']}'" if entry['rule'] else 'no rule matched',
            'kind': entry['values'] or {'values': "not logged (an XSLT saved before they were): see the examples"},
            'records': 0, 'raw_streams': [], 'examples': []})
        group['records'] += 1
        if entry['raw'] not in group['raw_streams'] and len(group['raw_streams']) < 50:
            group['raw_streams'].append(entry['raw'])
        if len(group['examples']) < 2:
            group['examples'].append({'raw_stream': entry['raw'], 'record': entry['record']})
    groups = sorted(by_key.values(), key=lambda g: -g['records'])
    if mapping:
        # A few raw records for examples; the grouping itself needs none.
        for group in groups[:10]:
            for example in group['examples'][:1]:
                example['holds'] = await _example(ctx, mapping, example['raw_stream'], example['record'])
    kept_unknown = [r.name for r in mapping.events if r.allow_unknown] if mapping else []
    logs_unknown = bool(kept) and KEPT_UNKNOWN.split("'")[0] in ((kept.get('xslt') or {}).get('data') or '')
    sampled: set[int] = set()
    if (kept_unknown or not mapping) and not logs_unknown and not continue_from:
        kinds, examples, sampled = await _sampled_unknown(stroom, pipeline, min(max_streams, 200), 200)
        for (type_id, names), count in kinds.most_common(20):
            groups.append({'found_as': 'kept as Unknown (its newest Events sampled: its XSLT logs no Unknown records; '
                                       'save it again from its mapping and they are found exactly)',
                           'kind': {'TypeId': type_id, 'Data': list(names)}, 'events': count,
                           'example': examples[(type_id, names)]})
    affected_ids = sorted(set(raws) | sampled)
    created = [c for c in raws.values() if c]
    affected = {'count': len(affected_ids), 'first_id': affected_ids[0] if affected_ids else None,
                'last_id': affected_ids[-1] if affected_ids else None,
                'from': _iso(min(created)) if created else None, 'to': _iso(max(created)) if created else None}
    result: dict[str, Any] = {
        'pipeline': pipeline.get('name'),
        'progress': {'error_streams_read': len(errors), 'after': continue_from or 0,
                     'continue_from': more, 'done': more is None,
                     **({'note': "More Error streams to read: call again with continue_from, and add this call's "
                                 "findings to the earlier ones'."} if more else {})},
        'missed': groups,
        'affected_raw_streams': affected,
        **({'affected_raw_stream_ids': affected_ids[:EXACT_LIMIT]} if affected_ids else {}),
    }
    so_far = '' if more is None and not continue_from else (
        f" (in the {len(errors)} Error streams this call read{', after earlier calls' if continue_from else ''}"
        f"{'; there are more' if more else ''})")
    if not groups:
        result['tell_user'] = (f"No untranslated events{so_far}: every record was translated by a rule"
                               + (" and none was kept as Unknown" if kept_unknown else '') + ".")
    else:
        total = sum(g.get('records', 0) + g.get('events', 0) for g in groups)
        result['tell_user'] = (f"Found untranslated events{so_far}: {total} record(s) of {len(groups)} kind(s) the "
                               f"sample didn't have, in {len(affected_ids)} raw stream(s). The mapping needs a rule for "
                               f"each; then only those streams need processing again, once the user approves.")
        in_build = any(t.startswith('mcp-build-') for t in await _tags(ctx, pipeline))
        many = len(affected_ids) > EXACT_LIMIT or more is not None
        if in_build and not many:
            reprocess = "reprocess_streams with the affected raw streams, at most 10 a call: each call asks the user's approval"
        elif in_build:
            reprocess = ("Too many affected streams to reprocess one by one in a build: the user processes them again "
                         "in production once the change is promoted")
        elif not many:
            reprocess = ("Processing the affected raw streams again is the user's, once promoted: exactly these streams "
                         "(affected_raw_stream_ids; Stroom's data browser: select them, Reprocess, or a processor filter "
                         "on their Ids)")
        else:
            reprocess = (f"Processing the affected streams again is the user's, once promoted. With this many, by "
                         f"criteria rather than one by one: a processor filter on the feed's Raw Events created from "
                         f"{affected['from']} to {affected['to']} (Ids {affected['first_id']} to {affected['last_id']}, "
                         f"over every batch read), so every stream in that window is processed again, affected or not "
                         f"(Stroom supersedes their Events streams). Every follow-on pipeline then takes that window again.")
        result['next'] = [
            "Show the user the kinds found (missed) and their examples; step_records on an example to see it.",
            "Add a rule for each with build_translation_xslt changes={events: [...]} and uuid=<its XSLT>"
            + ("" if in_build else " (the pipeline is in production: copy_pipeline working_copy=true into a build "
                                   "first, and change the copy)"),
            "write_documentation with stream_ids = the sample streams plus some affected raw streams, so the new kinds "
            "are documented to the field; change = what was found and added (one version row when the build is promoted)",
            reprocess,
        ]
    if follow_on:
        events: list[etree._Element] = []
        if events_stream_ids:
            from tools.validation import _stream_events
            xml, _ = await _stream_events(ctx, list(events_stream_ids))
            events = cef.events_of(xml)
        elif affected_ids and kept:
            loaded = await _Pipeline.load(stroom, pipeline_uuid)
            stepped = sorted({g['examples'][0]['raw_stream'] for g in groups if g.get('examples')})[:5]
            outputs = await _outputs(stroom, loaded, stepped or affected_ids[:3], kept['element'], None, 200)
            for output in outputs.values():
                try:
                    events += cef.events_of(output)
                except etree.XMLSyntaxError:
                    pass
        feeds = {r['meta'].get('feedName') for r in any_events if r['meta'].get('feedName')}
        old_events = (await _old_events(stroom, pipeline, affected_ids) if affected_ids and len(affected_ids) <= EXACT_LIMIT
                      and more is None else (None if affected_ids else []))
        reviewed = []
        for entry in await follow_on_pipelines(ctx, pipeline, feeds):
            try:
                reviewed.append(await _review_follow_on(ctx, entry, events, affected, old_events))
            except Exception as e:      # one unreadable pipeline must not hide the others
                reviewed.append({**entry, 'error': str(e)[:200]})
        result['follow_on'] = reviewed
        result['follow_on_events'] = (f"{len(events)} Events " + ("given" if events_stream_ids else
                                                                  "the pipeline writes now for some affected records")
                                      if events else "none to check against: give events_stream_ids once the changed "
                                                     "pipeline has written some")
    return result


async def _tags(ctx: Context, pipeline: dict[str, Any]) -> list[str]:
    from security.guard import guard_from
    try:
        return await guard_from(ctx).tags({'type': 'Pipeline', 'uuid': pipeline['uuid'], 'name': pipeline.get('name')})
    except Exception:
        return []


ALL_TOOLS = [review_coverage]
