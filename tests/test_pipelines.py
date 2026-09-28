from tools.pipelines import chain_order, merge_layers

TEMPLATE = {'type': 'Pipeline', 'uuid': 't-1', 'name': 'Event Data (JSON)'}
CHILD = {'type': 'Pipeline', 'uuid': 'c-1', 'name': 'Keycloak-V1.2-Events'}
MAXMIND = {'type': 'Pipeline', 'uuid': 'r-1', 'name': 'Reference Loader'}


def el(eid, etype='XSLTFilter'):
    return {'id': eid, 'type': etype}


def link(a, b):
    return {'from': a, 'to': b}


def prop(element, name, **value):
    return {'element': element, 'name': name, 'value': value}


# Shaped like the live Keycloak-V1.2-Events pipeline: the child removes the template's empty
# decoration step, re-links translation straight to schema validation and adds a GeoIP lookup.
LAYERS = [
    {'sourcePipeline': TEMPLATE, 'pipelineData': {
        'elements': {'add': [el('jsonParser', 'JSONParser'), el('translationFilter'), el('decorationFilter'),
                             el('schemaFilter', 'SchemaFilter'), el('streamAppender', 'StreamAppender')]},
        'links': {'add': [link('jsonParser', 'translationFilter'), link('translationFilter', 'decorationFilter'),
                          link('decorationFilter', 'schemaFilter'), link('schemaFilter', 'streamAppender')]},
        'properties': {'add': [prop('schemaFilter', 'schemaGroup', string='EVENTS'),
                               prop('streamAppender', 'streamType', string='Events')]},
    }},
    {'sourcePipeline': CHILD, 'pipelineData': {
        'elements': {'remove': [el('decorationFilter')]},
        'links': {'add': [link('translationFilter', 'schemaFilter')],
                  'remove': [link('translationFilter', 'decorationFilter')]},
        'properties': {'add': [prop('translationFilter', 'xslt', entity={'type': 'XSLT', 'uuid': 'x-1',
                                                                         'name': 'Keycloak-V1.2-Events'}),
                               prop('jsonParser', 'addRootObject', boolean=False)]},
        'pipelineReferences': {'add': [{'element': 'translationFilter', 'name': 'pipelineReference',
                                        'pipeline': MAXMIND, 'feed': {'type': 'Feed', 'uuid': 'f-1',
                                                                      'name': 'Maxmind-GeoLite2-City-IPv4-CSV'},
                                        'streamType': 'Reference'}]},
    }},
]


def test_child_removal_drops_the_element_and_its_dangling_link():
    merged = merge_layers(LAYERS)
    assert 'decorationFilter' not in {e['id'] for e in merged['elements']}
    assert link('decorationFilter', 'schemaFilter') not in merged['links']
    assert link('translationFilter', 'schemaFilter') in merged['links']
    assert chain_order(merged['elements'], merged['links']) == [
        'jsonParser', 'translationFilter', 'schemaFilter', 'streamAppender']


def test_properties_record_value_and_the_layer_that_set_them():
    props = {(p['element'], p['name']): p for p in merge_layers(LAYERS)['properties']}
    assert props[('schemaFilter', 'schemaGroup')] == {'element': 'schemaFilter', 'name': 'schemaGroup',
                                                      'value': 'EVENTS', 'from': TEMPLATE}
    assert props[('translationFilter', 'xslt')]['value'] == {'type': 'XSLT', 'uuid': 'x-1',
                                                             'name': 'Keycloak-V1.2-Events'}
    assert props[('jsonParser', 'addRootObject')]['value'] is False


def test_child_property_overrides_template_value():
    layers = LAYERS + [{'sourcePipeline': CHILD, 'pipelineData': {
        'properties': {'add': [prop('schemaFilter', 'schemaGroup', string='CUSTOM')]}}}]
    props = {(p['element'], p['name']): p['value'] for p in merge_layers(layers)['properties']}
    assert props[('schemaFilter', 'schemaGroup')] == 'CUSTOM'


def test_references_and_own_removals_are_reported():
    merged = merge_layers(LAYERS)
    assert merged['references'] == [{'element': 'translationFilter', 'name': 'pipelineReference',
                                     'pipeline': MAXMIND, 'stream_type': 'Reference',
                                     'feed': {'type': 'Feed', 'uuid': 'f-1',
                                              'name': 'Maxmind-GeoLite2-City-IPv4-CSV'}}]
    assert merged['removed_by_this_pipeline'] == {
        'elements': [el('decorationFilter')],
        'links': [link('translationFilter', 'decorationFilter')],
    }
