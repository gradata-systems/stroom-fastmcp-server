import re
from pathlib import Path

import pytest
from lxml import etree
from pydantic import ValidationError
from saxonche import PySaxonProcessor

from utils.eventschema import EventSchema
from utils.fielddoc import field_mapping_markdown, readable, sampled_events
from utils.xsltgen import (TranslationMapping, generate, literal, pattern_problem,
                           )

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
    xslt = generate(mapping(style={'layout': 'inline'}), SCHEMA, '4.1.0')['xslt']
    sheet = etree.fromstring(xslt.encode())
    ns = {'xsl': 'http://www.w3.org/1999/XSL/Transform', 'e': 'event-logging:3'}
    named = {t.get('name'): t for t in sheet.findall('xsl:template[@name]', ns)}
    assert set(named) == {'event_time', 'event_source'}
    assert len(sheet.findall('.//e:EventSource', ns)) == 1
    assert len(sheet.findall(".//xsl:call-template[@name='event_source']", ns)) == 2
    assert '<!-- event_source: logon, other -->' in xslt
    # One rule: nothing repeats, so everything stays inline.
    single = generate(mapping(events=[{'name': 'logon', 'fields': LOGON}], style={'layout': 'inline'}), SCHEMA,
                      '4.1.0')['xslt']
    assert 'call-template' not in single


def test_only_the_same_element_at_the_same_path_is_shared():
    # EventSource/User and Authenticate/User come out alike (both read user), but they mean different things.
    logoff = [f if f['path'] != 'EventDetail/Authenticate/Action' else {**f, 'value': 'Logoff'} for f in LOGON]
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': LOGON},
             {'name': 'logoff', 'when': [{'field': 'action', 'equals': 'logout'}], 'fields': logoff}]
    result = generate(mapping(events=rules, style={'layout': 'inline'}), SCHEMA, '4.1.0')
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
    result = generate(mapping(events=rules, style={'layout': 'inline'}), SCHEMA, '4.1.0')
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
    style = {'naming': 'camelCase', 'variable_min_reads': 1, 'inline_map_max_keys': 0, 'layout': 'inline'}
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


def test_a_missing_choice_points_to_the_same_member_mapped_elsewhere():
    # The user is mapped as the source's (BASE) but not as the one logging on: say so, and to keep both.
    no_user = [f for f in LOGON if f['path'] != 'EventDetail/Authenticate/User/Id']
    problems = generate(mapping(events=[{'name': 'logon', 'fields': no_user}]), SCHEMA, '4.1.0')['problems']
    problem = next(p for p in problems if 'Authenticate needs one of' in p)
    assert "EventSource/User/Id (field 'user')" in problem and 'EventDetail/Authenticate/User/Id' in problem
    assert 'keep the existing mapping' in problem
    # Nothing comparable mapped elsewhere: the plain message.
    base = [f for f in BASE if not f['path'].startswith(('EventSource/User', 'EventSource/Device'))]
    plain = generate(mapping(common=base, events=[{'name': 'logon', 'fields': no_user}]), SCHEMA, '4.1.0')['problems']
    assert any(p.endswith("Authenticate needs one of ['User', 'Device', 'Group']") for p in plain), plain


