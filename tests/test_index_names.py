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
    # ECS: an action's own element under the source's namespace (named after the index), the profile's names as ECS's.
    assert nested['EventDetail/Alert/Type'] == 'firewall_edge.alert.type'
    assert nested['EventDetail/Network/*/Source/Port'] == 'source.port'


async def test_following_ecs_what_happened_and_data_elements_are_named_as_ecs_names_them():
    # Asked by the user: the full ECS schema, not only the profile's few names. Any action's Action and Outcome as
    # ECS's event.action and event.outcome (success/failure), a Data element whose name is an ECS field's under that
    # name, and the template composed of Elastic's ecs@mappings.
    profiles = {p.stem: yaml.safe_load(p.read_text(encoding='utf-8')) for p in CONVENTIONS.glob('*.yaml')}
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(default_convention=None))})
    summary = {'path_population': POPULATED, 'data_names': {
        'EventDetail/Network/Permit/Data': {'user_agent_original': 35.0, 'session_id': 35.0}}}
    agreed = SimpleNamespace(require=AsyncMock(return_value=None))
    with patch.object(indexing, 'summarise_events', AsyncMock(return_value=summary)), \
            patch.object(indexing, '_conventions', lambda c: profiles), \
            patch.object(indexing, 'consent_from', lambda c: agreed), \
            patch.object(indexing, '_build_of_stream', AsyncMock(return_value=None)):
        result = await indexing.draft_index_mapping(ctx, 'elasticsearch', 'ecs-firewall-v1', events_stream_ids=[1],
                                                    convention='ecs', without_example=True)
    fields = {f['name']: f for f in result['plan']['fields']}
    assert fields['event.action']['source'] == 'EventDetail/*/Action'
    assert fields['event.outcome']['transform'] == 'outcome' and fields['process.command_line']['type'] == 'keyword'
    assert not {'authenticate.action', 'authenticate.outcome.success', 'process.action'} & set(fields)   # not twice
    # An action's own elements go under the source's namespace, as ECS asks: never a top-level group per action
    # (alert.*), nor a name in an ECS field set that ECS doesn't define (process.type).
    assert fields['firewall.process.type']['source'] == 'EventDetail/Process/Type' and 'firewall.alert.type' in fields
    assert not {'process.type', 'alert.type'} & set(fields) and result['plan']['custom_namespaces'] == ['firewall']
    assert fields['user_agent.original']['source'] == "EventDetail/Network/Permit/Data[@Name='user_agent_original']/@Value"
    assert not any('session_id' in f['source'] for f in fields.values())                 # not an ECS field's name
    assert result['ecs_data_elements'] and 'ecs_check' not in result
    assert result['plan']['convention'] == 'ecs' and result['rendered']['body']['composed_of'] == ['ecs@mappings']


async def test_an_example_template_declares_its_own_names_not_reported_as_un_ecs():
    _, result = await draft(example_template=EXAMPLE)
    assert 'ecs_check' not in result and {'Alert', 'Authenticate', 'User'} <= set(result['plan']['custom_namespaces'])


async def test_a_plan_is_amended_with_only_what_changes_and_the_tools_take_it_from_the_session():
    # Seen in Ask Stroom AI: past the client's step cap the agent had lost its earlier extra_fields and spent eight
    # calls recovering them, and the 50-field plan went inline to save_xslt, create_index_doc and
    # propose_index_template, five times in all.
    from fastmcp.exceptions import ToolError
    from tools import translation
    from utils.fieldplan import PlannedField
    profiles = {p.stem: yaml.safe_load(p.read_text(encoding='utf-8')) for p in CONVENTIONS.glob('*.yaml')}
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(default_convention=None))})
    agreed = SimpleNamespace(require=AsyncMock(return_value=None))
    summary = AsyncMock(return_value={'path_population': POPULATED})
    with patch.object(indexing, 'summarise_events', summary), patch.object(indexing, '_conventions', lambda c: profiles), \
            patch.object(indexing, 'consent_from', lambda c: agreed), \
            patch.object(indexing, '_build_of_stream', AsyncMock(return_value=None)):
        drafted = await indexing.draft_index_mapping(
            ctx, 'elasticsearch', 'ecs-firewall-v1', events_stream_ids=[1], convention='ecs', without_example=True,
            extra_fields=[PlannedField(name='firewall.configuration', type='keyword',
                                       source='EventDetail//Configuration/Type')])
        # A // source covers the paths below it: not reported as unmapped.
        assert not any('Configuration' in p for p in drafted['populated_paths_not_mapped'])
        summary.reset_mock()
        amended = await indexing.draft_index_mapping(ctx, 'elasticsearch', 'ecs-firewall-v1', changes={'fields': [
            {'name': 'firewall.alert.type', 'remove': True},
            {'name': 'firewall.configuration', 'type': 'keyword', 'source': 'EventDetail/Update/After/Configuration/Type'},
            {'name': 'acme.ticket', 'type': 'keyword', 'source': "EventDetail/Alert/Data[@Name='Ticket']/@Value"}]})
        with pytest.raises(ToolError, match=r"No plan for 'ecs-other-v1'.*Drafted in this session: \['ecs-firewall-v1'\]"):
            await indexing.draft_index_mapping(ctx, 'elasticsearch', 'ecs-other-v1', changes={'drop_when': []})
    assert amended['changed'] == ["field 'firewall.alert.type' removed", "field 'firewall.configuration' replaced",
                                  "field 'acme.ticket' added"]
    assert not summary.await_count and 'populated_paths_not_mapped' not in amended     # no streams given: not read
    names = [f['name'] for f in amended['plan']['fields']]
    assert 'firewall.alert.type' not in names and names[-1] == 'acme.ticket'
    assert len(names) == len(drafted['plan']['fields']) and 'firewall.configuration' in names     # earlier extras kept
    assert [p.split(' ')[0] for p in amended['ecs_check']] == ['acme.*']
    # Saved with no code and no plan: the one amended in the session, not pasted again.
    with patch.object(translation, 'create_xslt', AsyncMock(return_value={'uuid': 'i-1'})) as create:
        await translation.save_xslt(ctx, 'fw-v1', 'FW-INDEX-XSLT')
    saved = create.await_args.args[5]
    assert [f.name for f in saved.fields] == names and create.await_args.args[3] == saved.xslt()
    # The plan kept with a pipeline's indexing XSLT is what the pipeline writes: it comes first.
    kept = {'kind': 'index', 'payload': {**amended['plan'], 'drop_when': ['x']}}
    with patch('tools.builds.kept_mapping', AsyncMock(return_value=kept)):
        assert (await indexing.plan_given(ctx, None, pipeline_uuid='p1')).drop_when == ['x']
    assert (await indexing.plan_given(ctx, None, ['ecs-firewall-v1'])).fields[-1].name == 'acme.ticket'
    assert await indexing.plan_given(ctx, None, ['ecs-elsewhere-v1']) is None
