"""Source notes from the user's documentation: kept readable by the tools, followed by the draft, checked against."""
from pathlib import Path

from utils.draftmap import draft_mapping
from utils.eventschema import EventSchema
from utils.sourcenotes import block, check_mapping, fields_markdown, merged, outcome_map, read_notes
from utils.xsltgen import TranslationMapping, generate

SCHEMA = EventSchema.parse((Path(__file__).parent / 'fixtures' / 'event-logging-v4.1.0.xsd').read_bytes())

# Codes that mean nothing without the vendor's documentation.
SAMPLE = ("ts,host,evt,usr,src,res\n"
          "2026-10-01T08:00:00Z,dc01,4624,alice,10.0.0.1,0x0\n"
          "2026-10-01T08:05:00Z,dc01,4625,bob,10.0.0.2,0xC000006A\n"
          "2026-10-01T08:10:00Z,dc01,4634,alice,10.0.0.1,0x0\n"
          "2026-10-01T08:12:00Z,dc01,9999,carol,10.0.0.3,0x0\n")
NOTES = {
    'source': 'Acme directory',
    'fields': [
        {'field': 'evt', 'meaning': 'Event id'},
        {'field': 'usr', 'meaning': 'Account name', 'event_logging_path': 'EventSource/User/Id'},
        {'field': 'src', 'meaning': 'Workstation address', 'event_logging_path': 'EventSource/Client/IPAddress'},
        {'field': 'res', 'meaning': 'Status code', 'values': {'0x0': 'success', '0xC000006A': 'failed: bad password'}},
    ],
    'events': [
        {'event': '4624', 'description': 'An account logged on', 'event_detail': 'Authenticate', 'type_id': 'Logon',
         'field': 'evt', 'value': '4624', 'action': 'Logon', 'success': True},
        {'event': '4625', 'description': 'An account failed to log on', 'event_detail': 'Authenticate',
         'type_id': 'Logon failed', 'field': 'evt', 'value': '4625', 'action': 'Logon', 'success': False},
        {'event': '4634', 'description': 'An account logged off', 'event_detail': 'Authenticate', 'type_id': 'Logoff',
         'field': 'evt', 'value': '4634', 'action': 'Logoff'},
        {'event': '4648', 'description': 'A logon with explicit credentials', 'event_detail': 'Authenticate',
         'type_id': 'Explicit logon', 'field': 'evt', 'value': '4648', 'action': 'Logon'},
    ],
}


def test_the_notes_are_kept_in_the_doc_and_read_back():
    text = f"# Acme directory source notes\n\nSummary.\n\n{block(NOTES)}\n"
    assert read_notes(text) == NOTES and read_notes('no notes here') is None
    assert merged([NOTES, {'fields': [{'field': 'evt', 'meaning': 'other'}], 'events': []}])['fields'][0]['meaning'] == 'Event id'


def test_the_draft_follows_the_documentation_and_generates():
    draft = draft_mapping(SAMPLE, 'Acme directory', 'Acme', 'Eval', source_notes=NOTES)
    mapping = draft['mapping']
    common = {e['path']: e for e in mapping['common']}
    assert common['EventSource/User/Id']['field'] == 'usr' and common['EventSource/Client/IPAddress']['field'] == 'src'
    rules = {r['name']: r for r in mapping['events']}
    failed = {f['path']: f for f in rules['4625']['fields']}
    assert rules['4625']['when'] == [{'field': 'evt', 'equals': '4625'}]
    assert failed['EventDetail/TypeId']['value'] == 'Logon failed' and failed['EventDetail/Authenticate/Action']['value'] == 'Logon'
    assert failed['EventDetail/Authenticate/Outcome/Success']['value'] == 'false'
    assert failed['EventDetail/Description']['value'] == 'An account failed to log on'
    assert {f['path']: f for f in rules['4634']['fields']}['EventDetail/Authenticate/Action']['value'] == 'Logoff'
    assert '4648' in rules                          # documented, though the sample has none
    applied = draft['source_notes']
    assert applied['not_in_sample'] == ['evt=4648'] and applied['not_in_catalogue'] == ['evt=9999']
    assert draft['notes'][0].startswith('Drafted from the source documentation') and any('evt=9999' in n for n in draft['notes'])
    checked = generate(TranslationMapping.model_validate(mapping), SCHEMA, '4.1.0')
    assert checked['ok'], checked['problems']


