import json
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
    # Structure kept: dotted names become nested objects, not flat "user.name" keys.
    assert '<map key="user"><string key="name">alice</string></map>' in output
    assert '<map key="event"><boolean key="outcome">false</boolean></map>' in output
    assert 'user.name' not in output
    # Absent in the event, so left out rather than written empty, and so is the object holding only it.
    assert 'key="source"' not in output and 'key="ip"' not in output


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
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(get_doc=AsyncMock(return_value={}))})
    with patch('tools.indexing._shape', AsyncMock(return_value=shape)), \
            patch('tools.indexing._events_available', AsyncMock()), \
            patch('tools.indexing.create_pipeline', AsyncMock(return_value={'uuid': 'p'})) as create:
        with pytest.raises(ToolError, match='cluster'):
            await indexing.create_indexing_pipeline(ctx, 'b', 'n', 't', 'x', index_name='stroom-acme-v1')
        await indexing.create_indexing_pipeline(ctx, 'b', 'n', 't', 'x', index_name='stroom-acme-v1', cluster_uuid='c')
    props = {(p.element, p.name): (p.value or p.doc_uuid) for p in create.call_args.args[3]}
    assert props == {('xsltFilter', 'xslt'): 'x', ('elasticIndexingFilter', 'indexName'): 'stroom-acme-v1',
                     ('elasticIndexingFilter', 'cluster'): 'c'}


async def test_an_indexing_pipeline_gets_the_fields_its_xslt_writes():
    # Haiku created the Lucene index without its plan: every value was dropped with only a warning ("Attempt to index
    # unknown field"). The refusal that followed told it to call set_index_fields, which is not a tool; now the plan
    # kept with the XSLT supplies the fields, and only an XSLT written by hand is refused, naming steps it can take.
    from utils.mappingstore import with_mapping
    plan = FieldPlan(backend='lucene', index_name='acme', time_field='EventTime',
                     fields=[PlannedField(name='StreamId', type='id', source='@StreamId'),
                             PlannedField(name='EventTime', type='date', source='EventTime/TimeCreated')])
    generated = {'description': with_mapping('', 'index', plan.model_dump()), 'data': plan.xslt()}
    by_hand = {'description': '', 'data': plan.xslt()}

    def stroom(fields, xslt):
        docs = {'Index': {'name': 'ACME-INDEX'}, 'XSLT': xslt}
        return SimpleNamespace(get_doc=AsyncMock(side_effect=lambda t, u: docs[t]),
                               post=AsyncMock(return_value={'values': [{'fldName': f} for f in fields]}))

    missing, kept = await indexing._missing_index_fields(stroom([], generated), 'i-1', 'x-1')
    assert missing == ['StreamId', 'EventTime'] and kept == plan
    assert (await indexing._missing_index_fields(stroom(['StreamId'], generated), 'i-1', 'x-1'))[0] == ['EventTime']
    with pytest.raises(ToolError, match=r"has no fields for \['StreamId', 'EventTime'\].*save_xslt index_plan=") as e:
        await indexing._missing_index_fields(stroom([], by_hand), 'i-1', 'x-1')
    assert 'set_index_fields' not in str(e.value)   # not a tool the agent has
    assert await indexing._missing_index_fields(stroom(['StreamId', 'EventTime', 'Extra'], by_hand), 'i-1', 'x-1') == ([], None)

    # Created (after confirmation), the pipeline's index gets the plan's fields.
    shape = {'stage': 'indexing', 'backend': 'lucene', 'child_must_supply': [
        {'element': 'xsltFilter', 'property': 'xslt', 'type': 'XSLTFilter'}, {'element': 'indexingFilter', 'property': 'index'}]}
    gateway = stroom([], generated)
    with patch.object(indexing, 'gateway_from', lambda ctx: gateway),             patch.object(indexing, '_shape', AsyncMock(return_value=shape)),             patch.object(indexing, '_events_available', AsyncMock()),             patch.object(indexing, 'create_pipeline', AsyncMock(return_value={'uuid': 'p-1'})),             patch.object(indexing, 'set_index_fields', AsyncMock(return_value={'added': ['StreamId', 'EventTime']})) as add:
        result = await indexing.create_indexing_pipeline(SimpleNamespace(), 'acme-v1', 'ACME - Indexing', 't-1', 'x-1', index_uuid='i-1')
    assert result['index_fields_added'] == ['StreamId', 'EventTime'] and add.await_args.args[1:] == ('i-1', plan)


