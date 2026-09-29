"""Tools for builds: the workspace folder, Documentation docs, and promotion out of the workspace."""
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import GENERATED, MANAGED, build_tag, guard_from
from tools.processing_writes import create_promotion_filters, promotion_processing
from utils.consent import consent_from
from utils.stroom import StroomGateway, gateway_from

Build = Annotated[str, Field(description="Build name, e.g. 'keycloak-v1.3'.")]
_COPY_OF = 'mcp-copy-of-'


async def start_build(ctx: Context, build: Build) -> dict[str, Any]:
    """Create (or find) the build's workspace folder. Every write tool creates documents there."""
    folder = await guard_from(ctx).build_folder(build)
    return {'build': build, 'folder': folder['_path'], 'uuid': folder['uuid']}


async def _build_docs(ctx: Context, build: str) -> list[dict[str, Any]]:
    docs = []
    for doc in await guard_from(ctx).folder_contents(build):
        copy_of = next((tag[len(_COPY_OF):] for tag in doc['tags'] if tag.startswith(_COPY_OF)), None)
        docs.append({k: doc[k] for k in ('type', 'uuid', 'name', 'path')} | {'working_copy_of': copy_of})
    # A stable order: approvals are bound to the exact plan built from this list.
    return sorted(docs, key=lambda d: (d['type'], d['name'], d['uuid']))


async def list_build(ctx: Context, build: Build) -> dict[str, Any]:
    """Documents in a build, with those that are working copies of production documents marked."""
    return {'build': build, 'documents': await _build_docs(ctx, build)}


async def write_documentation(
        ctx: Context,
        build: Build,
        pipeline_uuid: Annotated[str, Field(description="The pipeline documented.")],
        markdown: Annotated[str, Field(description="The full documentation, using the sections in stroom://guide "
                                                   "(Purpose and data, Processing, Field mapping, Output, Conformance, "
                                                   "Open items). The change log is added by the tool.")],
        change: Annotated[str, Field(description="One line for the change log, e.g. 'Created' or 'Mapped CODE_TO_TOKEN'.")],
) -> dict[str, Any]:
    """
    Create or update the Documentation doc for a pipeline in the build (same name as the pipeline). An update
    replaces the body and keeps the change log, adding a line. Promoted with the pipeline.
    """
    stroom = gateway_from(ctx)
    guard = guard_from(ctx)
    pipeline = await stroom.get_doc('Pipeline', pipeline_uuid)
    existing = next((d for d in await _build_docs(ctx, build)
                     if d['type'] == 'Documentation' and d['name'] == pipeline['name']), None)
    if existing:
        doc = await stroom.get_doc('Documentation', existing['uuid'])
        old = doc.get('documentation') or ''
        log = old[old.index('## Change log'):] if '## Change log' in old else '## Change log\n'
    else:
        ref = await guard.create('Documentation', pipeline['name'], build)
        doc = await stroom.get_doc('Documentation', ref['uuid'])
        log = '## Change log\n'
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    body = markdown.split('## Change log')[0].rstrip()
    doc['documentation'] = f"{body}\n\n{log.rstrip()}\n- {stamp}: {change}\n"
    doc = await stroom.put_doc(doc)
    return {'type': 'Documentation', 'uuid': doc['uuid'], 'name': doc['name'], 'updated': bool(existing)}


async def _folder_node(stroom: StroomGateway, path: str) -> dict[str, Any]:
    parts = [p for p in path.replace(' / ', '/').split('/') if p]
    if not parts or parts[0] != 'System':
        raise ToolError(f"Destination '{path}' must be an explorer path starting with System/")
    name, parent = parts[-1], '/'.join(parts[:-1])
    found = await stroom.find_documents(name, ['Folder'], 200)
    for value in found.get('values') or []:
        if value['docRef'].get('name') == name and (value.get('path') or '').replace(' / ', '/') == parent:
            return await stroom.post('/explorer/v2/getFromDocRef', value['docRef'])
    raise ToolError(f"Folder '{path}' does not exist; ask the user to create it or choose another")


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
    destination folders (UUIDs are kept, so filters and references keep working). A working copy is written
    back over its original after the original is backed up to <workspace>/backups, then the copy is deleted.
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
            plan.append({'doc': doc, 'action': 'move', 'target': target})
    moving = [p['doc'] for p in plan if p['action'] == 'move' and p['doc']['type'] == 'Pipeline']
    surveys = [d['name'][:-len(' - Survey')] for d in docs if d['type'] == 'Documentation' and d['name'].endswith(' - Survey')]
    processing = await promotion_processing(ctx, [{'uuid': d['uuid'], 'name': d['name']} for d in moving], surveys)
    details = {'build': build, 'plan': [f"{p['action']} {p['doc']['type']} '{p['doc']['name']}' -> {p['target']}"
                                        for p in plan]}
    if processing:
        details['processing after promotion (created disabled, new data only)'] = [
            f"{e['pipeline']['name']}: feed {e['feed']} ({e['stream_type']})" for e in processing]
    gate = await consent_from(ctx).require(ctx, 'approval', 'promote_build', f"Promote build '{build}'", details,
                                           approval_id)
    if gate:
        return gate

    started_ms = int(time.time() * 1000)
    done = []
    order = {'write back': 0, 'move': 1, 'discard': 2}
    for step in sorted(plan, key=lambda p: order[p['action']]):
        doc = step['doc']
        if step['action'] == 'discard':
            await stroom.request('DELETE', '/explorer/v2/delete', {'docRefs': [{k: doc[k] for k in ('type', 'uuid', 'name')}]})
            done.append(f"deleted working-copy pipeline '{doc['name']}'")
        elif step['action'] == 'move':
            node = await stroom.post('/explorer/v2/getFromDocRef', {k: doc[k] for k in ('type', 'uuid', 'name')})
            folder = await _folder_node(stroom, step['target'])
            await stroom.request('PUT', '/explorer/v2/move', {'explorerNodes': [node], 'destinationFolder': folder,
                                                              'permissionInheritance': 'DESTINATION'})
            # Now production content: the agent may no longer change it directly.
            # mcp-generated stays, so it is still known as the server's own.
            await guard.untag([{k: doc[k] for k in ('type', 'uuid', 'name')}], [MANAGED, build_tag(build)])
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
    filters = await create_promotion_filters(ctx, processing, started_ms)
    result: dict[str, Any] = {'build': build, 'promoted': done}
    if filters:
        result['processing_filters'] = filters
        result['next'] = ("Tell the user each promoted pipeline has a processing filter for new data, created disabled: "
                          "review the pipeline (pipeline_link) and enable it on its Processors tab when ready. If an "
                          "earlier version still processes the same feed, disable that one first.")
    return result


ALL_TOOLS = [start_build, list_build, write_documentation, promote_build]