def test_unknown_in_a_rule_with_conditions_is_a_problem_unless_allowed():
    # The default mapping's 'other' rule has no conditions: Unknown is its job, so nothing is said.
    plain = generate(mapping(), SCHEMA, '4.1.0')
    assert plain['ok'] and not any('Unknown' in m for m in plain['problems'] + plain['warnings'])
    kind = {'name': 'status', 'when': [{'field': 'action', 'equals': 'status'}],
            'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'},
                       {'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}
    other = mapping().events[-1].model_dump(exclude_none=True)
    # Haiku kept the draft's Unknown placeholder through a warning in 6 of 36 runs: now nothing is generated.
    result = generate(mapping(events=[kind, other]), SCHEMA, '4.1.0')
    assert not result['ok'] and result['problems'][0].startswith('[status] writes EventDetail/Unknown')
    assert 'allow_unknown' in result['problems'][0] and result['xslt'] is None
    assert any('takes the reason' in p for p in generate(mapping(events=[{**kind, 'allow_unknown': True}, other]), SCHEMA, '4.1.0')['problems'])
    allowed = generate(mapping(events=[{**kind, 'allow_unknown': 'status lines carry no activity'}, other]), SCHEMA, '4.1.0')
    assert allowed['ok'] and not any('Unknown' in m for m in allowed['problems'] + allowed['warnings'])


def test_extracted_fields_read_through_any_of_are_declared_in_shared_templates():
    # The user is in two rules, so it is written once as a named template; it reads the extraction through any_of,
    # and the template has its own scope, so the extraction's variable must be declared there too.
    user = {'path': 'EventSource/User/Id', 'any_of': ['user_quoted', 'user_plain']}
    rules = [{'name': name, 'when': [{'field': 'action', 'equals': name}],
              'fields': [user, {'path': 'EventDetail/TypeId', 'value': name},
                         {'path': 'EventDetail/Authenticate/Action', 'value': action},
                         {'path': 'EventDetail/Authenticate/User/Id', 'any_of': ['user_quoted', 'user_plain']}]}
             for name, action in (('login', 'Logon'), ('logout', 'Logoff'))]
    m = mapping(common=[f for f in BASE if f['path'] != 'EventSource/User/Id'], events=rules,
                extract=[{'field': 'msg', 'regex': ' user=(?:"([^"]*)"|(\\S+))', 'names': ['user_quoted', 'user_plain']}])
    result = generate(m, SCHEMA, '4.1.0')
    assert not result['problems'], result['problems']
    root = etree.fromstring(result['xslt'].encode())
    xsl = '{http://www.w3.org/1999/XSL/Transform}'
    for template in root.iter(f'{xsl}template'):
        declared = {v.get('name') for v in template.iter(f'{xsl}variable', f'{xsl}param')}
        read = set(re.findall(r'\$([\w.-]+)', ' '.join((el.get('select') or '') + ' ' + (el.get('test') or '')
                                                         for el in template.iter())))
        assert read <= declared, (template.get('name') or template.get('match'), read - declared)


def test_a_rule_without_conditions_must_be_last():
    rules = [{'name': 'any', 'fields': LOGON}, {'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': LOGON}]
    assert any("Rules ['any'] have no conditions" in p for p in generate(mapping(events=rules), SCHEMA, '4.1.0')['problems'])


def test_unmatched_records_are_logged_when_every_rule_has_conditions():
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'one_of': ['login', 'logon']}], 'fields': LOGON}]
    xslt = generate(mapping(events=rules, unmatched='warn'), SCHEMA, '4.1.0')['xslt']
    assert "stroom:log('WARN', concat('No event mapping matched record ', stroom:record-no()))" in xslt
    assert "data[@name='action']/@value = ('login', 'logon')" in xslt  # one read: no variable


def test_each_field_needs_exactly_one_source():
    with pytest.raises(ValidationError, match='exactly one of field, any_of, value, xpath or lookup'):
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


