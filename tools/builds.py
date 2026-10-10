"""Tools for builds: the workspace folder, Documentation docs, and promotion out of the workspace."""
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from security.guard import GENERATED, MANAGED, build_tag, folder_parts, guard_from, copy_of_tag
from tools.instructions import applicable_instructions
from tools.processing_writes import agreement_problem, create_promotion_filters, elastic_destination, promotion_processing
from tools.pipelines import translation_docs
from tools.stepping import _outputs, _Pipeline, stepped_clean, stepped_tags, verified, verified_tags
from tools.streams import summarise_events
from utils.fielddoc import (discovery_field_markdown, field_mapping_markdown, index_documents, object_arrays,
                            index_field_mapping_markdown, sampled_events, written_fields_markdown)
from utils.fieldplan import FieldPlan
from utils.mappingstore import (DOC_MARK, digest, doc_digest, normalise_xslt, read_agreed_template, read_mapping,
                                replace_section)
from utils.xsltgen import TranslationMapping, generate
from utils.accepted import entry as accepted_entry, merge as merge_accepted, read_accepted
from utils.consent import consent_from
from utils.params import ONE_OR_MORE
from utils.stroom import body_text, gateway_from, set_body_text, doc_link

class AcceptedError(BaseModel):
    """An error the user says is benign, as triage showed it."""
    element: str | None = Field(None, description="The element it comes from, e.g. 'decorationFilter'.")
    example: str = Field(description="An example message, as triage showed it.")
    reason: str = Field(description="Why it is benign, in the user's words.")
    matches: str | None = Field(None, description="The kind of message it covers, with * for the parts that vary, e.g. "
                                                  "'No HR record for user svc-*'; defaults to the example (numbers and "
                                                  "quoted values vary).")


Build = Annotated[str, Field(description="Build name, e.g. 'acme-door-v1.3'.")]
_COPY_OF = 'mcp-copy-of-'
_LISTING_RETRIES, _LISTING_WAIT = 5, 1.0     # a build listed empty right after a write


async def start_build(
        ctx: Context,
        build: Build,
        feeds: Annotated[list[str] | str, ONE_OR_MORE, Field(description="Feeds the build is for, if known, so the standing "
                                                      "instructions for their folders are included.")] = [],
        folders: Annotated[list[str] | str, ONE_OR_MORE, Field(description="Folders the work will be promoted to, if known.")] = [],
) -> dict[str, Any]:
    """
    Create (or find) the build's workspace folder. Every write tool creates documents there. Returns the
    standing instructions (AGENTS docs) that apply, which the work must follow.
    """
    folder = await guard_from(ctx).build_folder(build)
    from tools.plan import checklist, next_step
    return {'build': build, 'folder': folder['_path'], 'uuid': folder['uuid'],
            'standing_instructions': await applicable_instructions(ctx, folders, feeds),
            'plan': checklist(), 'next': await next_step(ctx, build), 'done': False}


async def _build_docs(ctx: Context, build: str) -> list[dict[str, Any]]:
    docs = []
    for doc in await guard_from(ctx).folder_contents(build):
        copy_of = next((tag[len(_COPY_OF):] for tag in doc['tags'] if tag.startswith(_COPY_OF)), None)
        docs.append({k: doc[k] for k in ('type', 'uuid', 'name', 'path')} | {'working_copy_of': copy_of})
    # A stable order: approvals are bound to the exact plan built from this list.
    return sorted(docs, key=lambda d: (d['type'], d['name'], d['uuid']))


async def kept_mapping(ctx: Context, pipeline_uuid: str) -> dict[str, Any] | None:
    """The mapping or index plan kept with the pipeline's own XSLT: {'kind', 'payload', 'element', 'xslt'}."""
    stroom = gateway_from(ctx)
    for entry in translation_docs(pipeline_uuid, await stroom.pipeline_layers(pipeline_uuid)):
        if entry['inherited_from_template'] or entry['doc']['type'] != 'XSLT':
            continue
        xslt = await stroom.get_doc('XSLT', entry['doc']['uuid'])
        found = read_mapping(xslt.get('description'))
        if found:
            return {'kind': found[0], 'payload': found[1], 'element': entry['element'], 'xslt': xslt}
    return None


async def _shape_stage(stroom, pipeline_uuid: str) -> str:
    from tools.templates import _shape
    return (await _shape(stroom, pipeline_uuid))['stage']


async def written_fields_section(stroom, pipeline_uuid: str, stream_ids: list[int]) -> str:
    """The Field mapping section of an indexing pipeline whose XSLT keeps no plan: from the documents its own XSLT
    writes, stepped over the sample streams."""
    element = next((e['element'] for e in translation_docs(pipeline_uuid, await stroom.pipeline_layers(pipeline_uuid))
                    if not e['inherited_from_template'] and e['doc']['type'] == 'XSLT'), None)
    if not element:
        raise ToolError("The pipeline has no XSLT of its own to document: give it one, or document the template")
    loaded = await _Pipeline.load(stroom, pipeline_uuid)
    documents = index_documents(list((await _outputs(stroom, loaded, stream_ids, element, None, 200)).values()))
    if not documents:
        raise ToolError(f"Field mapping not written: stepping streams {stream_ids} gave no documents. An indexing "
                        f"pipeline's stream_ids are the Events streams it indexes (or the raw streams, for discovery).")
    return written_fields_markdown(documents)


def mapping_digest(kept: dict[str, Any]) -> str:
    return digest(json.dumps(kept['payload'], sort_keys=True), normalise_xslt(kept['xslt'].get('data') or ''))


