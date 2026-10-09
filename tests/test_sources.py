"""Timestamp inference, the Data Splitter spec and its dry run, the mapping's local checks against a sample, the
reference-data XSLT, and the generator's lookup, dictionary, any_of and transform sources."""
from pathlib import Path

import pytest
import yaml
from lxml import etree

from tests.test_xsltgen import SCHEMA, VALIDATOR, transform
from tools.reference import maps_in_xslt
from utils.dsgen import SplitterSpec, dry_run, generate_splitter
from utils.localcheck import check_mapping, sample_records, xml_value
from utils.profile import profile, profile_many
from utils.refgen import ReferenceMapping, generate_reference
from utils.timefmt import check_time_format, infer_time_pattern, pattern_regex
from utils.xsltgen import TranslationMapping, generate

NS = {'e': 'event-logging:3'}
CASES = Path(__file__).resolve().parents[1] / 'dev' / 'eval' / 'cases'


@pytest.mark.parametrize('value, pattern', [
    ('2026-09-28T10:00:00.000Z', "yyyy-MM-dd'T'HH:mm:ss.SSSX"), ('2026-09-28T10:00:00+01:00', "yyyy-MM-dd'T'HH:mm:ssXXX"),
    ('2026-09-28 10:00:00,123', 'yyyy-MM-dd HH:mm:ss,SSS'), ('28/Sep/2026:10:00:00 +0000', 'dd/MMM/yyyy:HH:mm:ss Z'),
    ('Sep  8 10:00:00', 'MMM  d HH:mm:ss'), ('Oct  1 10:00:00 2026', 'MMM  d HH:mm:ss yyyy'),
    ('10/01/2026 10:00:00 PM', 'MM/dd/yyyy hh:mm:ss a'), ('20261001T100000Z', "yyyyMMdd'T'HHmmssX"),
    ('Wed, 01 Oct 2026 10:00:00 GMT', 'EEE, dd MMM yyyy HH:mm:ss z'), ('1 Oct 2026 10:00:00', 'd MMM yyyy HH:mm:ss'),
    ('1790600000', 'epoch seconds'), ('alice', None), ('10.0.0.1', None),
])
def test_time_patterns_are_inferred_and_fit_their_values(value, pattern):
    assert infer_time_pattern(value) == pattern
    if pattern:
        assert check_time_format(pattern, [value]) is None


def test_a_wrong_time_format_is_named_with_the_shape_the_values_have():
    message = check_time_format('yyyy-MM-dd HH:mm:ss', ['2026-10-01T10:00:00Z'])
    assert "does not match the sample's values" in message and "yyyy-MM-dd'T'HH:mm:ssX" in message
    assert pattern_regex("yyyy-MM-dd'T'HH:mm:ss") .match('2026-10-01T10:00:00')
    assert check_time_format('epoch_ms', ['1790600000123']) is None


def test_profile_infers_unknown_timestamp_shapes_and_compares_files():
    one = profile('when,who\n01 Oct 2026 10:00:00 GMT,alice\n02 Oct 2026 11:30:00 GMT,bob\n')
    assert {f['field']: f['type'] for f in one['fields']}['when'] == 'timestamp (dd MMM yyyy HH:mm:ss z)'
    case = yaml.safe_load((CASES / '14_csv_two_files_variants.yaml').read_text(encoding='utf-8'))
    both = profile_many({'old.csv': case['samples'][0], 'new.csv': case['samples'][1]})
    assert both['format'] == 'delimited' and both['records'] == 6
    fields = {f['field']: f for f in both['fields']}
    assert fields['device']['only_in'] == ['new.csv'] and fields['time']['only_in'] == ['old.csv']
    assert any("'device' is only in ['new.csv']" in d for d in both['differences'])
    assert 'any_of' in both['hint']


