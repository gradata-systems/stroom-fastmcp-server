from tools.diagnosis import _changed_outside, _nth_event, translation_docs
from tools.stepping import record_key

OUTPUT = """<?xml version="1.1"?><Events xmlns="event-logging:3"><Event><EventSource><User><Id>a</Id></User></EventSource></Event>
<Event><EventSource><User><Id>b</Id></User></EventSource></Event></Events>"""


def test_nth_event_keeps_the_wrapper_and_namespace():
    second = _nth_event(OUTPUT, 2)
    assert second.startswith('<Events xmlns="event-logging:3">') and '<Id>b</Id>' in second and '<Id>a</Id>' not in second
    assert _nth_event(OUTPUT, 3) is None and _nth_event('not xml', 1) is None


def test_expected_paths_match_the_field_or_anything_below_it():
    paths = ['Event/EventSource/User/Id', 'Event/EventSource/Client/IPAddress', 'Event/EventDetail/Description']
    assert _changed_outside(paths, ['Event/EventSource/User', 'Event/EventSource/Client/IPAddress']) == \
        ['Event/EventDetail/Description']
    assert _changed_outside(['Event/EventSource/UserName'], ['Event/EventSource/User']) == ['Event/EventSource/UserName']


def test_translation_docs_mark_code_inherited_from_the_template():
    template = {'type': 'Pipeline', 'uuid': 't', 'name': 'Event Data (Text)'}
    own = {'type': 'Pipeline', 'uuid': 'p', 'name': 'Acme'}
    layers = [
        {'sourcePipeline': template, 'pipelineData': {
            'elements': {'add': [{'id': 'dsParser', 'type': 'DSParser'}, {'id': 'decorationFilter', 'type': 'XSLTFilter'},
                                 {'id': 'translationFilter', 'type': 'XSLTFilter'}]},
            'properties': {'add': [{'element': 'decorationFilter', 'name': 'xslt',
                                    'value': {'entity': {'type': 'XSLT', 'uuid': 'd', 'name': 'Decoration'}}}]}}},
        {'sourcePipeline': own, 'pipelineData': {'properties': {'add': [
            {'element': 'dsParser', 'name': 'textConverter', 'value': {'entity': {'type': 'TextConverter', 'uuid': 'tc', 'name': 'Acme'}}},
            {'element': 'translationFilter', 'name': 'xslt', 'value': {'entity': {'type': 'XSLT', 'uuid': 'x', 'name': 'Acme'}}}]}}},
    ]
    docs = {d['element']: d for d in translation_docs('p', layers)}
    assert docs['decorationFilter']['inherited_from_template'] and docs['decorationFilter']['set_by'] == 'Event Data (Text)'
    assert not docs['translationFilter']['inherited_from_template'] and docs['dsParser']['doc']['type'] == 'TextConverter'


def test_record_keys_name_the_part_past_the_first():
    assert record_key(38, {'partIndex': 0, 'recordIndex': 4}) == '38:4'
    assert record_key(38, {'partIndex': 1, 'recordIndex': 0}) == '38:1:0'
