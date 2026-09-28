import sys
from pathlib import Path

import pytest

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


def test_ten_cases_each_with_a_reference_mapping_the_schema_accepts():
    cases = ev.load_cases()
    assert len(cases) == 10 and len({c['id'] for c in cases}) == 10
    for case in cases:
        assert {'name', 'template', 'request', 'expected', 'sample', 'reference'} <= set(case), case['id']
        result = generate(TranslationMapping.model_validate(case['reference']['mapping']), SCHEMA, '4.1.0')
        assert result['ok'], (case['id'], result['problems'])
        assert 'Sample:\n' in ev.agent_request(case) and 'stroom-flat' in ev.agent_request(case)


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


@pytest.mark.parametrize('payload, reply', [
    ({'kind': 'confirmation'}, {'approved': True}), ({'kind': 'template'}, {'approved': True}),
    ({'kind': 'enable'}, {'approved': True, 'note': 'enable it for me'}),
])
def test_the_scripted_user_agrees_and_enables(payload, reply):
    assert ev.respond(payload, {}, ev.Score('x', 'test')) == reply


def test_help_uses_the_cases_hints_and_counts_them():
    score = ev.Score('x', 'test')
    case = {'hints': ['The time is in UTC.']}
    assert ev.respond({'kind': 'help'}, case, score) == {'note': 'The time is in UTC.'}
    assert ev.respond({'kind': 'help'}, case, score)['note'].startswith('No hint')
    assert score.hints == 2


def test_summary_applies_the_exit_criterion():
    scores = [ev.Score(f'c{i}', 'test', passed=i < 8) for i in range(10)]
    assert 'meets the exit criterion' in ev.summary(scores)
    assert 'does not meet' in ev.summary([ev.Score(f'c{i}', 'test', passed=i < 7) for i in range(10)])
    assert 'partial run' in ev.summary(scores[:3])