def test_splitter_specs_generate_converters_and_run_on_the_sample():
    syslog = SplitterSpec(kind='syslog', rfc='rfc3164', body=SplitterSpec(kind='key_value', quote='"'))
    xml = generate_splitter(syslog)
    assert etree.fromstring(xml.encode()).tag == '{data-splitter:3}dataSplitter'
    # Pairs by a regex matched along the body, a quoted value without its quotes (a split on '=' has no $2).
    assert '<group value="$6">' in xml and '<data name="$1" value="$2" />' in xml and 'split delimiter="="' not in xml
    run = dry_run(syslog, '<34>Sep 28 10:00:00 host sshd[1]: user=alice action="log in" src=10.0.0.1\nnoise line\n')
    assert run['records'] == [{'pri': '34', 'time': 'Sep 28 10:00:00', 'host': 'host', 'app': 'sshd', 'pid': '1',
                               'message': 'user=alice action="log in" src=10.0.0.1', 'user': 'alice', 'action': 'log in',
                               'src': '10.0.0.1'}]
    assert run['unmatched_lines'] == ['noise line'] and 'action' in run['fields']
    csv = dry_run(SplitterSpec(kind='delimited', header=True, quote='"'), 'time,user,msg\n2026-01-01T00:00:00Z,alice,"a, b"\n')
    assert csv['records'] == [{'time': '2026-01-01T00:00:00Z', 'user': 'alice', 'msg': 'a, b'}]
    named = SplitterSpec(kind='delimited', header=['time', 'user'], delimiter='|')
    assert '<regex pattern="^([^\\|]*)\\|([^\\|]*)$">' in generate_splitter(named)
    assert dry_run(named, 'a|b\nc|d|e\n')['records'] == [{'time': 'a', 'user': 'b'}]
    regex = SplitterSpec(kind='regex', pattern=r'^(\S+) (.*)$', names=['t', 'message'],
                         body=SplitterSpec(kind='regex', pattern=r'^user=(\S+)$', names=['user']))
    assert dry_run(regex, 'x user=bob')['records'] == [{'t': 'x', 'message': 'user=bob', 'user': 'bob'}]
    with pytest.raises(ValueError, match='regex needs pattern and names'):
        SplitterSpec(kind='regex')


