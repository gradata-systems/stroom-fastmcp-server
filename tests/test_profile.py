import json

from utils.profile import profile, value_type


def fields(result):
    return {f['field']: f for f in result['fields']}


def test_csv_with_header():
    result = profile("time,user,src_ip,result\n2026-09-28T10:00:00Z,alice,10.0.0.1,ok\n"
                     "2026-09-28T10:05:00Z,bob,10.0.0.2,fail\n")
    assert (result['format'], result['delimiter'], result['has_header']) == ('delimited', ',', True)
    f = fields(result)
    assert f['time']['type'] == "timestamp (yyyy-MM-dd'T'HH:mm:ssX)"
    assert f['src_ip']['type'] == 'ip'


def test_json_lines_flatten_and_flag_embedded_json():
    lines = [json.dumps({'ts': 1790600000, 'user': {'name': 'a'}, 'body': 'INFO x {"type":"LOGIN"}'}),
             json.dumps({'ts': 1790600001, 'user': {'name': 'b'}})]
    result = profile('\n'.join(lines))
    f = fields(result)
    assert result['format'] == 'json lines'
    assert f['user.name']['fill_rate'] == 100
    assert f['body']['type'] == 'embedded json after prefix' and f['body']['fill_rate'] == 50


def test_xml_records():
    result = profile('<logs><entry><when>2026-09-28 10:00:00</when><who>alice</who></entry>'
                     '<entry><when>2026-09-28 10:01:00</when><who>bob</who></entry></logs>')
    assert (result['format'], result['record_element'], result['records']) == ('xml', 'entry', 2)
    assert fields(result)['when']['type'] == 'timestamp (yyyy-MM-dd HH:mm:ss)'


def test_syslog_and_key_value():
    rfc5424 = '<34>1 2026-09-28T10:00:00Z host app 123 ID47 - message\n' * 3
    assert profile(rfc5424)['format'] == 'syslog rfc5424'
    rfc3164 = '<34>Sep 28 10:00:00 host sshd[1]: Accepted password\n' * 3
    assert profile(rfc3164)['format'] == 'syslog rfc3164'
    kv = 'date=2026-09-28 time=10:00:00 srcip=10.0.0.1 action=accept\n' * 3
    assert fields(profile(kv))['srcip']['type'] == 'ip'


def test_value_types():
    assert value_type('1790600000') == 'timestamp (epoch seconds)'
    assert value_type('28/Sep/2026:10:00:00 +0000') == 'timestamp (dd/MMM/yyyy:HH:mm:ss Z)'
    assert value_type('{"a": 1}') == 'embedded json'
    assert value_type('') == 'empty'
