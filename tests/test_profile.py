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


def test_text_that_starts_as_json_and_does_not_parse_is_said_so():
    # Seen: a FortiOS JSON file retyped by the agent with \+ in it, profiled as key=value from its records' body.
    import pytest
    from fastmcp.exceptions import ToolError
    retyped = r'[{"host": "gs-fw01", "body": "eventtime=1 tz=\"\+1000\" srcip=10.0.0.1"}]'
    with pytest.raises(ToolError, match=r"starts as JSON but isn't valid JSON: Invalid \\escape"):
        profile(retyped)
    assert profile(retyped.replace('\\+', '+'))['format'] == 'json array'
    # Text that only starts with a bracket is not taken for JSON.
    assert profile('[2026-10-01 08:00:00] INFO started\n[2026-10-01 08:00:01] INFO ok\n')['format'] != 'json array'


# A big single-document stream read only to its head (seen in production: a 54 MB <tweets> stream, read to 1 MB, was
# profiled as CSV with no records, and the plan then asked for an XMLFragmentParser).
TWEETS_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<tweets>'
               + ''.join(f'<tweet><data name="id" value="{n}" /><data name="text" value="t {n}" />'
                         f'<data name="user_mentions"><data><data name="screen_name" value="u{n}" /></data></data></tweet>'
                         for n in range(30))
               + '<tweet><data name="id" value="30" /><data na')


def test_a_document_read_only_to_its_head_is_xml_with_its_complete_records():
    from utils.profile import cut_document
    found = profile(TWEETS_HEAD)
    assert (found['format'], found['root'], found['record_element'], found['records']) == ('xml', 'tweets', 'tweet', 30)
    assert 'read only to here' in found['note']
    assert profile('<tweet><data name="id" value="1" /></tweet>\n<tweet><data name="id" value="2" /></tweet>\n')['format'] \
        == 'xml fragments'
    # A whole document, fragments, a syntax error: not a document cut short.
    assert cut_document('<r><a/></r>') is None and cut_document('<a/><a/>') is None and cut_document('<r><a/><b x=1>') is None


def test_a_document_read_only_to_its_head_drafts_and_checks_from_its_records():
    from utils.draftmap import draft_mapping
    from utils.localcheck import sample_records
    from utils.xsltgen import TranslationMapping
    draft = draft_mapping([TWEETS_HEAD], 'Twitter')
    assert draft['mapping']['input'] == 'xml' and draft['mapping']['record'] == 'tweet'
    mapping = TranslationMapping.model_validate({**draft['mapping'], 'input': 'xml', 'record': 'tweet'})
    records, note = sample_records(mapping, TWEETS_HEAD)
    assert len(records) == 30 and note is None
