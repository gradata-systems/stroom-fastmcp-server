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
    assert result['template']['index_patterns'] == ['ecs-acme-v2*'] and result['template_name'] == 'ecs-acme-v2'
    assert result['dev_tools'].startswith('PUT _index_template/ecs-acme-v2\n{')
    assert result['pipeline_link'] == 'https://stroom.example/?action=open-doc&docType=Pipeline&docUuid=p1'
    # the plan leaves some written fields unmapped (dynamic false): noted, not blocking
    assert result['self_check']['compatible']


async def test_check_reports_changes_and_unchecked_component_templates():
    body = template({**GOOD, 'source': {'properties': {'ip': {'type': 'ip'}}}}, composed_of=['ecs-base'])
    with patch('tools.indexing._destination', AsyncMock(return_value={'index name': 'ecs-acme-v2', 'cluster': 'ES_DEV'})), \
            patch('tools.indexing._documents', AsyncMock(return_value=DOCS)):
        result = await indexing.check_index_template(ctx(), 'p1', f"PUT _index_template/ecs-acme\n{json.dumps(body)}", [8])
        with pytest.raises(ToolError, match='not valid JSON'):
            await indexing.check_index_template(ctx(), 'p1', '{', [8])
    assert not result['compatible'] and result['template_name'] == 'ecs-acme'
    assert result['notes'][0].startswith("composed_of ['ecs-base'] not checked")
    assert 'Show the user pipeline_changes' in result['hint']
