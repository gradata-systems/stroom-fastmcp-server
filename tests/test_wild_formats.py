"""Sources as they are found in the wild: damaged lines, XML inside JSON, repeated keys, terminal output, records
ending at a marked line, and a byte order mark. Each was found by an evaluation case (48-53) and checked on the local
Stroom; these hold the server's side of them."""
from pathlib import Path

from lxml import etree

from utils.dsgen import CRI_END, DS_HEAD, SplitterSpec, dry_run, generate_splitter, infer_spec
from utils.eventschema import EventSchema
from utils.localcheck import check_schema_values, sample_records
from utils.profile import profile
from utils.survey import split_records
from utils.xsltgen import TranslationMapping, generate, transform_expr, xml_steps

SCHEMA = EventSchema.parse((Path(__file__).parent / 'fixtures' / 'event-logging-v4.1.0.xsd').read_bytes())
GOOD = '{"time":"2026-10-11T08:00:0%dZ","user":"u%d","action":"login"}\n'


def test_json_lines_with_damaged_lines_are_still_json_lines_and_the_damage_is_said():
    # Seen on the local Stroom: a line cut short stopped the JSONParser (fatal), losing every record after it; and
    # one damaged line made the profile call the file delimited, so the JSON template was refused.
    text = ''.join(GOOD % (i, i) for i in range(1, 6)) + '{"time":"2026-10-11T08:00:06Z","user":"cut\n' \
        + ''.join(GOOD % (i, i) for i in range(7, 10))
    info = profile(text)
    assert info['format'] == 'json lines' and [b['line'] for b in info['malformed_lines']] == [6]
    assert "stops the stream at the first such line" in info['note'] and "json_fields" in info['note']
    assert info['xslt_input']['mapping'] == {'input': 'data_splitter', 'json_fields': ['json']}
    # A last line cut short by a read limit is not damage.
    assert 'malformed_lines' not in profile(''.join(GOOD % (i, i) for i in range(1, 6)) + '{"time":')


def test_csv_and_key_value_with_bad_lines_are_recognised_and_the_lines_named():
    csv_text = ('time,user,action,comment\n' + ''.join(f'2026-10-11T08:00:0{i}Z,u{i},login,fine\n' for i in range(1, 7))
                + '2026-10-11T08:00:07Z,bob,login\n2026-10-11T08:00:08Z,carol,update,"no closing quote\n')
    info = profile(csv_text)
    assert info['format'] == 'delimited' and info['delimiter'] == ','
    assert [(b['line'], b['reason']) for b in info['malformed_lines']] == [
        (8, '3 columns where the rest have 4'), (9, 'a quote opened and not closed')]
    kv = ''.join(f'time={i} user=u{i} action=login\n' for i in range(4)) + '### garbage ###\n'
    info = profile(kv)
    assert info['format'] == 'key=value' and info['malformed_lines'][0]['reason'] == 'holds no key=value pairs'


def test_xml_held_in_a_field_is_read_by_local_name_and_safely():
    payload = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
               '<a:Login xmlns:a="urn:acme"><a:User>alice</a:User><a:Account id="ACC-1"/></a:Login></soap:Body></soap:Envelope>')
    assert xml_steps('/Envelope/Body/Login/User') == '/*:Envelope/*:Body/*:Login/*:User'
    assert xml_steps('//Account/@id') == '//*:Account/@*:id'
    mapping = TranslationMapping.model_validate({
        'input': 'json', 'json_layout': 'lines', 'xml_fields': ['payload'],
        'common': [{'path': 'EventTime/TimeCreated', 'field': 'time', 'time_format': "yyyy-MM-dd'T'HH:mm:ssX"},
                   {'path': 'EventSource/System/Name', 'value': 'GW'}, {'path': 'EventSource/System/Environment', 'value': 'T'},
                   {'path': 'EventSource/Generator', 'value': 'gw'}, {'path': 'EventSource/Device/HostName', 'value': 'gw'},
                   {'path': 'EventSource/User/Id', 'field': 'payload//User'}, {'path': 'EventDetail/TypeId', 'value': 'x'}],
        'events': [{'name': 'login', 'fields': [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                                                {'path': 'EventDetail/Authenticate/User/Id', 'field': 'payload//User'}]}]})
    xslt = generate(mapping, SCHEMA, '4.1.0')['xslt']
    assert 'mcp:parse-xml' in xslt and '//*:User' in xslt and 'parse-xml($xml)' in xslt
    records, _ = sample_records(mapping, '{"time": "2026-10-11T08:00:01Z", "payload": %s}\n' % __import__('json').dumps(payload))
    assert records[0]['payload//User'] == 'alice' and records[0]['payload//Account/@id'] == 'ACC-1'
    assert records[0]['payload/Envelope/Body/Login/User'] == 'alice'


