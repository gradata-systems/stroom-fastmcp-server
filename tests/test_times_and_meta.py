"""Times as sources write them, and values from stream meta. Each from testing in production: fractions of a second
dropped, zones not kept, and a sending host's name (MyHostName) not readable without hand-written XPath."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from utils.draftmap import draft_mapping
from utils.eventschema import EventSchema
from utils.profile import profile
from utils.timefmt import check_time_format, infer_time_pattern
from utils.xsltgen import FieldMapping, TranslationMapping, generate

SCHEMA = EventSchema.parse((Path(__file__).parent / 'fixtures' / 'event-logging-v4.1.0.xsd').read_bytes())


def time_entry(text: str) -> dict:
    draft = draft_mapping(text)
    mapping = draft.get('mapping', draft)
    return next(e for e in mapping['common'] if e['path'] == 'EventTime/TimeCreated'), mapping


def test_epoch_seconds_with_a_fraction_are_a_time_whatever_the_source():
    # Seen with a Bluecoat log: 1791712800.123 was a plain decimal, so the time was mapped by hand without its
    # milliseconds. Text (a W3C column) or a JSON number alike.
    assert infer_time_pattern('1791712800.123') == 'epoch seconds'
    assert infer_time_pattern('1791712800.123456') == 'epoch seconds'
    assert check_time_format('epoch seconds', ['1791712800.123']) is None
    fields = {f['field']: f['type'] for f in profile('{"ts": 1791712800.123, "n": 42}\n{"ts": 1791712801, "n": 7}\n')['fields']}
    assert fields == {'ts': 'timestamp (epoch seconds)', 'n': 'number'}


def test_the_more_precise_time_is_drafted_and_a_clocks_fraction_kept():
    w3c = ('#Software: SGOS 7.3\n#Fields: date time x-timestamp-unix c-ip cs-username sc-status\n'
           '2026-10-11 08:00:00 1791712800.123 10.0.0.1 alice 200\n2026-10-11 08:00:01 1791712801.456 10.0.0.2 bob 403\n')
    entry, _ = time_entry(w3c)
    assert entry == {'path': 'EventTime/TimeCreated', 'field': 'x-timestamp-unix', 'time_format': 'epoch_s'}
    entry, _ = time_entry('date,time,user,action\n2026-10-11,08:00:00.123,alice,login\n2026-10-11,08:00:01.456,bob,logout\n')
    assert entry['time_format'] == 'yyyy-MM-dd HH:mm:ss.SSS' and 'concat(' in entry['xpath']
    # Without fractions anywhere, the date and time columns are joined as before.
    entry, _ = time_entry('date,time,user\n2026-10-11,08:00:00,alice\n2026-10-11,08:00:01,bob\n')
    assert entry['time_format'] == 'yyyy-MM-dd HH:mm:ss'


@pytest.mark.parametrize('value, pattern', [
    ('2026-10-01T18:00:00.123+10:00', "yyyy-MM-dd'T'HH:mm:ss.SSSXXX"),
    ('2026-10-01T08:00:00.123Z', "yyyy-MM-dd'T'HH:mm:ss.SSSX"),
    ('2026-10-01 18:00:00.123 +1000', 'yyyy-MM-dd HH:mm:ss.SSS Z'),
    ('2026-10-01 18:00:00 AEST', 'yyyy-MM-dd HH:mm:ss z'),
    ('2026-10-01T18:00:00.123 Australia/Sydney', "yyyy-MM-dd'T'HH:mm:ss.SSS VV"),
    ('2026-10-01T18:00:00.123+10:00[Australia/Sydney]', "yyyy-MM-dd'T'HH:mm:ss.SSSXXX'['VV']'"),
])
def test_offsets_and_zones_are_read_into_the_pattern_so_the_time_converts_to_utc(value, pattern):
    assert infer_time_pattern(value) == pattern and check_time_format(pattern, [value]) is None


def test_a_zone_the_source_names_is_kept_with_the_event():
    entry, mapping = time_entry('{"ts": "2026-10-01T18:00:00.123 Australia/Sydney", "user": "alice"}\n'
                                '{"ts": "2026-10-01T18:01:00.456 Australia/Sydney", "user": "bob"}\n')
    assert entry['time_format'].endswith(' VV') and 'timezone' not in entry
    assert {'path': 'EventSource/Device/Location/TimeZoneName', 'field': 'time_zone'} in mapping['common']
    assert mapping['extract'][0]['names'] == ['time_zone'] and mapping['extract'][0]['field'] == 'ts'
    # A field of zone names, beside zone-less times.
    _, mapping = time_entry('time,tz,user\n2026-10-01 18:00:00,Australia/Sydney,alice\n2026-10-01 18:01:00,Europe/London,bob\n')
    assert {'path': 'EventSource/Device/Location/TimeZoneName', 'field': 'tz'} in mapping['common']


def test_a_time_read_in_a_named_zone_the_event_doesnt_keep_is_warned_about():
    from tools.generation import zone_not_kept
    base = {'input': 'json', 'common': [{'path': 'EventTime/TimeCreated', 'field': 'ts', 'time_format': 'yyyy-MM-dd HH:mm:ss',
                                         'timezone': 'Australia/Sydney'}],
            'events': [{'name': 'x', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'}]}]}
    assert 'EventSource/Device/Location/TimeZoneName' in zone_not_kept(TranslationMapping.model_validate(base))[0]
    kept = {**base, 'common': base['common'] + [{'path': 'EventSource/Device/Location/TimeZoneName', 'value': 'Australia/Sydney'}]}
    assert zone_not_kept(TranslationMapping.model_validate(kept)) == []
    utc = {**base, 'common': [{**base['common'][0], 'timezone': 'UTC'}]}
    assert zone_not_kept(TranslationMapping.model_validate(utc)) == []


def test_a_value_from_stream_meta_is_an_input_of_its_own():
    # Asked for by the user: the sending host (MyHostName) into EventSource/Device/HostName, without hand-written XPath.
    entry = FieldMapping.model_validate({'path': 'EventSource/Device/HostName', 'meta': 'MyHostName'})
    assert entry.xpath == "stroom:meta('MyHostName')" and entry.meta is None
    with pytest.raises(ValueError, match='only input'):
        FieldMapping.model_validate({'path': 'EventSource/Device/HostName', 'meta': 'MyHostName', 'field': 'host'})
    mapping = TranslationMapping.model_validate({
        'input': 'json', 'json_layout': 'lines',
        'common': [{'path': 'EventTime/TimeCreated', 'field': 'ts', 'time_format': "yyyy-MM-dd'T'HH:mm:ssX"},
                   {'path': 'EventSource/System/Name', 'value': 'X'}, {'path': 'EventSource/System/Environment', 'value': 'T'},
                   {'path': 'EventSource/Generator', 'value': 'g'},
                   {'path': 'EventSource/Device/HostName', 'meta': 'MyHostName'},
                   {'path': 'EventDetail/TypeId', 'value': 'x'}],
        'events': [{'name': 'a', 'fields': [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                                            {'path': 'EventDetail/Authenticate/User/Id', 'field': 'u'}]}]})
    xslt = generate(mapping, SCHEMA, '4.1.0')['xslt']
    assert "stroom:meta('MyHostName')" in xslt


async def test_stream_meta_no_sample_stream_carries_is_warned_about():
    from tools import generation
    mapping = TranslationMapping.model_validate({
        'input': 'json', 'common': [{'path': 'EventSource/Device/HostName', 'meta': 'MyHostName'},
                                    {'path': 'EventSource/Device/IPAddress', 'meta': 'RemoteAddress'}],
        'events': [{'name': 'x', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'}]}]})
    attrs = AsyncMock(return_value={'attributes': {'Feed': 'ACME', 'RemoteAddress': '10.0.0.1'}, 'stream': {}})
    with patch('tools.streams.get_stream_attributes', attrs):
        said = await generation._meta_not_sent(SimpleNamespace(), mapping, [7])
    assert len(said) == 1 and "stroom:meta('MyHostName') is not among the sample streams' attributes" in said[0]
    assert 'remoteaddress' in said[0]


def test_epoch_microseconds_and_nanoseconds_are_times_too():
    # Asked for by the user: microseconds, rounded to the event-logging schema's milliseconds.
    assert infer_time_pattern('1790841600123456') == 'epoch microseconds'
    assert infer_time_pattern('1790841600123456789') == 'epoch nanoseconds'
    entry, _ = time_entry('{"ts": 1790841600123456, "u": "a"}\n{"ts": 1790841900456789, "u": "b"}\n')
    assert entry['time_format'] == 'epoch_us'
    assert check_time_format('epoch_us', ['1790841600123456']) is None
    assert check_time_format('epoch_ns', ['1790841600123']) is not None


def test_finer_than_milliseconds_is_rounded_not_truncated():
    # Seen on the local stack: stroom:format-date truncates (.456789 -> .456, 08:07:00.9996 -> 08:07:00.999), and the
    # schema takes exactly three digits.
    from utils.xsltgen import time_expr
    assert time_expr('epoch_s', None, 'x') == 'stroom:format-date(string(xs:integer(round(xs:decimal(x) * 1000))))'
    assert time_expr('epoch_us', None, 'x') == 'stroom:format-date(string(xs:integer(round(xs:decimal(x) div 1000))))'
    assert time_expr("yyyy-MM-dd'T'HH:mm:ss.SSSSSSX", None, 'x') == \
        "mcp:time-ms(string(x), 'yyyy-MM-dd''T''HH:mm:ss.SSSSSSX', '')"
    assert time_expr("yyyy-MM-dd'T'HH:mm:ss.SSSX", None, 'x') == "stroom:format-date(x, 'yyyy-MM-dd''T''HH:mm:ss.SSSX')"
    entry, mapping = time_entry('{"ts": "2026-10-01T08:00:00.123456Z", "u": "a"}\n'
                                '{"ts": "2026-10-01T08:07:00.999600Z", "u": "b"}\n')
    assert entry['time_format'].endswith('.SSSSSSX')
    whole = TranslationMapping.model_validate({
        'input': 'json', 'json_layout': 'lines',
        'common': [entry, {'path': 'EventSource/System/Name', 'value': 'X'},
                   {'path': 'EventSource/System/Environment', 'value': 'T'}, {'path': 'EventSource/Generator', 'value': 'g'},
                   {'path': 'EventSource/Device/HostName', 'value': 'h'}, {'path': 'EventDetail/TypeId', 'value': 'x'}],
        'events': [{'name': 'a', 'fields': [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                                            {'path': 'EventDetail/Authenticate/User/Id', 'field': 'u'}]}]})
    result = generate(whole, SCHEMA, '4.1.0')
    assert 'name="mcp:time-ms"' in result['xslt'] and "mcp:time-ms(string(" in result['xslt'], result.get('problems')


def test_the_fractions_fourth_digit_is_the_one_rounded_on():
    import re
    from utils.xsltgen import FRACTION_DIGIT
    digit = lambda v: re.sub(FRACTION_DIGIT, r'\1', v) if re.match(FRACTION_DIGIT, v) else None
    assert digit('2026-10-01T08:00:00.123456Z') == '4'
    assert digit('2026-10-01T08:07:00.999600+10:00') == '6'
    assert digit('01.10.2026 08:00:00,4567') == '7'
    assert digit('2026-10-01T08:00:00.123Z') is None            # milliseconds already: nothing to round


def test_a_translation_read_back_keeps_its_time_formats():
    # The reader takes generated XSLTs back to their mapping: the rounded forms, and epoch_s as it was written before.
    from utils.xsltread import SHAPES, unify, shape
    assert unify(SHAPES['epoch_s, truncated'],
                 shape("stroom:format-date(string(xs:integer(xs:decimal(data[@name='ts']/@value[1]) * 1000)))"))
    assert unify(SHAPES['time, ms'], shape("mcp:time-ms(string(data[@name='ts']/@value[1]), 'yyyy SSSSSS', 'UTC')"))
