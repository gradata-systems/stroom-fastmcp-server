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