async def build_checks(ctx: Context, docs: list[dict[str, Any]]) -> list[str]:
    """What a build's pipelines still lack before promotion: a clean step of their current code (recorded as
    mcp-stepped-* tags on the pipeline), a Documentation doc (for new pipelines), a Field mapping section that
    matches the current mapping and XSLT, and an XSLT that is what its mapping generates."""
    documented = {d['name']: d for d in docs if d['type'] == 'Documentation'}
    problems = []
    for doc in docs:
        if doc['type'] != 'Pipeline':
            continue
        if not await stepped_clean(ctx, doc):
            problems.append(f"Pipeline '{doc['name']}': no clean step_sample or step_records of its current code is "
                            "recorded")
        if not doc['working_copy_of'] and doc['name'] not in documented:
            problems.append(f"Pipeline '{doc['name']}': no documentation (write_documentation)")
        if await _shape_stage(gateway_from(ctx), doc['uuid']) in ('indexing', 'discovery'):
            destination = await elastic_destination(gateway_from(ctx), doc['uuid'])
            if destination and await agreement_problem(gateway_from(ctx), await gateway_from(ctx).get_doc(
                    'Pipeline', doc['uuid']), destination):
                problems.append(f"Pipeline '{doc['name']}': no Elasticsearch index template agreed with the user for "
                                f"its current code (propose_index_template with their example)")
            if not await verified(ctx, doc):
                problems.append(f"Pipeline '{doc['name']}': the sample has not been indexed and verified with its "
                                f"current code (create_processor_filter, wait_for_processing, verify_index)")
        kept = await kept_mapping(ctx, doc['uuid'])
        if not kept:
            continue
        if kept['kind'] == 'translation':
            drift = await _drift(ctx, kept)
            if drift:
                problems.append(f"Pipeline '{doc['name']}': {drift}")
        if doc['name'] in documented:
            body = body_text(await gateway_from(ctx).get_doc('Documentation', documented[doc['name']]['uuid']))
            if doc_digest(body) != mapping_digest(kept):
                problems.append(f"Pipeline '{doc['name']}': the documentation's Field mapping predates the current mapping "
                                f"or XSLT (write_documentation again)")
    return problems


async def _drift(ctx: Context, kept: dict[str, Any]) -> str | None:
    """Whether the XSLT still is what its mapping generates."""
    from tools.generation import event_schema
    try:
        version = kept['payload'].get('schema_version') or gateway_from(ctx).settings.event_logging_version
        regenerated = generate(TranslationMapping.model_validate(kept['payload']['mapping']), await event_schema(ctx, version), version)
    except Exception as e:
        return f"the mapping kept with its XSLT no longer generates ({e})"
    if not regenerated['ok']:
        return f"the mapping kept with its XSLT no longer generates: {regenerated['problems'][:2]}"
    if normalise_xslt(regenerated['xslt']) != normalise_xslt(kept['xslt'].get('data') or ''):
        return ("its XSLT differs from what its mapping generates (edited by hand): change the mapping and build_translation_xslt "
                "(uuid=...) to save it again, or accept that the documentation says so")
    return None


async def field_mapping_section(ctx: Context, pipeline: dict[str, Any], kept: dict[str, Any],
                                stream_ids: list[int]) -> str:
    """The Field mapping section generated from the kept mapping (stepped over the sample streams, with each
    Event marked by its rule) or index plan (with the Events' path population)."""
    stroom = gateway_from(ctx)
    if kept['kind'] == 'translation':
        from tools.generation import event_schema
        version = kept['payload'].get('schema_version') or stroom.settings.event_logging_version
        mapping = TranslationMapping.model_validate(kept['payload']['mapping'])
        schema = await event_schema(ctx, version)
        marked = generate(mapping, schema, version, mark_rules=True)
        if not marked['ok']:
            raise ToolError(f"The mapping kept with the XSLT no longer generates: {marked['problems'][:3]}")
        loaded = await _Pipeline.load(stroom, pipeline['uuid'])
        outputs = await _outputs(stroom, loaded, stream_ids, kept['element'], {kept['element']: marked['xslt']}, 200)
        events = sampled_events(list(outputs.values()))
        if not events:
            # A section of "(not in the sample)" documents nothing: say what the streams are and what stopped them.
            raise ToolError(await _no_events(stroom, loaded, stream_ids, kept['element'], marked['xslt'], len(outputs)))
        section = field_mapping_markdown(mapping, schema, events)
        drift = await _drift(ctx, kept)
        if drift:
            section += f"\nNote: {drift[0].upper() + drift[1:]}.\n"
    elif kept['kind'] == 'cef':
        # The plan's tables, with the values the pipeline writes for the sample Events (stepping sends nothing).
        from tools.cef import _stepped
        from utils.cef import CefPlan, examples_from
        plan = CefPlan.model_validate(kept['payload'])
        lines, events, _, _ = await _stepped(ctx, pipeline['uuid'], stream_ids, kept['element'], 200)
        if not lines:
            raise ToolError(f"Field mapping not written: stepping pipeline '{pipeline.get('name')}' over streams "
                            f"{stream_ids} gave no CEF lines. Its stream_ids are the Events streams it sends "
                            f"(wait_for_processing on the events pipeline lists them).")
        section = plan.markdown(examples_from(plan, lines, [e for e in events if e is not None]))
    else:
        plan = FieldPlan.model_validate(kept['payload'])
        if plan.discovery:
            # Raw records, not Events: the documents the pipeline writes from its sample streams.
            documents, arrays = None, []
            if stream_ids:
                loaded = await _Pipeline.load(stroom, pipeline['uuid'])
                outputs = await _outputs(stroom, loaded, stream_ids, kept['element'], None, 200)
                documents = index_documents(list(outputs.values()))
                arrays = object_arrays(list(outputs.values()))
            return (discovery_field_markdown(plan, documents, arrays) + _agreed_line(pipeline)).rstrip() + '\n\n' + \
                DOC_MARK.format(digest=mapping_digest(kept))
        population = (await summarise_events(ctx, stream_ids, 200))['path_population'] if stream_ids else None
        if stream_ids and not population:
            kinds = [f"{m.get('id')} ({m.get('feedName')}, {m.get('typeName')})" for m in
                     [await _meta_or_none(stroom, sid) or {'id': sid} for sid in stream_ids]]
            raise ToolError(f"Field mapping not written: streams {kinds} hold no events to document. An indexing "
                            f"pipeline's stream_ids are the Events streams it indexes (wait_for_processing on the events "
                            f"pipeline lists them).")
        documents = None
        if stream_ids:
            # What the index gets: the indexing pipeline stepped over its Events streams, its XSLT's documents read.
            loaded = await _Pipeline.load(stroom, pipeline['uuid'])
            outputs = await _outputs(stroom, loaded, stream_ids, kept['element'], None, 200)
            documents = index_documents(list(outputs.values()))
        from tools.generation import event_schema
        try:
            schema = await event_schema(ctx, stroom.settings.event_logging_version)
        except Exception:   # descriptions then come from the plan and the sample alone
            schema = None
        section = index_field_mapping_markdown(plan, population, documents, schema) + _agreed_line(pipeline)
    return section.rstrip() + '\n\n' + DOC_MARK.format(digest=mapping_digest(kept))


