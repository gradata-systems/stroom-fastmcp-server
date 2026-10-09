import sys
from pathlib import Path

from utils.eventschema import EventSchema
from utils.xsltgen import TranslationMapping, generate

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dev' / 'eval'))
import run_eval as ev  # noqa: E402

SCHEMA = EventSchema.parse((Path(__file__).parent / 'fixtures' / 'event-logging-v4.1.0.xsd').read_bytes())
EVENTS = """<Events xmlns="event-logging:3"><Event><EventTime><TimeCreated>2026-09-28T08:01:12.000Z</TimeCreated></EventTime>
<EventSource><User><Id>alice</Id></User><Client><IPAddress>203.0.113.10</IPAddress></Client></EventSource>
<EventDetail><TypeId>login</TypeId><Authenticate><Action>Logon</Action></Authenticate></EventDetail></Event>
<Event><EventTime><TimeCreated>2026-09-28T08:03:40.000Z</TimeCreated></EventTime><EventSource><User><Id>bob</Id></User></EventSource>
<EventDetail><TypeId>login</TypeId><Authenticate><Action>Logon</Action></Authenticate></EventDetail></Event></Events>"""


def test_every_case_has_a_reference_mapping_the_schema_accepts():
    cases = ev.load_cases()
    assert len(cases) == 35 and len({c["id"] for c in cases}) == 35
    for case in cases:
        assert {'name', 'template', 'request', 'expected', 'reference'} <= set(case), case['id']
        assert ev.samples_of(case), case['id']
        result = generate(TranslationMapping.model_validate(case['reference']['mapping']), SCHEMA, '4.1.0')
        assert result['ok'], (case['id'], result['problems'])
        assert 'Sample' in ev.request_text(case) and 'stroom-flat' in ev.request_text(case)


def test_scoring_counts_events_types_and_missing_paths():
    case = {'expected': {'records': 2, 'event_types': ['Authenticate'],
                         'paths': ['EventSource/User/Id', 'EventSource/Client/IPAddress']}}
    score = ev.Score('x', 'test')
    ev.score_events(score, case, [EVENTS], [True])
    assert score.events == 2 and score.event_types == ['Authenticate'] and score.valid_events == 1
    assert score.missing_paths == ['EventSource/Client/IPAddress'] and not score.stage1
    good = ev.Score('y', 'test')
    ev.score_events(good, {'expected': {**case['expected'], 'paths': ['EventSource/User/Id']}}, [EVENTS], [True])
    assert good.stage1 and not good.problems


def test_scoring_checks_values_some_event_must_hold():
    expected = {'records': 2, 'event_types': ['Authenticate'], 'paths': ['EventSource/User/Id']}
    held = ev.Score('x', 'test')
    ev.score_events(held, {'expected': {**expected, 'values': {'EventSource/User/Id': ['alice', 'bob']}}}, [EVENTS], [True])
    assert held.stage1 and not held.problems
    lost = ev.Score('y', 'test')   # e.g. a quoted name cut at its space
    ev.score_events(lost, {'expected': {**expected, 'values': {'EventSource/User/Id': ['alice smith']}}}, [EVENTS], [True])
    assert not lost.stage1 and lost.problems == ["no event has EventSource/User/Id = ['alice smith']"]


def test_summary_applies_the_exit_criterion():
    total = len(ev.load_cases())   # every case, no hints
    scores = [ev.Score(f'c{i}', 'test', passed=True) for i in range(total)]
    assert f'meets the exit criterion (all {total} cases, no hints)' in ev.summary(scores)
    assert 'does not meet' in ev.summary([ev.Score(f'c{i}', 'test', passed=i > 0) for i in range(total)])
    assert 'partial run' in ev.summary(scores[:3])


def test_repeated_runs_pass_a_case_when_most_runs_pass():
    import run_agent
    total = len(ev.load_cases())
    runs = [run_agent.AgentScore(f'c{i}', 'agent', passed=(i > 0 or r < 2), run=r) for i in range(total) for r in range(3)]
    assert f'{total} of {total} cases passed in most of their 3 runs; meets' in run_agent.repeated_summary(runs, 3)
    assert '| c0 | 2/3 |' in run_agent.repeated_summary(runs, 3)
    flaky = [run_agent.AgentScore('c0', 'agent', passed=r == 0, run=r) for r in range(3)]   # 1 of 3: not passing
    assert '0 of 1 cases passed' in run_agent.repeated_summary(flaky, 3)


