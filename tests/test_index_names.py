"""The index plan's fields: what happened planned by default, named nested where fields belong together."""
import json
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

from tools import indexing


@pytest.fixture(autouse=True)
def _events_streams():
    """The streams these tests index are Events: the stream-type check has its own test (tests/test_formats.py)."""
    from unittest.mock import AsyncMock, patch as _patch
    with _patch('tools.indexing.require_events', AsyncMock()):
        yield


CONVENTIONS = Path(__file__).parent.parent / 'conventions'
# The user's example in a VS Code run: a Twitter index's template, nesting some names (User.Id) and not others.
EXAMPLE = 'PUT _index_template/stroom_twitter\n' + json.dumps({'index_patterns': ['stroom-twitter*'], 'priority': 1,
    'template': {'mappings': {'properties': {
        'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}, '@timestamp': {'type': 'date'},
        'Active': {'type': 'boolean'}, 'IpAddress': {'type': 'ip'}, 'Text': {'type': 'text'}, 'Id': {'type': 'long'},
        'User': {'properties': {'Id': {'type': 'keyword'}, 'Name': {'type': 'keyword'},
                                'Location': {'type': 'keyword'}}}}}}})
# The paths the firewall's Events populated, % of events.
POPULATED = {'EventTime/TimeCreated': 100, 'EventSource/System/Name': 100, 'EventSource/Device/HostName': 100,
             'EventDetail/TypeId': 100, 'EventDetail/Description': 100,
             'EventDetail/Network/Permit/Source/Device/IPAddress': 35, 'EventDetail/Network/Permit/Source/Port': 35,
             'EventDetail/Network/Deny/Source/Device/IPAddress': 35, 'EventDetail/Network/Deny/Source/Port': 35,
             'EventDetail/Network/Deny/Destination/Device/IPAddress': 35, 'EventDetail/Network/Permit/Data': 35,
             'EventDetail/Alert/Type': 15, 'EventDetail/Alert/Severity': 15,
             'EventDetail/Authenticate/Action': 15, 'EventDetail/Authenticate/User/Id': 15,
             'EventDetail/Authenticate/Outcome/Success': 10, 'EventDetail/Process/Action': 5,
             'EventDetail/Process/Type': 5, 'EventDetail/Process/Command': 5,
             'EventDetail/Update/After/Configuration/Type': 10}


async def draft(**kwargs):
    profiles = {p.stem: yaml.safe_load(p.read_text(encoding='utf-8')) for p in CONVENTIONS.glob('*.yaml')}
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(default_convention=None))})
    with patch.object(indexing, 'summarise_events', AsyncMock(return_value={'path_population': POPULATED})), \
            patch.object(indexing, '_conventions', lambda c: profiles), \
            patch.object(indexing, '_build_of_stream', AsyncMock(return_value=None)):
        result = await indexing.draft_index_mapping(ctx, 'elasticsearch', 'firewall-edge-v1', events_stream_ids=[1],
                                                    **kwargs)
    return {f['source']: f['name'] for f in result['plan']['fields']}, result


async def test_what_happened_is_planned_and_nested_where_fields_belong_together():
    # Seen: the plan left out Alert, Authenticate, Process and Update; the user asked for Alert.Type and Alert.Severity,
    # then "the same wherever we establish nested structures"; the agent named them AuthAction, then Auth.Action.
    names, result = await draft(example_template=EXAMPLE)
    assert names['EventDetail/Alert/Type'] == 'Alert.Type' and names['EventDetail/Alert/Severity'] == 'Alert.Severity'
    assert names['EventDetail/Authenticate/Action'] == 'Authenticate.Action'
    assert names['EventDetail/Authenticate/Outcome/Success'] == 'Authenticate.Outcome.Success'
    assert names['EventDetail/Process/Command'] == 'Process.Command'
    assert names['EventDetail/Network/*/Source/Device/IPAddress'] == 'Source.IPAddress'
    assert names['EventDetail/Network/*/Source/Port'] == 'Source.Port'
    # Alone in its group: flat, as the example's own single names are.
    assert names['EventDetail/Update/After/Configuration/Type'] == 'ConfigurationType'
    assert names['EventSource/Device/HostName'] == 'HostName'
    assert names['EventDetail/Authenticate/User/Id'] == 'User.Id'     # the example's own name for the user
    assert not any('/Data' in s for s in names)
    plan = {f['name']: f for f in result['plan']['fields']}
    assert plan['Authenticate.Outcome.Success']['type'] == 'boolean'


async def test_without_an_example_the_profiles_style_names_them():
    agreed = SimpleNamespace(require=AsyncMock(return_value=None))
    with patch.object(indexing, 'consent_from', lambda ctx: agreed):
        flat, _ = await draft(convention='stroom-flat', without_example=True)
        nested, _ = await draft(convention='ecs', without_example=True)
    assert flat['EventDetail/Alert/Type'] == 'AlertType' and flat['EventDetail/Network/*/Source/Port'] == 'SourcePort'
    assert nested['EventDetail/Alert/Type'] == 'alert.type' and nested['EventDetail/Network/*/Source/Port'] == 'source.port'
