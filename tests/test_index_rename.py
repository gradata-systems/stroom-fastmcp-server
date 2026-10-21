"""One index name for the build's Elastic Index doc, its indexing pipelines and their index template, and changes to
them only in a build. Seen in testing: the doc named as the user said, the pipeline the plan's name, and the index
template made for the pipeline's, so for an index Stroom doesn't search; then a 403 writing to it."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from security.guard import WriteGuard
from tools import indexing, pipeline_writes
from tools.pipeline_writes import PropertyValue

DOC = {'type': 'ElasticIndex', 'uuid': 'i', 'name': 'ACME-ES'}
PIPELINE = {'type': 'Pipeline', 'uuid': 'ix', 'name': 'ACME-Indexing'}


def _layers(index_name):
    """merge_layers' view of an Elasticsearch indexing pipeline writing to index_name."""
    return {'elements': [{'id': 'elasticIndexingFilter', 'type': 'ElasticIndexingFilter'}],
            'properties': [{'element': 'elasticIndexingFilter', 'name': 'indexName', 'value': index_name}]}


def _world(doc_index='acme-v1', pipeline_index='acme-v1', agreed=None, filters=()):
    docs = {'ElasticIndex': {**DOC, 'indexName': doc_index, 'clusterRef': {'uuid': 'c'}},
            'Pipeline': {**PIPELINE, 'description': agreed}}
    stroom = SimpleNamespace(settings=SimpleNamespace(), get_doc=AsyncMock(side_effect=lambda t, u: dict(docs[t])),
                             put_doc=AsyncMock(side_effect=lambda d: d), pipeline_layers=AsyncMock(return_value=[]),
                             processor_filters=AsyncMock(return_value=list(filters)))
    guard = SimpleNamespace(check_in_build=AsyncMock(return_value='b'),
                            folder_contents=AsyncMock(return_value=[DOC, PIPELINE]))
    return stroom, guard, (lambda layers: _layers(pipeline_index))


@pytest.mark.parametrize('index, name, pattern', [
    ('acme-v1', 'acme-v1', 'acme-v1'),
    ('ecs-windows-{_suffix}-v1', 'ecs-windows-v1', 'ecs-windows-*-v1'),
    ('stroom-index-{_year}', 'stroom-index', 'stroom-index-*'),
])
def test_a_template_for_an_index_name_built_from_values_matches_the_indices_it_makes(index, name, pattern):
    # Seen: 'ecs-windows{_suffix}v1*' as the pattern (matching no index) and the name, its commit button dropped.
    assert indexing.template_target(index) == (name, pattern)


async def test_the_index_doc_and_the_pipeline_must_name_the_same_index():
    stroom, guard, merged = _world(doc_index='acme-v1', pipeline_index='acme-test')
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    with patch.object(indexing, 'guard_from', lambda c: guard), patch.object(indexing, 'merge_layers', merged):
        said = await indexing.other_index(ctx, 'b', 'acme-test', 'ix')
        assert "writes to 'acme-test'" in said and "'ACME-ES' names 'acme-v1'" in said
        assert await indexing.other_index(ctx, 'b', 'acme-v1', 'ix') is None
        # A second index of the build, its doc to come (e2e_elastic_handover): the doc there is another pipeline's.
        two = [DOC, {**PIPELINE, 'uuid': 'ix2', 'name': 'ACME-Flat'}, PIPELINE]
        guard.folder_contents = AsyncMock(return_value=two)
        both = lambda layers: _layers('acme-v1')
        with patch.object(indexing, 'merge_layers', both):
            assert await indexing.other_index(ctx, 'b', 'acme-flat-v1', 'ix2') is None
        guard.folder_contents = AsyncMock(return_value=[DOC, PIPELINE])
        assert await indexing.other_index(ctx, None, 'acme-test') is None     # outside a build: nothing to compare
        # A doc naming a pattern takes each index under it; a name built from values, each it makes.
        stroom.get_doc.side_effect = lambda t, u: {**DOC, 'indexName': 'acme-*'}
        assert await indexing.other_index(ctx, 'b', 'acme-v1') is None
        stroom.get_doc.side_effect = lambda t, u: {**DOC, 'indexName': 'acme-2026'}
        assert await indexing.other_index(ctx, 'b', 'acme-{_year}') is None


