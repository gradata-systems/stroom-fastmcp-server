import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from config import Settings
from tools import indexing
from utils.fieldplan import FieldPlan, PlannedField
from utils.templatecheck import compare, json_xml_documents, parse_template

OUTPUT = """<?xml version="1.1"?><array xmlns="http://www.w3.org/2005/xpath-functions">
<map><number key="StreamId">7</number><number key="EventId">1</number><string key="@timestamp">2026-09-28T10:00:00.000Z</string>
<string key="user.name">alice</string><string key="source.ip">10.0.0.1</string><boolean key="event.outcome">false</boolean>
<map key="host"><string key="name">ws01</string></map></map>
<map><number key="StreamId">7</number><number key="EventId">2</number><string key="@timestamp">2026-09-28T10:05:00.000Z</string>
<string key="user.name">bob</string><string key="source.ip">unknown</string><boolean key="event.outcome">true</boolean>
<map key="host"><string key="name">ws02</string></map></map></array>"""
DOCS = json_xml_documents(OUTPUT)


def template(properties: dict, dynamic=False, patterns=('ecs-acme-v2*',), **extra) -> dict:
    return {'index_patterns': list(patterns), 'template': {'mappings': {'dynamic': dynamic, 'properties': properties}}, **extra}


GOOD = {'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}, '@timestamp': {'type': 'date'},
        'user': {'properties': {'name': {'type': 'keyword'}}}, 'source': {'properties': {'ip': {'type': 'keyword'}}},
        'event': {'properties': {'outcome': {'type': 'boolean'}}}, 'host': {'properties': {'name': {'type': 'keyword'}}}}


def test_documents_are_read_from_the_indexing_xslt_output():
    assert len(DOCS) == 2 and DOCS[0]['host'] == {'name': ('string', 'ws01')} and DOCS[0]['StreamId'] == ('number', '7')


def test_a_template_that_takes_every_field_is_compatible():
    result = compare(template(GOOD), DOCS, 'ecs-acme-v2')
    assert result['compatible'] and not result['pipeline_changes'] and result['documents_checked'] == 2


def test_the_template_must_apply_to_the_pipelines_index():
    result = compare(template(GOOD, patterns=['ecs-acme-v1*']), DOCS, 'ecs-acme-v2')
    assert not result['compatible'] and "do not match the pipeline's index 'ecs-acme-v2'" in result['blocking'][0]


def test_a_type_the_values_do_not_fit_asks_for_a_pipeline_change():
    changed = {**GOOD, 'source': {'properties': {'ip': {'type': 'ip'}}}}
    result = compare(template(changed), DOCS, 'ecs-acme-v2')
    assert not result['compatible'] and result['blocking'] == ["source.ip is 'unknown', not an IP address"]
    assert result['pipeline_changes'][0]['field'] == 'source.ip'


def test_a_renamed_field_is_flagged_as_a_rename_in_the_xslt():
    renamed = {k: v for k, v in GOOD.items() if k != 'user'} | {'user': {'properties': {'id': {'type': 'keyword'}}}}
    result = compare(template(renamed, dynamic='strict'), DOCS, 'ecs-acme-v2')
    assert not result['compatible']
    assert {'field': 'user.name', 'problem': "not in the template, which has 'user.id' instead",
            'change': "rename 'user.name' to 'user.id' in the indexing XSLT"} in result['pipeline_changes']


@pytest.mark.parametrize('dynamic, compatible, where', [('strict', False, 'blocking'), (False, True, 'notes'), (True, True, 'notes')])
def test_fields_missing_from_the_template_follow_its_dynamic_setting(dynamic, compatible, where):
    result = compare(template({k: v for k, v in GOOD.items() if k != 'event'}, dynamic=dynamic), DOCS, 'ecs-acme-v2')
    assert result['compatible'] is compatible and any('event.outcome' in m for m in result[where])


def test_a_field_mapped_as_a_value_cannot_also_hold_children():
    result = compare(template({**GOOD, 'host': {'type': 'keyword'}}), DOCS, 'ecs-acme-v2')
    assert not result['compatible'] and any("maps 'host' as keyword" in b for b in result['blocking'])


