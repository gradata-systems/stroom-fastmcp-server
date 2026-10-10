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
    question, answer = client.asked[0]
    from fastmcp.server.elicitation import parse_elicit_response_type
    field = parse_elicit_response_type(answer).schema['properties']['value']
    assert field['enum'] == ['From an index template', 'Follow an existing index in Stroom',
                             'ECS (Elastic Common Schema) convention', 'Stroom flat convention']
    # Titled with the question, which VS Code's chat history shows beside the answer (it showed "Q: Value").
    assert field['title'] == question == "How should the new index's fields be named?"
    assert got['status'] == 'chosen' and 'draft_index_mapping convention=stroom-flat' in got['hint']
    assert "isn't asked again" in got['hint']


async def drafting(ctx, **kw):
    """draft_index_mapping as far as its questions: past them, it reads the streams (stopped there)."""
    with patch.object(indexing, 'require_events', AsyncMock()),             patch.object(indexing, 'summarise_events', AsyncMock(side_effect=RuntimeError('past the questions'))),             patch('utils.consent._modern', lambda ctx: False):
        try:
            return await indexing.draft_index_mapping(ctx, 'elasticsearch', 'fortigate-v1', events_stream_ids=[7], **kw)
        except RuntimeError as e:
            return str(e)


async def test_a_convention_chosen_in_the_form_is_not_asked_about_again():
    # Seen in VS Code: the user picked ECS in the form, the agent drafted with convention=ecs (no without_example),
    # and the user was asked the same question again; with without_example, they'd have confirmed it again instead.
    client = FormClient('ECS (Elastic Common Schema) convention')
    ctx = ctx_with(client)
    profiles = {'ecs': {'description': 'ECS'}, 'stroom-flat': {}}
    with patch.object(indexing, '_conventions', lambda ctx: profiles), patch('utils.consent._modern', lambda ctx: False):
        await indexing.get_field_conventions(ctx, backend='elasticsearch')
        assert await drafting(ctx, convention='ecs') == 'past the questions'
        assert await drafting(ctx, convention='ecs', without_example=True) == 'past the questions'
        assert len(client.asked) == 1
        # Another convention than the one chosen is asked about again, once, and drafted as the user answers.
        client.picks.append('Stroom flat convention')
        assert await drafting(ctx, convention='stroom-flat') == 'past the questions' and len(client.asked) == 2


async def test_a_choice_the_agent_relays_is_still_confirmed():
    # Without forms, the agent says what the user chose: drafting from a convention alone stays the user's to confirm.
    ctx = ctx_with(FormClient())
    ctx.lifespan_context['consent'] = ConsentStore(use_elicitation=False)
    with patch.object(indexing, '_conventions', lambda ctx: {'ecs': {}}):
        await indexing.get_field_conventions(ctx, backend='elasticsearch')
        gate = await drafting(ctx, convention='ecs', without_example=True)
    assert gate['status'] == 'needs_confirmation'


async def test_an_index_picked_in_the_form_is_followed_without_asking_again():
    client = FormClient('Follow an existing index in Stroom', 'Fortigate (System / Elastic Indices)')
    ctx = ctx_with(client, [('k', 'Keycloak'), ('f', 'Fortigate')])
    read = ({'template': {}}, 'Read from Fortigate.', {'doc': 'Fortigate', 'index': 'fortigate', 'fields': 3,
                                                        'source': 'Stroom', 'examples': ['User.Id']})
    with patch.object(indexing, '_conventions', lambda ctx: {'ecs': {}}), patch('utils.consent._modern', lambda ctx: False),             patch.object(indexing, '_example_from_index', AsyncMock(return_value=read)):
        await indexing.get_field_conventions(ctx, backend='elasticsearch')
        assert await drafting(ctx, like_index='f') == 'past the questions'
    assert len(client.asked) == 2       # the two picks, and no confirmation after them


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