def _with_change(ctx: Context, text: str, old: str, change: str, kept: dict[str, Any] | None) -> str:
    """The doc's new text: its version control rows as they were, and this change pending until the build is
    promoted, when the build's changes become one row."""
    from tools.plan import _user
    from utils.versionlog import code_of, with_pending
    from utils.xsltversion import agent_line
    by = f"{_user(ctx)} (agent: {agent_line(ctx)})"
    return with_pending(text, old, change, by, code_of(kept))


async def consolidate_versions(ctx: Context, docs: list[dict[str, Any]]) -> list[str]:
    """A build's pending changes, as one new line of each XSLT's version history and one new row of each doc's
    version control: called when the build is promoted."""
    from utils import versionlog, xsltversion
    stroom = gateway_from(ctx)
    done = []
    for ref in docs:
        if ref['type'] == 'XSLT':
            doc = await stroom.get_doc('XSLT', ref['uuid'])
            pending = xsltversion.pending_of(doc.get('description'))
            if pending:
                doc['data'] = xsltversion.consolidate(doc.get('data') or '', pending)
                doc['description'] = xsltversion.without_pending(doc.get('description'))
                await stroom.put_doc(doc)
                done.append(f"recorded the build's changes as version {len(xsltversion.rows(doc['data']))} of XSLT "
                            f"'{ref['name']}'")
        elif ref['type'] == 'Documentation':
            doc = await stroom.get_doc('Documentation', ref['uuid'])
            text = versionlog.consolidate(body_text(doc))
            if text is not None:
                set_body_text(doc, text)
                await stroom.put_doc(doc)
                done.append(f"recorded the build's changes as version {len(versionlog.rows_of(text))} of "
                            f"documentation '{ref['name']}'")
    return done


async def cef_written_section(ctx: Context, pipeline_uuid: str, stream_ids: list[int]) -> str:
    """The Field mapping section of a CEF pipeline whose XSLT keeps no plan: what its lines carry, from the Events
    they were made from, and what is wrong with them."""
    from tools.cef import _stepped
    from utils import cef
    lines, events, _, sent = await _stepped(ctx, pipeline_uuid, stream_ids, None, 200)
    if not lines:
        raise ToolError(f"Field mapping not written: stepping the pipeline over streams {stream_ids} gave no CEF lines")
    given = [e for e in events if e is not None]
    reviewed = cef.review(lines, given, None)
    header = cef.parse(lines[0])['header']
    plan = cef.CefPlan(output=sent['output'], topic=sent.get('topic'), custom_keys=True,
                       **{n: cef.CefValue(value=v) for (n, _, _), v in zip(cef.HEADER, header[1:])},
                       events={k: [cef.CefField(**{x: m[x] for x in ('path', 'key', 'label') if x in m}) for m in maps]
                               for k, maps in reviewed['implied_mapping'].items()},
                       not_sent=[{'path': n['path'], 'event_type': n['event_type'], 'why': f"in none of its lines "
                                  f"({n['events']} events)"} for n in reviewed['not_sent']])
    section = ("Its XSLT keeps no CEF plan: this is the mapping its lines imply, each key matched to the Event value it "
               "carries in the sample, and the header as the first line has it.\n\n"
               + plan.markdown(cef.examples_from(plan, lines, given)))
    if reviewed['problems']:
        section += "\n### Problems in its lines\n\n" + '\n'.join(f"- {p}" for p in reviewed['problems']) + '\n'
    return section


def _agreed_line(pipeline: dict[str, Any]) -> str:
    """The agreed Elasticsearch index template, for the Field mapping section."""
    agreed = read_agreed_template(pipeline.get('description'))
    if not agreed:
        return ''
    parts = agreed.get('component_templates') or []
    return (f"\nElasticsearch index template `{agreed['name']}`, agreed with the user {agreed['agreed'][:10]}"
            + (f", composed of {', '.join(f'`{c}`' for c in parts)}" if parts else '') + ".\n")


async def _meta_or_none(stroom, stream_id: int) -> dict[str, Any] | None:
    from tools.streams import _meta
    try:
        return await _meta(stroom, stream_id)
    except ToolError:
        return None


async def _no_events(stroom, pipeline: _Pipeline, stream_ids: list[int], element: str, code: str, records: int) -> str:
    """Why stepping an events pipeline over stream_ids gave no events, stream by stream: missing, of another type
    (an Events stream where the raw input belongs), or stopped by an error before the first record."""
    from tools.stepping import _markers, _step
    found = []
    for stream_id in stream_ids:
        meta = await _meta_or_none(stroom, stream_id)
        if meta is None:
            found.append(f"stream {stream_id} does not exist")
            continue
        what = f"stream {stream_id} ({meta.get('feedName')}, {meta.get('typeName')})"
        first = await _step(stroom, pipeline, stream_id, 'FIRST', None, {element: code})
        if first.get('foundRecord'):
            found.append(f"{what}: its records produced no Event")
        else:
            errors = list(dict.fromkeys(m['message'] for m in _markers(first, stream_id)))[:2]
            found.append(f"{what}: no record stepped" + (f" ({'; '.join(errors)})" if errors else ''))
    return (f"Field mapping not written: stepping the pipeline over stream_ids gave no events ({records} records "
            f"stepped). {'; '.join(found)}. An events pipeline's stream_ids are its raw sample streams (Raw Events, "
            f"as uploaded); build_status lists them.")


