from pathlib import Path

import pytest
from lxml import etree
from pydantic import ValidationError
from saxonche import PySaxonProcessor

from utils.eventschema import EventSchema
from utils.xsltgen import TranslationMapping, generate, literal, pattern_problem

XSD = (Path(__file__).parent / 'fixtures' / 'event-logging-v4.1.0.xsd').read_bytes()
SCHEMA = EventSchema.parse(XSD)
VALIDATOR = etree.XMLSchema(etree.fromstring(XSD))

BASE = [{'path': 'EventTime/TimeCreated', 'field': 'time'},
        {'path': 'EventSource/System/Name', 'value': 'Acme VPN'},
        {'path': 'EventSource/System/Environment', 'value': 'Dev'},
        {'path': 'EventSource/Generator', 'value': 'vpnd'},
        {'path': 'EventSource/Device/HostName', 'field': 'host', 'default': 'unknown'},
        {'path': 'EventSource/User/Id', 'field': 'user'}]
LOGON = [{'path': 'EventDetail/TypeId', 'value': 'Logon'},
         {'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
         {'path': 'EventDetail/Authenticate/User/Id', 'field': 'user'},
         {'path': 'EventDetail/Authenticate/Outcome/Success', 'field': 'result', 'map': {'ok': 'true', 'fail': 'false'}},
         {'path': 'EventDetail/Authenticate/Data', 'data_name': 'session', 'field': 'sid'}]
RECORDS = """<records xmlns="records:2">
<record><data name="time" value="2026-09-28T10:00:00.000Z"/><data name="action" value="login"/><data name="user" value="o'neil"/>
<data name="result" value="fail"/><data name="sid" value="s1"/></record>
<record><data name="time" value="2026-09-28T10:01:00.000Z"/><data name="action" value="keepalive"/><data name="host" value="ws01"/></record>
</records>"""


def mapping(**overrides) -> TranslationMapping:
    return TranslationMapping.model_validate({
        'input': 'data_splitter', 'unmatched': 'skip', 'common': BASE,
        'events': [{'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': LOGON},
                   {'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'},
                                                {'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}],
        **overrides})


def transform(xslt: str, xml: str) -> etree._Element:
    with PySaxonProcessor(license=False) as proc:
        executable = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=xslt)
        return etree.fromstring(executable.transform_to_string(xdm_node=proc.parse_xml(xml_text=xml)).encode())


def test_generated_xslt_writes_valid_events_in_schema_order():
    result = generate(mapping(), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    events = transform(result['xslt'], RECORDS)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    ns = {'e': 'event-logging:3'}
    logon, other = events.findall('e:Event', ns)
    # map, Data attribute, and a quote in the input survive
    assert logon.findtext('.//e:Authenticate/e:Outcome/e:Success', namespaces=ns) == 'false'
    assert logon.find('.//e:Authenticate/e:Data', ns).attrib == {'Name': 'session', 'Value': 's1'}
    assert logon.findtext('.//e:Authenticate/e:User/e:Id', namespaces=ns) == "o'neil"
    # an empty input with a default gets the default; without one, the element is left out
    assert logon.findtext('e:EventSource/e:Device/e:HostName', namespaces=ns) == 'unknown'
    assert other.find('e:EventSource/e:User', ns) is None
    assert other.findtext('e:EventSource/e:Device/e:HostName', namespaces=ns) == 'ws01'
    assert [etree.QName(c).localname for c in logon.find('.//e:Authenticate', ns)] == ['Action', 'User', 'Outcome', 'Data']


def test_json_keys_and_xml_paths_address_the_record():
    json_mapping = mapping(input='json', common=BASE[:4] + [{'path': 'EventSource/User/Id', 'field': 'user.name'},
                                                            {'path': 'EventSource/Device/HostName', 'field': 'host'}])
    result = generate(json_mapping, SCHEMA, '4.1.0')
    assert "*[@key='user']/*[@key='name']" in result['xslt']
    assert 'xpath-default-namespace="http://www.w3.org/2013/XSL/json"' in result['xslt']
    events = transform(result['xslt'], """<array xmlns="http://www.w3.org/2013/XSL/json"><map>
        <string key="time">2026-09-28T10:00:00.000Z</string><string key="action">login</string><string key="host">h</string>
        <map key="user"><string key="name">dave</string></map><string key="result">ok</string></map></array>""")
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    assert events.findtext('.//{event-logging:3}EventSource/{event-logging:3}User/{event-logging:3}Id') == 'dave'
    xml = generate(mapping(input='xml'), SCHEMA, '4.1.0')
    assert not xml['ok'] and 'xml input needs root and record' in xml['problems'][0]


def test_time_formats_become_stroom_format_date():
    common = [{'path': 'EventTime/TimeCreated', 'field': 'time', 'time_format': "yyyy-MM-dd'T'HH:mm:ss", 'timezone': 'UTC'}] + BASE[1:]
    xslt = generate(mapping(common=common), SCHEMA, '4.1.0')['xslt']
    assert "stroom:format-date(data[@name='time']/@value[1], 'yyyy-MM-dd''T''HH:mm:ss', 'UTC')" in xslt
    epoch = [{'path': 'EventTime/TimeCreated', 'field': 'time', 'time_format': 'epoch_s'}] + BASE[1:]
    assert 'xs:integer(xs:decimal(' in generate(mapping(common=epoch), SCHEMA, '4.1.0')['xslt']


@pytest.mark.parametrize('change, expected', [
    ({'path': 'EventSource/IPAddress', 'field': 'ip'}, "exists at: ['Event/EventSource/Device/IPAddress'"),
    ({'path': 'EventDetail/Logon', 'value': 'x'}, "'Logon' is a value of: ['Event/EventDetail/Authenticate/Action']"),
    ({'path': 'EventDetail/Authenticate/Action', 'value': 'Login'}, "['Login'] not allowed; Action takes one of ['Logon'"),
    ({'path': 'EventDetail/Authenticate/Outcome/Success', 'value': 'yes'}, 'Success is true or false'),
    ({'path': 'EventDetail/Process/Action', 'value': 'Execute'}, "['Authenticate', 'Process'] are alternatives"),
    ({'path': 'EventSource/Device', 'field': 'host'}, 'Device holds other elements; map one of'),
    ({'path': 'EventDetail/Authenticate/Data', 'field': 'sid'}, 'a Data path needs data_name'),
    ({'path': 'EventTime/TimeCreated', 'field': 'time', 'time_format': 'yyyy-MM-ddTHH:mm:ss'},
     "unquoted letters ['T']; quote them, e.g. \"yyyy-MM-dd'T'HH:mm:ss\""),
])
def test_mapping_mistakes_come_back_as_problems_not_xslt(change, expected):
    rule = {'name': 'logon', 'fields': [f for f in LOGON if f['path'] != change['path']] + [change]}
    result = generate(mapping(events=[rule]), SCHEMA, '4.1.0')
    assert not result['ok'] and result['xslt'] is None
    assert any(expected in p for p in result['problems']), result['problems']


def test_missing_required_elements_and_choices_are_problems():
    no_type = {'name': 'bare', 'fields': [{'path': 'EventDetail/Description', 'value': 'x'}]}
    problems = generate(mapping(events=[no_type]), SCHEMA, '4.1.0')['problems']
    assert '[bare] Event/EventDetail/TypeId is required by the schema; map it' in problems
    assert any(p.startswith("[bare] Event/EventDetail needs one of ['Authenticate'") for p in problems)


def test_a_rule_without_conditions_must_be_last():
    rules = [{'name': 'any', 'fields': LOGON}, {'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': LOGON}]
    assert any("Rules ['any'] have no conditions" in p for p in generate(mapping(events=rules), SCHEMA, '4.1.0')['problems'])


def test_unmatched_records_are_logged_when_every_rule_has_conditions():
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'one_of': ['login', 'logon']}], 'fields': LOGON}]
    xslt = generate(mapping(events=rules, unmatched='warn'), SCHEMA, '4.1.0')['xslt']
    assert "stroom:log('WARN', concat('No event mapping matched record ', stroom:record-no()))" in xslt
    assert "data[@name='action']/@value = ('login', 'logon')" in xslt


