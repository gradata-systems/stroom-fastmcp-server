import re
from pathlib import Path

import pytest
from lxml import etree
from pydantic import ValidationError
from saxonche import PySaxonProcessor

from utils.eventschema import EventSchema
from utils.xsltgen import (TranslationMapping, field_mapping_markdown, generate, literal, pattern_problem,
                           sampled_events)

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


def test_elements_repeated_across_rules_are_written_once_as_named_templates():
    xslt = generate(mapping(), SCHEMA, '4.1.0')['xslt']
    sheet = etree.fromstring(xslt.encode())
    ns = {'xsl': 'http://www.w3.org/1999/XSL/Transform', 'e': 'event-logging:3'}
    named = {t.get('name'): t for t in sheet.findall('xsl:template[@name]', ns)}
    assert set(named) == {'event_time', 'event_source'}
    assert len(sheet.findall('.//e:EventSource', ns)) == 1
    assert len(sheet.findall(".//xsl:call-template[@name='event_source']", ns)) == 2
    assert '<!-- event_source: logon, other -->' in xslt
    # One rule: nothing repeats, so everything stays inline.
    single = generate(mapping(events=[{'name': 'logon', 'fields': LOGON}]), SCHEMA, '4.1.0')['xslt']
    assert 'call-template' not in single


def test_only_the_same_element_at_the_same_path_is_shared():
    # EventSource/User and Authenticate/User come out alike (both read user), but they mean different things.
    logoff = [f if f['path'] != 'EventDetail/Authenticate/Action' else {**f, 'value': 'Logoff'} for f in LOGON]
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': LOGON},
             {'name': 'logoff', 'when': [{'field': 'action', 'equals': 'logout'}], 'fields': logoff}]
    result = generate(mapping(events=rules), SCHEMA, '4.1.0')
    ns = {'xsl': 'http://www.w3.org/1999/XSL/Transform', 'e': 'event-logging:3'}
    sheet = etree.fromstring(result['xslt'].encode())
    named = {t.get('name'): t for t in sheet.findall('xsl:template[@name]', ns)}
    assert named['user'].find('.//e:User', ns) is not None  # Authenticate/User, called from both rules
    assert named['event_source'].find(".//xsl:call-template[@name='user']", ns) is None
    assert named['event_source'].find('.//e:User', ns) is not None
    events = transform(result['xslt'], RECORDS)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    assert events.findtext('.//{event-logging:3}Authenticate/{event-logging:3}User/{event-logging:3}Id') == "o'neil"


XSL_NS = '{http://www.w3.org/1999/XSL/Transform}'


def variable_reads(xslt: str) -> list[tuple[str, int]]:
    """(name, reads in its scope) for every variable declared in a template or a rule."""
    found = []
    for scope in etree.fromstring(xslt.encode()).iter(f'{XSL_NS}template', f'{XSL_NS}when', f'{XSL_NS}otherwise'):
        text = etree.tostring(scope, encoding='unicode')
        found += [(v.get('name'), len(re.findall(rf"\${re.escape(v.get('name'))}(?![\w.-])", text)))
                  for v in scope.findall(f'{XSL_NS}variable')]
    return found