async def list_build(ctx: Context, build: Build) -> dict[str, Any]:
    """
    Documents in a build, with those that are working copies of production documents marked, and what the
    build's pipelines still lack before promotion (a clean step of their current code, documentation).
    """
    docs = await _build_docs(ctx, build)
    from tools.plan import next_step
    return {'build': build, 'documents': docs, 'before_promotion': await build_checks(ctx, docs),
            'next': await next_step(ctx, build)}


async def write_documentation(
        ctx: Context,
        build: Build,
        pipeline_uuid: Annotated[str | None, Field(description="The pipeline documented (or index_uuid).")] = None,
        markdown: Annotated[str, Field(description="The full documentation, with the sections in "
                                                   "stroom://guide/documentation (Purpose and data, Processing, Field "
                                                   "mapping, Output, Conformance, Open items). The Field mapping section "
                                                   "is generated here from the mapping kept with the XSLT (or the index "
                                                   "plan); only an XSLT written by hand needs it written. The version "
                                                   "control block is added by the tool.")] = '',
        change: Annotated[str, Field(description=(
            "What changed and why, in a line, e.g. 'Created' or 'Mapped CODE_TO_TOKEN': kept pending, and with the "
            "build's other changes one row of the doc's version control when the build is promoted."))] = '',
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="The pipeline's sample streams (raw streams for an events pipeline, Events streams for an "
                        "indexing pipeline): the Field mapping section is generated from the mapping kept with the XSLT, "
                        "stepped over them. Required when the XSLT keeps a mapping.")] = [],
        accept_errors: Annotated[list[AcceptedError] | str, ONE_OR_MORE, Field(
            description="Errors the user says are benign and can be ignored, each with the error's element, an example "
                        "message as triage showed it, and the user's reason in their words. Only those the agent could "
                        "not resolve in its own content. Recorded after the user confirms; later reviews then report "
                        "them as benign, not as problems.")] = [],
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
        index_uuid: Annotated[str | None, Field(
            description="Instead of a pipeline, an existing index to document: its Elastic Index or Lucene Index doc, "
                        "confirmed with the user. The Field mapping section is generated from a survey of the index "
                        "through Stroom (describe_document shows it), and the doc is named after the index doc.")] = None,
) -> dict[str, Any]:
    """
    Create or update the Documentation doc for a pipeline in the build (same name as the pipeline), or, with
    index_uuid, for an existing index (same name as its index doc; the user confirms which index doc first). The
    reply has the doc's link to give the user. Promotion puts it beside the pipeline or index doc of that name unless
    the user chooses another folder. The Field
    mapping section is not taken from the markdown: it is generated from the mapping (or index plan) kept with the
    pipeline's XSLT, stepped over stream_ids, and put in place of whatever the markdown has there, so it always
    agrees with the XSLT. An events pipeline whose XSLT keeps no mapping must bring its own Field mapping
    section. The Errors section is generated too: every kind of error processing the streams produced (from their
    Error streams), with counts and an example, and the errors the user accepted as benign, with their reasons; those
    are kept with the doc, so triage does not raise them again. An update replaces the body and keeps the version
    control rows and the accepted errors; its change waits, with the build's others, for one row when the build is
    promoted. Promoted with the pipeline.
    """
    given = [AcceptedError.model_validate(x) if isinstance(x, dict) else x for x in accept_errors]
    if any('EventDetail/Unknown' in f"{a.example} {a.matches or ''}" for a in given):
        # Seen: the user asked to accept "3 of 50 records come out as EventDetail/Unknown" as benign, with nothing
        # to say which events those were.
        raise ToolError("Events written as EventDetail/Unknown aren't an error to accept as benign. Unknown is agreed "
                        "with the user per rule in build_translation_xslt (allow_unknown), whose form shows what the "
                        "records hold; or map them to their action element. Save the XSLT from its mapping there.")
    from utils.versionlog import body_of
    body = body_of(markdown)
    if not body.strip():
        raise ToolError("The documentation is empty: give the full text in markdown, with the sections in stroom://guide")
    change = change.strip()     # a new doc's is 'Created'; an update needs its own line (checked below)
    if index_uuid:
        if stream_ids or accept_errors:
            raise ToolError("stream_ids and accept_errors are for a pipeline's documentation; an existing index is "
                            "surveyed through Stroom instead. Leave them out with index_uuid.")
        return await _document_index(ctx, build, index_uuid, body, change or 'Created', confirmation_id)
    if not pipeline_uuid:
        raise ToolError("Give pipeline_uuid (a pipeline to document) or index_uuid (an existing index)")
    stroom = gateway_from(ctx)
    pipeline = await stroom.get_doc('Pipeline', pipeline_uuid)
    kept = await kept_mapping(ctx, pipeline_uuid)
    generated_section = None
    noted = []
    if kept and kept['kind'] == 'translation' and stream_ids:
        # An events pipeline's own Events output, given in place of its raw sample (seen in VS Code): the raw streams
        # they were made from.
        stream_ids, noted = await _raw_parents(stroom, stream_ids, pipeline_uuid)
    if kept:
        if not stream_ids:
            # Documentation goes down to the field, with the values the sample gave: it needs the sample.
            what = ("sample raw streams" if kept['kind'] == 'translation' or (kept['payload'] or {}).get('discovery')
                    else "Events streams it sends" if kept['kind'] == 'cef' else "Events streams it indexes")
            raise ToolError(f"Give stream_ids (the pipeline's {what}): the Field mapping section is generated from the "
                            f"{'mapping' if kept['kind'] == 'translation' else 'CEF plan' if kept['kind'] == 'cef' else 'index plan'} kept with the XSLT by "
                            f"stepping them, each field with the values the sample gave")
        generated_section = await field_mapping_section(ctx, pipeline, kept, stream_ids)
        if kept['kind'] == 'translation':
            # What the user's documentation says each source field holds, where the build keeps notes from it.
            from utils.sourcenotes import fields_markdown, merged, notes_in_build
            described = fields_markdown(merged(await notes_in_build(ctx, build)))
            if described:
                generated_section = generated_section.rstrip() + '\n\n' + described
        body = replace_section(body, 'Field mapping', generated_section)
    elif (await _shape_stage(stroom, pipeline_uuid)) in ('indexing', 'discovery'):
        # An indexing XSLT written by hand keeps no plan: the section comes from the documents it writes.
        if not stream_ids:
            raise ToolError("Give stream_ids (the Events streams the pipeline indexes): the Field mapping section is "
                            "generated from the documents it writes from them, each field with the values the sample gave")
        generated_section = await written_fields_section(stroom, pipeline_uuid, stream_ids)
        body = replace_section(body, 'Field mapping', generated_section)
    elif (await _shape_stage(stroom, pipeline_uuid)) == 'forwarding':
        # A CEF pipeline written by hand keeps no plan: the section is the mapping its lines imply.
        if not stream_ids:
            raise ToolError("Give stream_ids (the Events streams the pipeline sends): the Field mapping section is "
                            "generated from the CEF lines it writes from them")
        generated_section = await cef_written_section(ctx, pipeline_uuid, stream_ids)
        body = replace_section(body, 'Field mapping', generated_section)
    elif '## Field mapping' not in body:
        from tools.templates import _shape
        if (await _shape(stroom, pipeline_uuid))['stage'] == 'translation':
            raise ToolError("An events pipeline's documentation needs a '## Field mapping' section. Its XSLT keeps no "
                            "mapping (it was not saved by build_translation_xslt), so write the section from "
                            "describe_document and a stepped sample, or save the XSLT again from its mapping "
                            "(build_translation_xslt uuid=...) and the section is generated here")
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    accepting = [AcceptedError.model_validate(a) if isinstance(a, dict) else a for a in accept_errors]
    if accepting:
        # The user says these are benign: confirmed by them, as the agent cannot decide it for them.
        gate = await consent_from(ctx).require(
            ctx, 'confirmation', 'write_documentation',
            f"Record {len(accepting)} kind(s) of error as benign for pipeline '{pipeline['name']}', so they are not "
            f"raised again", {'errors': [f"{a.element or 'any element'}: {a.matches or a.example[:200]} -- {a.reason}"
                                         for a in accepting]}, confirmation_id)
        if gate:
            return gate
    new_entries = [accepted_entry(a.element, a.example, a.reason, stamp, a.matches) for a in accepting]
    errors_markdown = await _errors_markdown(ctx, pipeline_uuid, stream_ids)

    async def write(ref: dict[str, Any]) -> dict[str, Any]:
        doc = await stroom.get_doc('Documentation', ref['uuid'])
        old = body_text(doc)
        accepted = merge_accepted(read_accepted(old), new_entries)
        # Only the generated section: the agent's own sections on errors (an evaluation's analysis) stay as written.
        text = replace_section(body, 'Errors', _errors_section(errors_markdown, accepted), exact=True)
        set_body_text(doc, _with_change(ctx, text, old, change, kept))
        return await stroom.put_doc(doc)

    name, beside = pipeline['name'], None
    guard = guard_from(ctx)
    copy_of = next((t[len(_COPY_OF):] for t in await guard.tags({'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': name})
                    if t.startswith(_COPY_OF)), None)
    if copy_of:
        # An in-place change (a working copy): the documentation is the production pipeline's, under its name, and
        # its existing doc beside it is changed through a working copy that promotion writes back after a backup.
        original = await stroom.get_doc('Pipeline', copy_of)
        name = original['name']
        beside = await _documentation_beside(stroom, original)
    existing = next((d for d in await _build_docs(ctx, build)
                     if d['type'] == 'Documentation' and d['name'] == name), None)
    if not change:
        if existing or beside:
            raise ToolError("This updates the pipeline's documentation: give change, one line for its version "
                            "control on what changed and why, e.g. 'Mapped CODE_TO_TOKEN'")
        change = 'Created'
    if existing:
        doc = await write(existing)
    elif beside:
        ref = await guard.create('Documentation', name, build, [copy_of_tag(beside['uuid'])])
        copy, current = await stroom.get_doc('Documentation', ref['uuid']), await stroom.get_doc('Documentation', beside['uuid'])
        copy.update({k: current[k] for k in ('data', 'documentation') if k in current})
        await stroom.put_doc(copy)
        doc = await write(ref)
    else:
        doc = await guard.create_filled('Documentation', name, build, write)
    from tools.plan import with_next
    return await with_next(ctx, build, {'type': 'Documentation', 'uuid': doc['uuid'], 'name': doc['name'], 'updated': bool(existing),
                                        **({'field_mapping': generated_section} if generated_section else {}),
                                        **({'note': '; '.join(noted)} if noted else {})})


