"""build_translation_xslt hands back a documentation table only from a sampled run."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_xsltgen import SCHEMA, mapping
from tools import generation


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