def test_each_field_needs_exactly_one_source():
    with pytest.raises(ValidationError, match='exactly one of field, value or xpath'):
        mapping(common=[{'path': 'EventSource/User/Id', 'field': 'user', 'value': 'x'}])


def test_helpers():
    assert literal("o'neil") == "'o''neil'"
    assert pattern_problem("yyyy-MM-dd'T'HH:mm:ss.SSSX") is None and pattern_problem('dd/MMM/yyyy:HH:mm:ss Z') is None


def test_drop_rules_leave_records_untranslated_without_a_warning():
    rules = [{'name': 'keepalive', 'drop': True, 'when': [{'field': 'action', 'equals': 'keepalive'}]},
             {'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': LOGON}]
    # With warnings on, keepalive records are caught by their own rule before the warning.
    warned = generate(mapping(events=rules, unmatched='warn'), SCHEMA, '4.1.0')['xslt']
    assert warned.index('left untranslated on purpose') < warned.index("stroom:log('WARN'")
    # Stroom's own functions only compile inside Stroom, so run it without the warning.
    result = generate(mapping(events=rules), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    assert result['events'][0] == {'event': 'keepalive', 'when': ["data[@name='action']/@value = 'keepalive'"],
                                   'dropped': True}
    events = transform(result['xslt'], RECORDS)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    assert len(events.findall('{event-logging:3}Event')) == 1  # the keepalive record wrote nothing
    assert 'keepalive: left untranslated on purpose' in result['xslt']


def test_a_drop_rule_takes_no_fields():
    rules = [{'name': 'noise', 'drop': True, 'when': [{'field': 'action', 'equals': 'x'}],
              'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'}]},
             {'name': 'logon', 'fields': LOGON}]
    result = generate(mapping(events=rules), SCHEMA, '4.1.0')
    assert not result['ok'] and any('takes no fields' in p for p in result['problems'])