async def _raw_parents(stroom, stream_ids: list[int], pipeline_uuid: str) -> tuple[list[int], list[str]]:
    """(stream ids, notes): an Events stream this pipeline made, in place of the raw stream it was made from."""
    from tools.streams import _meta
    out, notes = [], []
    for stream_id in stream_ids:
        try:
            meta = await _meta(stroom, int(stream_id))
        except (ToolError, ValueError):
            out.append(stream_id)
            continue
        parent = meta.get('parentMetaId')
        if meta.get('typeName') == 'Events' and meta.get('pipelineUuid') == pipeline_uuid and parent:
            out.append(int(parent))
            notes.append(f"stream {stream_id} is this pipeline's Events output: documented from raw stream {parent}, "
                         f"which it was made from")
        else:
            out.append(stream_id)
    return list(dict.fromkeys(out)), notes


async def _document_index(ctx: Context, build: str, index_uuid: str, body: str, change: str,
                          confirmation_id: str | None) -> dict[str, Any]:
    """The documentation of an existing index, its Field mapping section generated from a survey through Stroom. A doc
    already beside the index doc is changed through a working copy, written back (after a backup) on promotion."""
    from tools.indexing import _path_of, survey_index
    from utils.fielddoc import existing_index_markdown, existing_index_summary
    stroom = gateway_from(ctx)
    doc_type, index = None, None
    for candidate in ('ElasticIndex', 'Index'):
        try:
            index = await stroom.get_doc(candidate, index_uuid)
            doc_type = candidate
            break
        except ToolError:
            continue
    if not doc_type:
        raise ToolError(f"No Elastic Index or Lucene Index doc {index_uuid}: find it with find_documents "
                        f"types=['ElasticIndex', 'Index']")
    ref = {'type': doc_type, 'uuid': index_uuid, 'name': index.get('name')}
    folder = await _path_of(stroom, ref)                      # the explorer's path is the doc's folder
    beside = await _documentation_beside(stroom, ref)
    existing = next((d for d in await _build_docs(ctx, build)
                     if d['type'] == 'Documentation' and d['name'] == ref['name']), None)
    # Asked before the survey, so the index is read once, after the user confirms.
    gate = await consent_from(ctx).require(
        ctx, 'confirmation', 'write_documentation',
        f"Document the existing {'Elastic Index' if doc_type == 'ElasticIndex' else 'Lucene Index'} doc "
        f"'{ref['name']}' ({folder})",
        {'index doc': f"{folder}/{ref['name']} ({doc_type})",
         'drafted in': f"build '{build}', as Documentation '{ref['name']}'",
         'promoted (once you agree)': (f"written back into the existing Documentation '{ref['name']}' beside it, after "
                                       f"a backup" if beside else
                                       f"beside the index doc, in {folder}, unless you choose another folder")},
        confirmation_id)
    if gate:
        return gate
    survey = await survey_index(ctx, doc_type, index_uuid)
    planned: dict[str, Any] = {}
    for p in survey['fed_by']:
        if p['plan']:
            for f in FieldPlan.model_validate(p['plan']).fields:
                planned.setdefault(f.name, f)
    schema = None
    if planned:
        from tools.generation import event_schema
        try:
            schema = await event_schema(ctx, stroom.settings.event_logging_version)
        except Exception:   # descriptions then come from the plan and the sample alone
            schema = None
    section = existing_index_markdown(survey, planned, schema)
    summary = existing_index_summary(survey)
    text = _below_section(replace_section(body, 'Field mapping', section), 'Purpose and data', summary)
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    async def write(target: dict[str, Any]) -> dict[str, Any]:
        doc = await stroom.get_doc('Documentation', target['uuid'])
        old = body_text(doc)
        set_body_text(doc, _with_change(ctx, text, old, change, None))
        return await stroom.put_doc(doc)

    guard = guard_from(ctx)
    if existing:
        doc = await write(existing)
    elif beside:
        # A working copy of the doc beside the index doc: its change log carries on, and promotion writes it back.
        copy_ref = await guard.create('Documentation', ref['name'], build, [copy_of_tag(beside['uuid'])])
        copy, current = await stroom.get_doc('Documentation', copy_ref['uuid']), await stroom.get_doc('Documentation', beside['uuid'])
        copy.update({k: current[k] for k in ('data', 'documentation') if k in current})
        await stroom.put_doc(copy)
        doc = await write(copy_ref)
    else:
        doc = await guard.create_filled('Documentation', ref['name'], build, write)
    destination = (f"written back into the existing doc beside the index doc ({folder}), after a backup" if beside
                   else f"beside the index doc ({folder}) unless they choose another folder "
                        f"(destinations={{'Documentation': '<folder>'}})")
    return {'type': 'Documentation', 'uuid': doc['uuid'], 'name': doc['name'], 'updated': bool(existing or beside),
            'link': doc_link(stroom.settings, 'Documentation', doc['uuid']), 'index': survey['index'],
            'field_mapping': section, 'data_surveyed': summary,
            'next': f"Give the user the link to review the draft. Once they agree, promote_build build='{build}': the "
                    f"doc is {destination}."}