def test_fields_the_template_adds_are_flagged_as_missing_from_the_pipeline():
    result = compare(template({**GOOD, 'event': {'properties': {'outcome': {'type': 'boolean'}, 'action': {'type': 'keyword'}}}}),
                     DOCS, 'ecs-acme-v2')
    assert result['compatible']
    assert result['pipeline_changes'] == [{'field': 'event.action', 'problem': 'in the template but the pipeline never writes it',
                                           'change': "add 'event.action' to the indexing XSLT (from the right event-logging "
                                                     "path), or drop it from the template"}]


def test_data_streams_need_timestamp_and_date_formats_are_checked():
    no_time = [{k: v for k, v in d.items() if k != '@timestamp'} for d in DOCS]
    assert not compare(template(GOOD, data_stream={}), no_time, 'ecs-acme-v2')['compatible']
    epoch_only = {**GOOD, '@timestamp': {'type': 'date', 'format': 'epoch_millis'}}
    assert any('date format epoch_millis rejects' in b for b in compare(template(epoch_only), DOCS, 'ecs-acme-v2')['blocking'])


def test_templates_are_read_as_users_paste_them():
    body = template(GOOD)
    assert parse_template(f"PUT _index_template/ecs-acme\n{json.dumps(body)}") == ('ecs-acme', body)
    assert parse_template(json.dumps({'index_templates': [{'name': 'x', 'index_template': body}]})) == ('x', body)
    with pytest.raises(ValueError, match='not valid JSON'):
        parse_template('PUT _index_template/x\n{oops')


SETTINGS = Settings(_env_file=None, stroom_url='https://stroom.example', dev_no_auth=True, stroom_api_key='k')


def ctx():
    return SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SETTINGS)})


async def test_proposed_template_targets_the_pipelines_index_and_links_to_it():
    plan = FieldPlan(backend='elasticsearch', index_name='whatever', time_field='@timestamp', fields=[
        PlannedField(name='StreamId', type='id', source='@StreamId'), PlannedField(name='EventId', type='id', source='@EventId'),
        PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
        PlannedField(name='user.name', type='keyword', source='EventSource/User/Id')])
    with patch('tools.indexing._destination', AsyncMock(return_value={'index name': 'ecs-acme-v2', 'cluster': 'ES_DEV'})), \
            patch('tools.indexing._documents', AsyncMock(return_value=DOCS)):
        result = await indexing.propose_index_template(ctx(), 'p1', plan, [8])
        rendered = plan.model_copy(update={'index_name': 'ecs-acme-v2'}).elastic_template('ecs-acme-v2', 200)['body']
        check = indexing.compare(rendered, DOCS, 'ecs-acme-v2')
    # Without the user's example nothing is built to commit: they are asked for it first.
    assert result['agreed'] is False and result['needs'] == 'example_template' and 'dev_tools' not in result
    assert result['index'] == 'ecs-acme-v2' and result['cluster'] == 'ES_DEV'
    assert result['pipeline_link'] == 'https://stroom.example/?action=open-doc&docType=Pipeline&docUuid=p1'
    assert rendered['index_patterns'] == ['ecs-acme-v2*']
    # the plan leaves some written fields unmapped (dynamic false): noted, not blocking
    assert check['compatible']


async def test_check_reports_changes_and_unchecked_component_templates():
    body = template({**GOOD, 'source': {'properties': {'ip': {'type': 'ip'}}}}, composed_of=['ecs-base'])
    with patch('tools.indexing._destination', AsyncMock(return_value={'index name': 'ecs-acme-v2', 'cluster': 'ES_DEV'})), \
            patch('tools.indexing._documents', AsyncMock(return_value=DOCS)):
        result = await indexing.check_index_template(ctx(), 'p1', f"PUT _index_template/ecs-acme\n{json.dumps(body)}", [8])
        with pytest.raises(ToolError, match='not valid JSON'):
            await indexing.check_index_template(ctx(), 'p1', '{', [8])
    assert not result['compatible'] and result['template_name'] == 'ecs-acme'
    assert result['notes'][0].startswith("composed_of ['ecs-base'] not given, so not checked")
    assert 'Show the user pipeline_changes' in result['hint']


EXAMPLE = '''PUT _index_template/ecs-keyfob-v1
{"index_patterns": ["ecs-keyfob-v1*"], "priority": 300, "composed_of": ["ecs-base", "ecs-source"],
 "template": {"settings": {"index": {"lifecycle": {"name": "logs-90d"}, "number_of_shards": 2}},
              "aliases": {"keyfob": {}},
              "mappings": {"dynamic": "strict", "date_detection": false,
                           "properties": {"user": {"properties": {"name": {"type": "keyword", "ignore_above": 256}}},
                                          "keyfob": {"properties": {"id": {"type": "keyword"}}}}}}}'''
