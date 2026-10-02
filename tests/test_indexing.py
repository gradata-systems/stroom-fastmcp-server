from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError
from saxonche import PySaxonProcessor

from tools import indexing
from utils.fieldplan import FieldPlan, PlannedField
from utils.templatecheck import read_mapping

EVENTS = """<Events xmlns="event-logging:3"><Event StreamId="7" EventId="2">
<EventTime><TimeCreated>2026-09-28T10:00:00.000Z</TimeCreated></EventTime>
<EventSource><Device><HostName>ws01</HostName></Device><User><Id>alice</Id></User></EventSource>
<EventDetail><Authenticate><Outcome><Success>false</Success></Outcome></Authenticate></EventDetail>
</Event></Events>"""

FIELDS = [PlannedField(name='StreamId', type='id', source='@StreamId'),
          PlannedField(name='EventId', type='id', source='@EventId'),
          PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
          PlannedField(name='user.name', type='keyword', source='EventSource/User/Id'),
          PlannedField(name='event.outcome', type='boolean', source='EventDetail/Authenticate/Outcome/Success'),
          PlannedField(name='source.ip', type='ip', source='EventSource/Client/IPAddress')]


def run(xslt: str) -> str:
    with PySaxonProcessor(license=False) as proc:
        exe = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=xslt)
        return exe.transform_to_string(xdm_node=proc.parse_xml(xml_text=EVENTS))


def test_elastic_plan_renders_a_nested_template_and_a_json_xml_xslt():
    plan = FieldPlan(backend='elasticsearch', index_name='stroom-acme-v1', time_field='@timestamp', fields=FIELDS)
    assert plan.required() == []
    template = plan.elastic_template('stroom-acme-v1')
    assert template['body']['index_patterns'] == ['stroom-acme-v1*']
    fields = read_mapping(template['body']['template']['mappings']).fields
    assert {path: f['type'] for path, f in fields.items() if f['type'] != 'object'} == {
        'StreamId': 'long', 'EventId': 'long', '@timestamp': 'date', 'user.name': 'keyword',
        'event.outcome': 'boolean', 'source.ip': 'ip'}
    output = run(plan.xslt())
    assert '<number key="StreamId">7</number>' in output
    assert '<string key="user.name">alice</string>' in output
    assert '<boolean key="event.outcome">false</boolean>' in output
    assert 'source.ip' not in output  # absent in the event, so left out rather than written empty


def test_lucene_plan_maps_keywords_to_text_with_keyword_analyzer():
    plan = FieldPlan(backend='lucene', index_name='ACME-INDEX', time_field='EventTime', fields=[
        PlannedField(name='StreamId', type='id', source='@StreamId'), PlannedField(name='EventId', type='id', source='@EventId'),
        PlannedField(name='EventTime', type='date', source='EventTime/TimeCreated'),
        PlannedField(name='UserId', type='keyword', source='EventSource/User/Id')])
    fields = {f['fldName']: (f['fldType'], f['analyzerType']) for f in plan.lucene_fields()}
    assert fields['UserId'] == ('TEXT', 'KEYWORD') and fields['StreamId'][0] == 'ID'
    assert '<data name="UserId" value="alice"/>' in run(plan.xslt())


def test_plan_reports_missing_required_fields():
    plan = FieldPlan(backend='elasticsearch', index_name='x', time_field='when', fields=FIELDS[3:])
    assert set(plan.required()) == {"Missing required field 'StreamId'", "Missing required field 'EventId'",
                                    "The time field 'when' is not in the plan",
                                    "Elasticsearch data streams need '@timestamp'"}


async def test_indexing_pipeline_for_elasticsearch_sets_index_name_and_open_cluster():
    shape = {'stage': 'indexing', 'backend': 'elasticsearch',
             'child_must_supply': [{'element': 'xsltFilter', 'type': 'XSLTFilter', 'property': 'xslt'},
                                   {'element': 'elasticIndexingFilter', 'type': 'ElasticIndexingFilter', 'property': 'indexName'},
                                   {'element': 'elasticIndexingFilter', 'type': 'ElasticIndexingFilter', 'property': 'cluster'}]}
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace()})
    with patch('tools.indexing._shape', AsyncMock(return_value=shape)), \
            patch('tools.indexing._events_available', AsyncMock()), \
            patch('tools.indexing.create_pipeline', AsyncMock(return_value={'uuid': 'p'})) as create:
        with pytest.raises(ToolError, match='cluster'):
            await indexing.create_indexing_pipeline(ctx, 'b', 'n', 't', 'x', index_name='stroom-acme-v1')
        await indexing.create_indexing_pipeline(ctx, 'b', 'n', 't', 'x', index_name='stroom-acme-v1', cluster_uuid='c')
    props = {(p.element, p.name): (p.value or p.doc_uuid) for p in create.call_args.args[3]}
    assert props == {('xsltFilter', 'xslt'): 'x', ('elasticIndexingFilter', 'indexName'): 'stroom-acme-v1',
                     ('elasticIndexingFilter', 'cluster'): 'c'}
