"""Write guard: everything the agent makes lives in its workspace and carries its tags.

Creates land in `<workspace>/<build>/`; updates are allowed only on documents tagged
`mcp-managed`. Changing anything else (a production XSLT, say) goes through a working copy that
`promote_build` writes back after approval and a backup.
"""
import re
from typing import Any

from fastmcp.exceptions import ToolError

from utils.stroom import StroomGateway

MANAGED = 'mcp-managed'
_BUILD = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$')


def build_tag(build: str) -> str:
    return f'mcp-build-{build.lower()}'


def copy_of_tag(uuid: str) -> str:
    return f'mcp-copy-of-{uuid}'


class WriteGuard:
    def __init__(self, stroom: StroomGateway, workspace: str):
        self._stroom = stroom
        self.workspace = workspace

    async def _child_folder(self, parent: dict[str, Any], name: str) -> dict[str, Any]:
        found = await self._stroom.find_documents(name, ['Folder'], 200)
        for value in found.get('values') or []:
            ref = value['docRef']
            if ref.get('type') == 'Folder' and ref.get('name') == name:
                node = await self._stroom.post('/explorer/v2/getFromDocRef', ref)
                parent_path = (value.get('path') or '').replace(' / ', '/')
                if parent_path == parent.get('_path'):
                    return {**node, '_path': f"{parent_path}/{name}"}
        node = await self._stroom.post('/explorer/v2/create', {
            'docType': 'Folder', 'docName': name, 'destinationFolder': _strip(parent),
            'permissionInheritance': 'DESTINATION'})
        await self.tag([_ref(node)], [MANAGED])
        return {**node, '_path': f"{parent.get('_path')}/{name}"}

    async def build_folder(self, build: str) -> dict[str, Any]:
        """The build's folder node, creating the workspace and build folders if needed."""
        if not _BUILD.match(build):
            raise ToolError("Build names are 2 to 64 letters, digits, '.', '_' or '-', e.g. 'keycloak-v1.3'")
        roots = await self._stroom.post('/explorer/v2/fetchExplorerNodes', {
            'openItems': [], 'temporaryOpenedItems': [], 'minDepth': 1, 'ensureVisible': None, 'showAlerts': False,
            'filter': {'includedTypes': None, 'includedRootTypes': None, 'tags': None, 'nodeFlags': None,
                       'requiredPermissions': ['VIEW'], 'nameFilter': None, 'nameFilterChange': False,
                       'recentItems': None}})
        system = {**next(r for r in roots['rootNodes'] if r['type'] == 'System'), '_path': 'System'}
        workspace = await self._child_folder(system, self.workspace)
        folder = await self._child_folder(workspace, build)
        return {**folder, '_open': [system['uniqueKey'], workspace['uniqueKey'], folder['uniqueKey']]}

    async def folder_contents(self, build: str) -> list[dict[str, Any]]:
        """The documents in a build folder, read from the explorer tree (the search index lags new docs)."""
        folder = await self.build_folder(build)
        tree = await self._stroom.post('/explorer/v2/fetchExplorerNodes', {
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
        node = find(tree['rootNodes']) or {}
        return [{'type': c['type'], 'uuid': c['uuid'], 'name': c['name'], 'tags': c.get('tags') or [],
                 'path': folder['_path']} for c in node.get('children') or [] if c['type'] != 'Folder']

    async def create(self, doc_type: str, name: str, build: str, extra_tags: list[str] | None = None) -> dict[str, Any]:
        """Create an empty document in the build folder and tag it as the agent's."""
        folder = await self.build_folder(build)
        node = await self._stroom.post('/explorer/v2/create', {
            'docType': doc_type, 'docName': name, 'destinationFolder': _strip(folder),
            'permissionInheritance': 'DESTINATION'})
        await self.tag([_ref(node)], [MANAGED, build_tag(build), *(extra_tags or [])])
        return _ref(node)

    async def tags(self, ref: dict[str, Any]) -> list[str]:
        node = await self._stroom.post('/explorer/v2/getFromDocRef', ref)
        return node.get('tags') or []

    async def check_managed(self, ref: dict[str, Any]) -> list[str]:
        tags = await self.tags(ref)
        if MANAGED not in tags:
            raise ToolError(f"{ref.get('type')} '{ref.get('name') or ref.get('uuid')}' was not created by this server. "
                            "Make a working copy in a build (copy_pipeline) and change that instead; "
                            "promote_build writes it back after approval.")
        return tags

    async def tag(self, refs: list[dict[str, Any]], tags: list[str]) -> None:
        await self._stroom.request('PUT', '/explorer/v2/addTags', {'docRefs': refs, 'tags': tags})

    async def untag(self, refs: list[dict[str, Any]], tags: list[str]) -> None:
        await self._stroom.request('DELETE', '/explorer/v2/removeTags', {'docRefs': refs, 'tags': tags})


def _ref(node: dict[str, Any]) -> dict[str, Any]:
    return {'type': node['type'], 'uuid': node['uuid'], 'name': node['name']}


def _strip(node: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in node.items() if not k.startswith('_')}


def guard_from(ctx: Any) -> WriteGuard:
    return WriteGuard(ctx.lifespan_context['stroom'], ctx.lifespan_context['stroom'].settings.workspace_folder)