COMPONENTS = ['''PUT _component_template/ecs-base
{"template": {"mappings": {"properties": {"@timestamp": {"type": "date"}, "host": {"properties": {"name": {"type": "keyword"}}}}}}}''',
              json.dumps({'component_templates': [{'name': 'ecs-source', 'component_template': {
                  'template': {'mappings': {'properties': {'source': {'properties': {'ip': {'type': 'ip'}}}}}}}}]})]


def test_the_final_template_follows_the_users_example_and_its_components():
    from utils.templatecheck import compose, from_example, parse_component_templates
    plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v2', time_field='@timestamp',
                     fields=[PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
                             PlannedField(name='user.name', type='text', source='EventSource/User/Id'),
                             PlannedField(name='source.ip', type='keyword', source='EventSource/Client/IPAddress'),
                             PlannedField(name='event.outcome', type='boolean', source='x')])
    planned = plan.elastic_template('ecs-acme-v2')['body']
    _, example = parse_template(EXAMPLE)
    components = parse_component_templates(COMPONENTS)
    assert sorted(components) == ['ecs-base', 'ecs-source']
    body, notes = from_example(planned, example, components)
    # The new index's pattern; the example's priority, composed_of, settings and mapping parameters; no aliases.
    assert body['index_patterns'] == ['ecs-acme-v2*'] and body['priority'] == 300
    assert body['composed_of'] == ['ecs-base', 'ecs-source'] and body['template']['settings']['index']['number_of_shards'] == 2
    mappings = body['template']['mappings']
    assert mappings['dynamic'] == 'strict' and mappings['date_detection'] is False and 'aliases' not in body['template']
    # Fields the components map are left to them; the example's own types are kept; the rest come from the plan.
    assert '@timestamp' not in mappings['properties'] and 'source' not in mappings['properties']
    assert mappings['properties']['user']['properties']['name'] == {'type': 'keyword', 'ignore_above': 256}
    assert mappings['properties']['event']['properties']['outcome'] == {'type': 'boolean'}
    assert any(n.startswith('left to the component templates') and "'source.ip'" in n for n in notes)
    assert any('aliases are not copied' in n for n in notes)
    # Composed as Elasticsearch would, it maps every field the plan writes.
    composed, missing = compose(body, components)
    assert not missing and composed['template']['mappings']['properties']['source']['properties']['ip'] == {'type': 'ip'}
    # Without the components, the note asks for them.
    _, notes = from_example(planned, example, {})
    assert any(n.startswith("composed_of ['ecs-base', 'ecs-source'] not given") for n in notes)


def test_an_index_mapping_serves_as_the_example_and_is_not_checked_for_patterns():
    name, body = parse_template(json.dumps({'ecs-keyfob-v1-000001': {'mappings': {'properties': {'a': {'type': 'keyword'}}}}}))
    assert name == 'ecs-keyfob-v1-000001' and body == {'template': {'mappings': {'properties': {'a': {'type': 'keyword'}}}}}
    assert compare(body, [], 'ecs-acme-v2')['notes'][0].startswith('a mapping, not an index template')
    with pytest.raises(ValueError, match='with its name'):
        from utils.templatecheck import parse_component_templates
        parse_component_templates(['{"template": {"mappings": {}}}'])


