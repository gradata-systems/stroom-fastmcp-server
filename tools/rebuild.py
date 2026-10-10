"""Rebuilding the mapping kept with a translation XSLT: when it was lost (the XSLT's Documentation tab cleared) or is
out of step with the XSLT (the XSLT changed by hand). Asked for by the user, with test coverage of both.

The mapping is read back from the XSLT (utils/xsltread), the XSLT regenerated from it, and both stepped in Stroom over
the sample streams: it is saved only when every record's output is the same (or the user accepts the differences
shown), after the user confirms, and the XSLT is then the one regenerated from it, so the two agree again.
"""
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import guard_from
from utils.consent import consent_from
from utils.mappingstore import normalise_xslt, read_mapping
from utils.params import ONE_OR_MORE
from utils.stroom import gateway_from
from utils.xsltgen import TranslationMapping, generate
from utils.xsltread import read, rebuild

PROVE_RECORDS = 200


async def _pipeline_of(ctx: Context, uuid: str) -> tuple[dict[str, Any], str]:
    """The pipeline that runs the XSLT as its own (not through a template), and the element it is set on."""
    from tools.pipelines import translation_docs
    stroom = gateway_from(ctx)
    body = await stroom.post('/explorer/v2/findInContent', {
        'filter': {'matchType': 'CONTAINS', 'pattern': uuid, 'caseSensitive': False},
        'pageRequest': {'offset': 0, 'length': 50}})
    found = []
    for value in (body or {}).get('values') or []:
        ref = (value.get('docContentMatch') or {}).get('docRef') or {}
        if ref.get('type') != 'Pipeline' or any(p['uuid'] == ref.get('uuid') for p, _ in found):
            continue
        try:
            docs = translation_docs(ref['uuid'], await stroom.pipeline_layers(ref['uuid']))
        except ToolError:
            continue
        element = next((d['element'] for d in docs if not d['inherited_from_template'] and d['doc']['uuid'] == uuid), None)
        if element:
            found.append((ref, element))
    if len(found) != 1:
        raise ToolError(f"Give pipeline_uuid: the pipeline that runs this XSLT, to step it over the sample "
                        f"({'none found' if not found else 'found ' + ', '.join(repr(p['name']) for p, _ in found)})")
    return found[0]


RAW_TYPES = ('Raw Events', 'Raw Reference')
NEWEST = 4


async def sample_streams(ctx: Context, pipeline_uuid: str) -> tuple[list[int], str]:
    """The streams to prove a rebuilt mapping on, and where they came from: the pipeline's original samples (the
    stream Ids of the processor filters the server made for them, those still there), else its feed's newest raw
    streams. Asked for by the user: the original sample data, without the agent having to find it."""
    from tools.coverage import filter_feeds, filter_terms
    stroom = gateway_from(ctx)
    filters = await stroom.processor_filters(pipeline_uuid)
    ids: list[int] = []
    for f in sorted(filters, key=lambda f: f.get('id') or 0):
        for term in filter_terms((f.get('queryData') or {}).get('expression')):
            if term.get('field') == 'Id' and term.get('condition') == 'EQUALS' and str(term.get('value', '')).isdigit():
                if int(term['value']) not in ids:
                    ids.append(int(term['value']))
    if ids:
        found = (await stroom.find_meta([{'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': str(i)}
                                         for i in ids], len(ids), op='OR')).get('values') or []
        there = {int(row['meta']['id']) for row in found if row['meta'].get('typeName') in RAW_TYPES}
        kept = [i for i in ids if i in there]
        if kept:
            return kept, "the pipeline's original sample streams (its sample processor filters)"
    # Why the original samples aren't used: say which, a pipeline only ever stepped never had sample filters.
    why = ("its sample streams are gone" if ids else "its sample processor filters are gone" if filters
           else "it has no sample processor filters: only ever stepped")
    known: dict = {}
    feeds = []
    for f in filters:
        feeds += [name for name in await filter_feeds(stroom, filter_terms((f.get('queryData') or {}).get('expression')),
                                                      known) if name not in feeds]
    if not feeds:
        # Stepped but never processed: no filters. The feeds of the build the pipeline is in.
        guard = guard_from(ctx)
        pipeline = await stroom.get_doc('Pipeline', pipeline_uuid)
        builds = [t[len('mcp-build-'):] for t in await guard.tags({'type': 'Pipeline', 'uuid': pipeline_uuid,
                                                                   'name': pipeline.get('name')})
                  if t.startswith('mcp-build-')]
        for build in builds:
            try:
                feeds += [d['name'] for d in await guard.folder_contents(build) if d['type'] == 'Feed']
            except ToolError:
                continue
    for feed in feeds:
        rows = (await stroom.find_meta([{'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': feed},
                                        {'type': 'term', 'field': 'Type', 'condition': 'EQUALS', 'value': 'Raw Events'}],
                                       NEWEST)).get('values') or []
        newest = [int(row['meta']['id']) for row in rows]
        if newest:
            return newest, f"the newest raw streams of feed '{rows[0]['meta'].get('feedName') or feed}' ({why})"
    raise ToolError(f"Give stream_ids: the pipeline's sample raw streams. {why[0].upper() + why[1:]}, and no feed of its "
                    f"filters or its build has raw streams.")


