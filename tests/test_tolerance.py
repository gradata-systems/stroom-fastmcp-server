"""create_pipeline takes the shapes small models send: the template by name, a description, no properties yet, and
the build this session is on."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tools import pipeline_writes, plan


def test_the_build_is_remembered_for_tools_called_without_it():
    ctx = SimpleNamespace(lifespan_context={})
    with pytest.raises(ToolError, match='create_pipeline needs build'):
        plan.resolve_build(ctx, None, 'create_pipeline')
    assert plan.resolve_build(ctx, 'onboard-fortios', 'create_pipeline') == 'onboard-fortios'
    assert plan.resolve_build(ctx, None, 'create_pipeline') == 'onboard-fortios'
    plan.remember_build(ctx, 'other')
    assert plan.resolve_build(ctx, None, 'save_xslt') == 'other'


async def test_the_template_may_be_a_name():
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value={'values': [
        {'docRef': {'type': 'Pipeline', 'uuid': 'd7c55950-7427-48ab-a1b6-11bc7b449dfd', 'name': 'Event Data (Text)'}}]}))
    assert await pipeline_writes._template_uuid(stroom, 'd7c55950-7427-48ab-a1b6-11bc7b449dfd') == 'd7c55950-7427-48ab-a1b6-11bc7b449dfd'
    assert await pipeline_writes._template_uuid(stroom, 'Event Data (Text)') == 'd7c55950-7427-48ab-a1b6-11bc7b449dfd'
    with pytest.raises(ToolError, match='Give template_uuid'):
        await pipeline_writes._template_uuid(stroom, None)