def test_new_fields_follow_the_examples_type_styles():
    from utils.templatecheck import from_example
    planned = {'index_patterns': ['ecs-door-v1*'], 'template': {'mappings': {'properties': {
        'user': {'properties': {'name': {'type': 'keyword'}}},
        'event': {'properties': {'action': {'type': 'keyword'}, 'created': {'type': 'date'}}},
        'message': {'type': 'text'}}}}}
    example = {'index_patterns': ['ecs-keyfob-v1*'], 'template': {'mappings': {'properties': {
        'host': {'properties': {'name': {'type': 'keyword', 'ignore_above': 1024}}},
        'source': {'properties': {'ip': {'type': 'ip'}}},
        'event': {'properties': {'kind': {'type': 'keyword', 'ignore_above': 1024},
                                 'ingested': {'type': 'date', 'format': 'strict_date_optional_time'}}},
        'error': {'properties': {'message': {'type': 'text', 'fields': {'keyword': {'type': 'keyword'}}}}}}}}}
    body, notes = from_example(planned, example, {})
    props = body['template']['mappings']['properties']
    assert props['user']['properties']['name'] == {'type': 'keyword', 'ignore_above': 1024}
    assert props['event']['properties']['created'] == {'type': 'date', 'format': 'strict_date_optional_time'}
    assert props['message'] == {'type': 'text', 'fields': {'keyword': {'type': 'keyword'}}}
    assert any("mapped in the example's style for their type" in n and 'user.name' in n for n in notes)
    # Objects the example leaves undeclared stay undeclared; its fields this source lacks are listed, not copied.
    assert 'type' not in props['user'] and 'host' not in props
    assert any(n.startswith('in the example but not written by this pipeline') and 'host.name' in n for n in notes)


def test_naming_styles_are_judged_per_dotted_part():
    from utils.templatecheck import example_dotted, example_naming, naming_style
    assert [naming_style(n) for n in ('User.Id', 'TypeId', 'IPAddress', 'user.name', 'source.ip', 'src_ip', 'message',
                                      'lastModified', 'userName', 'user.createdOn', 'User.created_on')] == \
        ['pascal', 'pascal', 'pascal', 'ecs', 'ecs', 'ecs', 'ecs', 'camel', 'camel', 'camel', 'other']
    assert [naming_style(n) for n in ('@timestamp', '_id', 'StreamId', 'EventId')] == [None] * 4
    assert example_naming(['User.Id', 'TypeId', 'Device.HostName', 'message']) == 'pascal'
    # One-word lower-case names fit camelCase too: the camelCase names decide it.
    assert example_naming(['lastModified', 'userName', 'user.createdOn', 'message', 'status']) == 'camel'
    assert example_naming(['user.name', 'source.ip', 'message']) == 'ecs'
    assert example_naming(['UserId', 'user_name']) is None
    assert example_dotted(['User.Id', 'Device.HostName', 'TypeId']) and not example_dotted(['UserId', 'TypeId', 'User.Id'])


# A door-access source, planned with the ECS convention, while the user's sibling index (key fobs) follows
# Stroom-style PascalCase with explicit objects. The two don't hold the same fields.
DOOR_PLAN = [
    {'name': 'StreamId', 'type': 'id', 'source': '@StreamId'}, {'name': 'EventId', 'type': 'id', 'source': '@EventId'},
    {'name': '@timestamp', 'type': 'date', 'source': 'EventTime/TimeCreated'},
    {'name': 'user.name', 'type': 'keyword', 'source': 'EventSource/User/Id'},
    {'name': 'event.code', 'type': 'keyword', 'source': 'EventDetail/TypeId'},
    {'name': 'host.name', 'type': 'keyword', 'source': 'EventSource/Device/HostName'},
    {'name': 'source.ip', 'type': 'ip', 'source': 'EventSource/Client/IPAddress'},
    {'name': 'message', 'type': 'text', 'source': 'EventDetail/Description'}]
DOOR_POPULATED = ['EventTime/TimeCreated', 'EventSource/User/Id', 'EventDetail/TypeId', 'EventSource/Device/HostName',
                  'EventSource/Client/IPAddress', 'EventDetail/Description', 'EventDetail/Authenticate/Outcome/Success',
                  'EventSource/Device/Location/Room']
CONVENTION_NAMES = {'EventSource/User/Id': {'user.name', 'UserId'}, 'EventDetail/TypeId': {'event.code', 'TypeId'},
                    'EventSource/Device/HostName': {'host.name', 'HostName'},
                    'EventSource/Device/IPAddress': {'host.ip', 'IPAddress'},
                    'EventSource/Client/IPAddress': {'source.ip'}, 'EventDetail/Description': {'message', 'Description'},
                    'EventTime/TimeCreated': {'@timestamp', 'EventTime'}}
