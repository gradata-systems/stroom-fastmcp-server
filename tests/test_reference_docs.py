"""Long reference documents read back a passage at a time."""
from tools.explorer import DOC_CHARS, _passages


def test_a_long_document_is_read_a_passage_at_a_time():
    lines = [f"## Event {n}" if n % 10 == 0 else f"line {n}" for n in range(20_000)]
    lines[12_345] = 'Event 4625: an account failed to log on (status 0xC000006A: bad password)'
    doc = {'type': 'Documentation', 'uuid': 'd', 'name': 'Acme reference - events', 'data': '\n'.join(lines)}
    whole = _passages(doc, None, 8)
    assert len(whole['data']) == DOC_CHARS and 'Cut short' in whole['note'] and whole['outline'][:2] == ['## Event 0', '## Event 10']
    found = _passages(doc, '0xc000006a', 3)            # case-insensitive
    assert found['matches'] == 1 and found['passages'][0]['lines'] == '12343-12349'
    assert '0xC000006A' in found['passages'][0]['text'] and 'data' not in found
    assert 'Nothing mentions it' in _passages(doc, 'no such code', 3)['note']
    short = {'type': 'Documentation', 'uuid': 'd', 'name': 'n', 'data': 'small'}
    assert _passages(short, None, 8)['data'] == 'small'
