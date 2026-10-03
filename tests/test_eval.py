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
    assert len(cases) == 19 and len({c['id'] for c in cases}) == 19
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
