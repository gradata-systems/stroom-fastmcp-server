"""A starting mapping drafted from the sample, and the draft handed back when a wrong mapping arrives."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_xsltgen import SCHEMA, SCHEMA_352, VALIDATOR, transform
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


def decided(mapping: dict) -> dict:
    """The draft as an agent leaves it once its placeholder kinds are decided; here, kept as Unknown on purpose."""
    return {**mapping, 'events': [{**r, 'allow_unknown': 'kept unknown for the test'}
                                  if any('/Unknown/' in f['path'] for f in r.get('fields', [])) else r
                                  for r in mapping['events']]}


def only_placeholders(result: dict) -> bool:
    """The draft's only problems are its Unknown placeholders, a rule with conditions each."""
    # The generator's, and with a sample the records each placeholder catches.
    return bool(result['problems']) and all(('writes EventDetail/Unknown' in p or 'keeps EventDetail/Unknown' in p)
                                            for p in result['problems'])


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
    # TRAFFIC's records are connections allowed between addresses: Network/Permit, not an Unknown placeholder.
    assert draft['kinds'] == ['TRAFFIC', 'LOGIN'] and [r['name'] for r in m['events']] == ['traffic_permitted', 'login', 'other']
    assert m['events'][0]['when'] == [{'field': 'event_type', 'equals': 'TRAFFIC'}, {'field': 'action', 'equals': 'ALLOW'}]
    assert {'path': 'EventDetail/Network/Permit/Destination/Port', 'field': 'dst_port'} in m['events'][0]['fields']
    login = m['events'][1]
    assert {'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'} in login['fields']
    assert {'path': 'EventDetail/Authenticate/User/Id', 'field': 'username'} in login['fields']
    assert {'path': 'EventDetail/Network/Permit/Data', 'data_name': 'rule_id', 'field': 'rule_id'} in m['events'][0]['fields']
    assert any(n.startswith("Drafted from the sample's values") and 'Network/Permit for action ALLOW' in n for n in draft['notes'])
    # Every kind has its action element: the note says so, rather than asking for Unknown placeholders to be replaced.
    assert any('each with its action element' in n for n in draft['notes']) and any('Environment' in n for n in draft['notes'])
    # As drafted it generates (no kind is left Unknown); it validates against the schema, and the events come out right.
    assert generate(TranslationMapping.model_validate(m), SCHEMA, '4.1.0')['ok']
    result = generate(TranslationMapping.model_validate({**decided(m), 'unmatched': 'skip'}), SCHEMA, '4.1.0')
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
    # accept and deny between addresses are Network/Permit and Network/Deny, so nothing is left to decide.
    assert [r['name'] for r in draft['mapping']['events']] == ['permitted', 'denied', 'other']
    assert generate(TranslationMapping.model_validate(draft['mapping']), SCHEMA, '4.1.0')['ok']


async def test_a_field_inventory_sent_as_the_mapping_gets_the_draft_back():
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))})
    with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)), \
            patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})):
        result = await generation.build_translation_xslt(ctx, INVENTORY, sample=FW_JSON)
        assert result['status'] == 'needs_mapping' and result['problems'][0] == 'mapping is a list of fields, not a translation mapping'
        assert result['draft_mapping']['input'] == 'json' and 'Edit draft_mapping' in result['hint']
        # The draft, sent back as the mapping, generates once its placeholders are decided.
        again = await generation.build_translation_xslt(ctx, result['draft_mapping'], sample=FW_JSON)
        assert again['ok'], again['problems']      # every kind of this sample drafted with its action element
        again = await generation.build_translation_xslt(ctx, decided(result['draft_mapping']), sample=FW_JSON)
        assert again['ok'] and again['xslt']
        drafted = await generation.draft_translation_mapping(ctx, {'fw.jsonl': FW_JSON}, 'FortiOS firewall', environment='Prod')
        assert drafted['schema_check']['ok'] and drafted['mapping']['common'][2]['value'] == 'Prod'


