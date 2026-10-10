"""CEF output for ArcSight: the plan drafted from Events, the XSLT it generates, reading and reviewing CEF lines, and
what the standing instructions say."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from lxml import etree
from saxonche import PySaxonProcessor

from tools import cef as cef_tool
from utils import cef

EVENTS = '''<Events xmlns="event-logging:3" Version="3.5.2">
<Event><EventTime><TimeCreated>2026-09-27T23:48:10.000Z</TimeCreated></EventTime>
 <EventSource><System><Name>SecretServer</Name><Environment>Prod</Environment><Organisation>Delinea</Organisation><Version>11.4</Version></System>
  <Device><HostName>ss-web-02</HostName><IPAddress>10.1.2.3</IPAddress></Device><Client><IPAddress>192.0.2.45</IPAddress></Client><User><Id>priya.patel</Id></User></EventSource>
 <EventDetail><TypeId>User-Login</TypeId><Description>User logged in</Description><Authenticate><Action>Logon</Action><User><Id>priya.patel</Id></User><Outcome><Success>false</Success></Outcome>
  <Data Name="session_id" Value="6857"/><Data Name="note" Value="a=b|c\\d&#10;second line"/></Authenticate></EventDetail></Event>
<Event><EventTime><TimeCreated>2026-09-27T23:56:15.000Z</TimeCreated></EventTime>
 <EventSource><System><Name>SecretServer</Name><Organisation>Delinea</Organisation><Version>11.4</Version></System>
  <Device><HostName>ss-web-01</HostName></Device><Client><IPAddress>ss-client.domain.com</IPAddress></Client><User><Id>riley.chen</Id></User></EventSource>
 <EventDetail><TypeId>Secret-View</TypeId><View><Resource><Type>Secret</Type><Name>Firewall | Edge</Name><Id>10005</Id></Resource></View></EventDetail></Event>
<Event><EventTime><TimeCreated>2026-09-28T00:01:06.000Z</TimeCreated></EventTime>
 <EventSource><System><Name>SecretServer</Name><Organisation>Delinea</Organisation></System><Device><HostName>fw1</HostName></Device></EventSource>
 <EventDetail><TypeId>Net</TypeId><Network><Deny><Source><Device><IPAddress>10.0.0.1</IPAddress></Device><Port>5555</Port></Source>
  <Destination><Device><IPAddress>10.0.0.2</IPAddress></Device><Port>443</Port></Destination><TransportProtocol>TCP</TransportProtocol></Deny></Network></EventDetail></Event>
</Events>'''

KAFKA_XSD = '''<xs:schema xmlns:krec="kafka-records:1" xmlns:xs="http://www.w3.org/2001/XMLSchema" elementFormDefault="qualified"
  targetNamespace="kafka-records:1">
  <xs:element name="kafkaRecords"><xs:complexType><xs:sequence><xs:element ref="krec:kafkaRecord"/></xs:sequence></xs:complexType></xs:element>
  <xs:element name="kafkaRecord"><xs:complexType><xs:sequence>
    <xs:element maxOccurs="unbounded" minOccurs="0" name="header"><xs:complexType><xs:sequence><xs:element name="key" type="xs:string"/>
      <xs:element minOccurs="0" name="value" type="xs:anyType"/></xs:sequence></xs:complexType></xs:element>
    <xs:choice><xs:sequence><xs:element name="key" type="xs:anyType"/><xs:element minOccurs="0" name="value" type="xs:anyType"/></xs:sequence>
      <xs:sequence><xs:element name="value" type="xs:anyType"/></xs:sequence></xs:choice></xs:sequence>
    <xs:attribute name="topic" type="xs:string" use="required"/><xs:attribute name="partition" type="xs:int"/>
    <xs:attribute name="timestamp"><xs:simpleType><xs:restriction base="xs:dateTime">
      <xs:pattern value="[\\d]{4}-[\\d]{2}-[\\d]{2}T[\\d]{2}:[\\d]{2}:[\\d]{2}.[\\d]{3}Z"/></xs:restriction></xs:simpleType></xs:attribute>
  </xs:complexType></xs:element>
</xs:schema>'''


def run(xslt: str, xml: str = EVENTS) -> str:
    with PySaxonProcessor(license=False) as proc:
        exe = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=xslt)
        return exe.transform_to_string(xdm_node=proc.parse_xml(xml_text=xml))


def drafted(custom_keys=False, overrides=(), **kwargs):
    return cef.draft(cef.events_of(EVENTS), custom_keys, [cef.Override.model_validate(o) for o in overrides],
                     topic='arcsight-cef', **kwargs)


def by_kind(plan):
    return {kind: {f.path: f for f in fields} for kind, fields in [('common', plan.common), *plan.events.items()]}


def test_a_draft_puts_values_in_arcsights_standard_keys_first_then_labelled_custom_slots():
    plan, notes = drafted()
    assert plan.problems() == []
    fields = by_kind(plan)
    assert fields['common']['EventTime/TimeCreated'].key == 'rt' and fields['common']['EventTime/TimeCreated'].transform == 'epoch_ms'
    assert fields['common']['EventSource/User/Id'].key == 'suser'
    assert fields['Authenticate']['EventDetail/Authenticate/User/Id'].key == 'duser'
    assert fields['Authenticate']['EventDetail/Authenticate/Outcome/Success'].transform == 'outcome'
    session = fields['Authenticate']["EventDetail/Authenticate/Data[@Name='session_id']/@Value"]
    assert session.key == 'cn1' and session.label == 'session_id'          # a number: a number slot
    environment = fields['common']['EventSource/System/Environment']
    assert environment.key.startswith('cs') and environment.label == 'System Environment'
    # The header carries the system, the TypeId and the Description; they're not repeated as fields.
    assert plan.product.source == 'EventSource/System/Name' and 'EventDetail/TypeId' not in fields['common']
    # Network's source is its own src (Client's is common, but no Network event has one); its action is Deny.
    assert fields['Network']['EventDetail/Network/*/Source/Device/IPAddress'].key == 'src'
    assert fields['Network']['local-name(EventDetail/Network/*[1])'].key == 'act'
    assert fields['View']["'View'"].key == 'act'


def test_a_value_that_does_not_fit_its_standard_key_goes_to_a_slot():
    # One Client "IPAddress" is a host name: src takes IPv4 addresses only.
    plan, notes = drafted()
    assert by_kind(plan)['common']['EventSource/Client/IPAddress'].key.startswith('cs')
    assert any("don't fit src" in n for n in notes)


def test_without_keys_outside_the_dictionary_values_past_the_slots_are_not_sent():
    many = ''.join(f'<Data Name="d{n}" Value="value {n}"/>' for n in range(12))
    xml = EVENTS.replace('<Data Name="session_id"', many + '<Data Name="session_id"')
    events = cef.events_of(xml)
    closed, _ = cef.draft(events, False, [], topic='t')
    assert closed.not_sent and all('custom slots are all used' in n['why'] for n in closed.not_sent)
    opened, _ = cef.draft(events, True, [], topic='t')
    assert not opened.not_sent and any(f.key not in cef.KEYS for f in opened.events['Authenticate'])
    assert opened.problems() == []


def test_overrides_and_standing_instructions_take_precedence():
    plan, _ = drafted(overrides=[
        {'path': 'EventDetail/Authenticate/User/Id', 'key': 'deviceCustomString6', 'label': 'Target user'},
        {'path': "EventDetail/Authenticate/Data[@Name='note']/@Value", 'drop': True}])
    target = by_kind(plan)['Authenticate']['EventDetail/Authenticate/User/Id']
    assert (target.key, target.label) == ('cs6', 'Target user')
    assert {'path': "EventDetail/Authenticate/Data[@Name='note']/@Value", 'event_type': 'Authenticate',
            'why': 'left out as instructed'} in plan.not_sent
    said = cef.from_instructions([
        "## CEF output\nCustom CEF keys are not allowed.\nKafka topic: arcsight-secretserver\n"
        "- EventDetail/Authorise/User/Name -> deviceCustomString6 (label: 'Authorised user')\n"
        "- EventSource/System/Environment -> cs1\nPipeline template: CEF to ArcSight\n"])
    assert said['custom_keys'] is False and said['topic'] == 'arcsight-secretserver'
    assert said['template'] == 'CEF to ArcSight'
    assert [(o.path, o.key, o.label) for o in said['overrides']] == [
        ('EventDetail/Authorise/User/Name', 'cs6', 'Authorised user'), ('EventSource/System/Environment', 'cs1', None)]


def test_the_kafka_xslt_writes_one_valid_record_an_event_its_value_the_escaped_cef_line():
    plan, _ = drafted()
    out = run(plan.xslt(), EVENTS.replace('<Event>', '<Event>', 1))
    records = etree.fromstring(out.encode())
    lines = cef.lines_in(out)
    assert len(lines) == 3 and records[0].get('topic') == 'arcsight-cef'
    assert records[0].get('timestamp') == '2026-09-27T23:48:10.000Z'
    first = cef.parse(lines[0])
    assert first['header'] == ['CEF:0', 'Delinea', 'SecretServer', '11.4', 'User-Login', 'User logged in', '3']
    pairs = dict(first['extension'])
    assert pairs['outcome'] == 'failure' and pairs['rt'] == '1790552890000'
    assert 'a\\=b|c\\\\d\\nsecond line' in lines[0]          # = and \ escaped, the line break as \n, | as it is
    assert next(v for k, v in first['extension'] if k.startswith('cs') and 'b|c' in v) == 'a=b|c\\d\nsecond line'
    second = cef.parse(lines[1])
    assert second['header'][4:6] == ['Secret-View', 'View'] and 'Firewall | Edge' in dict(second['extension']).values()
    # kafka-records:1 takes one record a document: with one Event a record (the split), each is valid.
    schema = etree.XMLSchema(etree.fromstring(KAFKA_XSD.encode()))
    for event in cef.events_of(EVENTS):
        single = '<Events xmlns="event-logging:3">' + etree.tostring(event).decode() + '</Events>'
        assert schema.validate(etree.fromstring(run(plan.xslt(), single).encode())), schema.error_log
    assert cef.review(lines, cef.events_of(EVENTS), False)['problems'] == []


def test_the_text_xslt_writes_a_line_an_event():
    plan, _ = drafted(output='text')
    lines = run(plan.xslt()).strip().split('\n')
    assert len(lines) == 3 and all(line.startswith('CEF:0|Delinea|SecretServer|') for line in lines)


def test_a_review_finds_what_arcsight_would_not_take_and_what_each_event_leaves_out():
    events = cef.events_of(EVENTS)[:1]
    line = ('CEF:0|Delinea|SecretServer|11.4|User-Login|User logged in|3|src=not-an-ip cs1=Prod myField=x '
            'suser=priya.patel rt=1790552890000')
    reviewed = cef.review([line], events, False)
    said = ' '.join(reviewed['problems'])
    assert "cs1 has no cs1Label" in said and "src holds 'not-an-ip'" in said
    assert "key 'myField' is not in ArcSight's CEF dictionary (keys outside it aren't allowed" in said
    implied = {m['key']: m['path'] for m in reviewed['implied_mapping']['Authenticate']}
    assert implied['suser'] in ('EventSource/User/Id', 'EventDetail/Authenticate/User/Id')
    assert implied['rt'] == 'EventTime/TimeCreated' and implied['cs1'] == 'EventSource/System/Environment'
    assert any(n['path'] == "EventDetail/Authenticate/Data[@Name='session_id']/@Value" for n in reviewed['not_sent'])
    broken = cef.review(['CEF:0|Delinea|SecretServer|only five|3|x=1'], [], None)
    assert any('a header of' in p for p in broken['problems'])


def test_the_documentation_names_each_key_its_arcsight_field_and_its_label():
    plan, _ = drafted()
    lines = cef.lines_in(run(plan.xslt()))
    text = plan.markdown(cef.examples_from(plan, lines, cef.events_of(EVENTS)))
    assert '### Header' in text and '| 2 | Device Vendor | `EventSource/System/Organisation` | Delinea |' in text
    assert "| `EventDetail/Authenticate/User/Id` | duser | destinationUserName |" in text
    assert "labelled 'session_id' (cn1Label = deviceCustomNumber1Label)" in text and '| 6857 |' in text
    assert "| the event's kind (View) | act | deviceAction |" in text
    assert 'on topic `arcsight-cef`, one Event a record' in text


def test_a_plan_with_keys_outside_the_dictionary_or_unlabelled_slots_has_problems():
    plan, _ = drafted()
    broken = plan.model_copy(update={'topic': None, 'events': {'View': [
        cef.CefField(path='EventDetail/View/Resource/Name', key='myField'),
        cef.CefField(path='EventDetail/View/Resource/Id', key='cs2')]}})
    said = ' '.join(broken.problems())
    assert 'needs topic' in said and "'myField' is not a key in ArcSight's CEF dictionary" in said
    assert 'cs2: a custom slot needs a label' in said


def ctx():
    return SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(
        settings=SimpleNamespace(max_sample_records=50), find_documents=AsyncMock(return_value={'values': []}),
        post=AsyncMock(return_value={'values': []}))})


async def test_the_user_is_asked_about_keys_outside_the_dictionary_unless_the_instructions_say():
    stroom = ctx().lifespan_context['stroom']
    with patch.object(cef_tool, 'gateway_from', return_value=stroom), \
            patch.object(cef_tool, 'applicable_instructions', AsyncMock(return_value={'instructions': []})), \
            patch.object(cef_tool, '_events', AsyncMock(return_value=cef.events_of(EVENTS))), \
            patch.object(cef_tool, '_templates', AsyncMock(return_value={})):
        asked = await cef_tool.draft_cef_mapping(ctx(), [7], topic='t')
        assert asked['status'] == 'needs_guidance' and asked['options'] == cef_tool.CUSTOM_OPTIONS
    said = {'instructions': [{'instructions': 'Custom CEF keys are not allowed. Kafka topic: arcsight-ss'}]}
    with patch.object(cef_tool, 'gateway_from', return_value=stroom), \
            patch.object(cef_tool, 'applicable_instructions', AsyncMock(return_value=said)), \
            patch.object(cef_tool, '_events', AsyncMock(return_value=cef.events_of(EVENTS))), \
            patch.object(cef_tool, '_templates', AsyncMock(return_value={})):
        result = await cef_tool.draft_cef_mapping(ctx(), [7])
    assert result['custom_keys'] is False and result['plan']['topic'] == 'arcsight-ss' and result['problems'] == []
    assert result['from_standing_instructions'] == {'custom_keys': False, 'topic': 'arcsight-ss'}
    assert result['example_line'].startswith('CEF:0|Delinea|SecretServer|11.4|User-Login|')
    assert result['documentation'].startswith('## Field mapping')


async def test_a_plan_with_problems_is_not_saved():
    stroom = ctx().lifespan_context['stroom']
    create = AsyncMock()
    with patch.object(cef_tool, 'gateway_from', return_value=stroom), \
            patch.object(cef_tool, 'applicable_instructions', AsyncMock(return_value={'instructions': []})), \
            patch.object(cef_tool, '_events', AsyncMock(return_value=cef.events_of(EVENTS))), \
            patch.object(cef_tool, '_templates', AsyncMock(return_value={})), \
            patch('tools.translation.create_xslt', create):
        result = await cef_tool.draft_cef_mapping(ctx(), [7], custom_keys=False, build='b', name='CEF')   # no topic
    assert result['saved'] is None and 'needs topic' in ' '.join(result['problems']) and not create.called


def test_forwarding_pipelines_are_found_by_their_kafka_producer_and_step_as_cef():
    from tools.stepping import sends_on
    from tools.templates import _classify
    assert _classify({'p': 'XMLParser', 'x': 'XSLTFilter', 'k': 'StandardKafkaProducer'}, {}) == ('forwarding', 'kafka')
    # Batch Search writes text from XML too: a text writer alone says nothing.
    assert _classify({'p': 'XMLParser', 'x': 'XSLTFilter', 't': 'TextWriter'}, {})[0] != 'forwarding'
    assert sends_on('CEF:0|a|b|1|x|y|3|act=z') and sends_on('<kafkaRecords><kafkaRecord topic="t"/></kafkaRecords>')
    assert not sends_on('some text the XSLT copied through')


def test_a_text_pipelines_lines_lose_strooms_xml_declaration_and_an_unescaped_pipe_is_named():
    # Seen stepping a CEF pipeline written by hand: its text output came with an XML declaration before it, and its
    # name's | (unescaped) was reported as an invalid severity.
    from utils import cef
    out = ('<?xml version="1.1" encoding="UTF-8"?>CEF:0|D|S|1|User-Login|User logged in|3|suser=a\n'
           'CEF:0|D|S|1|Secret-View|Secret viewed: a=b|c|3|suser=b\n'
           '<134>Oct 10 09:00:00 fw CEF:0|D|S|1|X|Y|3|suser=c\n')
    lines = cef.lines_in(out)
    assert lines[0].startswith('CEF:0|D|S|1|User-Login') and lines[2].startswith('<134>Oct 10')   # syslog prefix kept
    [problem] = cef.review(lines, [], False)['problems']
    assert problem.startswith("an unescaped | in the header's name or class id (in 'Secret viewed: a=b|c')")