def _below_section(markdown: str, heading: str, text: str) -> str:
    """markdown with text added at the end of the `## heading` section (the agent's own words stay first), or the
    section added at the top with just the text."""
    import re
    found = re.search(rf'^## {re.escape(heading)}[^\n]*\n(.*?)(?=^## |\Z)', markdown, re.M | re.S)
    if not found:
        return f"## {heading}\n\n{text.strip()}\n\n{markdown.lstrip()}"
    return markdown[:found.end(1)].rstrip() + f"\n\n{text.strip()}\n\n" + markdown[found.end(1):].lstrip()


async def _documentation_beside(stroom, pipeline: dict[str, Any]) -> dict[str, Any] | None:
    """The Documentation doc named after a pipeline, in the pipeline's own folder."""
    folder = await _folder_of(stroom, pipeline)
    found = (await stroom.find_documents(pipeline['name'], ['Documentation'], 20)).get('values') or []
    return next((v['docRef'] for v in found if v['docRef'].get('name') == pipeline['name']
                 and _path(v.get('path')) == folder), None)


def _path(path: Any) -> str:
    """An explorer path ('System / A / B', or a list of parts) as 'System/A/B'."""
    parts = path if isinstance(path, list) else str(path or '').split('/')
    return '/'.join(str(p.get('name', p) if isinstance(p, dict) else p).strip() for p in parts if str(p).strip())


async def _folder_of(stroom, ref: dict[str, Any]) -> str | None:
    found = (await stroom.find_documents(ref['name'], [ref.get('type', 'Pipeline')], 20)).get('values') or []
    return next((_path(v.get('path')) for v in found if v['docRef'].get('uuid') == ref['uuid']), None)


async def _errors_markdown(ctx: Context, pipeline_uuid: str, stream_ids: list[int]) -> list[dict[str, Any]] | None:
    """The error groups processing the streams gave, from the Error streams this pipeline wrote for them; None when no
    streams were given."""
    if not stream_ids:
        return None
    from tools.streams import summarise_errors
    stroom = gateway_from(ctx)
    groups: list[dict[str, Any]] = []
    for stream_id in stream_ids:
        children = (await stroom.find_meta([{'type': 'term', 'field': 'Parent Id', 'condition': 'EQUALS', 'value': str(stream_id)},
                                            {'type': 'term', 'field': 'Type', 'condition': 'EQUALS', 'value': 'Error'}],
                                           20)).get('values') or []
        for child in children:
            if (child['meta'].get('pipelineUuid') or pipeline_uuid) == pipeline_uuid:
                groups += (await summarise_errors(ctx, child['meta']['id'])).get('groups') or []
    return groups


