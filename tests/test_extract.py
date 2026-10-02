"""Fields parsed out of a text field with a regular expression (extract), and JSON lines as the JSONParser emits them."""
from lxml import etree

from tests.test_xsltgen import SCHEMA, VALIDATOR, transform
from utils.xsltgen import TranslationMapping, field_mapping_markdown, generate

NS = {'e': 'event-logging:3'}
# What the JSONParser emits for three JSON lines with addRootObject (its default): one map round them all.
JSON_LINES = """<map xmlns="http://www.w3.org/2013/XSL/json">
<map><string key="host">app01</string><string key="message">2026-10-01T10:00:00.000Z alice LOGIN Successful login from 10.0.0.1</string></map>
<map><string key="host">app02</string><string key="message">2026-10-01T10:05:00.000Z bob LOGOUT Session ended</string></map>
<map><string key="host">app02</string><string key="message">heartbeat</string></map>
</map>"""
MESSAGE = {'field': 'message', 'regex': r'^(\S+) (\S+) (\S+) (.*)$', 'names': ['ts', 'user', 'action', 'desc']}


def mapping(**overrides) -> TranslationMapping:
    return TranslationMapping.model_validate({
        'input': 'json', 'json_layout': 'lines', 'unmatched': 'skip',
        'extract': [MESSAGE],
        'common': [{'path': 'EventTime/TimeCreated', 'field': 'ts'},
                   {'path': 'EventSource/System/Name', 'value': 'App'},
                   {'path': 'EventSource/System/Environment', 'value': 'Dev'},
                   {'path': 'EventSource/Generator', 'value': 'app'},
                   {'path': 'EventSource/Device/HostName', 'field': 'host'},
                   {'path': 'EventSource/User/Id', 'field': 'user'},
                   {'path': 'EventDetail/TypeId', 'field': 'action'},
                   {'path': 'EventDetail/Description', 'field': 'desc'}],
        'events': [{'name': 'logon', 'when': [{'field': 'action', 'equals': 'LOGIN'}],
                    'fields': [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                               {'path': 'EventDetail/Authenticate/User/Id', 'field': 'user'}]},
                   {'name': 'logoff', 'when': [{'field': 'action', 'equals': 'LOGOUT'}],
                    'fields': [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logoff'},
                               {'path': 'EventDetail/Authenticate/User/Id', 'field': 'user'}]},
                   {'name': 'unparsed', 'drop': True, 'when': [{'field': 'ts', 'present': False}]}],
        **overrides})


def test_extracted_fields_read_like_input_fields_and_json_lines_match_the_parsers_root_map():
    result = generate(mapping(), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    xslt = result['xslt']
    assert 'xmlns:fn="http://www.w3.org/2005/xpath-functions"' in xslt
    assert "analyze-string(string((*[@key='message'])[1]), '^(\\S+) (\\S+) (\\S+) (.*)$')" in xslt
    assert 'select="/map/map" mode="event"' in xslt
    # The summary names the extracted field, not the group it comes from.
    assert result['events'][0]['when'] == ["action = 'LOGIN'"]
    events = transform(xslt, JSON_LINES)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    logon, logoff = events.findall('e:Event', NS)   # the heartbeat line matched nothing and was dropped
    assert logon.findtext('e:EventTime/e:TimeCreated', namespaces=NS) == '2026-10-01T10:00:00.000Z'
    assert logon.findtext('e:EventSource/e:User/e:Id', namespaces=NS) == 'alice'
    assert logon.findtext('e:EventDetail/e:Description', namespaces=NS) == 'Successful login from 10.0.0.1'
    assert logon.findtext('.//e:Authenticate/e:Action', namespaces=NS) == 'Logon'
    assert logoff.findtext('.//e:Authenticate/e:User/e:Id', namespaces=NS) == 'bob'


def test_a_json_array_is_matched_with_or_without_the_parsers_root_map():
    plain = [{'path': 'EventTime/TimeCreated', 'field': 'time'}, {'path': 'EventSource/System/Name', 'value': 'A'},
             {'path': 'EventSource/System/Environment', 'value': 'D'}, {'path': 'EventSource/Generator', 'value': 'g'},
             {'path': 'EventSource/Device/HostName', 'field': 'host'}]
    rules = [{'name': 'any', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'},
                                        {'path': 'EventDetail/Unknown/Data', 'data_name': 'h', 'field': 'host'}]}]
    result = generate(mapping(json_layout='array', extract=[], common=plain, events=rules), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    assert 'select="/array/map | /map/array/map" mode="event"' in result['xslt']
    record = '<map><string key="time">2026-10-01T10:00:00.000Z</string><string key="host">h</string></map>'
    for xml in (f'<array xmlns="http://www.w3.org/2013/XSL/json">{record}</array>',
                f'<map xmlns="http://www.w3.org/2013/XSL/json"><array>{record}</array></map>'):
        events = transform(result['xslt'], xml)
        assert len(events.findall('e:Event', NS)) == 1


def test_extraction_mistakes_are_problems_not_xslt():
    too_many = generate(mapping(extract=[{**MESSAGE, 'names': ['a', 'b', 'c', 'd', 'e']}]), SCHEMA, '4.1.0')
    assert not too_many['ok'] and '5 names but the regex has 4 capture groups' in too_many['problems'][0]
    lookahead = generate(mapping(extract=[{**MESSAGE, 'regex': r'^(?=x)(\S+) (\S+) (\S+) (.*)$'}]), SCHEMA, '4.1.0')
    assert not lookahead['ok'] and 'no named groups and no lookaround' in lookahead['problems'][0]
    twice = generate(mapping(extract=[MESSAGE, {**MESSAGE, 'field': 'host'}]), SCHEMA, '4.1.0')
    assert not twice['ok'] and "'ts' is the name of two extracted fields" in twice['problems'][0]
    fewer = generate(mapping(extract=[{**MESSAGE, 'names': ['ts', 'user', 'action']}]), SCHEMA, '4.1.0')
    assert fewer['ok'] and any('4 capture groups and 3 names' in w for w in fewer['warnings'])


def test_an_extraction_can_read_an_extracted_field():
    chained = [MESSAGE, {'field': 'desc', 'regex': r'from (\S+)$', 'names': ['client_ip']}]
    extra = [{'path': 'EventSource/Client/IPAddress', 'field': 'client_ip'}]
    m = mapping(extract=chained)
    m.common += [type(m.common[0]).model_validate(e) for e in extra]
    result = generate(m, SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    assert result['xslt'].index('name="message_parts"') < result['xslt'].index('name="desc_parts"')
    events = transform(result['xslt'], JSON_LINES)
    logon = events.findall('e:Event', NS)[0]
    assert logon.findtext('e:EventSource/e:Client/e:IPAddress', namespaces=NS) == '10.0.0.1'


def test_documentation_names_the_extracted_fields():
    text = field_mapping_markdown(mapping(), SCHEMA)
    assert text.startswith('### Extracted fields')
    assert '- `ts`, `user`, `action`, `desc`: parsed from `message` with `^(\\S+) (\\S+) (\\S+) (.*)$`' in text
    assert etree.fromstring(generate(mapping(), SCHEMA, '4.1.0')['xslt'].encode()) is not None
