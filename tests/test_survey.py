import json

from utils.survey import Shapes, mask, sample_text, split_records

SSH = """<38>1 2026-09-28T13:00:00.000Z bastion sshd 812 - - Accepted publickey for frank from 192.0.2.44 port 50122 ssh2
<38>1 2026-09-28T13:02:10.000Z bastion sshd 815 - - Failed password for invalid user admin from 192.0.2.99 port 40022 ssh2
<38>1 2026-09-28T13:04:55.000Z bastion sshd 820 - - Accepted password for grace from 192.0.2.45 port 50410 ssh2
<38>1 2026-09-28T13:05:00.000Z bastion sshd 821 - - Accepted password for henry from 192.0.2.46 port 50411 ssh2"""


def shapes_of(text: str, known=None) -> tuple[Shapes, object]:
    chunk = split_records(text)
    shapes = Shapes(known)
    for i in range(len(chunk.records)):
        shapes.add(chunk, i, 1)
    return shapes, chunk


def test_syslog_messages_group_by_template_across_users():
    shapes, chunk = shapes_of(SSH)
    assert chunk.format == 'syslog rfc5424'
    counts = {k.split('|')[1]: v['count'] for k, v in shapes.shapes.items()}
    assert counts == {'Accepted <*> for <*> from <ip> port <n> <n>': 3,
                      'Failed password for invalid user admin from <ip> port <n> <n>': 1}


def test_masking_hides_variable_parts():
    assert mask('user "Bob Smith" from 10.1.2.3:443 took 250ms id 0x1f2e3d4c5b6a7980') == \
        ['user', '<s>', 'from', '<ip>', 'took', '<n>', 'id', '<hex>']


def test_json_groups_by_fields_and_naming_values():
    text = json.dumps([{'ts': 't', 'user': 'a', 'action': 'login'}, {'ts': 't', 'user': 'b', 'action': 'login'},
                       {'ts': 't', 'user': 'a', 'action': 'logout'}, {'ts': 't', 'path': '/x', 'action': 'read'}])
    shapes, chunk = shapes_of(text)
    assert chunk.format == 'json array' and len(shapes.shapes) == 3
    assert shapes.shapes['fields:action,ts,user | action=login']['count'] == 2


def test_delimited_groups_by_naming_or_low_variety_columns():
    named = 'time,user,action\n' + '\n'.join(f'2026-09-28 08:0{i}:00,u{i},{a}' for i, a in enumerate('ab' * 4))
    shapes, chunk = shapes_of(named)
    assert chunk.naming_columns == ['action'] and set(shapes.shapes) == {'row:action=a', 'row:action=b'}
    rows = ['2026-09-28T16:0%d:00Z,user%d,%s,/src,/dst' % (i, i, s) for i, s in enumerate(['usb', 'net'] * 5)]
    shapes, chunk = shapes_of('\n'.join(rows))
    assert chunk.header is None and chunk.naming_columns and len(shapes.shapes) == 2


def test_xml_and_key_value_records():
    xml = ('<audit xmlns="urn:x"><entry><op>read</op><path>/a</path></entry><entry><op>delete</op><path>/b</path>'
           '</entry><entry><op>read</op><path>/c</path></entry></audit>')
    shapes, chunk = shapes_of(xml)
    assert chunk.format == 'xml' and len(shapes.shapes) == 2 and 'xmlns' not in chunk.records[0]
    assert sample_text(chunk, chunk.records[:1]).startswith('<audit xmlns="urn:x">\n<entry>')
    kv = '\n'.join(f'2026-09-28T15:00:0{i}Z fw01 action={a} src=10.0.0.{i} dst=8.8.8.8 dport=53'
                   for i, a in enumerate(['allow', 'deny', 'allow']))
    shapes, chunk = shapes_of(kv)
    assert chunk.format == 'key=value' and len(shapes.shapes) == 2


def test_known_shapes_are_recognised_and_samples_keep_the_format():
    first, chunk = shapes_of(SSH)
    known = list(first.shapes)
    again, _ = shapes_of(SSH, known)
    assert all(s['known'] for s in again.shapes.values())
    header = 'time,user,action\n2026-09-28 08:00:00,u1,login\n2026-09-28 08:01:00,u2,logout\n'
    _, csv_chunk = shapes_of(header)
    assert sample_text(csv_chunk, csv_chunk.records[1:]) == 'time,user,action\n2026-09-28 08:01:00,u2,logout\n'
    _, json_chunk = shapes_of(json.dumps([{'a': 1}, {'a': 2}]))
    assert json.loads(sample_text(json_chunk, json_chunk.records)) == [{'a': 1}, {'a': 2}]


def test_cut_off_heads_keep_only_complete_records():
    array = json.dumps([{'n': i, 'action': 'x'} for i in range(50)])
    chunk = split_records(array[:500], truncated=True)
    assert chunk.format == 'json array' and 0 < len(chunk.records) < 50
    assert all(json.loads(r) for r in chunk.records)
    xml = '<audit>' + ''.join(f'<entry><op>read</op><n>{i}</n></entry>' for i in range(50)) + '</audit>'
    chunk = split_records(xml[:600], truncated=True)
    assert chunk.format == 'xml' and chunk.records[-1].endswith('</entry>') and len(chunk.records) < 50
    lines = ''.join(f'2026-09-28T15:00:0{i % 10}Z fw action=allow src=10.0.0.{i} dport=53\n' for i in range(30))
    chunk = split_records(lines[:700], truncated=True)
    assert all(r.endswith('dport=53') for r in chunk.records)


def test_spread_order_covers_the_range_early():
    from tools.sampling import spread_order
    assert spread_order(5) == [4, 0, 2, 1, 3] and sorted(spread_order(10)) == list(range(10))
    assert spread_order(10)[:3] == [9, 0, 4]


def test_survey_doc_round_trips_and_merges():
    from utils.surveydoc import merge, read_state, render
    first_shapes = {'fields:action | action=login': {'count': 5, 'streams': [4], 'examples': [
        {'text': '{"action": "login", "user": "a|b"}', 'location': {'stream': 4, 'part': 0, 'record': 0}}]}}
    result = {'feed': 'SRC', 'format': 'json array', 'streams_read': [4, 1], 'records_read': 5, 'saturated': False,
              'new_shapes': 1, 'time_range': {'oldest': 't0', 'newest': 't1'}}
    state = merge(None, result, first_shapes, 2)
    markdown = render(state)
    assert '| 1 | fields:action \| action=login | 5 | 100.0% | 1 |' in markdown
    assert 'Stream 4, part 0, record 0:' in markdown and '{"action": "login", "user": "a|b"}' in markdown
    again = read_state(markdown)
    assert again == state
    more = {'fields:action | action=login': {'count': 2, 'streams': [2], 'examples': [
                {'text': '{"action": "login", "user": "c"}', 'location': {'stream': 2, 'part': 0, 'record': 1}}]},
            'fields:action | action=logout': {'count': 1, 'streams': [2], 'examples': [
                {'text': '{"action": "logout"}', 'location': {'stream': 2, 'part': 0, 'record': 2}}]}}
    merged = merge(again, {**result, 'streams_read': [2], 'records_read': 3, 'saturated': True}, more, 2)
    login = merged['shapes']['fields:action | action=login']
    assert login['count'] == 7 and login['streams'] == [2, 4] and len(login['examples']) == 2
    assert merged['streams_read'] == [1, 2, 4] and merged['saturated'] and len(merged['surveys']) == 2
    assert 'the feed looks covered' in render(merged)
    assert read_state('no state here') is None