def test_a_repeated_key_is_guarded_as_values_and_the_dry_run_reads_its_first():
    # Seen in eval case 50: normalize-space() over every rcpt failed each record with two recipients.
    spec = SplitterSpec(kind='key_value', delimiter=' ')
    assert dry_run(spec, 'rcpt=bob rcpt=carol from=alice\n')['records'][0]['rcpt'] == 'bob'
    mapping = TranslationMapping.model_validate({
        'input': 'data_splitter',
        'common': [{'path': 'EventTime/TimeCreated', 'field': 'time', 'time_format': "yyyy-MM-dd'T'HH:mm:ssX"},
                   {'path': 'EventSource/System/Name', 'value': 'MX'}, {'path': 'EventSource/System/Environment', 'value': 'T'},
                   {'path': 'EventSource/Generator', 'value': 'mx'}, {'path': 'EventSource/Device/HostName', 'value': 'mx'},
                   {'path': 'EventDetail/TypeId', 'value': 'send'}],
        'events': [{'name': 'send', 'fields': [
            {'path': 'EventDetail/Send/Source/User/EmailAddress', 'field': 'from'},
            {'path': 'EventDetail/Send/Destination/User/EmailAddress', 'field': 'rcpt', 'repeat': True}]}]})
    xslt = generate(mapping, SCHEMA, '4.1.0')['xslt']
    assert """test="(data[@name='rcpt']/@value)[normalize-space(.)]\"""" in xslt
    assert "normalize-space(data[@name='rcpt']/@value)" not in xslt


def test_terminal_output_and_a_byte_order_mark():
    assert transform_expr('strip_control', '$x') == r"replace($x, '\p{Cc}\[[0-9;]*[A-Za-z]|[\p{Cc}-[\t\n\r]]', '')"
    # Seen in eval case 51: Stroom keeps a byte order mark, which became part of the first key.
    root = etree.fromstring(generate_splitter(SplitterSpec(kind='key_value', delimiter=' ')).encode())
    first = root[0]
    assert etree.QName(first).localname == 'regex' and first.get('pattern') == '^\\uFEFF' and first.get('maxMatch') == '1'
    assert DS_HEAD.count('uFEFF') == 1


def test_a_record_that_ends_at_a_marked_line_kubernetes_cri():
    text = ('2026-10-11T08:00:01.1Z stdout F user=alice action=login\n2026-10-11T08:00:02.1Z stderr P user=bo\n'
            '2026-10-11T08:00:02.2Z stderr F b action=export\n2026-10-11T08:00:03.1Z stdout F user=carol action=logout\n')
    spec, info = infer_spec(text)
    assert info['format'] == 'cri' and spec.record_end == CRI_END
    messages = [r['message'] for r in dry_run(spec, text)['records']]
    assert len(messages) == 3 and messages[1] == 'user=bo\n2026-10-11T08:00:02.2Z stderr F b action=export'
    assert 'record_end' not in generate_splitter(spec) and '(?!(?:' in generate_splitter(spec)
    assert len(split_records(text).records) == 3


def test_sample_values_are_checked_against_the_schemas_patterns():
    # Seen in eval case 40: CloudTrail's sourceIPAddress is sometimes an AWS service's name, which Stroom's schema
    # check refuses in an IPAddress; the step failed where the mapping check could have said so.
    mapping = TranslationMapping.model_validate({
        'input': 'json', 'json_layout': 'lines', 'nil_values': ['-'],
        'common': [{'path': 'EventSource/Client/IPAddress', 'field': 'ip'}, {'path': 'EventDetail/TypeId', 'value': 'x'}],
        'events': [{'name': 'x', 'fields': [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'}]}]})
    records = [{'ip': '10.0.0.1'}, {'ip': 'cloudformation.amazonaws.com'}, {'ip': '-'}]
    problems = check_schema_values(mapping, records, SCHEMA)
    assert len(problems) == 1 and "['cloudformation.amazonaws.com'] in 1 of the 2 sample records" in problems[0]
    assert check_schema_values(mapping, [{'ip': '10.0.0.1'}], SCHEMA) == []
