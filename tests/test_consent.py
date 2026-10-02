from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from tools.stepping import _empty_output
from utils.consent import ConsentStore


async def test_id_flow_binds_the_id_to_the_exact_request():
    store = ConsentStore(use_elicitation=False)
    ctx = SimpleNamespace()
    pending = await store.require(ctx, 'approval', 'promote_build', 'Promote', {'plan': ['a']}, None)
    assert pending['status'] == 'needs_approval'
    with pytest.raises(ToolError, match='different request'):
        await store.require(ctx, 'approval', 'promote_build', 'Promote', {'plan': ['b']}, pending['approval_id'])
    pending = await store.require(ctx, 'approval', 'promote_build', 'Promote', {'plan': ['a']}, None)
    assert await store.require(ctx, 'approval', 'promote_build', 'Promote', {'plan': ['a']}, pending['approval_id']) is None
    with pytest.raises(ToolError, match='already used'):
        await store.require(ctx, 'approval', 'promote_build', 'Promote', {'plan': ['a']}, pending['approval_id'])


async def test_ids_are_self_contained_and_verifiable_by_any_replica_with_the_keys():
    key = 'k' * 32
    one, two, other = ConsentStore(False, keys=[key]), ConsentStore(False, keys=['rotated' * 5, key]), ConsentStore(False, keys=['x' * 32])
    ctx = SimpleNamespace()
    pending = await one.require(ctx, 'confirmation', 'create_feed', 'Create', {'feed name': 'A'}, None)
    token = pending['confirmation_id']
    assert token.startswith('conf-') and '.' in token
    # Another replica with the same (or a rotated set including the) key accepts it; one without refuses.
    assert await two.require(ctx, 'confirmation', 'create_feed', 'Create', {'feed name': 'A'}, token) is None
    with pytest.raises(ToolError, match='Unknown or expired'):
        await other.require(ctx, 'confirmation', 'create_feed', 'Create', {'feed name': 'A'}, token)
    # Tampered or expired tokens are refused; a kept token survives until discarded.
    body, _, sig = token.rpartition('.')
    with pytest.raises(ToolError, match='Unknown or expired'):
        await one.require(ctx, 'confirmation', 'create_feed', 'Create', {'feed name': 'A'}, f'{body}.{"0" * 32}')
    stale = 'conf-' + one._seal({'kind': 'confirmation', 'action': 'create_feed', 'digest': 'd', 'user': None, 'expires': 1})
    with pytest.raises(ToolError, match='Unknown or expired'):
        await one.require(ctx, 'confirmation', 'create_feed', 'Create', {'feed name': 'A'}, stale)
    assert await one.require(ctx, 'confirmation', 'create_feed', 'Create', {'feed name': 'A'}, token, keep=True) is None
    assert await one.require(ctx, 'confirmation', 'create_feed', 'Create', {'feed name': 'A'}, token, keep=True) is None
    one.discard(token)
    with pytest.raises(ToolError, match='already used'):
        await one.require(ctx, 'confirmation', 'create_feed', 'Create', {'feed name': 'A'}, token)


class Answer:
    def __init__(self, action, data):
        self.action, self.data = action, data


async def test_elicitation_is_used_when_the_client_supports_it():
    store = ConsentStore()
    asked = []

    async def elicit(message, response_type):
        asked.append(message)
        return Answer('accept', True)
    assert await store.require(SimpleNamespace(elicit=elicit), 'confirmation', 'create_feed', 'Create feed X',
                               {'feed name': 'X'}, None) is None
    assert 'feed name: X' in asked[0]

    async def decline(message, response_type):
        return Answer('decline', None)
    with pytest.raises(ToolError, match='did not agree'):
        await store.require(SimpleNamespace(elicit=decline), 'confirmation', 'create_feed', 'Create feed X', {}, None)


async def test_client_without_elicitation_falls_back_to_an_id():
    async def unsupported(message, response_type):
        raise RuntimeError('Client does not support elicitation')
    result = await ConsentStore().require(SimpleNamespace(elicit=unsupported), 'approval', 'x', 'X', {}, None)
    assert result['status'] == 'needs_approval'


def test_output_without_elements_is_an_error():
    result = {'stepData': {'elementMap': {'translationFilter': {'output': '<?xml version="1.1"?>2026-09-28alice'}}}}
    assert 'did not match' in _empty_output(result, 'translationFilter', 'r1')[0]['message']
    ok = {'stepData': {'elementMap': {'translationFilter': {'output': '<?xml version="1.1"?><Events/>'}}}}
    assert _empty_output(ok, 'translationFilter', 'r1') == []


class ModernCtx:
    """A 2026-07-28 request: no server-initiated requests; answers arrive with the repeated call."""

    def __init__(self, forms=True, responses=None, state=None):
        caps = {'elicitation': {'form': {}}} if forms else {}
        self.request_context = SimpleNamespace(meta={'io.modelcontextprotocol/clientCapabilities': caps})
        self.input_responses, self.request_state = responses, state

    def _is_modern_protocol(self):
        return True


async def test_modern_connections_ask_with_a_form_and_read_the_answer_on_the_repeated_call():
    import mcp_types
    store = ConsentStore()
    details = {'feed name': 'ACME'}
    asked = await store.require(ModernCtx(), 'confirmation', 'create_feed', "Create feed 'ACME'", details, None)
    assert isinstance(asked, mcp_types.InputRequiredResult)
    [(key, form)] = asked.input_requests.items()
    assert "Create feed 'ACME'" in form.params.message and form.params.requested_schema['properties']['value']['type'] == 'boolean'
    accept = {key: {'action': 'accept', 'content': {'value': True}}}
    assert await store.require(ModernCtx(responses=accept, state=asked.request_state), 'confirmation', 'create_feed',
                               "Create feed 'ACME'", details, None) is None
    decline = {key: {'action': 'accept', 'content': {'value': False}}}
    with pytest.raises(ToolError, match='did not agree'):
        await store.require(ModernCtx(responses=decline, state=asked.request_state), 'confirmation', 'create_feed',
                            "Create feed 'ACME'", details, None)


async def test_a_form_answer_only_counts_for_the_request_it_was_asked_for():
    store = ConsentStore()
    asked = await store.require(ModernCtx(), 'approval', 'promote_build', 'Promote', {'plan': ['a']}, None)
    [key] = asked.input_requests
    answer = {key: {'action': 'accept', 'content': {'value': True}}}
    # the same answer against different details is not an answer to this question: it asks again
    again = await store.require(ModernCtx(responses=answer, state=asked.request_state), 'approval', 'promote_build',
                                'Promote', {'plan': ['b']}, None)
    assert again.input_requests and list(again.input_requests) != [key]


async def test_modern_clients_without_forms_get_an_id():
    pending = await ConsentStore().require(ModernCtx(forms=False), 'approval', 'x', 'X', {}, None)
    assert pending['status'] == 'needs_approval'