def test_an_expected_path_may_leave_one_element_open():
    # A firewall decision is as well modelled under Network/Permit or /Deny as under Open: * takes any of them.
    from lxml import etree
    for action in ('Open', 'Permit', 'Deny'):
        event = etree.fromstring(f'<Event xmlns="event-logging:3"><EventDetail><Network><{action}><Source><Device>'
                                 f'<IPAddress>192.0.2.1</IPAddress></Device></Source></{action}></Network></EventDetail></Event>')
        assert ev.has_path(event, 'EventDetail/Network/*/Source/Device/IPAddress')
        assert ev.has_path(event, 'EventDetail/Network/Open/Source/Device/IPAddress') == (action == 'Open')
        assert ev.path_values([event], 'EventDetail/Network/*/Source/Device/IPAddress') == {'192.0.2.1'}


def test_a_case_number_names_that_case_only():
    # '16' once also picked 07_syslog3164_sudo, whose name contains it.
    assert [c['id'][:2] for c in ev.load_cases(['13', '16'])] == ['13', '16']
    assert {c['id'][:2] for c in ev.load_cases(['json'])} >= {'02', '03', '04', '11', '13', '16', '18', '19'}


def test_expected_types_and_paths_may_name_alternatives():
    from lxml import etree
    door = etree.fromstring('<Event xmlns="event-logging:3"><EventSource><User><UserDetails><Unit>Sales</Unit>'
                            '</UserDetails></User></EventSource><EventDetail><TypeId>badge</TypeId><Authorise>'
                            '<Action>Access</Action></Authorise></EventDetail></Event>')
    dept = 'EventSource/User/UserDetails/Organisation|EventSource/User/UserDetails/Unit'
    assert ev.has_path(door, dept) and not ev.has_path(door, 'EventSource/User/UserDetails/Organisation')
    assert ev.path_values([door], dept) == {'Sales'}
    xml = etree.tostring(etree.fromstring(f'<Events xmlns="event-logging:3">{etree.tostring(door).decode()}</Events>')).decode()
    score = ev.Score('x', 'test')
    ev.score_events(score, {'expected': {'records': 1, 'event_types': ['Authenticate|Authorise'],
                                         'paths': [dept, 'EventDetail/Authenticate/Action|EventDetail/Authorise/Action']}}, [xml], [True])
    assert score.stage1 and not score.missing_types and not score.missing_paths


def test_scoring_fails_unknown_events_and_data_away_from_where_it_belongs():
    # Case 21: every record is a connection (no Unknown), and destination_key, a field with no element, is Data under
    # Destination, not under the action element or left out.
    events = ('<Events xmlns="event-logging:3"><Event><EventDetail><TypeId>CONNECT</TypeId><Network><Connect>'
              '<Source><Device><IPAddress>198.51.100.23</IPAddress></Device><Data Name="source_zone" Value="internet"/>'
              '</Source><Destination><Device><IPAddress>10.20.0.11</IPAddress></Device><Port>443</Port>'
              '<Data Name="destination_key" Value="svc/payments-api"/></Destination></Connect></Network></EventDetail>'
              '</Event></Events>')
    wrong = events.replace('<Data Name="destination_key" Value="svc/payments-api"/></Destination></Connect>',
                           '</Destination><Data Name="destination_key" Value="svc/payments-api"/></Connect>')
    unknown = ('<Events xmlns="event-logging:3"><Event><EventDetail><TypeId>CONNECT</TypeId><Unknown>'
               '<Data Name="destination_key" Value="svc/payments-api"/></Unknown></EventDetail></Event></Events>')
    case = {'expected': {'records': 1, 'event_types': ['Network'], 'forbidden_types': ['Unknown'],
                         'paths': ['EventDetail/Network/Connect/Destination/Port'],
                         'data': [{'at': 'EventDetail/Network/*/Destination', 'name': 'destination_key'},
                                  {'at': 'EventDetail/Network/Connect/Destination', 'name': 'destination_key',
                                   'value': 'svc/payments-api', 'every': False},
                                  {'at': 'EventDetail/Network/Connect/Source', 'name': 'source_zone'}]}}
    good = ev.Score('21', 'reference')
    ev.score_events(good, case, [events], [True])
    assert good.stage1 and not good.problems
    moved = ev.Score('21', 'reference')
    ev.score_events(moved, case, [wrong], [True])
    assert not moved.stage1 and moved.problems == [
        "not every event has Data 'destination_key' under EventDetail/Network/*/Destination",
        "no event has Data 'destination_key' = 'svc/payments-api' under EventDetail/Network/Connect/Destination"]
    gave_up = ev.Score('21', 'reference')
    ev.score_events(gave_up, case, [unknown], [True])
    assert not gave_up.stage1 and "events of type ['Unknown'], which none should be" in gave_up.problems


def test_every_reference_passes_what_build_translation_xslt_checks_before_saving():
    # Offline: the mapping generates, the sample is read as the server's Data Splitter reads it (inferred, or from
    # the case's spec), every field and time format fits, the extractions match, and the records are counted.
    import offline
    for case in ev.load_cases():
        result = offline.check(case)
        assert not result['problems'], (case['id'], result['problems'])


