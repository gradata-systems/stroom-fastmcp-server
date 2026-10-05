"""Write guard: everything the agent makes lives in its workspace and carries its tags.

Creates land in `<workspace>/<build>/`; updates are allowed only on documents tagged
`mcp-managed`. Changing anything else (a production XSLT, say) goes through a working copy that
`promote_build` writes back after approval and a backup.
"""
import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp.exceptions import ToolError

from security.audit import audit
from utils.stroom import StroomGateway, explorer_filter

# The agent may change these; the tag comes off when a build is promoted.
MANAGED = 'mcp-managed'
# On everything the server creates, for good: find it in Stroom by this tag, promoted or not.
GENERATED = 'mcp-generated'
logger = logging.getLogger(__name__)
_BUILD = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$')


def build_tag(build: str) -> str:
    return f'mcp-build-{build.lower()}'


def copy_of_tag(uuid: str) -> str:
    return f'mcp-copy-of-{uuid}'


def _node_in(nodes: list[dict[str, Any]], uuid: str) -> dict[str, Any] | None:
    for node in nodes:
        if node.get('uuid') == uuid:
            return node
        hit = _node_in(node.get('children') or [], uuid)
        if hit:
            return hit
    return None


class WriteGuard:
    def __init__(self, stroom: StroomGateway, workspace: str):
        self._stroom = stroom
        self.workspace = workspace

    def _known_folders(self) -> dict[tuple[str, str], dict[str, Any]]:
        # Folders this server process found or made, by (parent uuid, name), kept with its Stroom gateway. The search
        # index lags a new folder: two docs created one after the other (copy_pipeline's pipeline and its XSLT)
        # each looked for the build folder there, and the second made a twin of the same name.
        known = getattr(self._stroom, '_known_folders', None)
        if known is None:
            known = {}
            setattr(self._stroom, '_known_folders', known)
        return known

    async def _tree_child(self, parent: dict[str, Any], name: str) -> dict[str, Any] | None:
        """The parent's child folder of that name as the explorer tree lists it (it shows a new folder before the
        search index does), when the parent's path in the tree is known."""
        if not parent.get('_open'):
            return None
        tree = await self._stroom.post('/explorer/v2/fetchExplorerNodes', {
            'openItems': parent['_open'], 'temporaryOpenedItems': [], 'minDepth': 1, 'ensureVisible': None,
            'showAlerts': False, 'filter': {'includedTypes': None, 'includedRootTypes': None, 'tags': None,
                                            'nodeFlags': None, 'requiredPermissions': ['VIEW'], 'nameFilter': None,
                                            'nameFilterChange': False, 'recentItems': None}})
        node = _node_in(tree.get('rootNodes') or [], parent['uuid'])
        return next((c for c in (node or {}).get('children') or []
                     if c.get('type') == 'Folder' and c.get('name') == name), None)

    def _placed(self, parent: dict[str, Any], name: str, node: dict[str, Any]) -> dict[str, Any]:
        node = {k: v for k, v in node.items() if not k.startswith('_')}
        self._known_folders()[(parent.get('uuid'), name)] = node
        placed = {**node, '_path': f"{parent.get('_path')}/{name}"}
        if parent.get('_open') and node.get('uniqueKey'):
            placed['_open'] = [*parent['_open'], node['uniqueKey']]
        return placed

    async def find_child_folder(self, parent: dict[str, Any], name: str) -> dict[str, Any] | None:
        known = self._known_folders().get((parent.get('uuid'), name))
        if known:
            if await self._stroom.post('/explorer/v2/getFromDocRef', _ref(known)):
                return self._placed(parent, name, known)
            self._known_folders().pop((parent.get('uuid'), name), None)    # deleted since
        child = await self._tree_child(parent, name)
        # Just deleted, the tree still lists it for a moment: only one that still resolves counts.
        if child and await self._stroom.post('/explorer/v2/getFromDocRef', _ref(child)):
            return self._placed(parent, name, child)
        found = await self._stroom.find_documents(name, ['Folder'], 200)
        for value in found.get('values') or []:
            ref = value['docRef']
            if ref.get('type') == 'Folder' and ref.get('name') == name:
                parent_path = (value.get('path') or '').replace(' / ', '/')
                if parent_path == parent.get('_path'):
                    node = await self._stroom.post('/explorer/v2/getFromDocRef', ref)
                    if not node:
                        continue    # just deleted: Stroom's explorer search can still list it for a moment
                    return self._placed(parent, name, node)
        return None

    async def create_folder(self, parent: dict[str, Any], name: str, tags: list[str]) -> dict[str, Any]:
        node = await self._stroom.post('/explorer/v2/create', {
            'docType': 'Folder', 'docName': name, 'destinationFolder': _strip(parent),
            'permissionInheritance': 'DESTINATION'})
        await self.tag([_ref(node)], tags)
        return self._placed(parent, name, node)

    async def _child_folder(self, parent: dict[str, Any], name: str) -> dict[str, Any]:
        return await self.find_child_folder(parent, name) or await self.create_folder(parent, name, [MANAGED, GENERATED])

    async def system_node(self) -> dict[str, Any]:
        roots = await self._stroom.post('/explorer/v2/fetchExplorerNodes', {
            'openItems': [], 'temporaryOpenedItems': [], 'minDepth': 1, 'ensureVisible': None, 'showAlerts': False,
            'filter': {'includedTypes': None, 'includedRootTypes': None, 'tags': None, 'nodeFlags': None,
                       'requiredPermissions': ['VIEW'], 'nameFilter': None, 'nameFilterChange': False,
                       'recentItems': None}})
        system = next(r for r in roots['rootNodes'] if r['type'] == 'System')
        return {**system, '_path': 'System', **({'_open': [system['uniqueKey']]} if system.get('uniqueKey') else {})}

    async def resolve_folder(self, path: str) -> tuple[dict[str, Any], list[str]]:
        """The deepest folder that exists along an explorer path such as 'System/Feeds/Events/Acme', and the
        names below it that don't exist yet (empty when the whole path does). Creates nothing."""
        parts = folder_parts(path)
        node = await self.system_node()
        for n, name in enumerate(parts[1:], start=1):
            child = await self.find_child_folder(node, name)
            if child is None:
                return node, parts[n:]
            node = child
        return node, []

    async def build_folder(self, build: str, create: bool = True) -> dict[str, Any] | None:
        """The build's folder node, creating the workspace and build folders if needed; with create=False,
        None when there is none (reads mustn't bring back a folder that promotion removed)."""
        if not _BUILD.match(build):
            raise ToolError("Build names are 2 to 64 letters, digits, '.', '_' or '-', e.g. 'keycloak-v1.3'")
        system = await self.system_node()
        child = self._child_folder if create else self.find_child_folder
        workspace = await child(system, self.workspace)
        folder = workspace and await child(workspace, build)
        if folder is None:
            return None
        return {**folder, '_open': [system['uniqueKey'], workspace['uniqueKey'], folder['uniqueKey']]}

    async def folder_contents(self, build: str) -> list[dict[str, Any]]:
        """The documents in a build folder, read from the explorer tree (the search index lags new docs)."""
        folder, node = await self._build_node(build)
        if folder is None:
            return []
        docs = {c['uuid']: {'type': c['type'], 'uuid': c['uuid'], 'name': c['name'], 'tags': c.get('tags') or [],
                            'path': folder['_path']} for c in node.get('children') or [] if c['type'] != 'Folder'}
        # The tree can briefly leave out a doc whose node was just updated (a tag added, say), and a promotion that
        # missed it would leave it behind: the docs tagged with the build, from the search index, are added too.
        tagged = explorer_filter(None, '*')
        tagged['tags'] = [build_tag(build)]
        found = await self._stroom.post('/explorer/v2/find', {'filter': tagged, 'pageRequest': {'offset': 0, 'length': 1000}})
        for value in found.get('values') or []:
            ref = value.get('docRef') or {}
            if ref.get('type') == 'Folder' or ref.get('uuid') in docs \
                    or (value.get('path') or '').replace(' / ', '/') != folder['_path']:
                continue
            node = await self._stroom.post('/explorer/v2/getFromDocRef', ref)
            if node:
                docs[ref['uuid']] = {'type': ref['type'], 'uuid': ref['uuid'], 'name': ref.get('name'),
                                     'tags': node.get('tags') or [], 'path': folder['_path']}
        return list(docs.values())

    async def remove_build_folder_if_empty(self, build: str) -> bool:
        """Delete a build's folder once nothing at all is left in it, subfolders included. Stroom deletes a
        folder with everything in it, so it must both list no children and be flagged a leaf (L)."""
        for attempt in range(5):
            folder, node = await self._build_node(build)
            if folder is None:
                return False
            if await self.folder_contents(build):
                return False    # docs are left (one the tree left out would go with the folder)
            if node and not node.get('children') and 'L' in (node.get('nodeFlags') or []):
                break
            # Nothing is listed in it, but the tree doesn't show it empty yet: right after docs are moved out it
            # still lists them for a moment.
            await asyncio.sleep(1 + attempt)
        else:
            return False
        # Right after a doc in it is deleted (a working-copy pipeline), Stroom can answer the delete without the
        # folder going, and right after the call it may not resolve yet still be there: it is removed only once it
        # still doesn't resolve a moment later.
        for attempt in range(5):
            await self._stroom.request('DELETE', '/explorer/v2/delete', {'docRefs': [_ref(folder)]})
            await asyncio.sleep(1 + attempt)
            if not await self._stroom.post('/explorer/v2/getFromDocRef', _ref(folder)):
                for key, node in list(self._known_folders().items()):
                    if node.get('uuid') == folder['uuid']:
                        del self._known_folders()[key]
                return True
        return False

    async def _build_node(self, build: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """The build's folder and its explorer node, opened so that its children are listed; (None, {}) when
        the build has no folder."""
        folder = await self.build_folder(build, create=False)
        if folder is None:
            return None, {}
        tree =await self._stroom.post('/explorer/v2/fetchExplorerNodes', {
            'openItems': folder['_open'], 'temporaryOpenedItems': [], 'minDepth': 1, 'ensureVisible': None,
            'showAlerts': False, 'filter': {'includedTypes': None, 'includedRootTypes': None, 'tags': None,
                                            'nodeFlags': None, 'requiredPermissions': ['VIEW'], 'nameFilter': None,
                                            'nameFilterChange': False, 'recentItems': None}})

        def find(nodes):
            for node in nodes:
                if node['uuid'] == folder['uuid']:
                    return node
                hit = find(node.get('children') or [])
                if hit:
                    return hit
            return None
        return folder, find(tree['rootNodes']) or {}

    async def create(self, doc_type: str, name: str, build: str, extra_tags: list[str] | None = None) -> dict[str, Any]:
        """Create an empty document in the build folder and tag it as the agent's."""
        folder = await self.build_folder(build)
        node = await self._stroom.post('/explorer/v2/create', {
            'docType': doc_type, 'docName': name, 'destinationFolder': _strip(folder),
            'permissionInheritance': 'DESTINATION'})
        await self.tag([_ref(node)], [MANAGED, GENERATED, build_tag(build), *(extra_tags or [])])
        return _ref(node)

    async def create_filled(self, doc_type: str, name: str, build: str,
                            fill: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """Create a document and fill it with fill(ref). Stroom creates documents empty, so if filling fails
        the document is deleted again rather than left behind empty, and the error is raised."""
        ref = await self.create(doc_type, name, build)
        try:
            return await fill(ref)
        except Exception:
            try:
                await self._stroom.request('DELETE', '/explorer/v2/delete', {'docRefs': [ref]})
            except Exception:
                logger.warning("Couldn't delete the empty %s '%s' (%s) after filling it failed",
                               doc_type, name, ref['uuid'])
            raise

    async def tags(self, ref: dict[str, Any]) -> list[str]:
        node = await self._stroom.post('/explorer/v2/getFromDocRef', ref)
        return node.get('tags') or []

    async def check_managed(self, ref: dict[str, Any]) -> list[str]:
        tags = await self.tags(ref)
        if MANAGED not in tags:
            audit('access_denied', reason='not_managed', doc={k: ref.get(k) for k in ('type', 'uuid', 'name')})
            raise ToolError(f"{ref.get('type')} '{ref.get('name') or ref.get('uuid')}' was not created by this server. "
                            "Make a working copy in a build (copy_pipeline) and change that instead; "
                            "promote_build writes it back after approval.")
        return tags

    async def check_built(self, ref: dict[str, Any]) -> list[str]:
        """A doc this server generated, whether still in the workspace or promoted."""
        tags = await self.tags(ref)
        if GENERATED not in tags and MANAGED not in tags:
            audit('access_denied', reason='not_built', doc={k: ref.get(k) for k in ('type', 'uuid', 'name')})
            raise ToolError(f"{ref.get('type')} '{ref.get('name') or ref.get('uuid')}' was not built by this server")
        return tags

    async def tag(self, refs: list[dict[str, Any]], tags: list[str]) -> None:
        await self._stroom.request('PUT', '/explorer/v2/addTags', {'docRefs': refs, 'tags': tags})

    async def untag(self, refs: list[dict[str, Any]], tags: list[str]) -> None:
        await self._stroom.request('DELETE', '/explorer/v2/removeTags', {'docRefs': refs, 'tags': tags})


def folder_parts(path: str) -> list[str]:
    """An explorer path's folder names, starting with System; 'System / Feeds' and 'System/Feeds/' both work."""
    parts = [p.strip() for p in path.replace(' / ', '/').split('/') if p.strip()]
    if not parts or parts[0] != 'System':
        raise ToolError(f"Destination '{path}' must be an explorer path starting with System/")
    return parts


def _ref(node: dict[str, Any]) -> dict[str, Any]:
    return {'type': node['type'], 'uuid': node['uuid'], 'name': node['name']}


def _strip(node: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in node.items() if not k.startswith('_')}


def guard_from(ctx: Any) -> WriteGuard:
    return WriteGuard(ctx.lifespan_context['stroom'], ctx.lifespan_context['stroom'].settings.workspace_folder)