KEYFOB = {'index_patterns': ['stroom-keyfob-v1*'], 'priority': 300, 'composed_of': ['stroom-base'],
          'template': {'mappings': {'dynamic': 'strict', 'properties': {
              'TypeId': {'type': 'keyword', 'ignore_above': 512},
              'User': {'type': 'object', 'properties': {'Id': {'type': 'keyword', 'ignore_above': 512},
                                                        'Name': {'type': 'keyword', 'ignore_above': 512}}},
              'Device': {'type': 'object', 'properties': {'HostName': {'type': 'keyword', 'ignore_above': 512},
                                                          'IPAddress': {'type': 'ip'}}},
              'Keyfob': {'type': 'object', 'properties': {'Serial': {'type': 'keyword', 'ignore_above': 512}}},
              'Outcome': {'type': 'object', 'dynamic': False, 'properties': {'Success': {'type': 'boolean'}}}}}}}
STROOM_BASE = {'stroom-base': {'template': {'mappings': {'properties': {
    'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}, 'EventTime': {'type': 'date'}, '@timestamp': {'type': 'date'}}}}}}


def _plan_template(fields):
    from utils.fieldplan import FieldPlan, PlannedField
    plan = FieldPlan(backend='elasticsearch', index_name='stroom-door-v1', time_field='@timestamp',
                     fields=[PlannedField(**f) for f in fields])
    return plan.elastic_template('stroom-door-v1')['body']


def test_a_pascal_case_example_with_objects_names_and_shapes_a_source_that_differs_from_it():
    from utils.templatecheck import compose, from_example, names_from_example, read_mapping_fields
    example, _ = compose(KEYFOB, STROOM_BASE)
    fields, notes = names_from_example(DOOR_PLAN, read_mapping_fields(example), CONVENTION_NAMES, DOOR_POPULATED)
    names = {f['source']: f['name'] for f in fields}
    # The key conventions: the user is User.Id and the event type TypeId, as in the example.
    assert names['EventSource/User/Id'] == 'User.Id' and names['EventDetail/TypeId'] == 'TypeId'
    assert names['EventSource/Device/HostName'] == 'Device.HostName'
    # Not in the example: named in its style. In the sample and in the example: added, typed as it types it.
    assert names['EventSource/Client/IPAddress'] == 'Client.IPAddress' and names['EventDetail/Description'] == 'Description'
    assert {'name': 'Outcome.Success', 'type': 'boolean', 'source': 'EventDetail/Authenticate/Outcome/Success'} in fields
    # Stroom's ids and the time field keep their names; a populated path the example has no field for is not added.
    assert [names['@StreamId'], names['@EventId'], names['EventTime/TimeCreated']] == ['StreamId', 'EventId', '@timestamp']
    assert 'EventSource/Device/Location/Room' not in names
    assert any(n.startswith('named as the example names them') and 'user.name -> User.Id' in n for n in notes)
    assert any(n.startswith('not in the example, named in its style (PascalCase') and 'source.ip -> Client.IPAddress' in n
               for n in notes)
    assert any(n.startswith('in the example but not in this source') and 'Keyfob.Serial' in n and 'User.Name' in n
               for n in notes)

    body, built = from_example(_plan_template(fields), KEYFOB, STROOM_BASE)
    props = body['template']['mappings']['properties']
    assert props['User'] == {'type': 'object', 'properties': {'Id': {'type': 'keyword', 'ignore_above': 512}}}
    assert props['TypeId'] == {'type': 'keyword', 'ignore_above': 512}
    assert props['Outcome'] == {'type': 'object', 'dynamic': False, 'properties': {'Success': {'type': 'boolean'}}}
    # A new object, declared as the example declares its objects; a new keyword in the example's keyword style.
    assert props['Client'] == {'type': 'object', 'properties': {'IPAddress': {'type': 'ip'}}}
    assert props['Description'] == {'type': 'text'}
    assert 'Keyfob' not in props and 'StreamId' not in props and body['composed_of'] == ['stroom-base']
    assert body['template']['mappings']['dynamic'] == 'strict'
    assert not any(n.startswith('named unlike') for n in built)


def test_an_ecs_example_renames_a_flat_plan_with_known_ecs_names():
    from utils.templatecheck import names_from_example, read_mapping_fields
    flat = [{'name': 'StreamId', 'type': 'id', 'source': '@StreamId'},
            {'name': 'UserId', 'type': 'keyword', 'source': 'EventSource/User/Id'},
            {'name': 'TypeId', 'type': 'keyword', 'source': 'EventDetail/TypeId'},
            {'name': 'IPAddress', 'type': 'ip', 'source': 'EventSource/Device/IPAddress'},
            {'name': 'DoorName', 'type': 'keyword', 'source': 'EventDetail/Authenticate/Door/Name'}]
    ecs = {'template': {'mappings': {'properties': {
        'user': {'properties': {'name': {'type': 'keyword'}}}, 'event': {'properties': {'code': {'type': 'keyword'}}},
        'host': {'properties': {'name': {'type': 'keyword'}}}, 'message': {'type': 'text'}}}}}
    fields, notes = names_from_example(flat, read_mapping_fields(ecs), CONVENTION_NAMES, [])
    assert [f['name'] for f in fields] == ['StreamId', 'user.name', 'event.code', 'host.ip', 'door.name']
    assert any('IPAddress -> host.ip' in n and 'DoorName -> door.name' in n for n in notes)


def test_a_camel_case_example_names_the_new_source_in_camel_case():
    from utils.templatecheck import from_example, names_from_example, read_mapping_fields
    camel = {'index_patterns': ['app-audit-v3*'], 'template': {'mappings': {'properties': {
        'userId': {'type': 'keyword'}, 'userName': {'type': 'keyword'}, 'typeId': {'type': 'keyword'},
        'hostName': {'type': 'keyword'}, 'lastModified': {'type': 'date', 'format': 'epoch_millis'},
        'user': {'type': 'object', 'properties': {'createdOn': {'type': 'date', 'format': 'epoch_millis'}}},
        'message': {'type': 'text'}}}}}
    fields, notes = names_from_example(DOOR_PLAN, read_mapping_fields(camel), CONVENTION_NAMES, DOOR_POPULATED)
    names = {f['source']: f['name'] for f in fields}
    assert names['EventSource/User/Id'] == 'userId' and names['EventDetail/TypeId'] == 'typeId'
    assert names['EventSource/Device/HostName'] == 'hostName'
    assert names['EventSource/Client/IPAddress'] == 'clientIpAddress' and names['EventDetail/Description'] == 'message'
    assert any('lastModified' in n and 'user.createdOn' in n and 'userName' in n for n in notes
               if n.startswith('in the example but not in this source'))
    body, _ = from_example(_plan_template(fields), camel, {})
    props = body['template']['mappings']['properties']
    assert props['userId'] == {'type': 'keyword'} and props['clientIpAddress'] == {'type': 'ip'}
    # A date new to the example takes its date format; its fields this source lacks are not copied.
    assert props['@timestamp'] == {'type': 'date', 'format': 'epoch_millis'}
    assert 'lastModified' not in props and 'user' not in props


async def test_the_user_confirms_the_template_or_their_correction_and_it_is_kept_with_the_pipeline():
    from utils.consent import ConsentStore
    from utils.mappingstore import read_agreed_template
    plan = FieldPlan(backend='elasticsearch', index_name='whatever', time_field='@timestamp', fields=[
        PlannedField(name='StreamId', type='id', source='@StreamId'), PlannedField(name='EventId', type='id', source='@EventId'),
        PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
        PlannedField(name='user.name', type='keyword', source='EventSource/User/Id'),
        PlannedField(name='event.outcome', type='keyword', source='EventDetail/Authenticate/Outcome/Success')])
    documents = [{k: v for k, v in d.items() if k != 'source.ip'} for d in DOCS]
    saved = {'type': 'Pipeline', 'uuid': 'p1', 'name': 'Acme - Indexing', 'description': 'Indexes Acme.'}

    async def put_doc(doc):
        saved.update(doc)
        return doc
    stroom = SimpleNamespace(settings=SETTINGS, get_doc=AsyncMock(side_effect=lambda t, u: dict(saved)), put_doc=put_doc)
    context = SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': ConsentStore(False)})
    with patch('tools.indexing._destination', AsyncMock(return_value={'index name': 'ecs-acme-v2', 'cluster': 'ES_DEV'})), \
            patch('tools.indexing._documents', AsyncMock(return_value=documents)), \
            patch('tools.indexing.indexing_xslt_digest', AsyncMock(return_value='abc')), \
            patch('tools.indexing.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())):
        # Shown first: the whole request for the chat, formatted (seen in VS Code: in a form it was one unformatted
        # line, and an invalid template was agreed).
        shown = await indexing.propose_index_template(context, 'p1', plan, [8], example_template=EXAMPLE,
                                                      component_templates=COMPONENTS)
        assert shown['status'] == 'needs_review' and read_agreed_template(saved['description']) is None
        assert shown['dev_tools'].startswith('PUT _index_template/ecs-acme-v2\n{\n  ')
        assert 'json code block' in shown['hint'] and 'reviewed=true' in shown['hint']
        # Then confirmed in a short form: lines, not the JSON.
        asked = await indexing.propose_index_template(context, 'p1', plan, [8], example_template=EXAMPLE,
                                                      component_templates=COMPONENTS, reviewed=True)
        assert asked['status'] == 'needs_confirmation'
        assert asked['summary'] == ("Use Elasticsearch index template 'ecs-acme-v2' for index 'ecs-acme-v2' (cluster "
                                    "ES_DEV), as shown in the chat")
        details = asked['details']
        assert details['index template'] == 'ecs-acme-v2: the Dev Tools request shown in the chat'
        assert details['applies to'].startswith('ecs-acme-v2* (priority ') and details['fields'].split(':')[0].isdigit()
        assert '{' not in json.dumps({k: v for k, v in details.items() if k != 'notes'}).replace('{"', '').replace('"}', '')
        assert 'check_index_template with their version' in asked['hint']
        agreed = await indexing.propose_index_template(context, 'p1', plan, [8], example_template=EXAMPLE,
                                                       component_templates=COMPONENTS, reviewed=True,
                                                       confirmation_id=asked['confirmation_id'])
        assert agreed['agreed'] and 'commit to the cluster' in agreed['hint']
        kept = read_agreed_template(saved['description'])
        assert kept['name'] == 'ecs-acme-v2' and kept['index'] == 'ecs-acme-v2' and kept['xslt'] == 'abc'
        assert kept['dev_tools'] == agreed['dev_tools'] and saved['description'].startswith('Indexes Acme.')

        # The user corrects it (a higher priority): their version is confirmed and replaces the agreed one.
        corrected = {**agreed['template'], 'priority': 500}
        text = f"PUT _index_template/ecs-acme-v2\n{json.dumps(corrected)}"
        shown = await indexing.check_index_template(context, 'p1', text, [8], component_templates=COMPONENTS)
        assert shown['status'] == 'needs_review' and '"priority": 500' in shown['dev_tools']
        asked = await indexing.check_index_template(context, 'p1', text, [8], component_templates=COMPONENTS,
                                                    reviewed=True)
        assert asked['status'] == 'needs_confirmation' and '(priority 500)' in asked['details']['applies to']
        await indexing.check_index_template(context, 'p1', text, [8], component_templates=COMPONENTS, reviewed=True,
                                            confirmation_id=asked['confirmation_id'])
        assert json.loads(read_agreed_template(saved['description'])['dev_tools'].split('\n', 1)[1])['priority'] == 500
        assert saved['description'].count('agreed index template (') == 1


def test_the_users_own_fields_are_matched_by_path_before_convention_names():
    # The user's example has user.id, user.name and user.emailAddress: the id is user.id, though ECS (the plan's
    # convention) calls it user.name; the name and email, which the convention leaves out, are added from the sample.
    from utils.templatecheck import names_from_example, read_mapping_fields
    plan = [{'name': 'StreamId', 'type': 'id', 'source': '@StreamId'},
            {'name': 'user.name', 'type': 'keyword', 'source': 'EventSource/User/Id'}]
    example = {'template': {'mappings': {'properties': {'user': {'properties': {
        'id': {'type': 'keyword'}, 'name': {'type': 'keyword'}, 'emailAddress': {'type': 'keyword'}}}}}}}
    fields, notes = names_from_example(plan, read_mapping_fields(example), CONVENTION_NAMES,
                                       ['EventSource/User/Id', 'EventSource/User/Name', 'EventSource/User/EmailAddress',
                                        'EventDetail/Authenticate/User/Id'])
    assert {f['source']: f['name'] for f in fields} == {
        '@StreamId': 'StreamId', 'EventSource/User/Id': 'user.id', 'EventSource/User/Name': 'user.name',
        'EventSource/User/EmailAddress': 'user.emailAddress'}


def test_an_example_with_subobjects_false_maps_names_flat_and_allows_a_value_beside_its_dotted_names():
    # subobjects: false: the index maps user.id as a field of its own (no user object), and time beside time.min.
    from utils.templatecheck import from_example
    plan = FieldPlan(backend='elasticsearch', index_name='metrics-v1', time_field='@timestamp', subobjects=False, fields=[
        PlannedField(name='StreamId', type='id', source='@StreamId'), PlannedField(name='EventId', type='id', source='@EventId'),
        PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
        PlannedField(name='time', type='keyword', source='EventDetail/Data[@Name="time"]/@Value'),
        PlannedField(name='time.min', type='keyword', source='EventDetail/Data[@Name="min"]/@Value'),
        PlannedField(name='user.id', type='keyword', source='EventSource/User/Id')])
    assert plan.required() == []
    planned = plan.elastic_template('metrics-v1')['body']
    assert planned['template']['mappings']['subobjects'] is False
    assert set(planned['template']['mappings']['properties']) == {'StreamId', 'EventId', '@timestamp', 'time', 'time.min', 'user.id'}
    example = {'index_patterns': ['metrics-sibling*'], 'template': {'mappings': {'subobjects': False, 'properties': {
        'user.id': {'type': 'keyword', 'ignore_above': 512}, 'time': {'type': 'keyword'}}}}}
    body, _ = from_example(planned, example, {})
    props = body['template']['mappings']['properties']
    assert body['template']['mappings']['subobjects'] is False and 'user' not in props
    assert props['user.id'] == {'type': 'keyword', 'ignore_above': 512} and 'time' in props and 'time.min' in props
    nested = FieldPlan(**{**plan.model_dump(), 'subobjects': True})
    assert any("'time' is a value and also the object holding ['time.min']" in p for p in nested.required())
    # The document is nested either way; only time.min, beside its value time, is a flat key.
    xslt = plan.xslt()
    assert '<map key="user">' in xslt and 'key="user.id"' not in xslt
    assert 'key="time"' in xslt and 'key="time.min"' in xslt


def test_an_alias_in_the_example_becomes_a_field_when_the_pipeline_writes_it():
    # Seen in VS Code: the example (stroom_twitter) had User.Id as an alias of User.Name. Copied into the new
    # template, Elasticsearch refused it ("an alias must refer to an existing field"), yet both checks passed it.
    from utils.templatecheck import compare, from_example
    example = {'index_patterns': ['stroom-twitter*'], 'priority': 1, 'template': {'mappings': {'dynamic': True, 'properties': {
        'StreamId': {'type': 'long'}, '@timestamp': {'type': 'date'},
        'User.Name': {'type': 'text', 'fields': {'keyword': {'type': 'keyword'}}},
        'User.Id': {'path': 'User.Name', 'type': 'alias'}}}}}
    plan = FieldPlan(backend='elasticsearch', index_name='fortios-firewall', time_field='@timestamp', fields=[
        PlannedField(name='StreamId', type='id', source='@StreamId'), PlannedField(name='EventId', type='id', source='@EventId'),
        PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
        PlannedField(name='User.Id', type='keyword', source='EventSource/User/Id')])
    planned = plan.elastic_template('fortios-firewall', 200)['body']
    body, notes = from_example(planned, example, {})
    user = body['template']['mappings']['properties']['User']['properties']['Id']
    assert user == {'type': 'text', 'fields': {'keyword': {'type': 'keyword'}}}      # the target's type, not an alias
    assert any("aliases in the example, but written by this pipeline" in n and 'User.Id' in n for n in notes)
    docs = [{'StreamId': ('number', '1'), 'EventId': ('number', '1'), '@timestamp': ('string', '2026-10-01T00:00:00Z'),
             'User': {'Id': ('string', 'admin')}}]
    assert compare(body, docs, 'fortios-firewall')['compatible']
    # The template as it was agreed in that session: both problems are blocking now.
    agreed = {'index_patterns': ['fortios-firewall*'], 'template': {'mappings': {'properties': {
        'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}, '@timestamp': {'type': 'date'},
        'User': {'type': 'object', 'properties': {'Id': {'path': 'User.Name', 'type': 'alias'}}}}}}}
    check = compare(agreed, docs, 'fortios-firewall')
    assert not check['compatible']
    assert any("an alias of 'User.Name', which this template doesn't map" in b for b in check['blocking'])
    assert any('User.Id: written by the pipeline, but mapped as an alias' in b for b in check['blocking'])