RAW = """<array xmlns="http://www.w3.org/2013/XSL/json"><map><string key="ts">2026-10-01T09:00:00Z</string>
<map key="user"><string key="name">alice</string><array key="roles"><string>admin</string></array></map>
<number key="status">200</number><string key="token">secret</string>
<string key="message">{"action": "login", "client": {"ip": "10.1.1.1"}}</string>
<string key="note">{not json}</string></map></array>"""


def run_raw(xslt: str) -> str:
    # Plain Saxon has no stroom: functions: the stream id, record number and meta are given fixed values here.
    xslt = (xslt.replace('stroom:stream-id()', '7').replace('stroom:record-no()', '1')
            .replace("stroom:meta('Feed')", "'ACME'"))
    with PySaxonProcessor(license=False) as proc:
        exe = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=xslt)
        return exe.transform_to_string(xdm_node=proc.parse_xml(xml_text=RAW))


def test_a_discovery_plan_copies_records_as_they_are_and_maps_only_what_stroom_needs():
    from utils.fieldplan import Discovery
    plan = FieldPlan.for_discovery('stroom-discovery-acme-v1',
                                   Discovery(timestamp_field='ts', meta={'stroom.feed': 'Feed'}, drop=['token']))
    assert plan.required() == [] and [f.name for f in plan.fields] == ['StreamId', 'EventId', '@timestamp']
    body = plan.elastic_template('stroom-discovery-acme-v1')['body']
    mappings = body['template']['mappings']
    assert mappings['dynamic'] is True and mappings['date_detection'] is False
    assert mappings['dynamic_templates'] == [{'strings_as_keywords': {
        'match_mapping_type': 'string', 'mapping': {'type': 'keyword', 'ignore_above': 1024}}}]
    assert mappings['properties'] == {'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}, '@timestamp': {'type': 'date'}}
    assert body['template']['settings']['index']['mapping'] == {'total_fields': {'limit': 2000}, 'ignore_malformed': True}
    output = run_raw(plan.xslt())
    assert '<number key="StreamId">7</number>' in output and '<number key="EventId">1</number>' in output
    assert '<string key="@timestamp">2026-10-01T09:00:00Z</string>' in output
    assert '<string key="stroom.feed">ACME</string>' in output
    assert '<map key="user"><string key="name">alice</string><array key="roles"><string>admin</string></array></map>' in output
    assert '<number key="status">200</number>' in output and 'secret' not in output
    # JSON held in a string: kept, and parsed beside it; text that only looks like JSON is left alone.
    assert '<map key="message_json"><string key="action">login</string><map key="client"><string key="ip">10.1.1.1</string>' in output
    assert '<string key="note">{not json}</string>' in output and 'note_json' not in output


def test_a_discovery_timestamp_can_be_nested_and_formatted_and_unpacking_turned_off():
    from utils.fieldplan import Discovery
    plan = FieldPlan.for_discovery('d', Discovery(timestamp_field='event.created', timestamp_format='dd/MM/yyyy HH:mm:ss',
                                                  unpack_json=False))
    xslt = plan.xslt()
    assert "stroom:format-date(string(*[@key='event']/*[@key='created']), 'dd/MM/yyyy HH:mm:ss')" in xslt
    assert 'json-to-xml' not in xslt


async def test_a_discovery_draft_reads_nothing_and_is_elasticsearch_only():
    from utils.fieldplan import Discovery
    stroom = SimpleNamespace()   # any read would fail: nothing is surveyed
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', 'stroom-discovery-acme-v1',
                                               discovery=Discovery(timestamp_field='ts'))
    assert draft['plan']['discovery']['timestamp_field'] == 'ts' and 'json-to-xml' in draft['xslt']
    with pytest.raises(ToolError, match='A discovery index is Elasticsearch'):
        await indexing.draft_index_mapping(ctx, 'lucene', 'x', discovery=Discovery(timestamp_field='ts'))
    with pytest.raises(ToolError, match='Give events_stream_ids'):
        await indexing.draft_index_mapping(ctx, 'lucene', 'x', convention='ecs')