FIREWALL_CSV = (
    'timestamp,device,event_type,severity,src_ip,src_port,dst_ip,dst_port,protocol,action,rule_id,username,message\n'
    '2026-10-01T09:00:12+10:00,FW-EDGE-01,TRAFFIC,INFO,192.0.2.10,54321,198.51.100.20,443,TCP,ALLOW,1001,,Outbound HTTPS allowed\n'
    '2026-10-01T09:01:05+10:00,FW-EDGE-01,TRAFFIC,WARNING,203.0.113.45,49822,192.0.2.25,22,UDP,DENY,2003,,Inbound SSH blocked\n'
    '2026-10-01T09:10:03+10:00,FW-EDGE-01,SYSTEM,INFO,,,,,N/A,START,,,Firewall logging service started\n'
    '2026-10-01T09:10:30+10:00,FW-EDGE-01,SYSTEM,INFO,,,,,N/A,CONFIG_SAVED,,,Configuration saved\n'
    '2026-10-01T09:11:15+10:00,FW-EDGE-01,ADMIN,INFO,192.0.2.100,,,,HTTPS,LOGIN_SUCCESS,,admin,Administrator logged in\n'
    '2026-10-01T09:12:02+10:00,FW-EDGE-01,ADMIN,WARNING,203.0.113.90,,,,HTTPS,LOGIN_FAILED,,admin,Failed administrator login\n'
    '2026-10-01T09:16:08+10:00,FW-EDGE-01,ADMIN,INFO,192.0.2.100,,,,HTTPS,LOGOUT,,admin,Administrator logged out\n'
    '2026-10-01T09:17:00+10:00,FW-EDGE-01,ADMIN,NOTICE,192.0.2.100,,,,HTTPS,CONFIG_CHANGE,,admin,Policy 12 changed\n')


