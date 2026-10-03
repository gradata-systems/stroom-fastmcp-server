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
    assert '<group value="$6">' in xml and 'containerStart="&quot;"' in xml
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
    'events': [{'name': 'vip', 'allow_unknown': True, 'when': [{'field': 'user', 'in_dictionary': 'VIP users'}],
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


def test_source_rules_are_enforced_by_the_model():
    with pytest.raises(ValueError, match='dictionary needs field'):
        TranslationMapping.model_validate({**MAPPING, 'common': [{'path': 'EventSource/User/Name', 'value': 'x', 'dictionary': 'd'}]})
    with pytest.raises(ValueError, match='transform applies to an input'):
        TranslationMapping.model_validate({**MAPPING, 'common': [{'path': 'EventSource/User/Name', 'value': 'x', 'transform': 'lower'}]})
    with pytest.raises(ValueError, match='exactly one of field or xpath for the key'):
        TranslationMapping.model_validate({**MAPPING, 'common': [{'path': 'EventSource/User/Name', 'lookup': {'map': 'M'}}]})
