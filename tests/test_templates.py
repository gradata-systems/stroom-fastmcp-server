from tools.templates import _classify, _slot


def test_stage_and_backend_from_elements():
    events = ({'jsonParser': 'JSONParser', 'schemaFilter': 'SchemaFilter'}, {('schemaFilter', 'schemaGroup'): 'EVENTS'})
    assert _classify(*events) == ('translation', None)
    lucene = ({'xmlParser': 'XMLParser', 'indexingFilter': 'IndexingFilter'}, {})
    assert _classify(*lucene) == ('indexing', 'lucene')
    elastic = ({'xmlParser': 'XMLParser', 'elasticIndexingFilter': 'ElasticIndexingFilter'}, {})
    assert _classify(*elastic) == ('indexing', 'elasticsearch')
    discovery = ({'jsonParser': 'JSONParser', 'elasticIndexingFilter': 'ElasticIndexingFilter'}, {})
    assert _classify(*discovery) == ('discovery', 'elasticsearch')


def test_only_the_first_unset_xslt_is_required():
    open_slots, shared = [], []
    _slot('translationFilter', 'XSLTFilter', 'xslt', None, open_slots, shared)
    _slot('decorationFilter', 'XSLTFilter', 'xslt', None, open_slots, shared)
    _slot('elasticIndexingFilter', 'ElasticIndexingFilter', 'cluster', {'name': 'ES_PROD'}, open_slots, shared)
    assert 'optional' not in open_slots[0] and 'optional' in open_slots[1]
    assert shared == [{'element': 'elasticIndexingFilter', 'type': 'ElasticIndexingFilter', 'property': 'cluster',
                       'value': {'name': 'ES_PROD'}}]


async def test_one_child_stroom_cannot_load_does_not_hide_the_others():
    # Live: a pipeline storing schemaFilter.schemaLocation (a property the element lacks) made Stroom answer 500,
    # and describe_template failed outright; the agent then tried to "fix" the schema filter.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    import pytest
    from fastmcp.exceptions import ToolError
    from tools import templates
    index = {'a': {'uuid': 'a', 'name': 'Bad', 'path': 'X/Bad', 'parent_uuid': 't'},
             'b': {'uuid': 'b', 'name': 'Good', 'path': 'X/Good', 'parent_uuid': 't'}}

    async def layers(uuid):
        if uuid == 'a':
            raise ToolError('Stroom rejected the request (500): Attempt to set property "schemaLocation" on element '
                            '"schemaFilter" but property is unknown.')
        return [{'pipelineData': {'properties': {'add': [{'element': 'xsltFilter', 'name': 'xslt'}]}}}]
    stroom = SimpleNamespace(post=AsyncMock(return_value={'values': []}), pipeline_layers=layers)
    with patch.object(templates, 'gateway_from', lambda ctx: stroom), \
            patch.object(templates, '_pipeline_index', AsyncMock(return_value=index)):
        result = await templates.list_template_children(None, 't')
        bad, good = result['children']
        assert 'schemaLocation' in bad['unreadable'] and good['sets'] == ['xsltFilter.xslt']
        # The template itself unreadable: said to be Stroom's stored settings, for an administrator, not to set here.
        with patch.object(templates, '_shape', AsyncMock(side_effect=ToolError('Stroom rejected the request (500): x'))):
            with pytest.raises(ToolError, match='an administrator fixes the template in the Stroom UI'):
                await templates.describe_template_contract(None, 't')


async def test_a_dangling_xslt_is_skipped_and_named_not_a_failure():
    # Seen on live in VS Code: the explorer listed an XSLT whose document was deleted, and describe_template failed
    # with Stroom's 500, so the agent never saw the template's children and contract.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from fastmcp.exceptions import ToolError
    from tools import templates
    own = ('<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="2.0">'
           '<xsl:import href="shared-lib"/><xsl:template match="/"/></xsl:stylesheet>')
    lib = ('<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="2.0">'
           '<xsl:template name="device"/></xsl:stylesheet>')
    texts = {'own': own, 'live': lib}

    async def get_doc(doc_type, uuid):
        if uuid not in texts:
            raise ToolError(f"Stroom rejected the request (500): Document not found: {uuid}")
        return {'uuid': uuid, 'data': texts[uuid]}
    stroom = SimpleNamespace(
        get_doc=AsyncMock(side_effect=get_doc), pipeline_layers=AsyncMock(return_value=[]),
        find_documents=AsyncMock(return_value={'values': [
            {'docRef': {'type': 'XSLT', 'uuid': 'dead', 'name': 'shared-lib'}},
            {'docRef': {'type': 'XSLT', 'uuid': 'live', 'name': 'shared-lib'}}]}))
    docs = {'a': [{'doc': {'type': 'XSLT', 'uuid': 'gone'}, 'inherited_from_template': False}],
            'b': [{'doc': {'type': 'XSLT', 'uuid': 'own'}, 'inherited_from_template': False}]}
    with patch.object(templates, 'gateway_from', lambda c: stroom), \
            patch('tools.pipelines.translation_docs', lambda uuid, layers: docs[uuid]):
        shared = await templates.shared_xslt_usage(None, [{'uuid': 'a', 'name': 'A'}, {'uuid': 'b', 'name': 'B'}])
    contents = [r for r in shared if 'document_contents' in r]
    assert contents and contents[0]['document_contents']['uuid'] == 'live'
    assert sorted(r['unreadable_xslt'] for r in shared if 'unreadable_xslt' in r) == ['dead', 'gone']
