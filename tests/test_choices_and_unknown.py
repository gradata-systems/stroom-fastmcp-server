"""Choices asked in a form whatever the model; Unknown events said for what they are, and never 'accepted as benign'."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tools import indexing
from tools.stepping import _what_unknown_holds
from utils.consent import ConsentStore


class FormClient:
    """A client that shows forms (VS Code holds the call open); answers with the given picks, in turn."""

    def __init__(self, *picks):
        self.picks, self.asked = list(picks), []

    async def elicit(self, message, response_type):
        self.asked.append((message, response_type))
        pick = self.picks.pop(0)
        return SimpleNamespace(action='accept', data=pick) if pick else SimpleNamespace(action='cancel', data=None)


def ctx_with(client, existing=()):
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value={'values': [
        {'docRef': {'type': 'ElasticIndex', 'uuid': u, 'name': n}, 'path': 'System / Elastic Indices'} for u, n in existing]}),
        settings=SimpleNamespace(default_convention=None))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': ConsentStore(use_elicitation=True)},
                          elicit=client.elicit)
    return ctx


async def test_the_naming_choice_is_a_form_with_the_options_as_a_picker():
    # Seen: given the choices, agents (Gemma, and Claude too) asked in the chat, with no picker.
    client = FormClient('Stroom flat convention')
    profiles = {'ecs': {'description': 'ECS'}, 'stroom-flat': {}}
    with patch.object(indexing, '_conventions', lambda ctx: profiles), patch('utils.consent._modern', lambda ctx: False):
        got = await indexing.get_field_conventions(ctx_with(client), backend='elasticsearch')
    question, options = client.asked[0]
    assert options == ['From an index template', 'Follow an existing index in Stroom',
                       'ECS (Elastic Common Schema) convention', 'Stroom flat convention']
    assert got['status'] == 'chosen' and 'convention=stroom-flat without_example=true' in got['hint']


async def test_following_an_index_asks_which_and_a_cancelled_form_stops():
    client = FormClient('Follow an existing index in Stroom', 'Fortigate (System / Elastic Indices)')
    with patch.object(indexing, '_conventions', lambda ctx: {}), patch('utils.consent._modern', lambda ctx: False):
        got = await indexing.get_field_conventions(ctx_with(client, [('k', 'Keycloak'), ('f', 'Fortigate')]),
                                                   backend='elasticsearch')
    assert got['like_index'] == 'f' and 'like_index=f' in got['hint']
    with patch.object(indexing, '_conventions', lambda ctx: {}), patch('utils.consent._modern', lambda ctx: False), \
            pytest.raises(ToolError, match='The user made no choice'):
        await indexing.get_field_conventions(ctx_with(FormClient(None)), backend='elasticsearch')


async def test_without_forms_the_agent_is_given_the_choices_to_ask():
    ctx = ctx_with(FormClient())
    ctx.lifespan_context['consent'] = ConsentStore(use_elicitation=False)
    with patch.object(indexing, '_conventions', lambda ctx: {'ecs': {}}):
        got = await indexing.get_field_conventions(ctx, backend='elasticsearch')
    assert got['status'] == 'needs_guidance' and got['options'][0]['choice'] == 'From an index template'


def test_the_unknown_records_are_said_for_what_they_hold():
    # Seen: the user asked to agree to "3 of 50 records" as Unknown, with nothing to say which events they were.
    out = ('<Events xmlns="event-logging:3"><Event><EventDetail><TypeId>traffic</TypeId><Unknown>'
           '<Data Name="log_action" Value="server-rst"/><Data Name="service" Value="HTTPS"/></Unknown></EventDetail>'
           '</Event></Events>')
    held = _what_unknown_holds([out, out.replace('server-rst', 'timeout')])
    assert held == 'TypeId: traffic; log_action: server-rst, timeout; service: HTTPS'


async def test_unknown_is_not_accepted_as_a_benign_error():
    from tools import builds
    with patch.object(builds, 'gateway_from', lambda ctx: None), pytest.raises(ToolError, match="aren't an error to accept"):
        await builds.write_documentation(SimpleNamespace(lifespan_context={}), build='b', pipeline_uuid='p',
                                         markdown='## Purpose\n', accept_errors=[{
                                             'element': 'translationFilter', 'reason': 'odd actions',
                                             'example': '3 of 50 records come out as EventDetail/Unknown'}])