def _errors_section(groups: list[dict[str, Any]] | None, accepted: list[dict[str, Any]]) -> str:
    """The Errors section: what processing the sample gave, and the errors the user accepted as benign."""
    from utils.accepted import block
    lines = []
    if groups is None:
        lines.append('Not measured: no processed streams were given.')
    elif not groups:
        lines.append('Processing the sample streams produced no errors.')
    else:
        lines += ['Errors processing the sample streams produced, by kind:', '',
                  '| Class | Element | Severity | Count | Example | Note |', '| --- | --- | --- | --- | --- | --- |']
        for g in groups:
            example = ((g.get('examples') or [{}])[0].get('message') or '')[:160].replace('|', '\\|').replace('\n', ' ')
            lines.append(f"| {g['class']} | `{g.get('element')}` | {g.get('severity')} | {g.get('count')} | {example} | "
                         f"{g.get('reason', '') if g.get('accepted') else ''} |")
    if accepted:
        lines += ['', 'Accepted as benign by the user, so not raised again:', '']
        lines += [f"- `{e.get('element') or 'any element'}`: {e.get('example', '')[:160]} ({e.get('accepted')}): "
                  f"{e.get('reason')}" for e in accepted]
        lines += ['', block(accepted)]
    return '\n'.join(lines) + '\n'


