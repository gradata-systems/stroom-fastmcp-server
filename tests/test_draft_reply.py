"""The draft as the agent receives it: small enough for the client to show it whole, and read back the same."""
import json
from pathlib import Path

import pytest

from utils.draftmap import draft_mapping
from utils.eventschema import EventSchema
from utils.fielddoc import field_mapping_markdown
from utils.xsltgen import TranslationMapping, compact_rules, generate, grouped, kept_unknown

SCHEMA = EventSchema.parse((Path(__file__).parent / 'fixtures' / 'event-logging-v3.5.2.xsd').read_bytes())
FIREWALL = Path(__file__).parent / 'fixtures' / 'firewall_logs_20.csv'   # the VS Code runs' sample
SAMPLE = ("timestamp,device,event_type,severity,src_ip,src_port,dst_ip,dst_port,protocol,action,rule_id,bytes_sent,"
          "bytes_received,username,message\n"
          "2026-10-01T09:00:12+10:00,FW1,TRAFFIC,INFO,192.0.2.10,54321,198.51.100.20,443,TCP,ALLOW,1001,1520,8450,,ok\n"
          "2026-10-01T09:01:05+10:00,FW1,TRAFFIC,WARNING,203.0.113.45,49822,192.0.2.25,22,TCP,DENY,2003,0,0,,blocked\n"
          "2026-10-01T09:10:00+10:00,FW1,ADMIN,INFO,192.0.2.5,50000,192.0.2.1,443,HTTPS,LOGIN_SUCCESS,0,0,0,admin,in\n"
          "2026-10-01T09:20:00+10:00,FW1,SYSTEM,WARNING,,,,,N/A,HIGH_CPU,0,0,0,,CPU over 85 percent\n")


def test_a_rules_data_list_reads_back_as_its_data_entries():
    draft = draft_mapping({'fw.csv': SAMPLE}, 'FW', 'FW', 'Prod')['mapping']
    compact = compact_rules(draft)
    permitted = next(r for r in compact['events'] if r['name'] == 'traffic_permitted')
    assert permitted['data'] == ['severity', 'rule_id', 'bytes_sent', 'bytes_received']
    assert not any(f['path'].endswith('/Data') for f in permitted['fields'])
    # Unknown's Data stays as it was: data needs an action element to go under.
    other = next(r for r in compact['events'] if r['name'] == 'other')
    assert other['fields'] and not other.get('data')
    # Read back, it is the same mapping: the same XSLT, and a third smaller.
    same = generate(TranslationMapping.model_validate(compact), SCHEMA, '3.5.2')
    assert same['ok'] and same['xslt'] == generate(TranslationMapping.model_validate(draft), SCHEMA, '3.5.2')['xslt']
    assert len(json.dumps(compact)) < 0.75 * len(json.dumps(draft))
    rule = TranslationMapping.model_validate(compact).events[0]
    assert rule.data == [] and any(f.path == 'EventDetail/Network/Permit/Data' and f.data_name == 'rule_id'
                                   for f in rule.fields)


def test_data_without_an_action_element_is_refused_with_what_to_do():
    with pytest.raises(ValueError, match=r"\[x\] data needs the rule's action element"):
        TranslationMapping.model_validate({'input': 'data_splitter', 'events': [{'name': 'x', 'data': ['rule_id']}]})


def test_the_same_warning_for_several_rules_is_one():
    messages = ["[a] required ['EventTime', 'EventDetail/TypeId'] are left out when empty.",
                "[b] required ['EventTime', 'EventDetail/Update/After'] are left out when empty.",
                "[c] something else"]
    assert grouped(messages) == ["[a, b] required ['EventTime', 'EventDetail/TypeId', 'EventDetail/Update/After'] are "
                                 "left out when empty.", "[c] something else"]


def test_the_sessions_draft_fits_the_client_inline():
    # Copilot saved a 10.6 KB draft to a file; the agent read 100 of its 502 lines and wrote its own mapping.
    draft = draft_mapping([FIREWALL.read_text(encoding='utf-8')], 'Firewall Logs', None, None)
    draft['mapping'] = compact_rules(draft['mapping'])
    checked = generate(TranslationMapping.model_validate(draft['mapping']), SCHEMA, '3.5.2')
    draft['schema_check'] = {'ok': checked['ok'], 'problems': grouped(checked['problems']),
                             'warnings': grouped(checked['warnings'])[:6]}
    assert len(json.dumps(draft, separators=(',', ':'))) < 8000


def test_allow_unknown_on_a_rule_that_writes_an_action_element_means_nothing():
    # Seen: a rule moved from Unknown to Alert kept its allow_unknown; the user was asked to keep it Unknown ("none of
    # the 20 records"), and the documentation listed it under Kept as Unknown.
    mapping = TranslationMapping.model_validate({'input': 'data_splitter', 'common': [
        {'path': 'EventTime/TimeCreated', 'field': 'timestamp'}, {'path': 'EventSource/System/Name', 'value': 'FW'},
        {'path': 'EventSource/System/Environment', 'value': 'Prod'}, {'path': 'EventSource/Generator', 'value': 'FW'},
        {'path': 'EventSource/Device/HostName', 'field': 'device'}, {'path': 'EventDetail/TypeId', 'field': 'action'}],
        'events': [{'name': 'system_alert', 'when': [{'field': 'event_type', 'equals': 'SYSTEM'}],
                    'allow_unknown': 'System events', 'fields': [{'path': 'EventDetail/Alert/Type', 'value': 'Other'}]},
                   {'name': 'other', 'allow_unknown': 'anything else',
                    'fields': [{'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}]})
    assert [r.name for r in kept_unknown(mapping)] == ['other']
    warnings = generate(mapping, SCHEMA, '3.5.2')['warnings']
    assert any(w.startswith('[system_alert] allow_unknown is ignored: the rule writes Alert') for w in warnings)
    kept = field_mapping_markdown(mapping, SCHEMA).split('### Kept as Unknown', 1)[1].split('###', 1)[0]
    assert '`other`' in kept and 'system_alert' not in kept