def test_without_notes_the_draft_guesses_as_before():
    draft = draft_mapping(SAMPLE, 'Acme directory', 'Acme', 'Eval')
    assert draft['source_notes'] is None and not any(r['name'] == '4625' and any(
        f['path'] == 'EventDetail/Authenticate/Action' for f in r['fields']) for r in draft['mapping']['events'])


def test_a_mapping_that_contradicts_the_catalogue_is_reported():
    mapping = draft_mapping(SAMPLE, 'Acme directory', 'Acme', 'Eval', source_notes=NOTES)['mapping']
    assert check_mapping(mapping, NOTES) == []
    for rule in mapping['events']:
        if rule['name'] == '4625':
            rule['fields'] = [{'path': 'EventDetail/TypeId', 'value': 'Logon'},
                              {'path': 'EventDetail/Unknown/Data', 'data_name': 'evt', 'field': 'evt'}]
    problems = check_mapping(mapping, NOTES)
    assert any("4625 (evt=4625): the documentation says Authenticate; rule '4625' writes Unknown" in p for p in problems)
    assert any("TypeId is 'Logon failed'; rule '4625' writes 'Logon'" in p for p in problems)
    mapping['events'] = [r for r in mapping['events'] if r['name'] != '4634' and r.get('when')]
    assert any('4634 (evt=4634): no rule takes it' in p for p in check_mapping(mapping, NOTES))


def test_codes_become_outcomes_only_when_every_meaning_says_one():
    assert outcome_map({'0x0': 'success', '0xC000006A': 'failed: bad password'}) == {'0x0': 'true', '0xC000006A': 'false'}
    assert outcome_map({'0x0': 'success', '0x1': 'pending'}) is None


def test_the_documentation_lists_the_source_fields_with_their_meanings():
    text = fields_markdown(NOTES)
    assert '| `res` | Status code | `0x0`: success; `0xC000006A`: failed: bad password |' in text
    assert fields_markdown({'fields': []}) == ''


# The catalogue an agent recorded from a firewall sample alone (no documentation), as it was given.
FIREWALL = ("timestamp,device,event_type,src_ip,src_port,dst_ip,dst_port,protocol,action,rule_id,username,message\n"
            "2026-10-01T09:00:12+10:00,FW1,TRAFFIC,192.0.2.10,54321,198.51.100.20,443,TCP,ALLOW,1001,,HTTPS allowed\n"
            "2026-10-01T09:01:05+10:00,FW1,TRAFFIC,203.0.113.45,49822,192.0.2.25,22,TCP,DENY,2003,,SSH blocked\n"
            "2026-10-01T09:02:17+10:00,FW1,TRAFFIC,192.0.2.11,53002,198.51.100.53,53,UDP,ALLOW,1002,,DNS allowed\n"
            "2026-10-01T09:10:00+10:00,FW1,ADMIN,192.0.2.5,50000,192.0.2.1,443,HTTPS,LOGIN_SUCCESS,0,admin,Admin logged in\n"
            "2026-10-01T09:11:00+10:00,FW1,ADMIN,192.0.2.5,50000,192.0.2.1,443,HTTPS,LOGIN_FAILED,0,root,Bad password\n"
            "2026-10-01T09:12:00+10:00,FW1,ADMIN,192.0.2.5,50000,192.0.2.1,443,HTTPS,CONFIG_CHANGE,0,admin,Policy changed\n"
            "2026-10-01T09:20:00+10:00,FW1,SYSTEM,192.0.2.1,0,192.0.2.1,0,N/A,VPN_TUNNEL_DOWN,0,,Tunnel down\n")
