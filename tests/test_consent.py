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
    with pytest.raises(ToolError, match='Unknown or expired'):
        await store.require(ctx, 'approval', 'promote_build', 'Promote', {'plan': ['a']}, pending['approval_id'])


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