async def test_an_elasticsearch_index_is_drafted_from_the_users_example_or_their_confirmed_choice():
    from utils.consent import ConsentStore
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value={'values': []}),
                             settings=SimpleNamespace(default_convention=None))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': ConsentStore(use_elicitation=False)})
    with patch.object(indexing, '_conventions', lambda c: {'ecs': {}, 'stroom-flat': {}}):
        # A convention alone: not drafted; the user is to be offered their example first.
        asked = await indexing.draft_index_mapping(ctx, 'elasticsearch', 'x', convention='stroom-flat', events_stream_ids=[1])
        assert asked['drafted'] is False and asked['options'][0]['option'].startswith('example index template')
        # Without an example only when the user confirms it.
        gate = await indexing.draft_index_mapping(ctx, 'elasticsearch', 'x', convention='stroom-flat',
                                                  events_stream_ids=[1], without_example=True)
        assert gate['status'] == 'needs_confirmation' and 'without an example index template' in gate['summary']


def test_a_discovery_template_takes_the_examples_settings_but_not_its_fields_or_rules():
    from utils.fieldplan import Discovery
    from utils.templatecheck import from_example
    plan = FieldPlan.for_discovery('stroom-discovery-acme-v1', Discovery(timestamp_field='ts'))
    example = {'index_patterns': ['ecs-x*'], 'composed_of': ['base'], 'template': {
        'settings': {'index': {'number_of_shards': 3, 'mapping': {'total_fields': {'limit': 500}}}},
        'mappings': {'dynamic': 'strict', 'properties': {'User': {'properties': {'Id': {'type': 'keyword'}}},
                                                         '@timestamp': {'type': 'date', 'format': 'epoch_millis'}}}}}
    body, notes = from_example(plan.elastic_template('x')['body'], example,
                               {'base': {'template': {'mappings': {'properties': {'StreamId': {'type': 'long'}}}}}},
                               discovery=True)
    mappings = body['template']['mappings']
    assert mappings['dynamic'] is True and 'dynamic_templates' in mappings and 'User' not in mappings['properties']
    # The example maps @timestamp itself: its definition is kept, as in any template built from an example.
    assert mappings['properties'] == {'EventId': {'type': 'long'}, '@timestamp': {'type': 'date', 'format': 'epoch_millis'}}
    assert body['template']['settings']['index'] == {'number_of_shards': 3, 'mapping': {
        'total_fields': {'limit': 2000}, 'ignore_malformed': True}}
    assert any(n.startswith('a discovery index') for n in notes) and not any('named unlike' in n for n in notes)


