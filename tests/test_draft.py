"""A starting mapping drafted from the sample, and the draft handed back when a wrong mapping arrives."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_xsltgen import SCHEMA, VALIDATOR, transform
from tools import generation
from utils.draftmap import draft_mapping
from utils.xsltgen import TranslationMapping, generate

FW_JSON = ('{"timestamp":"2026-10-01T09:00:12+10:00","device":"FW-EDGE-01","event_type":"TRAFFIC","severity":"INFO",'
           '"src_ip":"192.0.2.10","src_port":54321,"dst_ip":"198.51.100.20","dst_port":443,"protocol":"TCP","action":"ALLOW",'
           '"rule_id":1001,"bytes_sent":1520,"bytes_received":8450,"username":"","message":"Outbound HTTPS connection allowed"}\n'
           '{"timestamp":"2026-10-01T09:01:00+10:00","device":"FW-EDGE-01","event_type":"LOGIN","severity":"INFO",'
           '"src_ip":"192.0.2.11","src_port":1,"dst_ip":"198.51.100.1","dst_port":22,"protocol":"TCP","action":"ALLOW",'
           '"rule_id":7,"bytes_sent":0,"bytes_received":0,"username":"alice","message":"Admin login"}\n')
INVENTORY = [{'field': 'timestamp', 'type': 'timestamp'}, {'field': 'device', 'type': 'string'}, {'field': 'src_ip', 'type': 'ip'}]


def test_the_draft_is_a_valid_mapping_with_the_obvious_homes_and_a_rule_per_kind():
    draft = draft_mapping({'fw.jsonl': FW_JSON}, 'FortiOS firewall')
    m = draft['mapping']
    assert (m['input'], m['json_layout']) == ('json', 'lines')
    by_path = {e['path']: e for e in m['common']}
    assert by_path['EventTime/TimeCreated'] == {'path': 'EventTime/TimeCreated', 'field': 'timestamp', 'time_format': "yyyy-MM-dd'T'HH:mm:ssXXX"}
    assert by_path['EventSource/Device/HostName']['field'] == 'device'
    assert by_path['EventSource/Client/IPAddress']['field'] == 'src_ip' and by_path['EventSource/Client/Port']['field'] == 'src_port'
    assert by_path['EventSource/Server/IPAddress']['field'] == 'dst_ip' and by_path['EventSource/Server/Port']['field'] == 'dst_port'
    assert by_path['EventSource/User/Id']['field'] == 'username'
    assert by_path['EventDetail/TypeId']['field'] == 'event_type' and by_path['EventDetail/Description']['field'] == 'message'
    assert by_path['EventSource/System/Name']['value'] == 'FortiOS firewall'
    assert draft['kinds'] == ['TRAFFIC', 'LOGIN'] and [r['name'] for r in m['events']] == ['traffic', 'login', 'other']
    login = m['events'][1]
    assert {'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'} in login['fields']
    assert {'path': 'EventDetail/Authenticate/User/Id', 'field': 'username'} in login['fields']
    assert any(f.get('data_name') == 'protocol' for f in m['events'][0]['fields'])   # unmapped fields ride as Data
    assert any('replace EventDetail/Unknown' in n for n in draft['notes']) and any('Environment' in n for n in draft['notes'])
    # It generates, validates against the schema, and the events come out right.
    result = generate(TranslationMapping.model_validate({**m, 'unmatched': 'skip'}), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    xml = ('<map xmlns="http://www.w3.org/2013/XSL/json"><map><string key="timestamp">2026-10-01T09:01:00.000Z</string>'
           '<string key="device">FW</string><string key="event_type">LOGIN</string><string key="username">alice</string>'
           '<string key="src_ip">192.0.2.11</string><string key="protocol">TCP</string></map></map>')
    events = transform(result['xslt'].replace("stroom:format-date(", "string(").replace(", 'yyyy-MM-dd''T''HH:mm:ssXXX')", ')'), xml)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    assert events.findtext('.//{event-logging:3}Authenticate/{event-logging:3}User/{event-logging:3}Id') == 'alice'


def test_text_formats_come_with_their_splitter_and_naive_times_get_a_zone_note():
    kv = ('date=2026-10-01 time=10:00:00 devname="fw01" srcip=10.0.0.1 dstip=203.0.113.5 dstport=443 action="accept" logid=0000000013\n'
          'date=2026-10-01 time=10:00:05 devname="fw01" srcip=10.0.0.2 dstip=203.0.113.6 dstport=80 action="deny" logid=0000000013\n')
    draft = draft_mapping([kv], 'FortiOS')
    assert draft['mapping']['input'] == 'data_splitter' and draft['splitter']['kind'] == 'key_value'
    by_path = {e['path']: e for e in draft['mapping']['common']}
    assert by_path['EventSource/Device/HostName']['field'] == 'devname'
    assert by_path['EventSource/Client/IPAddress']['field'] == 'srcip' and by_path['EventSource/Server/Port']['field'] == 'dstport'
    assert by_path['EventDetail/TypeId']['field'] == 'action' and draft['kinds'] == ['accept', 'deny']
    # FortiOS splits date= and time=: the draft joins them, read as UTC until the user says otherwise.
    assert by_path['EventTime/TimeCreated'] == {'path': 'EventTime/TimeCreated', 'timezone': 'UTC', 'time_format': 'yyyy-MM-dd HH:mm:ss',
                                                'xpath': "concat(data[@name='date']/@value, ' ', data[@name='time']/@value)"}
    assert any("'date' and 'time' are joined" in n for n in draft['notes'])
    assert generate(TranslationMapping.model_validate(draft['mapping']), SCHEMA, '4.1.0')['ok']


async def test_a_field_inventory_sent_as_the_mapping_gets_the_draft_back():
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))})
    with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)), \
            patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})):
        result = await generation.build_translation_xslt(ctx, INVENTORY, sample=FW_JSON)
        assert result['status'] == 'needs_mapping' and result['problems'][0] == 'mapping is a list of fields, not a translation mapping'
        assert result['draft_mapping']['input'] == 'json' and 'Edit draft_mapping' in result['hint']
        # The draft, sent back as the mapping, generates.
        again = await generation.build_translation_xslt(ctx, result['draft_mapping'], sample=FW_JSON)
        assert again['ok'] and again['xslt']
        drafted = await generation.draft_translation_mapping(ctx, {'fw.jsonl': FW_JSON}, 'FortiOS firewall', environment='Prod')
        assert drafted['schema_check']['ok'] and drafted['mapping']['common'][2]['value'] == 'Prod'
