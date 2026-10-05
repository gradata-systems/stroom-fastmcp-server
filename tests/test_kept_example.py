"""The user's example index template, kept in the build: a summarised conversation lost it, and the agent proposed a
template it wrote itself as the user's example."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tools import indexing

EXAMPLE = ('PUT _index_template/stroom_twitter\n{"index_patterns": ["stroom-twitter*"], "priority": 1, '
           '"template": {"mappings": {"properties": {"User": {"properties": {"Id": {"type": "keyword"}}}}}}}')


class Store:
    """Documentation docs in one build folder, as the guard and gateway see them."""

    def __init__(self):
        self.docs = {}

    async def folder_contents(self, build):
        return [{'type': 'Documentation', 'uuid': u, 'name': d['name'], 'tags': [], 'path': build}
                for u, d in self.docs.items()]

    async def create_filled(self, doc_type, name, build, fill):
        uuid = f'd{len(self.docs) + 1}'
        self.docs[uuid] = {'uuid': uuid, 'name': name, 'data': ''}
        return await fill({'type': doc_type, 'uuid': uuid, 'name': name})

    async def get_doc(self, doc_type, uuid):
        return dict(self.docs[uuid])

    async def put_doc(self, doc):
        self.docs[doc['uuid']] = doc
        return doc


async def test_the_pasted_example_is_kept_once_and_read_back_whole():
    store = Store()
    ctx = SimpleNamespace(lifespan_context={})
    with patch.object(indexing, 'guard_from', lambda ctx: store), patch.object(indexing, 'gateway_from', lambda ctx: store), \
            patch('tools.plan.build_of', AsyncMock(return_value='b1')), \
            patch.object(indexing, 'set_body_text', lambda doc, text: doc.__setitem__('data', text)):
        await indexing._keep_example(ctx, 'b1', 'firewall-edge-v1', EXAMPLE, [])
        await indexing._keep_example(ctx, 'b1', 'firewall-edge-v1', EXAMPLE, ['PUT _component_template/base {}'])
        assert [d['name'] for d in store.docs.values()] == ['firewall-edge-v1 example index template']   # updated
        kept = await indexing._kept_example(ctx, 'pipeline-1', ['firewall-edge-v1'])
    assert kept == (EXAMPLE, ['PUT _component_template/base {}'])
    assert '```\nPUT _index_template/stroom_twitter' in next(iter(store.docs.values()))['data']   # readable in Stroom


def test_a_template_is_told_from_something_else():
    assert indexing._is_template(EXAMPLE)
    # What the agent sent as the user's example: their PUT line and settings, its own fields, no index_patterns.
    assert not indexing._is_template('PUT _index_template/stroom_twitter\n{"priority": 1, "template": {"settings": {}, '
                                     '"mappings": {"properties": {"SourcePort": {"type": "long"}}}}}')
