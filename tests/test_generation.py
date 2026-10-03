"""build_translation_xslt: a documentation table only from a sampled run, and saving the XSLT by reference."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tests.test_xsltgen import SCHEMA, mapping
from tools import generation
from utils.xsltgen import generate


async def test_no_field_mapping_without_a_sample():
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))})
    with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)), \
            patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})):
        result = await generation.build_translation_xslt(ctx, mapping())
    assert result['ok'] and result['xslt']
    # A table from the mapping alone shows how values are computed; it used to be copied into docs as it was.
    assert result['field_mapping'] is None
    assert 'pipeline_uuid and stream_ids' in result['field_mapping_needs']


async def test_a_single_stream_id_is_taken_as_a_list_of_one():
    # Clients send the model's arguments as they are; one sample stream often arrives as a bare number, which
    # failed validation ("invalid input") and left the documentation to be written by hand.
    from fastmcp import Client, FastMCP

    server = FastMCP('test', lifespan=None)
    server.tool(generation.build_translation_xslt)
    stepped = AsyncMock(return_value={})
    pipeline = SimpleNamespace(default_outputs=lambda: ['translationFilter'])
    with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)), \
            patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})), \
            patch.object(generation, 'gateway_from', lambda ctx: SimpleNamespace(
                settings=SimpleNamespace(event_logging_version='4.1.0'))), \
            patch.object(generation._Pipeline, 'load', AsyncMock(return_value=pipeline)), \
            patch.object(generation, '_outputs', stepped):
        async with Client(server) as client:
            result = await client.call_tool('build_translation_xslt', {
                'mapping': mapping().model_dump(exclude_none=True), 'pipeline_uuid': 'p-1', 'stream_ids': 15783601})
    assert not result.is_error
    assert stepped.await_args.args[2] == [15783601]


def ctx_and_patches():
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))})
    return ctx, (patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)),
                 patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})))


async def test_saving_returns_the_document_not_the_code():
    # The XSLT need not pass through the model: generated, saved with its mapping, referred to by uuid.
    from tools import translation
    ctx, (schema, instructions) = ctx_and_patches()
    saved = {'type': 'XSLT', 'uuid': 'x-1', 'name': 'ACME-Events', 'version': 'v1', 'next': {'step': 'pipeline'}, 'done': False}
    with schema, instructions, patch.object(translation, 'create_xslt', AsyncMock(return_value=saved)) as create, \
            patch.object(translation, 'update_xslt', AsyncMock(return_value={**saved, 'version': 'v2'})) as update:
        result = await generation.build_translation_xslt(ctx, mapping(), build='acme-v1', name='ACME-Events')
        assert 'xslt' not in result and result['saved'] == {'type': 'XSLT', 'uuid': 'x-1', 'name': 'ACME-Events', 'version': 'v1'}
        assert result['next'] == {'step': 'pipeline'} and "uuid='x-1'" in result['hint']
        (_, build, name, code), kwargs = create.await_args
        assert (build, name) == ('acme-v1', 'ACME-Events') and code.startswith('<?xml') and kwargs['mapping'] == mapping()
        again = await generation.build_translation_xslt(ctx, mapping(), uuid='x-1', include_xslt=True)
        assert update.await_args.args[1] == 'x-1' and again['saved']['version'] == 'v2' and again['xslt']
        broken = mapping(events=[{'name': 'bare', 'fields': [{'path': 'EventDetail/Nope', 'value': 'x'}]}])
        failed = await generation.build_translation_xslt(ctx, broken, build='acme-v1', name='ACME-Events')
        assert not failed['ok'] and 'saved' not in failed and create.await_count == 1   # nothing saved
        with pytest.raises(ToolError, match='give name'):
            await generation.build_translation_xslt(ctx, mapping(), build='acme-v1')


async def test_save_xslt_generates_the_code_it_is_not_given():
    from tools import translation
    from utils.fieldplan import FieldPlan, PlannedField
    ctx, (schema, _) = ctx_and_patches()
    plan = FieldPlan(backend='lucene', index_name='acme', time_field='EventTime',
                     fields=[PlannedField(name='StreamId', type='id', source='@StreamId'),
                             PlannedField(name='EventTime', type='date', source='EventTime/TimeCreated')])
    with schema, patch.object(translation, 'event_schema', AsyncMock(return_value=SCHEMA), create=True), \
            patch.object(translation, 'gateway_from', lambda ctx: SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))), \
            patch.object(translation, 'create_xslt', AsyncMock(return_value={'uuid': 'i-1'})) as create:
        await translation.save_xslt(ctx, 'acme-v1', 'ACME-INDEX-XSLT', index_plan=plan)
        assert create.await_args.args[3] == plan.xslt()
        await translation.save_xslt(ctx, 'acme-v1', 'ACME-Events', mapping=mapping())
        assert create.await_args.args[3] == generate(mapping(), SCHEMA, '4.1.0')['xslt']
        with pytest.raises(ToolError, match='Give code'):
            await translation.save_xslt(ctx, 'acme-v1', 'ACME-Events')
