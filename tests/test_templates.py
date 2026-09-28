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
