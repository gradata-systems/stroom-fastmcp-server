"""An in-memory Stroom explorer for tests of what the write guard and the build record do: folders and documents
with tags, as the explorer tree, getFromDocRef and find show them, and documents that refuse a stale save."""
import copy
import fnmatch
from types import SimpleNamespace

from fastmcp.exceptions import ToolError


class Explorer:
    def __init__(self, workspace: str = 'MCP Workspace'):
        self.settings = SimpleNamespace(workspace_folder=workspace, stroom_url='http://stroom', stroom_ui_url=None)
        self.nodes = {'0': {'type': 'System', 'uuid': '0', 'name': 'System', 'tags': [], 'children': []}}
        self.parent: dict[str, str] = {}
        self.docs: dict[str, dict] = {}
        self.saves = 0
        self._n = 0

    # Setting up
    def add(self, doc_type: str, name: str, parent: str = '0', uuid: str | None = None, tags=()) -> dict:
        self._n += 1
        uuid = uuid or f'{doc_type.lower()}-{self._n}'
        node = {'type': doc_type, 'uuid': uuid, 'name': name, 'tags': list(tags), 'children': []}
        self.nodes[uuid] = node
        self.parent[uuid] = parent
        self.nodes[parent]['children'].append(node)
        return node

    def folder(self, path: str) -> dict:
        """The folder at 'System/a/b', made if missing."""
        node = self.nodes['0']
        for name in path.split('/')[1:]:
            child = next((c for c in node['children'] if c['type'] == 'Folder' and c['name'] == name), None)
            node = child or self.add('Folder', name, node['uuid'])
        return node

    def in_build(self, build: str, doc_type: str, name: str, uuid: str | None = None, tags=('mcp-managed', 'mcp-generated')) -> dict:
        folder = self.folder(f"System/{self.settings.workspace_folder}/{build}")
        return self.ref(self.add(doc_type, name, folder['uuid'], uuid, tags))

    @staticmethod
    def ref(node: dict) -> dict:
        return {k: node[k] for k in ('type', 'uuid', 'name')}

    def path(self, uuid: str) -> str:
        names = []
        while uuid in self.parent:
            uuid = self.parent[uuid]
            names.append(self.nodes[uuid]['name'])
        return ' / '.join(reversed(names))

    def tags(self, uuid: str) -> list[str]:
        return self.nodes[uuid]['tags']

    # Stroom's API, as the gateway calls it
    def _node(self, node: dict) -> dict:
        out = {k: v for k, v in node.items() if k != 'children'}
        out['uniqueKey'] = {'type': node['type'], 'uuid': node['uuid'], 'rootNodeUuid': '0'}
        return out

    def _tree(self, node: dict) -> dict:
        out = self._node(node)
        out['children'] = [self._tree(c) for c in node['children']] or None
        out['nodeFlags'] = [] if node['children'] else ['L']
        return out

    async def post(self, path: str, body: dict):
        if path == '/explorer/v2/getFromDocRef':
            node = self.nodes.get(body.get('uuid'))
            return copy.deepcopy(self._node(node)) if node else None
        if path == '/explorer/v2/fetchExplorerNodes':
            return {'rootNodes': [self._tree(self.nodes['0'])]}
        if path == '/explorer/v2/find':
            wanted = body['filter']
            values = []
            for uuid, node in self.nodes.items():
                if uuid == '0' or (wanted.get('includedTypes') and node['type'] not in wanted['includedTypes']):
                    continue
                if wanted.get('tags') and not set(wanted['tags']) <= set(node['tags']):
                    continue
                if not fnmatch.fnmatchcase(node['name'], wanted.get('nameFilter') or '*'):
                    continue
                values.append({'docRef': self.ref(node), 'path': self.path(uuid)})
            return {'values': values}
        if path == '/explorer/v2/create':
            return self._node(self.add(body['docType'], body['docName'], body['destinationFolder']['uuid']))
        raise AssertionError(f'unexpected POST {path}')

    async def request(self, method: str, path: str, body=None):
        if path == '/explorer/v2/addTags':
            for ref in body['docRefs']:
                tags = self.nodes[ref['uuid']]['tags']
                tags += [t for t in body['tags'] if t not in tags]
        elif path == '/explorer/v2/removeTags':
            for ref in body['docRefs']:
                node = self.nodes[ref['uuid']]
                node['tags'] = [t for t in node['tags'] if t not in body['tags']]
        elif path == '/explorer/v2/delete':
            for ref in body['docRefs']:
                self._delete(ref['uuid'])
        else:
            raise AssertionError(f'unexpected {method} {path}')
        return {}

    def _delete(self, uuid: str) -> None:
        for child in list(self.nodes[uuid]['children']):
            self._delete(child['uuid'])
        parent = self.nodes[self.parent.pop(uuid)]
        parent['children'] = [c for c in parent['children'] if c['uuid'] != uuid]
        del self.nodes[uuid]
        self.docs.pop(uuid, None)

    async def find_documents(self, name, types, limit, offset=0):
        return await self.post('/explorer/v2/find', {'filter': {'includedTypes': types, 'nameFilter': name}})

    async def get_doc(self, doc_type: str, uuid: str) -> dict:
        if uuid not in self.nodes:
            raise ToolError(f'Stroom rejected the request (404): no {doc_type} {uuid}')
        doc = self.docs.setdefault(uuid, {'type': doc_type, 'uuid': uuid, 'name': self.nodes[uuid]['name'], 'version': 'v0'})
        return copy.deepcopy(doc)

    async def put_doc(self, doc: dict) -> dict:
        if doc.get('type') == 'Documentation' and 'description' in doc:
            raise ToolError('Stroom rejected the request (400): Unable to process JSON (Unrecognized field '
                            '"description" (class stroom.documentation.shared.DocumentationDoc))')
        current = self.docs.get(doc['uuid']) or {}
        if current.get('version') != doc.get('version'):
            raise ToolError(f"Stroom rejected the request (500): DocRef{{uuid='{doc['uuid']}'}} has been modified by "
                            f"another user")
        self.saves += 1
        saved = {**copy.deepcopy(doc), 'version': f'v{self.saves}'}
        self.docs[doc['uuid']] = saved
        return copy.deepcopy(saved)