async def test_a_discovery_template_waits_for_the_users_example_like_any_other():
    from config import Settings
    from utils.consent import ConsentStore
    from utils.fieldplan import Discovery
    settings = Settings(_env_file=None, stroom_url='https://stroom.example', dev_no_auth=True, stroom_api_key='k')
    stroom = SimpleNamespace(settings=settings, get_doc=AsyncMock(return_value={'uuid': 'p1', 'name': 'Acme'}))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': ConsentStore(False)})
    plan = FieldPlan.for_discovery('stroom-discovery-acme-v1', Discovery(timestamp_field='ts'))
    documents = [{'StreamId': ('number', '7'), 'EventId': ('number', '1'), '@timestamp': ('string', '2026-10-01T09:00:00Z'),
                  'host': ('string', 'web01')}]
    with patch('tools.indexing._destination', AsyncMock(return_value={'index name': 'stroom-discovery-acme-v1',
                                                                       'cluster': 'ES'})), \
            patch('tools.indexing._documents', AsyncMock(return_value=documents)), \
            patch('tools.indexing.guard_from', lambda c: SimpleNamespace(check_managed=AsyncMock())):
        result = await indexing.propose_index_template(ctx, 'p1', plan, [7])
        # Without an example there is no template to commit: one applied to the cluster unagreed would still be
        # refused at indexing (seen in VS Code: the user committed it, and indexing was refused again).
        assert result['agreed'] is False and result['needs'] == 'example_template'
        assert 'template' not in result and 'dev_tools' not in result
        assert 'a sibling discovery index uses' in result['hint'] and 'without_example=true' in result['hint']
        # Only when the user says they have none: built from the plan, and they confirm it as shown.
        asked = await indexing.propose_index_template(ctx, 'p1', plan, [7], without_example=True, reviewed=True)
    assert asked['status'] == 'needs_confirmation' and asked['template_name'] == 'stroom-discovery-acme-v1'
    assert asked['details']['notes'] == ['built from the field plan alone: the user has no example index template']



def test_documents_are_nested_whatever_subobjects_says_and_a_value_cannot_also_be_an_object():
    fields = FIELDS[:4]
    flat = FieldPlan(backend='elasticsearch', index_name='x', time_field='@timestamp', fields=fields, subobjects=False)
    assert '<map key="user"><string key="name">alice</string></map>' in run(flat.xslt())
    clash = FieldPlan(backend='elasticsearch', index_name='x', time_field='@timestamp',
                      fields=fields + [PlannedField(name='user', type='keyword', source='EventSource/User/Id')])
    assert any("'user' is a value and also the object holding ['user.name']" in p for p in clash.required())


def test_a_field_starting_with_an_underscore_is_reported_as_dropped():
    fields = FIELDS[:3] + [PlannedField(name='meta._source_host', type='keyword', source='EventSource/Device/HostName')]
    plan = FieldPlan(backend='elasticsearch', index_name='x', time_field='@timestamp', fields=fields)
    assert any("['meta._source_host']: Stroom's Elasticsearch indexing filter drops fields" in p for p in plan.required())


def test_discovery_keys_stroom_or_elasticsearch_would_lose_are_renamed():
    from utils.fieldplan import Discovery
    raw = ('<array xmlns="http://www.w3.org/2013/XSL/json"><map><string key="ts">t</string><string key="_id">s</string>'
           '<string key="">blank</string><number key="a..b">2</number><map key="deep"><string key="_x">in</string></map>'
           '<string key="StreamId">theirs</string></map></array>')
    plan = FieldPlan.for_discovery('d', Discovery(timestamp_field='ts'))
    xslt = plan.xslt().replace('stroom:stream-id()', '7').replace('stroom:record-no()', '1')
    with PySaxonProcessor(license=False) as proc:
        output = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=xslt).transform_to_string(
            xdm_node=proc.parse_xml(xml_text=raw))
    for kept in ('<string key="id_original">s</string>', '<string key="empty_key">blank</string>',
                 '<number key="a.b">2</number>', '<map key="deep"><string key="x_original">in</string></map>',
                 '<string key="StreamId_original">theirs</string>', '<number key="StreamId">7</number>'):
        assert kept in output, kept


def test_searches_that_would_mislead_on_elasticsearch_are_refused_with_what_to_use():
    S = indexing.SearchCheck
    with pytest.raises(ToolError) as e:
        indexing._searchable('elasticsearch', [S(field='user', condition='STARTS_WITH', value='al'),
                                               S(field='msg', condition='CONTAINS', value='err'),
                                               S(field='tags', condition='IS_NULL'),
                                               S(field='tags', condition='IS_NOT_NULL'),
                                               S(field='user', condition='IN', value='alice bob')])
    text = str(e.value)
    assert "use EQUALS 'al*'" in text and "use EQUALS '*err*'" in text and 'commas' in text
    assert "tags IS_NULL" in text and "tags IS_NOT_NULL: Stroom matches every document" in text and "EQUALS '*'" in text
    indexing._searchable('lucene', [S(field='user', condition='STARTS_WITH', value='al')])     # not known to mislead
    indexing._searchable('elasticsearch', [S(field='user', condition='EQUALS', value='al*'),
                                           S(field='status', condition='BETWEEN', value='100,300')])