def test_the_mapping_is_checked_against_the_sample_before_stepping():
    mapping = TranslationMapping.model_validate({'input': 'json', 'json_layout': 'lines', 'common': [
        {'path': 'EventTime/TimeCreated', 'field': 'ts', 'time_format': 'yyyy-MM-dd HH:mm:ss'},
        {'path': 'EventSource/User/Id', 'field': 'usr'}],
        'events': [{'name': 'a', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'}]}]})
    records, note = sample_records(mapping, '{"ts": "2026-10-01T10:00:00Z", "user": {"name": "a"}}\n{"ts": "2026-10-01T10:00:01Z", "user": {"name": "b"}}')
    check = check_mapping(mapping, records)
    assert check['records'] == 2 and check['fields_seen'] == ['ts', 'user.name']
    assert check['problems'] == ["EventTime/TimeCreated: time_format 'yyyy-MM-dd HH:mm:ss' does not match the sample's "
                                 "values, e.g. ['2026-10-01T10:00:00Z', '2026-10-01T10:00:01Z']; the values look like "
                                 "\"yyyy-MM-dd'T'HH:mm:ssX\""]
    assert check['warnings'] == ["field 'usr' (used for EventSource/User/Id) is in none of the 2 sample records. Fields "
                                 "seen: ['ts', 'user.name']",
                                 "fields in the sample that nothing reads: ['user.name']: map each to the element that "
                                 "means it (a Data entry if nothing else fits), or leave it out on purpose."]
    # XML fragments: paths with attributes and predicates, by local name whatever the namespace
    case = yaml.safe_load((CASES / '12_xml_fragments_events.yaml').read_text(encoding='utf-8'))
    xml_mapping = TranslationMapping.model_validate(case['reference']['mapping'])
    fragments, _ = sample_records(xml_mapping, case['sample'])
    assert len(fragments) == 3
    assert xml_value(fragments[0], 'System/TimeCreated/@SystemTime') == '2026-10-01T10:00:00.000Z'
    assert xml_value(fragments[0], "EventData/Data[@Name='IpAddress']") == '10.0.0.1'
    assert check_mapping(xml_mapping, fragments)['problems'] == []
    # Data Splitter input needs the spec, else the check says it cannot look
    csv_case = yaml.safe_load((CASES / '14_csv_two_files_variants.yaml').read_text(encoding='utf-8'))
    csv_mapping = TranslationMapping.model_validate(csv_case['reference']['mapping'])
    assert sample_records(csv_mapping, csv_case['samples'][0])[1].startswith('no splitter spec')
    records, _ = sample_records(csv_mapping, csv_case['samples'], SplitterSpec.model_validate(csv_case['reference']['splitter']))
    assert check_mapping(csv_mapping, records)['warnings'] == []   # any_of covers both files' names


def test_reference_xslt_writes_maps_and_is_read_back_by_find_reference_data():
    mapping = ReferenceMapping.model_validate({'input': 'data_splitter', 'maps': [
        {'name': 'USER_TO_ORG', 'key': 'user', 'values': [{'element': 'org', 'field': 'org'}, {'element': 'name', 'field': 'name'}]},
        {'name': 'USER_TO_NAME', 'key_xpath': "lower-case(data[@name='user']/@value)", 'values': [{'field': 'name'}]}]})
    result = generate_reference(mapping)
    assert result['ok'] and result['maps'] == ['USER_TO_ORG', 'USER_TO_NAME']
    out = transform(result['xslt'], '<records xmlns="records:2"><record><data name="user" value="Alice"/>'
                                    '<data name="org" value="Ops"/><data name="name" value="Alice A"/></record></records>')
    ref = {'r': 'reference-data:2'}
    first, second = out.findall('r:reference', ref)
    assert first.findtext('r:map', namespaces=ref) == 'USER_TO_ORG' and first.findtext('r:key', namespaces=ref) == 'Alice'
    [details] = first.find('r:value', ref)   # one element is allowed in a value: the parts sit inside it
    assert details.tag == 'details' and [c.tag for c in details] == ['org', 'name']   # no namespace, as lookups read them
    assert second.findtext('r:key', namespaces=ref) == 'alice' and second.findtext('r:value', namespaces=ref) == 'Alice A'
    assert maps_in_xslt(result['xslt']) == [
        {'map': 'USER_TO_ORG', 'key': "data[@name='user']/@value", 'value': ['details', 'name', 'org'], 'ranges': False},
        {'map': 'USER_TO_NAME', 'key': "lower-case(data[@name='user']/@value)", 'value': 'text', 'ranges': False}]
    twice = generate_reference(ReferenceMapping.model_validate({'input': 'json', 'maps': [
        {'name': 'A', 'key': 'k', 'values': [{'field': 'v'}]}, {'name': 'A', 'key': 'k', 'values': [{'field': 'v'}]}]}))
    assert not twice['ok'] and "map 'A' is defined twice" in twice['problems']


MAPPING = {
    'input': 'data_splitter', 'unmatched': 'skip',
    'common': [{'path': 'EventTime/TimeCreated', 'field': 'time'},
               {'path': 'EventSource/System/Name', 'value': 'A'}, {'path': 'EventSource/System/Environment', 'value': 'D'},
               {'path': 'EventSource/Generator', 'value': 'g'},
               {'path': 'EventSource/Device/HostName', 'any_of': ['hostname', 'host'], 'transform': 'lower'},
               {'path': 'EventSource/User/Id', 'field': 'user', 'transform': 'strip_domain'},
               {'path': 'EventSource/User/Domain', 'field': 'user', 'transform': 'domain'},
               {'path': 'EventSource/User/UserDetails/Organisation', 'lookup': {'map': 'USER_TO_ORG', 'field': 'user', 'path': 'org'}},
               {'path': 'EventSource/User/Name', 'field': 'user', 'dictionary': 'User names', 'default': 'unknown'}],
    'events': [{'name': 'vip', 'allow_unknown': 'a VIP list match has no action of its own', 'when': [{'field': 'user', 'in_dictionary': 'VIP users'}],
                'fields': [{'path': 'EventDetail/TypeId', 'value': 'vip'}, {'path': 'EventDetail/Unknown/Data', 'data_name': 'u', 'field': 'user'}]},
               {'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'}, {'path': 'EventDetail/Unknown/Data', 'data_name': 'u', 'field': 'user'}]}]}
RECORDS = """<records xmlns="records:2">
<record><data name="time" value="2026-09-28T10:00:00.000Z"/><data name="user" value="CORP\\alice"/><data name="hostname" value="WS01"/></record>
<record><data name="time" value="2026-09-28T10:01:00.000Z"/><data name="user" value="bob@corp.example"/><data name="host" value="ws02"/></record></records>"""


def test_lookups_dictionaries_any_of_and_transforms():
    result = generate(TranslationMapping.model_validate(MAPPING), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    assert result['reference_maps'] == ['USER_TO_ORG'] and result['dictionaries'] == ['User names', 'VIP users']
    assert any('needs the feed that loads each as a pipeline reference' in w for w in result['warnings'])
    xslt = result['xslt']
    assert "stroom:lookup('USER_TO_ORG', string((data[@name='user']/@value)[1]))//*:org" in xslt
    assert "map:merge(for $line in tokenize(stroom:dictionary('User names'), '\\r?\\n')" in xslt
    assert "tokenize(stroom:dictionary('VIP users'), '\\r?\\n') ! normalize-space(.)" in xslt
    # Stroom's functions only exist in Stroom: stand them in for Saxon.
    stub = (xslt.replace("stroom:lookup('USER_TO_ORG', string((data[@name='user']/@value)[1]))",
                         "parse-xml('&lt;value&gt;&lt;org&gt;Ops&lt;/org&gt;&lt;/value&gt;')/*")
            .replace("stroom:dictionary('User names')", "'CORP\\alice = Alice A&#10;bob=Bob B'")
            .replace("stroom:dictionary('VIP users')", "'CORP\\alice&#10;zed'"))
    events = transform(stub, RECORDS)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    alice, bob = events.findall('e:Event', NS)
    assert alice.findtext('e:EventSource/e:User/e:Id', namespaces=NS) == 'alice'
    assert alice.findtext('e:EventSource/e:User/e:Domain', namespaces=NS) == 'CORP'
    assert alice.findtext('e:EventSource/e:User/e:Name', namespaces=NS) == 'Alice A'
    assert alice.findtext('e:EventSource/e:User/e:UserDetails/e:Organisation', namespaces=NS) == 'Ops'
    assert alice.findtext('e:EventSource/e:Device/e:HostName', namespaces=NS) == 'ws01'
    assert alice.findtext('e:EventDetail/e:TypeId', namespaces=NS) == 'vip'
    assert bob.findtext('e:EventSource/e:User/e:Id', namespaces=NS) == 'bob'
    assert bob.findtext('e:EventSource/e:User/e:Domain', namespaces=NS) == 'corp.example'
    assert bob.findtext('e:EventSource/e:User/e:Name', namespaces=NS) == 'unknown'   # not in the dictionary: default
    assert bob.findtext('e:EventSource/e:Device/e:HostName', namespaces=NS) == 'ws02'
    assert bob.findtext('e:EventDetail/e:TypeId', namespaces=NS) == 'x'


def test_a_directorys_department_looked_up_into_security_groups_is_warned():
    # Eval case 15 on Haiku: the department from a user directory written as EventSource/User/Groups/Group/Name.
    groups = {'path': 'EventSource/User/Groups/Group/Name', 'lookup': {'map': 'USERS', 'field': 'user', 'path': 'department'}}
    result = generate(TranslationMapping.model_validate({**MAPPING, 'common': [*MAPPING['common'], groups]}), SCHEMA, '4.1.0')
    assert result['ok'] and any(w.startswith('EventSource/User/Groups/Group/Name is looked up from USERS (department)')
                                and 'User/UserDetails' in w for w in result['warnings'])
    # And then as the action's Data, in a later run.
    data = {'path': 'EventDetail/Unknown/Data', 'data_name': 'department',
            'lookup': {'map': 'USERS', 'field': 'user', 'path': 'details/department'}}
    other = {**MAPPING['events'][1], 'fields': [*MAPPING['events'][1]['fields'], data]}
    result = generate(TranslationMapping.model_validate({**MAPPING, 'events': [MAPPING['events'][0], other]}), SCHEMA, '4.1.0')
    assert any(w.startswith('EventDetail/Unknown/Data is looked up from USERS (department)') for w in result['warnings'])
    plain = generate(TranslationMapping.model_validate(MAPPING), SCHEMA, '4.1.0')
    assert not any('security groups' in w for w in plain['warnings'])

def test_source_rules_are_enforced_by_the_model():
    with pytest.raises(ValueError, match='dictionary needs field'):
        TranslationMapping.model_validate({**MAPPING, 'common': [{'path': 'EventSource/User/Name', 'value': 'x', 'dictionary': 'd'}]})
    with pytest.raises(ValueError, match='transform applies to an input'):
        TranslationMapping.model_validate({**MAPPING, 'common': [{'path': 'EventSource/User/Name', 'value': 'x', 'transform': 'lower'}]})
    with pytest.raises(ValueError, match='exactly one of field or xpath for the key'):
        TranslationMapping.model_validate({**MAPPING, 'common': [{'path': 'EventSource/User/Name', 'lookup': {'map': 'M'}}]})


def test_a_near_miss_of_a_sample_field_is_a_problem_and_a_field_nothing_like_is_a_warning():
    # Haiku mapped srcip, dstport and msg (FortiOS names) over a CSV with src_ip, dst_port and message.
    from utils.dsgen import SplitterSpec
    csv = 'timestamp,src_ip,dst_port,message\n2026-10-01T09:00:12+10:00,192.0.2.10,443,allowed\n'
    m = TranslationMapping.model_validate({'input': 'data_splitter', 'common': [
        {'path': 'EventTime/TimeCreated', 'field': 'timestamp', 'time_format': "yyyy-MM-dd'T'HH:mm:ssXXX"},
        {'path': 'EventSource/Client/IPAddress', 'field': 'srcip'},
        {'path': 'EventSource/Device/HostName', 'field': 'appliance'}],
        'events': [{'name': 'any', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'}]}]})
    records, _ = sample_records(m, csv, SplitterSpec.model_validate({'kind': 'delimited', 'delimiter': ',', 'header': True}))
    check = check_mapping(m, records)
    [problem] = check['problems']
    assert problem.startswith("field 'srcip' (used for EventSource/Client/IPAddress)") and "did you mean ['src_ip']" in problem
    assert any(w.startswith("field 'appliance'") for w in check['warnings'])   # may be in other data


FIREWALL = ('timestamp,device,event_type,action,username,message\n'
            '2026-10-01T09:00:12+10:00,FW-EDGE-01,TRAFFIC,ALLOW,,Outbound HTTPS allowed\n'
            '2026-10-01T09:11:15+10:00,FW-EDGE-01,ADMIN,LOGIN_SUCCESS,admin,Administrator logged in via web console\n'
            '2026-10-01T09:16:08+10:00,FW-EDGE-01,ADMIN,LOGOUT,admin,Administrator logged out\n'
            '2026-10-01T09:17:24+10:00,FW-EDGE-01,SYSTEM,VPN_TUNNEL_DOWN,,Site-to-site VPN tunnel branch-01 went down\n')


def firewall_mapping(**admin) -> TranslationMapping:
    base = [{'path': 'EventTime/TimeCreated', 'field': 'timestamp', 'time_format': "yyyy-MM-dd'T'HH:mm:ssXXX"},
            {'path': 'EventSource/Device/HostName', 'field': 'device'}, {'path': 'EventDetail/TypeId', 'field': 'action'}]
    unknown = [{'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]
    return TranslationMapping.model_validate({'input': 'data_splitter', 'common': base, 'events': [
        {'name': 'traffic', 'when': [{'field': 'event_type', 'equals': 'TRAFFIC'}],
         'fields': [{'path': 'EventDetail/Network/Permit/Source/Device/HostName', 'field': 'device'}]},
        {'name': 'admin', 'when': [{'field': 'event_type', 'equals': 'ADMIN'}], 'fields': unknown, **admin},
        {'name': 'other', 'fields': unknown}]})


def test_the_records_rules_keep_as_unknown_are_named_by_what_they_hold():
    # Haiku kept a firewall's ADMIN records (logins) and SYSTEM records as Unknown, through allow_unknown: true.
    from utils.localcheck import rule_of
    spec = SplitterSpec.model_validate({'kind': 'delimited', 'delimiter': ',', 'header': True})
    m = firewall_mapping()
    records, _ = sample_records(m, FIREWALL, spec)
    assert [rule_of(m, r) for r in records] == ['traffic', 'admin', 'admin', 'other']
    problems = check_mapping(m, records)['problems']
    admin = next(p for p in problems if p.startswith('[admin]'))
    assert 'keeps EventDetail/Unknown for 2 of the 4 sample records' in admin and 'action: LOGIN_SUCCESS, LOGOUT' in admin
    assert 'username: admin' in admin and 'e.g. message: "Administrator logged in via web console"' in admin
    other = next(p for p in problems if p.startswith('[other]'))
    assert 'the rule for records no other rule matches' in other and 'VPN_TUNNEL_DOWN' in other and 'allow_unknown' in other
    assert 'Authenticate for action LOGIN_SUCCESS; Authenticate for action LOGOUT' in admin     # and the rules to add
    # A reason doesn't keep logons Unknown (seen in VS Code: the agent gave up on the schema and asked the user to
    # agree): refused, with the rules that describe them.
    kept = check_mapping(firewall_mapping(allow_unknown='admin console activity has no action element'), records)
    refused = next(p for p in kept['problems'] if p.startswith('[admin]'))
    assert "can't be kept as Unknown: 2 of its 2 sample records" in refused and '"admin_logon"' in refused
    assert not kept.get('kept_unknown')


def test_data_with_an_element_of_its_own_is_warned():
    # A Delinea SecretServer run (Qwen, VS Code): the source IP carried as Data on every event.
    data = {'path': 'EventDetail/Unknown/Data', 'data_name': 'source_ip', 'field': 'user'}
    other = {**MAPPING['events'][1], 'fields': [*MAPPING['events'][1]['fields'], data]}
    result = generate(TranslationMapping.model_validate({**MAPPING, 'events': [MAPPING['events'][0], other]}), SCHEMA, '4.1.0')
    assert any(w.startswith("[other] Data 'source_ip' looks like EventSource/Client/IPAddress") for w in result['warnings'])
    # Mapped to its element as well: nothing to say.
    client = {'path': 'EventSource/Client/IPAddress', 'field': 'user'}
    result = generate(TranslationMapping.model_validate({**MAPPING, 'common': [*MAPPING['common'], client],
                                                         'events': [MAPPING['events'][0], other]}), SCHEMA, '4.1.0')
    assert not any('looks like' in w for w in result['warnings'])


def test_a_type_id_shared_by_different_kinds_of_event_in_the_sample_is_warned():
    # The SecretServer TypeId was its category ('Secret') for views, creates and deletes alike; a firewall's action
    # (already one value per kind) is fine though its rules test the event type as well.
    from utils.localcheck import shared_type_ids
    def mapping(type_field):
        return TranslationMapping.model_validate({**MAPPING, 'common': [
            *[c for c in MAPPING['common'] if c['path'] != 'EventDetail/TypeId'],
            {'path': 'EventDetail/TypeId', 'field': type_field}], 'events': [
            {'name': 'view', 'when': [{'field': 'category', 'equals': 'Secret'}, {'field': 'action', 'equals': 'View'}],
             'fields': [{'path': 'EventDetail/View/Document/Name', 'field': 'item'}]},
            {'name': 'delete', 'when': [{'field': 'category', 'equals': 'Secret'}, {'field': 'action', 'equals': 'Delete'}],
             'fields': [{'path': 'EventDetail/Delete/Document/Name', 'field': 'item'}]}]})
    records = [{'category': 'Secret', 'action': 'View', 'item': 'a'}, {'category': 'Secret', 'action': 'Delete', 'item': 'b'}]
    warned = shared_type_ids(mapping('category'), records)
    assert warned and "'Secret' for rules delete, view" in warned[0]
    assert shared_type_ids(mapping('action'), records) == []
