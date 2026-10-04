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