async def test_a_hit_is_traced_to_its_record_and_a_mismatch_fails():
    step_output = ('<array xmlns="http://www.w3.org/2005/xpath-functions"><map><number key="StreamId">9</number>'
                   '<number key="EventId">2</number><map key="user"><string key="name">bob</string></map></map></array>')
    layers = [{'sourcePipeline': {'type': 'Pipeline', 'uuid': 'ix', 'name': 'ix'}, 'pipelineData': {
        'elements': {'add': [{'id': 'xsltFilter', 'type': 'XSLTFilter'}]},
        'properties': {'add': [{'element': 'xsltFilter', 'name': 'xslt',
                                'value': {'entity': {'type': 'XSLT', 'uuid': 'x', 'name': 'x'}}}]}}}]
    stroom = SimpleNamespace(pipeline_layers=AsyncMock(return_value=layers))
    stepped = AsyncMock(return_value={'elements': {'xsltFilter': {'output': step_output}}})
    with patch('tools.indexing.gateway_from', return_value=stroom), patch('tools.stepping.step_pipeline', stepped):
        ok = await indexing._traced(None, 'ix', {'StreamId': '9', 'EventId': '2'},
                                    {'field': 'user.name', 'condition': 'EQUALS', 'value': 'bob'})
        wrong_value = await indexing._traced(None, 'ix', {'StreamId': '9', 'EventId': '2'},
                                             {'field': 'user.name', 'condition': 'EQUALS', 'value': 'alice'})
        wrong_record = await indexing._traced(None, 'ix', {'StreamId': '9', 'EventId': '5'}, {})
    assert ok == {'traced': True, 'stream': 9, 'event': 2}
    assert stepped.await_args_list[0].args[1:] == ('ix', 9, 1)       # EventId 2 is the second record, index 1
    assert not wrong_value['traced'] and "user.name = ['bob'], not alice" in wrong_value['why']
    assert not wrong_record['traced'] and 'no document with EventId 5' in wrong_record['why']


def test_a_delimited_discovery_plan_indexes_each_column_by_its_header():
    from utils.fieldplan import Discovery
    plan = FieldPlan.for_discovery('d', Discovery(input='delimited', timestamp_field='time', drop=['secret']))
    assert plan.elastic_template('d')['body']['template']['mappings']['numeric_detection'] is True
    raw = ('<records xmlns="records:2"><record><data name="time" value="2026-10-01T09:00:00Z"/>'
           '<data name="user" value="alice"/><data name="secret" value="x"/><data name="_id" value="s1"/>'
           '<data name="msg" value="{&quot;a&quot;: 1}"/></record></records>')
    xslt = plan.xslt().replace('stroom:stream-id()', '7').replace('stroom:record-no()', '1')
    with PySaxonProcessor(license=False) as proc:
        out = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=xslt).transform_to_string(
            xdm_node=proc.parse_xml(xml_text=raw))
    for part in ('<string key="@timestamp">2026-10-01T09:00:00Z</string>', '<string key="user">alice</string>',
                 '<string key="id_original">s1</string>', '<map key="msg_json"><number key="a">1</number></map>'):
        assert part in out, part
    assert 'secret' not in out