async def promote_build(
        ctx: Context,
        build: Build,
        destinations: Annotated[dict[str, str], Field(
            description="Destination folder per document type or per document UUID, e.g. {'Feed': "
                        "'System/Feeds/Events/Acme', 'Pipeline': 'System/Feeds/Events/Acme', 'XSLT': ...}. "
                        "Working copies ignore this: they are written back over their originals. 'keep' for a "
                        "document UUID leaves it in the build's workspace folder, not promoted (a test feed made "
                        "while fixing, say).")],
        approval_id: Annotated[str | None, Field(description="From an earlier needs_approval reply.")] = None,
) -> dict[str, Any]:
    """
    Promote a build out of the workspace, after approval. New documents and new versions are moved to their
    destination folders (UUIDs are kept, so filters and references keep working); destination folders that
    don't exist yet are listed in the approval and created first. A working copy is written back over its
    original after the original is backed up to <workspace>/backups, then the copy is deleted. The build's
    workspace folder is removed afterwards if nothing is left in it.
    """
    stroom = gateway_from(ctx)
    guard = guard_from(ctx)
    docs = await _build_docs(ctx, build)
    for _ in range(_LISTING_RETRIES):
        if docs:
            break
        # Stroom's explorer can lag a doc written moments ago (on a busy instance): look again before saying so.
        await asyncio.sleep(_LISTING_WAIT)
        docs = await _build_docs(ctx, build)
    if not docs:
        raise ToolError(f"Build '{build}' has no documents")
    plan = []
    for doc in docs:
        if doc['working_copy_of'] and doc['type'] == 'Pipeline':
            # The copied pipeline only existed to step the working copies; the original keeps running.
            plan.append({'doc': doc, 'action': 'discard', 'target': 'deleted after write-back'})
        elif doc['working_copy_of']:
            plan.append({'doc': doc, 'action': 'write back', 'target': doc['working_copy_of']})
        elif destinations.get(doc['uuid'], '').strip().lower() == 'keep':
            # Seen (fix_errors, Haiku): a test feed the agent made had nowhere to go but production, and with no way
            # to leave it out the agent never promoted the fix.
            plan.append({'doc': doc, 'action': 'keep', 'target': 'left in the workspace'})
        else:
            target = destinations.get(doc['uuid']) or destinations.get(doc['type'])
            if not target and doc['type'] == 'Documentation' and (
                    doc['name'].endswith(' source notes') or ' reference - ' in doc['name']):
                # The source's notes and reference documents: beside the feed, where later work on it looks.
                feeds = [d for d in docs if d['type'] == 'Feed']
                target = (destinations.get(feeds[0]['uuid']) or destinations.get('Feed')) if len(feeds) == 1 else None
            if not target and doc['type'] == 'Documentation':
                # The documentation of a pipeline promoted with it: wherever that pipeline goes.
                same = next((d for d in docs if d['type'] == 'Pipeline' and d['name'] == doc['name']
                             and not d['working_copy_of']), None)
                target = (destinations.get(same['uuid']) or destinations.get('Pipeline')) if same else None
            if not target and doc['type'] == 'Dashboard':
                # A verification dashboard: wherever the index it searches goes (seen in VS Code: promotion stopped
                # for want of its destination), else beside the pipelines.
                try:
                    config = (await stroom.get_doc('Dashboard', doc['uuid'])).get('dashboardConfig') or {}
                    searched = {(c.get('settings') or {}).get('dataSource', {}).get('uuid')
                                for c in config.get('components') or [] if c.get('type') == 'query'}
                except ToolError:
                    searched = set()
                index = next((d for d in docs if d['type'] in ('ElasticIndex', 'Index') and d['uuid'] in searched), None)
                target = ((destinations.get(index['uuid']) or destinations.get(index['type'])) if index else None) \
                    or destinations.get('Pipeline')
            if not target and doc['type'] == 'Documentation':
                # The documentation of a production pipeline (an in-place change's) or an existing index: beside it.
                kinds = ('Pipeline', 'ElasticIndex', 'Index')
                found = (await stroom.find_documents(doc['name'], list(kinds), 20)).get('values') or []
                outside = [v['docRef'] for v in found if v['docRef'].get('name') == doc['name']
                           and v['docRef'].get('type') in kinds
                           and not any(d['uuid'] == v['docRef'].get('uuid') for d in docs)]
                target = await _folder_of(stroom, outside[0]) if len(outside) == 1 else None
            if not target:
                raise ToolError(f"No destination for {doc['type']} '{doc['name']}'; add it to destinations")
            plan.append({'doc': doc, 'action': 'move', 'target': '/'.join(folder_parts(target))})
    # Every destination is resolved before approval, so a missing folder can't stop a promotion halfway.
    folders: dict[str, dict[str, Any]] = {}       # path -> folder node, for those that exist
    creating: list[str] = []                      # paths to create, each after its parent
    for target in dict.fromkeys(p['target'] for p in plan if p['action'] == 'move'):
        node, missing = await guard.resolve_folder(target)
        folders[node['_path']] = node
        for n in range(1, len(missing) + 1):
            path = '/'.join([node['_path'], *missing[:n]])
            if path not in creating:
                creating.append(path)
    moving = [p['doc'] for p in plan if p['action'] == 'move' and p['doc']['type'] == 'Pipeline']
    surveys = [d['name'][:-len(' - Survey')] for d in docs if d['type'] == 'Documentation' and d['name'].endswith(' - Survey')]
    processing = await promotion_processing(ctx, [{'uuid': d['uuid'], 'name': d['name']} for d in moving], surveys)
    details = {'build': build, 'plan': [f"create folder {path}" for path in creating]
               + [f"{p['action']} {p['doc']['type']} '{p['doc']['name']}' -> {p['target']}" for p in plan]}
    warnings = await build_checks(ctx, docs)
    if warnings:
        # Shown in the approval, so the user decides with them in view.
        details['warnings'] = warnings
    if processing:
        details['processing after promotion (created disabled, new data only)'] = [
            f"{e['pipeline']['name']}: feed {e['feed']} ({e['stream_type']})" for e in processing]
    gate = await consent_from(ctx).require(ctx, 'approval', 'promote_build', f"Promote build '{build}'", details,
                                           approval_id)
    if gate:
        return gate

    started_ms = int(time.time() * 1000)
    # The build is done: its changes become one line of each XSLT's version history and one row of each doc's.
    versioned = await consolidate_versions(ctx, docs)
    done = []
    for path in creating:
        parent, name = path.rsplit('/', 1)
        # Production folders, so not managed: only generated, to show the server made them.
        folders[path] = await guard.create_folder(folders[parent], name, [GENERATED])
        done.append(f"created folder {path}")
    done += versioned
    from tools.processing_writes import development_batch
    for step in plan:
        if step['action'] == 'move' and step['doc']['type'] == 'Pipeline' and await development_batch(
                ctx, step['doc']['uuid'], False):
            done.append(f"restored the default batch size on '{step['doc']['name']}'")
    order = {'write back': 0, 'move': 1, 'discard': 2, 'keep': 3}
    for step in sorted(plan, key=lambda p: order[p['action']]):
        doc = step['doc']
        if step['action'] == 'keep':
            done.append(f"left {doc['type']} '{doc['name']}' in the workspace")
        elif step['action'] == 'discard':
            await stroom.request('DELETE', '/explorer/v2/delete', {'docRefs': [{k: doc[k] for k in ('type', 'uuid', 'name')}]})
            done.append(f"deleted working-copy pipeline '{doc['name']}'")
        elif step['action'] == 'move':
            node = await stroom.post('/explorer/v2/getFromDocRef', {k: doc[k] for k in ('type', 'uuid', 'name')})
            folder = {k: v for k, v in folders[step['target']].items() if not k.startswith('_')}
            await stroom.request('PUT', '/explorer/v2/move', {'explorerNodes': [node], 'destinationFolder': folder,
                                                              'permissionInheritance': 'DESTINATION'})
            # Now production content: the agent may no longer change it directly.
            # mcp-generated stays, so it is still known as the server's own.
            ref = {k: doc[k] for k in ('type', 'uuid', 'name')}
            # Clean-step and verification records only mean something inside a build.
            tags = await guard.tags(ref)
            await guard.untag([ref], [MANAGED, build_tag(build)] + stepped_tags(tags) + verified_tags(tags))
            done.append(f"moved {doc['type']} '{doc['name']}' to {step['target']}")
        else:
            original = await stroom.get_doc(doc['type'], step['target'])
            copy_doc = await stroom.get_doc(doc['type'], doc['uuid'])
            fields = ('data', 'converterType', 'documentation', 'description')
            unchanged = all(copy_doc.get(k) == original.get(k) for k in fields)
            if unchanged:
                await stroom.request('DELETE', '/explorer/v2/delete', {'docRefs': [{k: doc[k] for k in ('type', 'uuid', 'name')}]})
                done.append(f"discarded unchanged working copy {doc['type']} '{doc['name']}'")
                continue
            backups = await guard.build_folder('backups')
            original_node = await stroom.post('/explorer/v2/getFromDocRef',
                                              {'type': doc['type'], 'uuid': original['uuid'], 'name': original['name']})
            copied = await stroom.post('/explorer/v2/copy', {
                'explorerNodes': [original_node], 'destinationFolder': {k: v for k, v in backups.items() if not k.startswith('_')},
                'permissionInheritance': 'DESTINATION', 'allowRename': True,
                'docName': f"{original['name']} backup {datetime.now(timezone.utc):%Y%m%d%H%M%S}"})
            backup_refs = [n.get('docRef', n) for n in (copied or {}).get('explorerNodes') or []]
            if backup_refs:
                await guard.tag([{k: r[k] for k in ('type', 'uuid', 'name')} for r in backup_refs], [GENERATED])
            for key in fields:
                if key in copy_doc:
                    original[key] = copy_doc[key]
            await stroom.put_doc(original)
            await stroom.request('DELETE', '/explorer/v2/delete', {'docRefs': [{k: doc[k] for k in ('type', 'uuid', 'name')}]})
            done.append(f"wrote {doc['type']} '{doc['name']}' back over '{original['name']}' (backup kept)")
    if await guard.remove_build_folder_if_empty(build):
        done.append("removed the build's workspace folder, now empty")
    filters =await create_promotion_filters(ctx, processing, started_ms)
    result: dict[str, Any] = {'build': build, 'promoted': done}
    if warnings:
        result['promoted_with_warnings'] = warnings
    if filters:
        result['processing_filters'] = filters
        result['next'] = ("Tell the user each promoted pipeline has a processing filter for new data, created disabled: "
                          "review the pipeline (pipeline_link) and enable it on its Processors tab when ready. If an "
                          "earlier version still processes the same feed, disable that one first.")
    return result


ALL_TOOLS = [start_build, write_documentation, promote_build]   # list_build is reached through build_status