async def test_on_a_modern_connection_the_choices_are_a_form_returned_and_answered_with_the_repeated_call():
    # VS Code may connect with the 2026-07-28 protocol, with no server-initiated requests: the form is the result.
    import mcp_types
    from tests.test_consent import ModernCtx

    def ctx(**kwargs):
        c = ModernCtx(**kwargs)
        base = ctx_with(FormClient(), [('k', 'Keycloak'), ('f', 'Fortigate')])
        c.lifespan_context = base.lifespan_context
        return c
    store = ConsentStore(use_elicitation=True)
    with patch.object(indexing, '_conventions', lambda ctx: {}):
        first = ctx()
        first.lifespan_context['consent'] = store
        asked = await indexing.get_field_conventions(first, backend='elasticsearch')
        assert isinstance(asked, mcp_types.InputRequiredResult)
        [(key, form)] = asked.input_requests.items()
        assert form.params.requested_schema['properties']['choice']['enum'][1] == 'Follow an existing index in Stroom'
        second = ctx(responses={key: {'action': 'accept', 'content': {'choice': 'Follow an existing index in Stroom'}}},
                     state=asked.request_state)
        second.lifespan_context['consent'] = store
        which = await indexing.get_field_conventions(second, backend='elasticsearch')
        [(key2, form2)] = which.input_requests.items()
        assert form2.params.message == 'Which existing index should it follow?'
        third = ctx(responses={key2: {'action': 'accept', 'content': {'choice': 'Fortigate (System / Elastic Indices)'}}},
                    state=which.request_state)
        third.lifespan_context['consent'] = store
        got = await indexing.get_field_conventions(third, backend='elasticsearch')
    assert got['status'] == 'chosen' and got['like_index'] == 'f'


async def test_a_pipeline_copy_leaves_out_the_documents_set_properties_replaces():
    # Seen in e2e: v2's XSLT was made first, then v1's was copied and renamed to the same name, which the build refused.
    from tools import pipeline_writes
    from tools.pipeline_writes import PropertyValue
    source = {'name': 'ACME-V1 - Indexing', 'pipelineData': {'properties': {'add': [
        {'element': 'xsltFilter', 'name': 'xslt', 'value': {'entity': {'type': 'XSLT', 'uuid': 'x1', 'name': 'ACME-V1-XSLT'}}},
        {'element': 'dsParser', 'name': 'textConverter',
         'value': {'entity': {'type': 'TextConverter', 'uuid': 't1', 'name': 'ACME-V1'}}}]}}}
    stroom = SimpleNamespace(get_doc=AsyncMock(return_value=source))
    ctx = SimpleNamespace(lifespan_context={'consent': ConsentStore(use_elicitation=False)})
    with patch.object(pipeline_writes, 'gateway_from', lambda ctx: stroom), \
            patch.object(pipeline_writes, 'guard_from', lambda ctx: SimpleNamespace()), \
            patch('tools.templates.template_reason', AsyncMock(return_value=None)):
        asked = await pipeline_writes.copy_pipeline(
            ctx, build='acme-v2', source_uuid='p1', new_name='ACME-V2 - Indexing', rename={'V1': 'V2'},
            set_properties=[PropertyValue(element='xsltFilter', name='xslt', doc_uuid='x2', doc_type='XSLT')])
    assert asked['status'] == 'needs_confirmation' and asked['details']['copied documents'] == ['ACME-V2']


async def test_a_choice_made_in_the_drafts_own_form_drafts_as_chosen():
    # Seen in VS Code: drafted with no choice yet, draft_index_mapping asked in its form, the user picked ECS, and it
    # still returned "not drafted: ask the user", so the agent asked them a third time in the chat.
    client = FormClient('ECS (Elastic Common Schema) convention')
    ctx = ctx_with(client)
    with patch.object(indexing, '_conventions', lambda ctx: {'ecs': {}, 'stroom-flat': {}}):
        assert await drafting(ctx, convention='stroom-flat') == 'past the questions'
    assert len(client.asked) == 1
    # Following an index picked there too.
    client = FormClient('Follow an existing index in Stroom', 'Fortigate (System / Elastic Indices)')
    ctx = ctx_with(client, [('k', 'Keycloak'), ('f', 'Fortigate')])
    read = ({'template': {}}, None, {'doc': 'Fortigate', 'index': 'fortigate', 'fields': 3, 'source': 'Stroom',
                                     'examples': ['User.Id']})
    with patch.object(indexing, '_conventions', lambda ctx: {'ecs': {}}), \
            patch.object(indexing, '_example_from_index', AsyncMock(return_value=read)) as followed:
        assert await drafting(ctx, convention='ecs') == 'past the questions'
    assert followed.await_args.args[1] == 'f' and len(client.asked) == 2


async def test_a_confirmation_is_titled_with_what_it_agrees_to():
    # Seen in VS Code: every answer in the chat history read "Q: Value"; the question is the field's title now.
    from fastmcp.server.elicitation import parse_elicit_response_type
    client = FormClient(SimpleNamespace(value=True))
    store = ConsentStore(use_elicitation=True)
    ctx = SimpleNamespace(lifespan_context={'consent': store}, elicit=client.elicit)
    with patch('utils.consent._modern', lambda ctx: False):
        assert await store.require(ctx, 'confirmation', 'create_feed', 'Create feed ACME-VPN-V1.0', {'a': 1}, None) is None
    field = parse_elicit_response_type(client.asked[0][1]).schema['properties']['value']
    assert field == {'title': 'Create feed ACME-VPN-V1.0', 'type': 'boolean'}