def test_field_mapping_tables_show_the_mapping_and_the_sample():
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'one_of': ['login', 'logon']}],
              'fields': LOGON + [{'path': 'EventSource/Client/IPAddress', 'field': 'ip'}]},
             {'name': 'keepalive', 'drop': True, 'when': [{'field': 'action', 'equals': 'keepalive'}]},
             {'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'},
                                          {'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}]
    m = mapping(events=rules)
    assert 'field_mapping' not in generate(m, SCHEMA, '4.1.0')
    # From the mapping alone: From says where each value comes from, in schema order.
    text = field_mapping_markdown(m, SCHEMA)
    source, events = text.split('### Event types')
    rows = [line for line in source.splitlines() if line.startswith('| `')]
    assert [r.split(' | ')[0] for r in rows] == [
        '| `EventTime/TimeCreated`', '| `EventSource/System/Name`', '| `EventSource/System/Environment`',
        '| `EventSource/Generator`', '| `EventSource/Device/HostName`', '| `EventSource/Client/IPAddress`',
        '| `EventSource/User/Id`']
    assert '| `EventSource/System/Name` | The name of the system. | "Acme VPN" |' in source
    assert '| `host`, or "unknown" when empty |' in source
    assert '| `ip` (logon) |' in source
    assert ('| **logon**<br>`action` in login, logon | "Logon" |  | `Authenticate/Action` <- "Logon"<br>'
            '`Authenticate/User/Id` <- `user`<br>`Authenticate/Outcome/Success` <- `result`: ok -> true, fail -> false'
            "<br>`Authenticate/Data[@Name='session']/@Value` <- `sid` |") in events
    assert '| **keepalive**<br>`action` = keepalive |  | Left untranslated on purpose |  |' in events
    assert '| **other**<br>any other record | `action` |' in events


def test_sampled_field_mapping_matches_events_to_rules_by_the_marker():
    logoff = [f if f['path'] != 'EventDetail/Authenticate/Action' else {**f, 'value': 'Logoff'} for f in LOGON]
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}],
              'fields': LOGON + [{'path': 'EventDetail/Description', 'field': 'result'}]},
             {'name': 'logoff', 'when': [{'field': 'action', 'equals': 'logout'}],
              'fields': logoff + [{'path': 'EventSource/Client/IPAddress', 'field': 'ip'}]},
             {'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'},
                                          {'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}]
    m = mapping(events=rules)
    marked = generate(m, SCHEMA, '4.1.0', mark_rules=True)['xslt']
    assert '<!--stroom-mcp rule: logon-->' in marked and 'stroom-mcp rule' not in generate(m, SCHEMA, '4.1.0')['xslt']
    events = sampled_events([etree.tostring(transform(marked, RECORDS), encoding='unicode')])
    source, text = field_mapping_markdown(m, SCHEMA, events).split('### Event types')
    # Both halves: the mapping and the values the sample's events got, with which kinds had an element.
    assert '| `EventSource/System/Name` | The name of the system. | "Acme VPN" | "Acme VPN" |' in source
    assert '| `host`, or "unknown" when empty | "unknown"<br>"ws01" |' in source
    assert """| `user` | "o'neil"<br>(logon events only) |""" in source
    assert '| `EventSource/Client/IPAddress` | ' in source and '| `ip` (logoff) | (not in the sample) |' in source
    rows = [line.split(' | ') for line in text.splitlines() if line.startswith('| **')]
    assert [r[:4] for r in rows] == [
        ['| **logon**<br>`action` = login', '"Logon"', '`result`<br>"fail"', '1 of 2'],
        ['| **logoff**<br>`action` = logout', '"Logon"', '', '0 of 2<br>(not in the sample)'],   # TypeId stays LOGON's
        ['| **other**<br>any other record', '`action`<br>"keepalive"', '', '1 of 2']]
    assert ("`Authenticate/Action` <- \"Logon\" = \"Logon\"<br>`Authenticate/User/Id` <- `user` = \"o'neil\"<br>"
            "`Authenticate/Outcome/Success` <- `result`: ok -> true, fail -> false = \"false\"") in text
    # An event the mapping did not write is reported, not silently dropped.
    foreign = etree.fromstring('<Event xmlns="event-logging:3"><EventDetail><TypeId>x</TypeId><View/></EventDetail></Event>')
    assert "1 of the sample's 3 events were not written by a rule of this mapping" in field_mapping_markdown(m, SCHEMA, events + [foreign])


def test_expressions_in_the_documentation_name_fields_not_selectors():
    common = BASE[:5] + [{'path': 'EventSource/User/Id', 'xpath': "normalize-space(data[@name='user']/@value)"}]
    rules = [{'name': 'logon', 'when': [{'xpath': "concat(data[@name='action']/@value, '-', data[@name='a']/data[@name='b']/@value)",
                                         'equals': 'login-x'}], 'fields': LOGON}]
    text = field_mapping_markdown(mapping(common=common, events=rules), SCHEMA)
    assert '| `normalize-space(user)` |' in text
    assert "`concat(action, '-', a/b)` = login-x" in text
    assert readable("*[@key='user']/*[@key='name']") == 'user.name'


def test_an_action_user_without_the_acting_user_and_unread_extractions_are_warnings():
    # Haiku mapped the user to Authenticate/User only (case 13), and extracted src without reading it.
    no_source_user = [f for f in BASE if f['path'] != 'EventSource/User/Id']
    m = mapping(common=no_source_user, extract=[{'field': 'msg', 'regex': r'^(\S+) src=(\S+)$', 'names': ['who', 'src']}],
                events=[{'name': 'logon', 'fields': LOGON[:2] + [{'path': 'EventDetail/Authenticate/User/Id', 'field': 'who'}]}])
    warnings = generate(m, SCHEMA, '4.1.0')['warnings']
    assert any(w.startswith('[logon] maps EventDetail/Authenticate/User but not EventSource/User') for w in warnings)
    assert "extract names ['src'], which nothing reads" in ' '.join(warnings)
    # With both users mapped and every extracted name read, neither is said.
    fine = mapping(extract=[{'field': 'msg', 'regex': r'^(\S+)$', 'names': ['who']}],
                   events=[{'name': 'logon', 'fields': LOGON[:2] + [{'path': 'EventDetail/Authenticate/User/Id', 'field': 'who'}]}])
    assert not any('EventSource/User' in w or 'nothing reads' in w for w in generate(fine, SCHEMA, '4.1.0')['warnings'])


SHARED_DEVICE = """<xsl:stylesheet xmlns="event-logging:3" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:template name="eventSourceDevice">
    <Device><HostName>from-meta</HostName></Device>
  </xsl:template>
  <xsl:template name="eventMeta">
    <Meta><Source><Type>stream</Type><Id>guid-1</Id></Source></Meta>
  </xsl:template>
</xsl:stylesheet>"""


def test_shared_templates_are_imported_and_called_in_their_elements_place():
    base = [e for e in BASE if not e['path'].startswith('EventSource/Device')]
    shared = [{'href': 'Common-Event-V1', 'template': 'eventSourceDevice', 'at': 'EventSource/Device'}]
    result = generate(mapping(common=base, shared=shared), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    sheet = etree.fromstring(result['xslt'].encode())
    ns = {'xsl': 'http://www.w3.org/1999/XSL/Transform', 'e': 'event-logging:3'}
    assert etree.QName(sheet[0]).localname == 'import' and sheet[0].get('href') == 'Common-Event-V1'
    assert len(sheet.findall(".//xsl:call-template[@name='eventSourceDevice']", ns)) == 1
    assert sheet.find('.//e:Device', ns) is None
    # Run with the shared XSLT beside it, as Stroom resolves the import by name: one Device, in its place.
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / 'Common-Event-V1').write_text(SHARED_DEVICE, encoding='utf-8')
        main = Path(folder) / 'main.xsl'
        main.write_text(result['xslt'], encoding='utf-8')
        with PySaxonProcessor(license=False) as proc:
            executable = proc.new_xslt30_processor().compile_stylesheet(stylesheet_file=str(main))
            events = etree.fromstring(executable.transform_to_string(xdm_node=proc.parse_xml(xml_text=RECORDS)).encode())
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    for event in events.findall('e:Event', ns):
        assert [etree.QName(c).localname for c in event.find('e:EventSource', ns)][:4] == ['System', 'Generator', 'Device', 'User'][:len(event.find('e:EventSource', ns))]
        assert len(event.findall('e:EventSource/e:Device', ns)) == 1
        assert event.findtext('e:EventSource/e:Device/e:HostName', namespaces=ns) == 'from-meta'


def test_an_element_a_shared_template_writes_cannot_be_mapped_too():
    shared = [{'href': 'Common-Event-V1', 'template': 'eventSourceDevice', 'at': 'EventSource/Device'}]
    result = generate(mapping(shared=shared), SCHEMA, '4.1.0')     # BASE maps EventSource/Device/HostName
    assert not result['ok']
    assert any('EventSource/Device is written by the shared template eventSourceDevice (Common-Event-V1), so it must '
               'not be mapped as well' in p and 'Event/EventSource/Device/HostName' in p for p in result['problems'])
    nowhere = generate(mapping(shared=[{'href': 'C', 'template': 't', 'at': 'EventSource/Nowhere'}]), SCHEMA, '4.1.0')
    assert any(p.startswith('shared t (C): at EventSource/Nowhere') for p in nowhere['problems'])


def test_a_shared_template_at_event_level_and_generated_names_avoid_shared_ones():
    shared = [{'href': 'Common-Event-V1', 'template': 'event_source', 'at': 'Meta'}]
    result = generate(mapping(shared=shared), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    sheet = etree.fromstring(result['xslt'].encode())
    ns = {'xsl': 'http://www.w3.org/1999/XSL/Transform'}
    named = {t.get('name') for t in sheet.findall('xsl:template[@name]', ns)}
    # The generator's own EventSource template is not named event_source: that would override the shared one.
    assert 'event_source' not in named and len(sheet.findall(".//xsl:call-template[@name='event_source']", ns)) == 2


def test_a_value_no_element_means_is_offered_as_data_of_the_action_element():
    # Seen in VS Code: the agent invented elements (Network/Protocol, Alert/AlertSeverity) for fields the schema has
    # none for, was refused, and in the end left the events Unknown.
    rules = [{'name': 'traffic', 'when': [{'field': 'action', 'equals': 'connect'}],
              'fields': [{'path': 'EventDetail/TypeId', 'value': 'Traffic'},
                                             {'path': 'EventDetail/Network/Permit/Source/Device/IPAddress', 'field': 'ip'},
                                             {'path': 'EventDetail/Network/Protocol', 'field': 'result'}]},
             {'name': 'alert', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'Alert'},
                                          {'path': 'EventDetail/Alert/Type', 'value': 'Other'},
                                          {'path': 'EventDetail/Alert/AlertSeverity', 'field': 'result'}]}]
    problems = generate(mapping(events=rules), SCHEMA_352, '3.5.2')['problems']
    network = next(p for p in problems if 'Network/Protocol' in p)
    assert '{"path": "EventDetail/Network/Permit/Data", "data_name": "result", "field": "result"}' in network
    alert = next(p for p in problems if 'AlertSeverity' in p)
    assert "Did you mean ['Severity']" in alert and '"path": "EventDetail/Alert/Data"' in alert
    assert 'no reason to leave the event Unknown' in alert
    # Carried as Data, as offered, they generate.
    rules[0]['fields'][-1] = {'path': 'EventDetail/Network/Permit/Data', 'data_name': 'result', 'field': 'result'}
    rules[1]['fields'][-1] = {'path': 'EventDetail/Alert/Data', 'data_name': 'result', 'field': 'result'}
    assert generate(mapping(events=rules), SCHEMA_352, '3.5.2')['ok']


