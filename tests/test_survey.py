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