def test_variables_only_for_fields_read_often_and_declared_where_used():
    # user is read by Authenticate/User and by the session Data: four reads, all in the logon rule.
    logon = LOGON + [{'path': 'EventDetail/Authenticate/Data', 'data_name': 'account', 'field': 'user'}]
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': logon},
             {'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'},
                                          {'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}]
    result = generate(mapping(events=rules), SCHEMA, '4.1.0')
    xslt = result['xslt']
    assert all(reads >= 3 for _, reads in variable_reads(xslt)), variable_reads(xslt)
    sheet = etree.fromstring(xslt.encode())
    record = sheet.find(f"{XSL_NS}template[@mode='event']")
    # action is read by the rules' tests and the other rule: the template's; user only by the logon rule.
    assert [v.get('name') for v in record.findall(f'{XSL_NS}variable')] == ['action']
    assert [v.get('name') for v in record.findall(f'{XSL_NS}choose/{XSL_NS}when/{XSL_NS}variable')] == ['user']
    # A field read only by its own element stays where it is used, with a short guard.
    assert """<xsl:if test="normalize-space(data[@name='sid']/@value)">""" in xslt
    assert """<xsl:attribute name="Value" select="data[@name='sid']/@value"/>""" in xslt
    events = transform(xslt, RECORDS)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    assert events.find(".//{event-logging:3}Data[@Name='account']").get('Value') == "o'neil"


def test_style_sets_names_and_when_to_use_variables():
    style = {'naming': 'camelCase', 'variable_min_reads': 1, 'inline_map_max_keys': 0}
    xslt = generate(mapping(style=style), SCHEMA, '4.1.0')['xslt']
    sheet = etree.fromstring(xslt.encode())
    assert {t.get('name') for t in sheet.findall(f'{XSL_NS}template[@name]')} == {'eventTime', 'eventSource'}
    assert {v.get('name') for v in sheet.findall(f'{XSL_NS}variable')} == {'resultToSuccess'}  # always xsl:map
    body = etree.tostring(sheet.find(f"{XSL_NS}template[@mode='event']"), encoding='unicode')
    assert '$sid' in body and "data[@name='sid']/@value[normalize-space(.)]" in body  # one read is enough
    assert VALIDATOR.validate(transform(xslt, RECORDS))
    with pytest.raises(ValidationError):
        mapping(style={'naming': 'SCREAMING'})


def test_shared_or_long_value_maps_are_declared_once_as_xsl_maps():
    result_map = {'ok': 'true', 'fail': 'false'}
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': LOGON + [
                 {'path': 'EventDetail/Authenticate/Data', 'data_name': 'outcome', 'field': 'result', 'map': result_map}]},
             {'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'},
                                          {'path': 'EventDetail/Unknown/Data', 'data_name': 'kind', 'field': 'action',
                                           'map': {'login': 'Logon'}, 'default': 'Other'}]}]
    result = generate(mapping(events=rules), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    xslt = result['xslt']
    ns = {'xsl': 'http://www.w3.org/1999/XSL/Transform', 'e': 'event-logging:3'}
    sheet = etree.fromstring(xslt.encode())
    maps = {v.get('name'): {e.get('key'): e.get('select') for e in v.iterfind('xsl:map/xsl:map-entry', ns)}
            for v in sheet.findall('xsl:variable', ns)}
    # Success and the outcome Data use the same map, so it is declared once; the one-key map used once
    # reads better inline.
    assert maps == {'result_to_success': {"'ok'": "'true'", "'fail'": "'false'"}}
    assert xslt.count('$result_to_success?($result)') == 4  # guard and value of Outcome/Success and of the Data
    assert "if ($action = 'login') then 'Logon' else 'Other'" in xslt
    events = transform(xslt, RECORDS)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    logon, other = events.findall('e:Event', ns)
    assert logon.findtext('.//e:Authenticate/e:Outcome/e:Success', namespaces=ns) == 'false'
    assert logon.find(".//e:Authenticate/e:Data[@Name='outcome']", ns).get('Value') == 'false'
    # keepalive isn't a key: the default
    assert other.find(".//e:Unknown/e:Data[@Name='kind']", ns).get('Value') == 'Other'


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
    assert "data[@name='action']/@value = ('login', 'logon')" in xslt  # one read: no variable


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


XSD_352 = (Path(__file__).parent / 'fixtures' / 'event-logging-v3.5.2.xsd').read_bytes()
SCHEMA_352 = EventSchema.parse(XSD_352)


def test_v352_objects_from_a_repeating_choice_are_alternatives_not_all_required():
    # In 3.5.2, View's objects come from an extended base type whose choice repeats (maxOccurs unbounded).
    view = [{'path': 'EventDetail/TypeId', 'value': 'FileRead'},
            {'path': 'EventDetail/View/File/Path', 'field': 'action'}]
    result = generate(mapping(events=[{'name': 'view', 'fields': view}]), SCHEMA_352, '3.5.2')
    assert result['ok'], result['problems']
    events = transform(result['xslt'], RECORDS)
    validator = etree.XMLSchema(etree.fromstring(XSD_352))
    assert validator.validate(events), [e.message for e in validator.error_log]

    empty = [{'path': 'EventDetail/TypeId', 'value': 'FileRead'}, {'path': 'EventDetail/View/Outcome/Success', 'value': 'true'}]
    problems = generate(mapping(events=[{'name': 'view', 'fields': empty}]), SCHEMA_352, '3.5.2')['problems']
    assert any('EventDetail/View needs one of' in p and 'File' in p for p in problems), problems


def test_field_mapping_tables_for_the_documentation():
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'one_of': ['login', 'logon']}],
              'fields': LOGON + [{'path': 'EventSource/Client/IPAddress', 'field': 'ip'}]},
             {'name': 'keepalive', 'drop': True, 'when': [{'field': 'action', 'equals': 'keepalive'}]},
             {'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'},
                                          {'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}]
    text = generate(mapping(events=rules), SCHEMA, '4.1.0')['field_mapping']
    source, events = text.split('### Event types')
    rows = [line for line in source.splitlines() if line.startswith('| `')]
    # Schema order, the schema's description, constants quoted, defaults, and which kinds have an element.
    assert [r.split(' | ')[0] for r in rows] == [
        '| `EventTime/TimeCreated`', '| `EventSource/System/Name`', '| `EventSource/System/Environment`',
        '| `EventSource/Generator`', '| `EventSource/Device/HostName`', '| `EventSource/Client/IPAddress`',
        '| `EventSource/User/Id`']
    assert '| `EventSource/System/Name` | The name of the system. | "Acme VPN" |' in source
    assert '| `host`, or "unknown" when empty |' in source
    assert '| `ip` (logon) |' in source
    # Without a sample, each EventDetail element as XPath="value": constants as they are, inputs in braces.
    assert ('| **logon**<br>`action` in login, logon | "Logon" |  | `Authenticate/Action="Logon"`<br>'
            '`Authenticate/User/Id="{user}"`<br>`Authenticate/Outcome/Success="{result: ok → true, fail → false}"`'
            "<br>`Authenticate/Data[@Name='session']/@Value=\"{sid}\"` |") in events
    assert '| **keepalive**<br>`action` = keepalive |  | Left untranslated on purpose |  |' in events
    assert '| **other**<br>any other record | `action` |' in events


def test_field_mapping_takes_type_id_and_description_from_the_sample():
    logoff = [f if f['path'] != 'EventDetail/Authenticate/Action' else {**f, 'value': 'Logoff'} for f in LOGON]
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}],
              'fields': LOGON + [{'path': 'EventDetail/Description', 'field': 'result'}]},
             {'name': 'logoff', 'when': [{'field': 'action', 'equals': 'logout'}], 'fields': logoff},
             {'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'},
                                          {'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}]
    m = mapping(events=rules)
    result = generate(m, SCHEMA, '4.1.0')
    events = sampled_events([etree.tostring(transform(result['xslt'], RECORDS), encoding='unicode')])
    text = field_mapping_markdown(m, SCHEMA, events).split('### Event types')[1]
    assert 'Values are those written for the 2 events of the sample' in text
    rows = [line.split(' | ')[:3] for line in text.splitlines() if line.startswith('| **')]
    # The login record is the logon rule's (Action Logon), the keepalive the catch-all's; logoff wasn't sampled.
    assert rows == [['| **logon**<br>`action` = login', 'Logon', 'fail'],
                    ['| **logoff**<br>`action` = logout', '(not in the sample)', ''],
                    ['| **other**<br>any other record', 'keepalive', '']]
    # EventDetail is one sampled event of the row's TypeId, element by element.
    assert ('''| `Authenticate/Action="Logon"`<br>`Authenticate/User/Id="o'neil"`<br>`Authenticate/Outcome/Success="false"`'''
            "<br>`Authenticate/Data[@Name='session']/@Value=\"s1\"` |") in text
    assert "| keepalive |  | `Unknown/Data[@Name='action']/@Value=\"keepalive\"` |" in text
