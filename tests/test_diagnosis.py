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


async def test_a_fix_is_held_back_only_by_errors_it_brings_not_those_already_there():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from config import Settings
    from tools import diagnosis
    settings = Settings(_env_file=None, stroom_url='https://stroom.example', dev_no_auth=True, stroom_api_key='k')
    layers = [{'sourcePipeline': {'type': 'Pipeline', 'uuid': 'p', 'name': 'p'}, 'pipelineData': {
        'elements': {'add': [{'id': 'translationFilter', 'type': 'XSLTFilter'}]},
        'properties': {'add': [{'element': 'translationFilter', 'name': 'xslt',
                                'value': {'entity': {'type': 'XSLT', 'uuid': 'x', 'name': 'X'}}}]}}}]
    stroom = SimpleNamespace(settings=settings, pipeline_layers=AsyncMock(return_value=layers),
                             get_doc=AsyncMock(return_value={'data': '<old/>'}), get=AsyncMock(return_value={'name': 'P'}))
    schema = {'class': 'blocking', 'element': 'schemaFilter', 'severity': 'ERROR', 'reason': 'Output fails schema validation',
              'count': 1, 'examples': [{'message': "The value 'n/a' of element 'IPAddress' is not valid."}]}
    fatal = {'class': 'blocking', 'element': 'translationFilter', 'severity': 'FATAL', 'reason': 'Fatal error',
             'count': 3, 'examples': [{'message': 'XPath error at line 12'}]}
    changed = {'records_compared': 6, 'records_changed': 2, 'fields_changed': [{'path': 'Event/EventDetail/X'}]}

    async def run(draft_groups, saved_groups):
        steps = AsyncMock(side_effect=[{'verdict': 'blocking' if draft_groups else 'clean', 'groups': draft_groups},
                                       {'verdict': 'blocking' if saved_groups else 'clean', 'groups': saved_groups}])
        with patch.object(diagnosis, 'gateway_from', return_value=stroom), \
                patch.object(diagnosis, 'compare_outputs', AsyncMock(return_value=changed)), \
                patch.object(diagnosis, 'step_sample', steps):
            return await diagnosis.summarise_fix(None, 'p', 'translationFilter', '<new/>', [1], ['Event/EventDetail/X'])
    unrelated = await run([schema], [schema])            # there before too: not the fix's doing
    assert unrelated['ready'] and unrelated['errors_before_too'][0]['element'] == 'schemaFilter'
    resolved = await run([], [schema])                    # the fix clears it
    assert resolved['ready'] and resolved['errors_resolved'][0]['example'].startswith("The value 'n/a'")
    broken = await run([schema, fatal], [schema])         # the fix brings a new one
    assert not broken['ready'] and 'blocking errors the saved code does not' in broken['problems'][0]
    assert 'XPath error' in broken['problems'][0]