GUESSED = {'events': [
    {'event': 'TRAFFIC-ALLOW', 'description': 'Network traffic allowed', 'field': 'event_type', 'value': 'TRAFFIC',
     'event_detail': 'Allow'},
    {'event': 'TRAFFIC-DENY', 'description': 'Network traffic blocked', 'field': 'event_type', 'value': 'TRAFFIC',
     'event_detail': 'Deny'},
    {'event': 'LOGIN-SUCCESS', 'description': 'Administrator logged in', 'field': 'action', 'value': 'LOGIN_SUCCESS',
     'event_detail': 'Authenticate', 'success': True},
    {'event': 'LOGIN-FAILED', 'description': 'Administrator failed to log in', 'field': 'action',
     'value': 'LOGIN_FAILED', 'event_detail': 'Authenticate', 'success': False},
    {'event': 'CONFIG-CHANGE', 'description': 'Configuration modified', 'field': 'action', 'value': 'CONFIG_CHANGE',
     'event_detail': 'Update'},
    {'event': 'VPN-DOWN', 'description': 'VPN tunnel disconnected', 'field': 'action', 'value': 'VPN_TUNNEL_DOWN',
     'event_detail': 'Unknown'},
]}


def check(detail):
    from utils.sourcenotes import detail_problem
    return detail_problem(SCHEMA, detail)


def test_a_catalogue_naming_no_action_element_or_twin_events_is_refused():
    from utils.sourcenotes import catalogue_problems
    problems = catalogue_problems(GUESSED['events'], check)
    assert any(p.startswith("TRAFFIC-ALLOW: 'Allow' is not an EventDetail action element") and 'Network/Permit' in p
               for p in problems)
    assert any(p.startswith('TRAFFIC-ALLOW and TRAFFIC-DENY are both event_type=TRAFFIC') for p in problems)
    assert len(problems) == 3                        # Authenticate, Update and Unknown are elements
    assert "'Network' needs its action below it: " in check('Network') and 'Network/Permit' in check('Network')
    assert check('Network/Permit') is None and check('Authenticate') is None
    assert "'TypeId' is not an EventDetail action element" in check('TypeId')


def test_where_the_catalogue_cant_be_followed_the_sample_s_own_rules_are_kept():
    draft = draft_mapping(FIREWALL, 'Firewall', 'FW', 'Eval', source_notes=GUESSED, detail_check=check)
    rules = {r['name']: r for r in draft['mapping']['events']}
    elements = {name: {f['path'].split('/')[2] for f in r['fields'] if f['path'].startswith('EventDetail/Network/')}
                for name, r in rules.items()}
    assert elements['traffic_permitted'] == {'Permit'} and elements['traffic_denied'] == {'Deny'}
    assert not any(f['path'].startswith(('EventDetail/Allow', 'EventDetail/Deny')) for r in rules.values()
                   for f in r['fields'])
    assert any(n.startswith("Kept from the sample's values") and 'event_type=TRAFFIC' in n for n in draft['notes'])
    # The catalogue's own events, completed from the sample's rule for the same records where the schema wants more.
    login = {f['path']: f for f in rules['login_success']['fields']}
    assert 'EventDetail/Authenticate/Action' in login and login['EventDetail/Authenticate/Outcome/Success']['value'] == 'true'
    assert 'EventDetail/Update/After/Configuration/Type' in {f['path'] for f in rules['config_change']['fields']}
    # Unknown is left for the user to agree when the XSLT is built, never agreed by the draft.
    assert not any(r.get('allow_unknown') for r in rules.values())
    checked = generate(TranslationMapping.model_validate(draft['mapping']), SCHEMA, '4.1.0')
    assert not [p for p in checked['problems'] if 'vpn_down' not in p], checked['problems']


def test_a_catalogue_saying_unknown_is_not_held_against_a_better_rule():
    mapping = draft_mapping(FIREWALL, 'Firewall', 'FW', 'Eval')['mapping']
    vpn = {'events': [GUESSED['events'][-1]]}
    for rule in mapping['events']:
        if rule.get('when'):
            continue
        rule['fields'] = [{'path': 'EventDetail/TypeId', 'field': 'action'},
                          {'path': 'EventDetail/Alert/Type', 'value': 'Network'}]
    assert not [p for p in check_mapping(mapping, vpn) if 'documentation says Unknown' in p]