async def rebuild_mapping(
        ctx: Context,
        uuid: Annotated[str, Field(description="The translation XSLT whose mapping to rebuild.")],
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description=(
            "The raw streams to prove the rebuilt mapping on: both the XSLT and the one regenerated from it are stepped "
            "over them, and the mapping is only saved when every record's output is the same. Leave out for the "
            "pipeline's original sample streams (else its feed's newest raw streams); the reply says which."))] = [],
        pipeline_uuid: Annotated[str | None, Field(description=(
            "The pipeline that runs the XSLT; found from the XSLT when only one does."))] = None,
        change: Annotated[str | None, Field(description=(
            "For the version history: what was rebuilt and why, e.g. 'Mapping rebuilt after the Documentation tab "
            "was cleared'."))] = None,
        accept_differences: Annotated[bool, Field(description=(
            "Save even though the regenerated XSLT writes some records differently (shown in differences): only when "
            "the user, shown them, accepts what the mapping writes instead."))] = False,
        agent_model: Annotated[str | None, Field(description="The model you are, for the version history.")] = None,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Rebuild the mapping kept with a translation XSLT, from the XSLT itself: when it was lost (its Documentation tab
    cleared; build_status says so) or no longer matches the XSLT (changed by hand in Stroom; build_status shows what
    differs). With the kept mapping, every element that still reads as it generates keeps its entry exactly, and the
    hand edits become new or removed entries; without one, the mapping is read from the XSLT whole (fields, key=value
    extractions, maps, time formats, defaults, reference data lookups, dictionaries, the shared templates and
    functions of the XSLTs it imports, read from Stroom by name; anything else as an xpath entry). The XSLT regenerated
    from it is then stepped beside the XSLT over the pipeline's original sample streams (or stream_ids given), and only
    when every record's output is the same, and the user confirms, are the mapping and the regenerated XSLT saved. Then step_sample, and write_documentation again. Reads Stroom
    until it saves; saves only an XSLT in a build (a working copy for a promoted one).
    """
    from tools.generation import event_schema
    from tools.stepping import _field_values, _Pipeline, record_key, step_spread
    from tools.translation import update_xslt
    from utils.xsltversion import remember_model
    remember_model(ctx, agent_model)
    stroom = gateway_from(ctx)
    ids = [int(i) for i in (stream_ids if isinstance(stream_ids, list) else str(stream_ids).split(',')) if str(i).strip()]
    xslt = await stroom.get_doc('XSLT', uuid)
    ref = {'type': 'XSLT', 'uuid': uuid, 'name': xslt.get('name')}
    await guard_from(ctx).check_managed(ref)
    code = xslt.get('data') or ''
    kept = read_mapping(xslt.get('description'))
    if kept and kept[0] != 'translation':
        raise ToolError(f"XSLT '{xslt.get('name')}' keeps a {kept[0]} plan, not a translation mapping: rebuild_mapping "
                        f"is for translations (for CEF, draft_cef_mapping pipeline_uuid=... reviews what it writes)")
    version = (kept[1].get('schema_version') if kept else None) or stroom.settings.event_logging_version
    schema = await event_schema(ctx, version)
    base = TranslationMapping.model_validate(kept[1]['mapping']) if kept else None
    base_code = generate(base, schema, version)['xslt'] if base else None
    if base_code and normalise_xslt(base_code) == normalise_xslt(code):
        return {'xslt': ref, 'status': 'in_step', 'hint': "The XSLT is what its kept mapping generates: nothing to rebuild."}
    imported: dict[str, str] = {}
    if not base:
        # The XSLTs it imports, by name as Stroom resolves them: what a shared template writes is in its own XSLT.
        for href in dict.fromkeys(read(code).imports):
            found = [v['docRef'] for v in (await stroom.find_documents(href, ['XSLT'], 20)).get('values') or []
                     if v['docRef'].get('type') == 'XSLT' and v['docRef'].get('name') == href]
            if len(found) == 1:
                imported[href] = (await stroom.get_doc('XSLT', found[0]['uuid'])).get('data') or ''
    rebuilt = rebuild(code, base, base_code, imported)
    # Nothing new or removed: the XSLT reads as the mapping, written by an earlier generator, not changed by hand.
    upgraded = bool(base) and not rebuilt.new and not rebuilt.removed and not rebuilt.raw
    result: dict[str, Any] = {'xslt': ref, 'mode': ('written by an earlier generator' if upgraded else
                                                    'out of step with the kept mapping' if base else 'mapping lost'),
                              'rebuilt': rebuilt.summary()}
    if rebuilt.mapping is None or rebuilt.problems:
        result.update(saved=None, hint="The mapping couldn't be read back: give build_translation_xslt the whole "
                                       "mapping instead (draft_translation_mapping drafts one from the samples).")
        return result
    regenerated = generate(rebuilt.mapping, schema, version)
    if not regenerated['ok']:
        result.update(saved=None, problems=regenerated['problems'][:10],
                      hint="The mapping read back from the XSLT doesn't generate: the XSLT writes something a mapping "
                           "can't (the problems say what). Fix the XSLT or give build_translation_xslt the whole mapping.")
        return result
    if pipeline_uuid:
        from tools.pipelines import translation_docs
        docs = translation_docs(pipeline_uuid, await stroom.pipeline_layers(pipeline_uuid))
        element = next((d['element'] for d in docs if d['doc']['uuid'] == uuid), None)
        if not element:
            raise ToolError(f"Pipeline {pipeline_uuid} doesn't run XSLT '{xslt.get('name')}'")
        pipeline = {'uuid': pipeline_uuid}
    else:
        pipeline, element = await _pipeline_of(ctx, uuid)
    samples = 'the streams given'
    if not ids:
        ids, samples = await sample_streams(ctx, pipeline['uuid'])
    loaded = await _Pipeline.load(stroom, pipeline['uuid'])
    as_saved = await step_spread(stroom, loaded, ids, None, PROVE_RECORDS)
    as_rebuilt = await step_spread(stroom, loaded, ids, {element: regenerated['xslt']}, PROVE_RECORDS)

    def outputs(steps: list) -> dict[str, str]:
        return {record_key(sid, location): (((step.get('stepData') or {}).get('elementMap') or {}).get(element) or {})
                .get('output', '') for sid, location, step in steps}
    before, after = outputs(as_saved), outputs(as_rebuilt)
    differences: dict[str, dict[str, Any]] = {}
    for record, old_xml in before.items():
        old, new = _field_values(old_xml), _field_values(after.get(record, ''))
        for path in sorted(set(old) | set(new)):
            if old.get(path) != new.get(path):
                entry = differences.setdefault(path, {'path': path, 'records': 0, 'example': None})
                entry['records'] += 1
                entry['example'] = entry['example'] or {'record': record, 'xslt': old.get(path), 'rebuilt': new.get(path)}
    result['proven_on'] = {'records': len(before), 'streams': ids, 'which': samples}
    if not before:
        result.update(saved=None, hint="Stepping gave no records: check stream_ids are the pipeline's raw streams.")
        return result
    if differences:
        result['differences'] = sorted(differences.values(), key=lambda d: -d['records'])[:20]
        if not accept_differences:
            result.update(saved=None, hint=(
                "The regenerated XSLT writes these records differently from the XSLT: nothing was saved. Usually an "
                "edit a mapping can't express (an element written when its value is empty, a choose inside an "
                "event). Show the user the differences: to keep the XSLT as it is, leave the mapping as it is; if "
                "they accept what the mapping writes, call again with accept_differences=true."))
            return result
    mode = ('mapping lost' if not base else 'the mapping unchanged: the XSLT written by an earlier generator'
            if upgraded else 'carrying hand edits into the mapping')
    said = change or ('Mapping rebuilt from the XSLT, after its Documentation tab was cleared' if not base else
                      'Regenerated from its mapping (written by an earlier generator)' if upgraded
                      else 'Mapping rebuilt from the XSLT, carrying in its hand edits')
    gate = await consent_from(ctx).require(
        ctx, 'confirmation', 'rebuild_mapping',
        f"Save the mapping rebuilt from XSLT '{xslt.get('name')}' ({mode}), and the XSLT regenerated from it: "
        + (f"it wrote the same output as the XSLT for all {len(before)} records stepped"
           if not differences else f"{len(differences)} output paths differ, as shown"),
        {'kept entries': rebuilt.reused, 'new entries': rebuilt.new[:20], 'removed entries': rebuilt.removed[:20],
         'kept as xpath': rebuilt.raw[:20], **({'differences': [d['path'] for d in result.get('differences', [])]}
                                                if differences else {})},
        confirmation_id)
    if gate:
        return {**result, **gate}
    saved = await update_xslt(ctx, uuid, regenerated['xslt'], mapping=rebuilt.mapping, change=said)
    result.update(saved={k: saved.get(k) for k in ('type', 'uuid', 'name', 'version')},
                  next=("step_sample the pipeline over its sample streams (its code changed to what the mapping "
                        "generates), then write_documentation again: its Field mapping section comes from the rebuilt "
                        "mapping. From now on, change the mapping (build_translation_xslt changes=), not the XSLT."))
    return result


ALL_TOOLS = [rebuild_mapping]