def test_an_invented_child_is_offered_as_data_of_the_nearest_element_that_takes_it():
    # destination_key given a made-up Destination/Key: Data under Destination, not under the action element.
    rules = [{'name': 'c', 'when': [{'field': 'action', 'equals': 'connect'}], 'fields': [
        {'path': 'EventDetail/TypeId', 'value': 'Connect'},
        {'path': 'EventDetail/Network/Connect/Source/Device/IPAddress', 'field': 'ip'},
        {'path': 'EventDetail/Network/Connect/Destination/Key', 'field': 'result'}]}]
    problem = next(p for p in generate(mapping(events=rules), SCHEMA_352, '3.5.2')['problems'] if 'Destination/Key' in p)
    assert '{"path": "EventDetail/Network/Connect/Destination/Data", "data_name": "result", "field": "result"}' in problem


XSL_URI = 'http://www.w3.org/1999/XSL/Transform'


def test_by_default_each_event_kind_and_shared_part_is_a_template_rule_with_its_own_mode():
    # The house style seen in the live translations: match="node()" mode="eventTypeLogon", applied with select=".".
    result = generate(mapping(style={'naming': 'camelCase'}), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    sheet = etree.fromstring(result['xslt'].encode())
    ns = {'xsl': XSL_URI, 'e': 'event-logging:3'}
    modes = {t.get('mode'): t for t in sheet.findall("xsl:template[@match='node()']", ns)}
    assert set(modes) == {'eventTypeLogon', 'eventTypeOther', 'eventTime', 'eventSource'}
    assert not sheet.findall('xsl:template[@name]', ns) and 'call-template' not in result['xslt']
    record = sheet.find("xsl:template[@mode='event']", ns)
    applied = [a.get('mode') for a in record.iterfind('.//xsl:apply-templates', ns)]
    assert applied == ['eventTypeLogon', 'eventTypeOther']
    assert all(a.get('select') == '.' for a in record.iterfind('.//xsl:apply-templates', ns))
    assert modes['eventTypeLogon'].find('e:Event/xsl:apply-templates[@mode="eventSource"]', ns) is not None
    assert record.find('.//e:Event', ns) is None    # the events are in their kinds' templates
    events = transform(result['xslt'], RECORDS)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    assert events.findtext('.//{event-logging:3}Authenticate/{event-logging:3}User/{event-logging:3}Id') == "o'neil"


def test_named_layout_calls_a_template_per_event_kind():
    result = generate(mapping(style={'layout': 'named'}), SCHEMA, '4.1.0')
    sheet = etree.fromstring(result['xslt'].encode())
    ns = {'xsl': XSL_URI}
    assert {t.get('name') for t in sheet.findall('xsl:template[@name]', ns)} == {
        'event_type_logon', 'event_type_other', 'event_time', 'event_source'}
    record = sheet.find("xsl:template[@mode='event']", ns)
    assert [c.get('name') for c in record.iterfind('.//xsl:call-template', ns)] == ['event_type_logon', 'event_type_other']
    assert VALIDATOR.validate(transform(result['xslt'], RECORDS))
    with pytest.raises(ValidationError):
        mapping(style={'layout': 'one-big-template'})


SHARED_FUNCTIONS = """<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:gs="urn:gs"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" version="3.0">
  <xsl:function name="gs:shout" as="xs:string"><xsl:param name="text" as="xs:string"/>
    <xsl:value-of select="upper-case($text)"/></xsl:function>
</xsl:stylesheet>"""


def test_shared_functions_are_imported_and_called_in_xpaths():
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': LOGON + [
                 {'path': 'EventDetail/Authenticate/Data', 'data_name': 'loud', 'xpath': "gs:shout(data[@name='user']/@value)"}]},
             {'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'},
                                          {'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}]
    shared = {'href': 'Common Functions', 'prefix': 'gs', 'namespace': 'urn:gs'}
    result = generate(mapping(events=rules, functions=[shared]), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    sheet = etree.fromstring(result['xslt'].encode())
    assert [i.get('href') for i in sheet.findall(f'{{{XSL_URI}}}import')] == ['Common Functions']
    assert sheet.nsmap['gs'] == 'urn:gs' and 'gs' in sheet.get('exclude-result-prefixes').split()
    # Run as Stroom would, with the import resolved to the shared document.
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, 'Common Functions').write_text(SHARED_FUNCTIONS, encoding='utf-8')
        main = Path(tmp, 'main.xsl')
        main.write_text(result['xslt'], encoding='utf-8')
        with PySaxonProcessor(license=False) as proc:
            executable = proc.new_xslt30_processor().compile_stylesheet(stylesheet_file=str(main))
            events = etree.fromstring(executable.transform_to_string(xdm_node=proc.parse_xml(xml_text=RECORDS)).encode())
    assert events.find(".//{event-logging:3}Data[@Name='loud']").get('Value') == "O'NEIL"
    # A prefix called but not bound, and one taken by the XSLT itself, are problems; an unused entry a warning.
    unbound = generate(mapping(events=rules), SCHEMA, '4.1.0')
    assert any("calls gs:... functions, but no functions entry binds 'gs'" in p for p in unbound['problems'])
    taken = generate(mapping(events=rules, functions=[{**shared, 'prefix': 'xs'}]), SCHEMA, '4.1.0')
    assert any("prefix 'xs' is the XSLT's own" in p for p in taken['problems'])
    unused = generate(mapping(functions=[shared]), SCHEMA, '4.1.0')
    assert unused['ok'] and any('nothing calls gs:...' in w for w in unused['warnings'])


DOMAIN_RECORDS = r"""<records xmlns="records:2">
<record><data name="time" value="2026-09-28T10:00:00.000Z"/><data name="action" value="login"/><data name="user" value="CORP\bob"/>
<data name="result" value="ok"/><data name="sid" value="s1"/></record>
</records>"""


def test_a_conversion_several_elements_use_is_one_function_of_the_xslts_own():
    # The environment's translations keep repeated conversions in xsl:functions (gs:parseTimestamp): so does this.
    logon = [f if f['path'] != 'EventDetail/Authenticate/User/Id' else {**f, 'transform': 'strip_domain'} for f in LOGON]
    base = [f if f['path'] != 'EventSource/User/Id' else {**f, 'transform': 'strip_domain'} for f in BASE]
    rules = [{'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': logon}]
    result = generate(mapping(common=base, events=rules), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    xslt = result['xslt']
    sheet = etree.fromstring(xslt.encode())
    functions = sheet.findall(f'{{{XSL_URI}}}function')
    assert [f.get('name') for f in functions] == ['mcp:strip_domain']
    assert xslt.count("replace(replace(") == 1 and xslt.count('mcp:strip_domain(') == 2   # defined once, called twice
    events = transform(xslt, DOMAIN_RECORDS)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    assert [u.text for u in events.iter('{event-logging:3}Id')] == ['bob', 'bob']
    # 0: always inline; one use: inline too.
    inline = generate(mapping(common=base, events=rules, style={'function_min_uses': 0}), SCHEMA, '4.1.0')['xslt']
    assert 'xsl:function' not in inline and inline.count('replace(replace(') == 2
    once = generate(mapping(events=rules), SCHEMA, '4.1.0')['xslt']
    assert 'xsl:function' not in once


def test_a_time_format_several_elements_use_is_one_function():
    timed = [{'path': 'EventTime/TimeCreated', 'field': 'time', 'time_format': "dd/MM/yyyy HH:mm:ss", 'timezone': '+10:00'}]
    rules = [{'name': 'logon', 'fields': LOGON + [
        {'path': 'EventDetail/Authenticate/Data', 'data_name': 'seen', 'field': 'time', 'time_format': "dd/MM/yyyy HH:mm:ss",
         'timezone': '+10:00'},
        {'path': 'EventDetail/Authenticate/Data', 'data_name': 'stamp', 'field': 'sid', 'time_format': 'epoch_ms'}]}]
    result = generate(mapping(common=timed + BASE[1:], events=rules, style={'naming': 'camelCase'}), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    sheet = etree.fromstring(result['xslt'].encode())
    [function] = sheet.findall(f'{{{XSL_URI}}}function')
    assert function.get('name') == 'mcp:parseTime'
    assert function.find(f'{{{XSL_URI}}}sequence').get('select') == \
        "stroom:format-date($value, 'dd/MM/yyyy HH:mm:ss', '+10:00')"
    assert result['xslt'].count('mcp:parseTime(') == 2 and 'mcp' in sheet.get('exclude-result-prefixes').split()
    # Used once, epoch_ms stays inline.
    assert 'stroom:format-date(string(' in result['xslt']
