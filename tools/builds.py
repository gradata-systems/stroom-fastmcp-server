"""Tools for builds: the workspace folder, Documentation docs, and promotion out of the workspace."""
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import GENERATED, MANAGED, build_tag, folder_parts, guard_from
from tools.instructions import applicable_instructions
from tools.processing_writes import create_promotion_filters, promotion_processing
from tools.stepping import stepped_clean, stepped_tags
from utils.consent import consent_from
from utils.stroom import body_text, gateway_from, set_body_text

Build = Annotated[str, Field(description="Build name, e.g. 'keycloak-v1.3'.")]
_COPY_OF = 'mcp-copy-of-'


async def start_build(
        ctx: Context,
        build: Build,
        feeds: Annotated[list[str], Field(description="Feeds the build is for, if known, so the standing "
                                                      "instructions for their folders are included.")] = [],
        folders: Annotated[list[str], Field(description="Folders the work will be promoted to, if known.")] = [],
) -> dict[str, Any]:
    """
    Create (or find) the build's workspace folder. Every write tool creates documents there. Returns the
    standing instructions (AGENTS docs) that apply, which the work must follow.
    """
    folder = await guard_from(ctx).build_folder(build)
    return {'build': build, 'folder': folder['_path'], 'uuid': folder['uuid'],
            'standing_instructions': await applicable_instructions(ctx, folders, feeds)}


async def _build_docs(ctx: Context, build: str) -> list[dict[str, Any]]:
    docs = []
    for doc in await guard_from(ctx).folder_contents(build):
        copy_of = next((tag[len(_COPY_OF):] for tag in doc['tags'] if tag.startswith(_COPY_OF)), None)
        docs.append({k: doc[k] for k in ('type', 'uuid', 'name', 'path')} | {'working_copy_of': copy_of})
    # A stable order: approvals are bound to the exact plan built from this list.
    return sorted(docs, key=lambda d: (d['type'], d['name'], d['uuid']))


async def build_checks(ctx: Context, docs: list[dict[str, Any]]) -> list[str]:
    """What a build's pipelines still lack before promotion: a clean step of their current code (recorded as
    mcp-stepped-* tags on the pipeline), and (for new pipelines) a Documentation doc."""
    documented = {d['name'] for d in docs if d['type'] == 'Documentation'}
    problems = []
    for doc in docs:
        if doc['type'] != 'Pipeline':
            continue
        if not await stepped_clean(ctx, doc):
            problems.append(f"Pipeline '{doc['name']}': no clean step_sample or step_records of its current code is "
                            "recorded")
        if not doc['working_copy_of'] and doc['name'] not in documented:
            problems.append(f"Pipeline '{doc['name']}': no documentation (write_documentation)")
    return problems


async def list_build(ctx: Context, build: Build) -> dict[str, Any]:
    """
    Documents in a build, with those that are working copies of production documents marked, and what the
    build's pipelines still lack before promotion (a clean step of their current code, documentation).
    """
    docs = await _build_docs(ctx, build)
    return {'build': build, 'documents': docs, 'before_promotion': await build_checks(ctx, docs)}


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
) -> dict[str, Any]:
    """
    Create or update the Documentation doc for a pipeline in the build (same name as the pipeline). An update
    replaces the body and keeps the change log, adding a line. Promoted with the pipeline.
    """
    body = markdown.split('## Change log')[0].rstrip()
    if not body.strip():
        raise ToolError("The documentation is empty: give the full text in markdown, with the sections in stroom://guide")
    stroom = gateway_from(ctx)
    pipeline = await stroom.get_doc('Pipeline', pipeline_uuid)
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
    return {'type': 'Documentation', 'uuid': doc['uuid'], 'name': doc['name'], 'updated': bool(existing)}


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


ALL_TOOLS = [start_build, list_build, write_documentation, promote_build]
