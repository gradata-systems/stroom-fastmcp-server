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


def test_a_json_array_read_only_in_part_is_still_a_json_array():
    # Seen: a 755 KB one-line array read to 20,000 characters profiled as key=value (its records' body field), and
    # create_pipeline refused the JSON parser the stream needed.
    import json
    from utils.profile import profile
    records = [{'timestamp': f'2026-10-02T09:00:{i % 60:02d}Z', 'hostname': 'gs-fw01',
                'body': f'eventtime={i} type="traffic" action="deny" srcip=10.0.0.{i % 250} dstport=443'}
               for i in range(400)]
    text = json.dumps(records)
    cut = profile(text[:20_000])
    assert cut['format'] == 'json array' and 0 < cut['records'] < 400 and 'cut short' in cut['note']
    assert 'JSONParser' in cut['suggested_parser']
    assert profile(text)['records'] == 400 and 'note' not in profile(text)
    lines = '\n'.join(json.dumps(r) for r in records[:5])
    assert profile(lines[:-10])['format'] == 'json lines'      # the last line cut short