def test_an_xml_discovery_plan_keeps_the_structure_and_needs_its_record_element():
    from utils.fieldplan import Discovery
    plan = FieldPlan.for_discovery('d', Discovery(input='xml', record='logon', timestamp_field='@at'))
    raw = ('<logons xmlns="urn:x"><logon id="7" at="2026-10-01T09:00:00Z"><who>alice</who>'
           '<roles><role>admin</role><role>ops</role></roles><client ip="10.1.1.1">laptop</client></logon></logons>')
    xslt = plan.xslt().replace('stroom:stream-id()', '7').replace('stroom:record-no()', '1')
    with PySaxonProcessor(license=False) as proc:
        out = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=xslt).transform_to_string(
            xdm_node=proc.parse_xml(xml_text=raw))
    for part in ('<string key="@timestamp">2026-10-01T09:00:00Z</string>', '<string key="id">7</string>',
                 '<string key="who">alice</string>', '<map key="roles"><array key="role"><string>admin</string><string>ops</string></array></map>',
                 '<map key="client"><string key="ip">10.1.1.1</string><string key="value">laptop</string></map>'):
        assert part in out, part
    with pytest.raises(ValueError, match="needs the record element's name"):
        FieldPlan.for_discovery('d', Discovery(input='xml', timestamp_field='when')).xslt()


async def test_an_xml_discovery_pipeline_needs_no_events_though_its_template_looks_like_indexing():
    # XMLParser, XSLT, indexing filter: the same shape as indexing Events. The plan kept with the XSLT tells them apart.
    from utils.fieldplan import Discovery
    from utils.mappingstore import with_mapping
    shape = {'stage': 'indexing', 'backend': 'elasticsearch',
             'child_must_supply': [{'element': 'xsltFilter', 'type': 'XSLTFilter', 'property': 'xslt'},
                                   {'element': 'elasticIndexingFilter', 'type': 'ElasticIndexingFilter', 'property': 'indexName'}]}
    plan = FieldPlan.for_discovery('d', Discovery(input='xml', record='logon', timestamp_field='when'))
    discovery_xslt = {'description': with_mapping('', 'index', plan.model_dump())}
    events_xslt = {'description': with_mapping('', 'index', FieldPlan(
        backend='elasticsearch', index_name='x', time_field='@timestamp', fields=FIELDS).model_dump())}
    for xslt, checked in ((discovery_xslt, False), (events_xslt, True)):
        ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(get_doc=AsyncMock(return_value=xslt))})
        with patch('tools.indexing._shape', AsyncMock(return_value=shape)), \
                patch('tools.indexing._events_available', AsyncMock()) as events, \
                patch('tools.indexing.create_pipeline', AsyncMock(return_value={'uuid': 'p'})):
            await indexing.create_indexing_pipeline(ctx, 'b', 'n', 't', 'x', index_name='d')
        assert events.called is checked


def test_elasticsearch_output_says_which_json_schema_it_follows():
    # A template's schema filter with group JSON holds two schemas (json.xsd, xpath-functions.xsd): without
    # xsi:schemaLocation every record fails with "No schema locations specified".
    import re
    from utils.fieldplan import Discovery
    declared = ('xsi:schemaLocation="http://www.w3.org/2005/xpath-functions file://xpath-functions.xsd"')
    indexed = run(FieldPlan(backend='elasticsearch', index_name='stroom-acme-v1', time_field='@timestamp',
                            fields=FIELDS).xslt())
    discovered = run_raw(FieldPlan.for_discovery('stroom-raw-v1', Discovery(timestamp_field='time')).xslt())
    for output in (indexed, discovered):
        root = re.search(r'<array\b[^>]*>', output).group(0)
        assert declared in root and 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' in output


async def test_elasticsearch_asks_for_the_users_example_first_offering_existing_indexes():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from tools import indexing
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value={'values': [
        {'docRef': {'type': 'ElasticIndex', 'uuid': 'k', 'name': 'Keycloak'}, 'path': 'System / Elastic Indices'}]}),
        settings=SimpleNamespace(default_convention=None))
    with patch.object(indexing, '_conventions', lambda ctx: {'ecs': {'description': 'ECS'}, 'stroom-flat': {}}):
        asked = await indexing.get_field_conventions(SimpleNamespace(lifespan_context={'stroom': stroom}),
                                                     backend='elasticsearch')
    options = [o['option'] for o in asked['options']]
    assert asked['status'] == 'needs_guidance' and options[0].startswith('example index template')
    assert options[1] == 'follow an existing index in Stroom' and asked['options'][1]['existing_indexes'][0]['uuid'] == 'k'
    # One choice per option, labelled, the template first (an agent offered the rest and dropped the template).
    assert [o['choice'] for o in asked['options']] == ['From an index template', 'Follow an existing index in Stroom',
                                                       'ECS (Elastic Common Schema) convention', 'Stroom flat convention']
    assert 'convention=ecs without_example=true' in asked['options'][2]['how']
    assert 'exactly these 4 choices, in this order' in asked['hint'] and 'From an index template;' in asked['hint']