async def test_an_indexing_pipeline_takes_the_index_its_doc_names():
    shape = {'stage': 'indexing', 'backend': 'elasticsearch', 'child_must_supply': [
        {'element': 'xsltFilter', 'type': 'XSLTFilter', 'property': 'xslt'},
        {'element': 'elasticIndexingFilter', 'type': 'ElasticIndexingFilter', 'property': 'indexName'}], 'shared': []}
    docs = {'XSLT': {'description': None}, 'ElasticIndex': {**DOC, 'indexName': 'acme-v1', 'clusterRef': {'uuid': 'c'}}}
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(get_doc=AsyncMock(side_effect=lambda t, u: docs[t]))})
    with patch('tools.indexing._shape', AsyncMock(return_value=shape)), \
            patch('tools.indexing._events_available', AsyncMock()), \
            patch('tools.indexing.create_pipeline', AsyncMock(return_value={'uuid': 'p'})) as create:
        with pytest.raises(ToolError, match="names index 'acme-v1', and index_name 'acme-test' doesn't write to it"):
            await indexing.create_indexing_pipeline(ctx, 'b', 'n', 't', 'x', index_uuid='i', index_name='acme-test')
        create.assert_not_called()
        await indexing.create_indexing_pipeline(ctx, 'b', 'n', 't', 'x', index_uuid='i', index_name='acme-v1')
        props = {(p.element, p.name): p.value for p in create.call_args.args[3]}
        assert props[('elasticIndexingFilter', 'indexName')] == 'acme-v1'


async def test_the_index_name_is_set_on_a_pipeline_only_as_its_doc_names_it():
    stroom, guard, merged = _world(doc_index='acme-v1')
    stroom.get_doc.side_effect = lambda t, u: {**PIPELINE, 'pipelineData': {}} if t == 'Pipeline' else \
        {**DOC, 'indexName': 'acme-v1'}
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    with patch.object(pipeline_writes, 'guard_from', lambda c: guard), patch.object(indexing, 'guard_from', lambda c: guard), \
            patch.object(pipeline_writes, 'merge_layers', merged), patch.object(indexing, 'merge_layers', merged), \
            patch.object(pipeline_writes, 'typed_properties', AsyncMock(return_value={})):
        with pytest.raises(ToolError, match="rename_index changes the index name in both"):
            await pipeline_writes.set_pipeline_property(ctx, 'ix', PropertyValue(
                element='elasticIndexingFilter', name='indexName', value='acme-other'))
        stroom.put_doc.assert_not_called()
        await pipeline_writes.set_pipeline_property(ctx, 'ix', PropertyValue(
            element='elasticIndexingFilter', name='indexName', value='acme-v1'))
        stroom.put_doc.assert_awaited_once()


def test_a_template_never_takes_the_examples_own_name():
    with pytest.raises(ToolError, match="the example index template's own name.*Leave template_name out"):
        indexing._not_theirs('ecs-sibling-v1', 'ecs-sibling-v1', 'ecs-sibling-v1', [])
    with pytest.raises(ToolError, match="The index name is the example's: rename_index"):
        indexing._not_theirs('ecs-sibling-v1', None, 'ecs-sibling-v1', [])
    with pytest.raises(ToolError, match="a component template's name"):
        indexing._not_theirs('ecs@mappings', 'ecs@mappings', 'ecs-sibling-v1', ['ecs@mappings'])
    indexing._not_theirs('acme-v1', None, 'ecs-sibling-v1', ['ecs@mappings'])


async def _renamed(stroom, guard, merged, consent, **kw):
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    with patch.object(indexing, 'guard_from', lambda c: guard), patch.object(indexing, 'merge_layers', merged), \
            patch.object(indexing, 'consent_from', lambda c: consent), \
            patch.object(indexing, '_set_index_name', AsyncMock()) as named, \
            patch('tools.plan.with_next', AsyncMock(side_effect=lambda c, b, r: r)):
        return await indexing.rename_index(ctx, 'i', **kw), named