def test_a_firewalls_traffic_and_admin_records_are_drafted_with_their_action_elements():
    # Seen in VS Code (three sessions on this sample): the draft left every kind Unknown, the agent found Network's
    # and Authenticate's structure hard, gave up, and asked the user to accept traffic and admin as Unknown.
    from utils.draftmap import _records
    from utils.localcheck import unknown_coverage
    draft = draft_mapping({'fw.csv': FIREWALL_CSV}, 'Firewall', 'FW', 'Prod')
    rules = {r['name']: r for r in draft['mapping']['events']}
    assert list(rules) == ['admin_logon', 'admin_logoff', 'admin_config_change', 'traffic_permitted', 'traffic_denied',
                           'system_config_change', 'system_service', 'other']     # kinds most common first
    paths = lambda name: {f['path'] for f in rules[name]['fields']}  # noqa: E731
    assert {'EventDetail/Network/Permit/Source/Device/IPAddress', 'EventDetail/Network/Permit/Destination/Port'} <= paths('traffic_permitted')
    assert 'EventDetail/Network/Deny/Source/Port' in paths('traffic_denied')
    protocol = next(f for f in rules['traffic_denied']['fields'] if f['path'].endswith('TransportProtocol'))
    assert protocol['map'] == {'TCP': 'TCP', 'UDP': 'UDP'} and protocol['default'] == 'Other'   # the values it takes
    logon = next(f for f in rules['admin_logon']['fields'] if f['path'] == 'EventDetail/Authenticate/Outcome/Success')
    assert logon['map'] == {'LOGIN_SUCCESS': 'true', 'LOGIN_FAILED': 'false'}
    assert {'path': 'EventDetail/Authenticate/Action', 'value': 'Logoff'} in rules['admin_logoff']['fields']
    assert 'EventDetail/Update/After/Configuration/Type' in paths('admin_config_change')
    # The logging service's START is a service starting (Process); no kind is left Unknown, and all of it validates.
    service = {f['path']: f for f in rules['system_service']['fields']}
    assert service['EventDetail/Process/Action']['map'] == {'START': 'Startup'}
    assert service['EventDetail/Process/Type']['value'] == 'Service'
    assert generate(TranslationMapping.model_validate(draft['mapping']), SCHEMA_352, '3.5.2')['problems'] == []

    # The agent's give-up: each kind Unknown with a reason. Traffic and admin are refused with the rules to use, and
    # so is system now: every record it catches has a rule from its own values (a service starting, a configuration
    # saved), so there is nothing to put to the user.
    gave_up = {**draft['mapping'], 'events': [
        {'name': kind, 'when': [{'field': 'event_type', 'equals': kind.upper()}], 'allow_unknown': 'the schema is complex',
         'fields': [{'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}
        for kind in ('traffic', 'admin', 'system')]}
    _, records, _ = _records({'fw.csv': FIREWALL_CSV})
    problems, kept = unknown_coverage(TranslationMapping.model_validate(gave_up), records)
    assert sorted(p.split(']')[0] for p in problems) == ['[admin', '[system', '[traffic']
    assert "can't be kept as Unknown: 2 of its 2 sample records" in problems[0]
    offered = json.loads(problems[0][problems[0].index('[{'):])
    assert generate(TranslationMapping.model_validate({**draft['mapping'], 'events': offered}), SCHEMA_352, '3.5.2')['ok']
    system = next(p for p in problems if p.startswith('[system]'))
    assert kept == [] and 'Update for action CONFIG_SAVED; Process for action START' in system


def test_fields_with_no_element_are_drafted_as_data_on_the_side_they_name():
    # Eval case 21: a connection broker's CONNECT and DISCONNECT records, with fields the schema has no element for.
    import yaml
    from pathlib import Path
    case = yaml.safe_load((Path(__file__).parents[1] / 'dev' / 'eval' / 'cases' /
                           '21_csv_connections_odd_fields.yaml').read_text(encoding='utf-8'))
    draft = draft_mapping({'broker.csv': case['sample']}, 'Connection Broker', 'Broker', 'Eval')
    rules = {r['name']: r for r in draft['mapping']['events']}
    assert list(rules) == ['connected', 'closed', 'other']      # the kinds are themselves the actions
    connect = {(f['path'], f.get('data_name')) for f in rules['connected']['fields']}
    assert {('EventDetail/Network/Connect/Destination/Port', None),
            ('EventDetail/Network/Connect/Destination/Device/IPAddress', None),
            ('EventDetail/Network/Connect/Destination/Data', 'destination_key'),
            ('EventDetail/Network/Connect/Destination/Data', 'destination_zone'),
            ('EventDetail/Network/Connect/Source/Data', 'source_zone'),
            ('EventDetail/Network/Connect/Data', 'tls_ja3')} <= connect
    assert ('EventDetail/Network/Close/Destination/Data', 'destination_key') in \
        {(f['path'], f.get('data_name')) for f in rules['closed']['fields']}
    assert {'path': 'EventSource/Device/HostName', 'field': 'sensor'} in draft['mapping']['common']
    # No kind is Unknown, and it generates as drafted.
    assert generate(TranslationMapping.model_validate(draft['mapping']), SCHEMA, '4.1.0')['ok']


def test_health_and_state_records_are_drafted_as_alerts():
    # The firewall sample of a VS Code run: CPU and VPN tunnel records were left Unknown by the draft; the agent made
    # them Alert itself, taking two rounds over Alert/Type (it tried 'Firewall').
    sample = ("timestamp,device,event_type,severity,src_ip,dst_ip,action,message\n"
              "2026-10-01T09:00:00Z,FW1,SYSTEM,WARNING,,,HIGH_CPU,CPU over 85 percent\n"
              "2026-10-01T09:01:00Z,FW1,SYSTEM,ERROR,,,VPN_TUNNEL_DOWN,Tunnel branch-01 down\n"
              "2026-10-01T09:02:00Z,FW1,SYSTEM,INFO,,,VPN_TUNNEL_UP,Tunnel branch-01 restored\n"
              "2026-10-01T09:03:00Z,FW1,SYSTEM,INFO,,,START,Logging service started\n")
    rules = {r['name']: r for r in draft_mapping({'fw.csv': sample}, 'FW', 'FW', 'Prod')['mapping']['events']}
    alert = {f['path']: f for f in rules['system_alert']['fields']}
    assert rules['system_alert']['when'][-1] == {'field': 'action', 'one_of': ['HIGH_CPU', 'VPN_TUNNEL_DOWN', 'VPN_TUNNEL_UP']}
    assert alert['EventDetail/Alert/Type']['map'] == {'HIGH_CPU': 'Other', 'VPN_TUNNEL_DOWN': 'Network',
                                                      'VPN_TUNNEL_UP': 'Network'}
    severity = alert['EventDetail/Alert/Severity']['map']
    assert (severity['WARNING'], severity['ERROR'], severity['INFO']) == ('Minor', 'Major', 'Info')
    assert severity['CRITICAL'] == 'Critical'       # a word the sample lacks, written as the sample writes them
    assert 'system_service' in rules and not any(r.get('when') and any(
        f['path'].startswith('EventDetail/Unknown') for f in r['fields']) for r in rules.values())
    mapping = TranslationMapping.model_validate(draft_mapping({'fw.csv': sample}, 'FW', 'FW', 'Prod')['mapping'])
    assert generate(mapping, SCHEMA_352, '3.5.2')['problems'] == []


def test_the_user_can_keep_unknown_after_seeing_what_the_values_suggest():
    # Refused first, with the rules the values show; kept only when the user, shown them, still wants Unknown, and
    # then put to them in the form with those suggestions.
    from utils.draftmap import _records
    from utils.localcheck import unknown_coverage
    draft = draft_mapping({'fw.csv': FIREWALL_CSV}, 'Firewall', 'FW', 'Prod')
    _, records, _ = _records({'fw.csv': FIREWALL_CSV})

    def traffic(**extra):
        return {**draft['mapping'], 'events': [
            {'name': 'traffic', 'when': [{'field': 'event_type', 'equals': 'TRAFFIC'}], 'allow_unknown': 'the user said so',
             'fields': [{'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}], **extra}]}
    problems, kept = unknown_coverage(TranslationMapping.model_validate(traffic()), records)
    assert problems[0].startswith("[traffic] can't be kept as Unknown") and 'keep_unknown: true' in problems[0]
    problems, kept = unknown_coverage(TranslationMapping.model_validate(traffic(keep_unknown=True)), records)
    assert problems == [] and kept[0]['rule'] == 'traffic' and kept[0]['against_suggestion']
    assert kept[0]['suggested'].startswith('Network/Permit for action ALLOW')