async def test_an_existing_index_in_stroom_becomes_the_example_template():
    import json as jsonlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tools import indexing
    # The doc's own field list carries Elasticsearch's types (live FortiOS-V1: nativeType keyword, long, ip...).
    fields = [{'fldName': 'user.name', 'fldType': 'KEYWORD', 'nativeType': 'keyword'},
              {'fldName': 'source.ip', 'fldType': 'IPV4_ADDRESS', 'nativeType': 'ip'},
              {'fldName': '@timestamp', 'fldType': 'DATE', 'nativeType': 'date'},
              {'fldName': 'message', 'fldType': 'TEXT', 'nativeType': 'match_only_text'}]
    stroom = SimpleNamespace(get_doc=AsyncMock(return_value={'name': 'Keycloak', 'indexName': 'ecs-keycloak-v1',
                                                             'fields': fields}), post=AsyncMock())
    text, note, read = await indexing._example_from_index(SimpleNamespace(lifespan_context={'stroom': stroom}), 'k')
    head, body = text.split('\n', 1)
    template = jsonlib.loads(body)
    assert head == 'PUT _index_template/ecs-keycloak-v1' and template['index_patterns'] == ['ecs-keycloak-v1*']
    props = template['template']['mappings']['properties']
    assert props['user']['properties']['name'] == {'type': 'keyword'} and props['source']['properties']['ip'] == {'type': 'ip'}
    assert props['message'] == {'type': 'match_only_text'} and not stroom.post.called      # the exact type, kept
    assert read == {'doc': 'Keycloak', 'index': 'ecs-keycloak-v1', 'fields': 4,
                    'source': "the index doc's field list, with Elasticsearch's own types",
                    'examples': ['user.name (keyword)', 'source.ip (ip)', '@timestamp (date)', 'message (match_only_text)']}
    assert "Keycloak" in note and 'index template itself' in note
    # A doc without its field list: Stroom's findFields, with Stroom's types mapped.
    stroom.get_doc = AsyncMock(return_value={'name': 'Keycloak', 'indexName': 'ecs-keycloak-v1'})
    stroom.post = AsyncMock(return_value={'values': [{'fldName': 'source.ip', 'fldType': 'IPV4_ADDRESS'}]})
    text, _, read = await indexing._example_from_index(SimpleNamespace(lifespan_context={'stroom': stroom}), 'k')
    assert jsonlib.loads(text.split('\n', 1)[1])['template']['mappings']['properties']['source']['properties']['ip'] == {'type': 'ip'}
    assert read['source'].startswith("Stroom's field list")
    # Nothing listed (an unreachable cluster): no form, the user is to paste the template.
    stroom.post = AsyncMock(return_value={'values': []})
    with pytest.raises(ToolError, match='paste its index template'):
        await indexing._example_from_index(SimpleNamespace(lifespan_context={'stroom': stroom}), 'k')


