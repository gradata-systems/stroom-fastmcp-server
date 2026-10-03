"""Tools for builds: the workspace folder, Documentation docs, and promotion out of the workspace."""
import json
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import GENERATED, MANAGED, build_tag, folder_parts, guard_from
from tools.instructions import applicable_instructions
from tools.processing_writes import create_promotion_filters, promotion_processing
from tools.pipelines import translation_docs
from tools.stepping import _outputs, _Pipeline, stepped_clean, stepped_tags
from tools.streams import summarise_events
from utils.fielddoc import (discovery_field_markdown, field_mapping_markdown, index_documents,
                            index_field_mapping_markdown, sampled_events)
from utils.fieldplan import FieldPlan
from utils.mappingstore import (DOC_MARK, digest, doc_digest, normalise_xslt, read_agreed_template, read_mapping,
                                replace_section)
from utils.xsltgen import TranslationMapping, generate
from utils.consent import consent_from
from utils.params import ONE_OR_MORE
from utils.stroom import body_text, gateway_from, set_body_text

Build = Annotated[str, Field(description="Build name, e.g. 'acme-door-v1.3'.")]
_COPY_OF = 'mcp-copy-of-'


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
    else:
        plan = FieldPlan.model_validate(kept['payload'])
        if plan.discovery:
            # Raw records, not Events: the documents the pipeline writes from its sample streams.
            documents = None
            if stream_ids:
                loaded = await _Pipeline.load(stroom, pipeline['uuid'])
                outputs = await _outputs(stroom, loaded, stream_ids, kept['element'], None, 200)
                documents = index_documents(list(outputs.values()))
            return (discovery_field_markdown(plan, documents) + _agreed_line(pipeline)).rstrip() + '\n\n' + \
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
        section = index_field_mapping_markdown(plan, population, documents) + _agreed_line(pipeline)
    return section.rstrip() + '\n\n' + DOC_MARK.format(digest=mapping_digest(kept))


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
        pipeline_uuid: Annotated[str, Field(description="The pipeline documented.")],
        markdown: Annotated[str, Field(description="The full documentation, with the sections in "
                                                   "stroom://guide/documentation (Purpose and data, Processing, Field "
                                                   "mapping, Output, Conformance, Open items). For Field mapping use "
                                                   "field_mapping from build_translation_xslt as it is. The change log "
                                                   "is added by the tool.")],
        change: Annotated[str, Field(description="One line for the change log, e.g. 'Created' or 'Mapped CODE_TO_TOKEN'.")],
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="The pipeline's sample streams (raw streams for an events pipeline, Events streams for an "
                        "indexing pipeline): the Field mapping section is generated from the mapping kept with the XSLT, "
                        "stepped over them. Required when the XSLT keeps a mapping.")] = [],
) -> dict[str, Any]:
    """
    Create or update the Documentation doc for a pipeline in the build (same name as the pipeline). The Field
    mapping section is not taken from the markdown: it is generated from the mapping (or index plan) kept with the
    pipeline's XSLT, stepped over stream_ids, and put in place of whatever the markdown has there, so it always
    agrees with the XSLT. An events pipeline whose XSLT keeps no mapping must bring its own Field mapping
    section. An update replaces the body and keeps the change log, adding a line. Promoted with the pipeline.
    """
    body = markdown.split('## Change log')[0].rstrip()
    if not body.strip():
        raise ToolError("The documentation is empty: give the full text in markdown, with the sections in stroom://guide")
    stroom = gateway_from(ctx)
    pipeline = await stroom.get_doc('Pipeline', pipeline_uuid)
    kept = await kept_mapping(ctx, pipeline_uuid)
    generated_section = None
    if kept:
        if kept['kind'] == 'translation' and not stream_ids:
            raise ToolError("Give stream_ids (the pipeline's sample raw streams): the Field mapping section is generated "
                            "from the mapping kept with the XSLT by stepping them")
        generated_section = await field_mapping_section(ctx, pipeline, kept, stream_ids)
        body = replace_section(body, 'Field mapping', generated_section)
    elif '## Field mapping' not in body:
        from tools.templates import _shape
        if (await _shape(stroom, pipeline_uuid))['stage'] == 'translation':
            raise ToolError("An events pipeline's documentation needs a '## Field mapping' section. Its XSLT keeps no "
                            "mapping (it was not saved by build_translation_xslt), so write the section from "
                            "describe_document and a stepped sample, or save the XSLT again from its mapping "
                            "(build_translation_xslt uuid=...) and the section is generated here")
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    async def write(ref: dict[str, Any]) -> dict[str, Any]:
        doc = await stroom.get_doc('Documentation', ref['uuid'])
        old = body_text(doc)
        log = old[old.index('## Change log'):] if '## Change log' in old else '## Change log\n'
        set_body_text(doc, f"{body}\n\n{log.rstrip()}\n- {stamp}: {change}\n")
        return await stroom.put_doc(doc)

    existing = next((d for d in await _build_docs(ctx, build)
                     if d['type'] == 'Documentation' and d['name'] == pipeline['name']), None)
    if existing:
        doc = await write(existing)
    else:
        doc = await guard_from(ctx).create_filled('Documentation', pipeline['name'], build, write)
    from tools.plan import with_next
    return await with_next(ctx, build, {'type': 'Documentation', 'uuid': doc['uuid'], 'name': doc['name'], 'updated': bool(existing),
                                        **({'field_mapping': generated_section} if generated_section else {})})


async def promote_build(
        ctx: Context,
        build: Build,
        destinations: Annotated[dict[str, str], Field(
            description="Destination folder per document type or per document UUID, e.g. {'Feed': "
                        "'System/Feeds/Events/Acme', 'Pipeline': 'System/Feeds/Events/Acme', 'XSLT': ...}. "
                        "Working copies ignore this: they are written back over their originals.")],
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
    if not docs:
        raise ToolError(f"Build '{build}' has no documents")
    plan = []
    for doc in docs:
        if doc['working_copy_of'] and doc['type'] == 'Pipeline':
            # The copied pipeline only existed to step the working copies; the original keeps running.
            plan.append({'doc': doc, 'action': 'discard', 'target': 'deleted after write-back'})
        elif doc['working_copy_of']:
            plan.append({'doc': doc, 'action': 'write back', 'target': doc['working_copy_of']})
        else:
            target = destinations.get(doc['uuid']) or destinations.get(doc['type'])
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
    done = []
    for path in creating:
        parent, name = path.rsplit('/', 1)
        # Production folders, so not managed: only generated, to show the server made them.
        folders[path] = await guard.create_folder(folders[parent], name, [GENERATED])
        done.append(f"created folder {path}")
    order = {'write back': 0, 'move': 1, 'discard': 2}
    for step in sorted(plan, key=lambda p: order[p['action']]):
        doc = step['doc']
        if step['action'] == 'discard':
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
            # Clean-step records only mean something inside a build.
            await guard.untag([ref], [MANAGED, build_tag(build)] + stepped_tags(await guard.tags(ref)))
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