def test_a_large_source_is_counted_whole_and_values_none_may_hold_are_failed():
    events = EVENTS.replace('<Id>bob</Id>', '<Id>-</Id>')
    case = {'expected': {'records': 32000, 'event_types': ['Authenticate'], 'paths': ['EventSource/User/Id'],
                         'forbidden_values': {'EventSource/User/Id': ['-']}}}
    score = ev.Score('x', 'test')
    ev.score_events(score, case, [events], [True], total=32000)   # 2 read of the 32000 the streams hold
    assert score.events == 32000 and score.problems == ["events have EventSource/User/Id = ['-'], which none should"]
    assert not score.stage1
    clean = ev.Score('y', 'test')
    ev.score_events(clean, case, [EVENTS], [True], total=32000)
    assert clean.stage1 and not clean.problems


def test_a_case_of_files_shows_their_first_lines_and_keeps_their_bytes():
    hr = ev.load_cases(['33'])[0]
    [(name, data)] = ev.case_files(hr)
    assert name == 'hr-changes-2026-10.csv' and 'José Núñez'.encode('cp1252') in data and b'\xc3' not in data
    assert 'José Núñez' in ev.sample_text(hr) and name in ev.request_text(hr)
    proxy = ev.load_cases(['29'])[0]
    files = ev.case_files(proxy)
    assert [len(d.splitlines()) for _, d in files] == [16001, 16001] and files == ev.case_files(proxy)
    assert ev.sample_text(proxy).count('\n2026-10-0') == 2 * (ev.EXCERPT - 1)


def test_the_user_runs_the_upload_commands_and_applies_the_index_template(tmp_path):
    import asyncio
    import json
    import httpx
    import respx
    import run_agent
    (tmp_path / 'a.csv').write_bytes(b'time,user\n1,alice\n')
    upload = 'http://127.0.0.1:8765/upload/TICKET'
    commands = {'commands': [{'file': 'a.csv', 'bash': f'curl -sS --fail-with-body --data-binary "@a.csv" "{upload}"'}]}
    template = 'PUT _index_template/eval-x\n{"index_patterns": ["eval-x*"]}'
    events = [{'type': 'user', 'message': {'content': [
        {'type': 'tool_result', 'content': [{'type': 'text', 'text': json.dumps(commands)}]},
        {'type': 'tool_result', 'content': json.dumps({'status': 'proposed', 'dev_tools': template})}]}}]
    with respx.mock:
        posted = respx.post(upload).mock(return_value=httpx.Response(200, text='{"stream_id": 41}'))
        put = respx.put(f'{run_agent.ES}/_index_template/eval-x').mock(return_value=httpx.Response(200, json={}))
        side = run_agent.UserSide(tmp_path)
        said = asyncio.run(side.act(events))
        assert posted.calls[0].request.content == b'time,user\n1,alice\n' and put.called
        assert 'a.csv: {"stream_id": 41}' in said and 'applied PUT _index_template/eval-x: HTTP 200' in said
        assert asyncio.run(side.act(events)) is None     # each once


def test_the_user_knows_what_the_case_says_only_when_asked():
    import run_agent
    times = ev.load_cases(['31'])[0]
    message = run_agent.first_message('PROMPT', times, 'eval-31-x', 'EVAL-31-X')
    assert '+10:00' not in message and 'Sydney' in message   # the request names the place, not the offset
    proxy = ev.load_cases(['29'])[0]
    assert 'proxy-2026-10-01.csv, proxy-2026-10-02.csv' in run_agent.first_message('PROMPT', proxy, 'b', 'F')


def test_the_workflows_and_their_comparison_of_events():
    from lxml import etree
    import workflows
    assert {'document_index', 'fix_errors', 'change_event_type', 'records_output'} <= set(workflows.WORKFLOWS)
    a = etree.fromstring('<Event xmlns="event-logging:3"><EventDetail><TypeId>LOGON</TypeId><Authenticate>'
                         '<Action>Logon</Action><Data Name="x" Value="1"/></Authenticate></EventDetail></Event>')
    b = etree.fromstring('<Event xmlns="event-logging:3">\n  <EventDetail>\n    <TypeId>LOGON</TypeId>\n    '
                         '<Authenticate><Action>Logon</Action>\n<Data Value="1" Name="x" /></Authenticate>\n  '
                         '</EventDetail>\n</Event>')
    assert workflows._canonical(a) == workflows._canonical(b)
    assert workflows._canonical(a) != workflows._canonical(etree.fromstring(
        etree.tostring(a).decode().replace('Logon', 'Logoff')))