async def test_following_an_existing_index_is_the_users_choice_confirmed_in_a_form():
    # Seen in VS Code: the user chose to paste their own template, and the agent drafted from FortiOS-V1 anyway.
    from utils.consent import ConsentStore
    stroom = SimpleNamespace(settings=SimpleNamespace(default_convention=None),
                             get_doc=AsyncMock(return_value={'name': 'FortiOS-V1'}))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': ConsentStore(use_elicitation=False)})
    example = ("PUT _index_template/fortios-v1\n" + json.dumps({'index_patterns': ['fortios-v1*'], 'template': {
        'mappings': {'properties': {'source': {'properties': {'ip': {'type': 'ip'}}}}}}}),
               "followed the fields of Elastic Index doc 'FortiOS-V1' (2 fields, ...)",
               {'doc': 'FortiOS-V1', 'index': 'ecs-fortios-v1', 'fields': 2,
                'source': "the index doc's field list, with Elasticsearch's own types",
                'examples': ['source.ip (ip)', 'rule.id (keyword)']})
    read = AsyncMock(return_value=example)
    summarise = AsyncMock(return_value={'path_population': {}})
    with patch.object(indexing, '_example_from_index', read), \
            patch.object(indexing, 'summarise_events', summarise), \
            patch.object(indexing, '_conventions', lambda c: {'ecs': {}}):
        gate = await indexing.draft_index_mapping(ctx, 'elasticsearch', 'fortinet-firewall-v1', like_index='a96e',
                                                  events_stream_ids=[9])
    # The fields are read first, so the user sees what they'd get; nothing is drafted before they agree.
    assert gate['status'] == 'needs_confirmation' and read.called and not summarise.called
    assert gate['summary'] == "Name the fields of index 'fortinet-firewall-v1' after an existing index in Stroom"
    assert gate['details']['follow'] == "Elastic Index doc 'FortiOS-V1' (index ecs-fortios-v1)"
    assert gate['details']['read through Stroom'].startswith('2 fields from the index doc') and 'source.ip (ip)' in \
        gate['details']['read through Stroom']
    # What Stroom can't give: the template itself, which the user may still paste.
    assert 'component templates' in gate['details']['not readable through Stroom']
    assert 'paste its index template' in gate['details']['or']

async def test_without_a_backend_the_only_indexing_backend_is_taken():
    # Seen: asked without the backend, the agent got the profiles, looked the backend up and asked again.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from tools import indexing, templates
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value={'values': []}),
                             settings=SimpleNamespace(default_convention=None))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    elastic = {'candidates': [{'uuid': 't1', 'name': 'Events to Elasticsearch', 'path': 'System', 'backend': 'elasticsearch'}]}
    with patch.object(indexing, '_conventions', lambda ctx: {'ecs': {'description': 'ECS'}}), \
            patch.object(templates, 'find_pipeline_templates', AsyncMock(return_value=elastic)):
        asked = await indexing.get_field_conventions(ctx)
    assert asked['options'][0]['choice'] == 'From an index template' and asked['backend'].startswith('elasticsearch')
    assert asked['indexing_templates'][0]['uuid'] == 't1'
    both = {'candidates': elastic['candidates'] + [{'uuid': 't2', 'name': 'Indexing', 'backend': 'lucene'}]}
    with patch.object(indexing, '_conventions', lambda ctx: {'ecs': {'description': 'ECS'}}), \
            patch.object(templates, 'find_pipeline_templates', AsyncMock(return_value=both)):
        asked = await indexing.get_field_conventions(ctx)
    assert 'profiles' in asked and asked['hint'].startswith('Which backend first')


async def test_an_index_doc_given_as_the_cluster_is_named_with_its_cluster():
    # Seen: the uuid of the FortiOS-V1 Elastic Index (offered as an index to follow) given as cluster_uuid, and
    # Stroom's 500 "Document not found" passed on.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastmcp.exceptions import ToolError
    from tools import indexing

    async def get_doc(doc_type, uuid):
        if doc_type == 'ElasticIndex':
            return {'name': 'FortiOS-V1', 'clusterRef': {'name': 'ES_PROD', 'uuid': 'c1'}}
        raise ToolError("Stroom rejected the request (500): Document not found")
    stroom = SimpleNamespace(get_doc=AsyncMock(side_effect=get_doc), settings=SimpleNamespace())
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': None})
    with pytest.raises(ToolError, match=r"is the Elastic Index doc 'FortiOS-V1', not an Elastic Cluster: its cluster is "
                                        r"'ES_PROD' \(cluster_uuid=c1\)"):
        await indexing.create_index_doc(ctx, build='b', backend='elasticsearch', name='fw', index_name='fw-v1',
                                        cluster_uuid='a96e', time_field='@timestamp')