async def test_a_new_index_name_goes_to_the_doc_and_every_pipeline_writing_to_it():
    from utils.mappingstore import with_agreed_template
    agreed = with_agreed_template(None, {'name': 'acme-v1', 'index': 'acme-v1', 'cluster': 'ES', 'dev_tools': ''})
    stroom, guard, merged = _world(agreed=agreed, filters=[{'id': 3}])
    consent = SimpleNamespace(require=AsyncMock(return_value=None))
    result, named = await _renamed(stroom, guard, merged, consent, index_name='acme-prod-v1')
    assert stroom.put_doc.await_args.args[0]['indexName'] == 'acme-prod-v1'
    assert named.await_args.args[1]['uuid'] == 'ix' and named.await_args.args[2] == 'acme-prod-v1'
    details = consent.require.await_args.args[4]
    assert details['indexing pipelines changed with it'] == ['ACME-Indexing']
    # What the old name left on the cluster, for the admin: its template, and the index this build wrote to.
    assert result['for_the_cluster_admin'][0] == 'DELETE _index_template/acme-v1'
    assert result['for_the_cluster_admin'][1].startswith('DELETE ')
    assert any(a['request'] == 'DELETE _index_template/acme-v1' for a in result['user_actions'])
    assert 'propose_index_template again' in result['hint']


async def test_renaming_is_refused_outside_a_build_and_for_names_built_from_values():
    stroom, guard, merged = _world(pipeline_index='acme-{_year}', doc_index='acme-2026')
    consent = SimpleNamespace(require=AsyncMock(return_value=None))
    with pytest.raises(ToolError, match="builds its index name from values"):
        await _renamed(stroom, guard, merged, consent, index_name='acme-prod')
    with pytest.raises(ToolError, match="not an Elasticsearch index name"):
        await _renamed(stroom, guard, merged, consent, index_name='Acme Prod')
    guard.check_in_build.side_effect = ToolError("ElasticIndex 'ACME-ES' is not in a build folder")
    with pytest.raises(ToolError, match="not in a build folder"):
        await _renamed(stroom, guard, merged, consent, index_name='acme-prod')
    stroom.put_doc.assert_not_called()


async def test_a_name_the_user_changes_in_the_index_doc_form_goes_to_the_pipeline_too():
    stroom, guard, merged = _world(pipeline_index='acme-test')
    stroom.get_doc.side_effect = lambda t, u: {'name': 'ES_DEV'} if t == 'ElasticCluster' else {'uuid': 'i', 'name': 'ACME-ES'}
    stroom.post = AsyncMock(return_value={'ok': True})
    guard.create = AsyncMock(return_value={'uuid': 'i'})
    consent = SimpleNamespace(require=AsyncMock(return_value=None))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    args = dict(build='b', backend='elasticsearch', name='ACME-ES', cluster_uuid='dev', time_field='@timestamp')
    with patch.object(indexing, 'consent_from', lambda c: consent), patch.object(indexing, 'guard_from', lambda c: guard), \
            patch.object(indexing, 'merge_layers', merged), \
            patch.object(indexing, '_set_index_name', AsyncMock()) as named, \
            patch('tools.plan.with_next', AsyncMock(side_effect=lambda c, b, r: r)):
        with pytest.raises(ToolError, match="writes to 'acme-test', and no index doc names it yet: give index_name='acme-test'"):
            await indexing.create_index_doc(ctx, index_name='acme-v1', **args)
        with patch.object(indexing, 'edited', lambda c, key, value: 'acme-prod-v1' if key == 'index_name' else value):
            made = await indexing.create_index_doc(ctx, index_name='acme-test', **args)
    assert stroom.put_doc.await_args.args[0]['indexName'] == 'acme-prod-v1'
    assert named.await_args.args[2] == 'acme-prod-v1' and 'ACME-Indexing' in made['pipelines_renamed']


async def test_index_docs_and_pipelines_change_only_in_a_build_folder():
    # A doc moved out of its build in Stroom keeps the server's tag: refused all the same (asked for by the user).
    guard = WriteGuard(AsyncMock(post=AsyncMock(return_value={'tags': ['mcp-managed']})), 'MCP Workspace')
    with patch.object(guard, 'build_of', AsyncMock(return_value=None)), patch('security.guard.audit') as audit, \
            pytest.raises(ToolError, match="is not in a build folder"):
        await guard.check_in_build(DOC)
    audit.assert_called_once_with('access_denied', reason='not_in_build', doc=DOC)
    with patch.object(guard, 'build_of', AsyncMock(return_value='acme-v1.0')):
        assert await guard.check_in_build(DOC) == 'acme-v1.0'
