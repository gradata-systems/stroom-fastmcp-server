"""Stream types taken from the feed and the pipeline, and templates and loaders found by what they are."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from security.policy import AccessPolicy
from tools import processing_writes, reference, templates
from utils.profile import profile


def test_a_header_of_names_over_text_columns_is_a_header():
    # Seen: user,name,department,site over people's names, read as a record, its columns col1 ...
    assert profile('user,name,department,site\nalice,Alice Anderson,Operations,HQ\nbob,Bob Brown,Finance,HQ\n')['columns'] \
        == ['user', 'name', 'department', 'site']
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dev'))
    from format_samples import SAMPLES
    assert profile(SAMPLES['csv_noheader'])['has_header'] is False


async def test_a_pipeline_that_leaves_its_xslt_for_a_child_is_a_template_wherever_it_is():
    index = {'t': {'uuid': 't', 'name': 'json-in v3', 'path': 'Acme/Pipeline Bases', 'parent_uuid': None},
             'p': {'uuid': 'p', 'name': 'ACME-V1-Events', 'path': 'Acme/Sources', 'parent_uuid': None}}

    async def shape(stroom, uuid, markers=None):
        open_slots = [{'element': 'translationFilter', 'property': 'xslt'}] if uuid == 't' else []
        return {'stage': 'translation', 'backend': None, 'parser': 'JSONParser', 'child_must_supply': open_slots,
                'properties': {}}
    ctx = SimpleNamespace(lifespan_context={'policy': AccessPolicy()})
    with patch.object(templates, '_pipeline_index', AsyncMock(return_value=index)), \
            patch.object(templates, 'gateway_from', lambda c: None), patch.object(templates, '_shape', shape):
        found = (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
    # A source's own complete pipeline (its XSLT set) is not a template, though it has no parent either.
    assert [(c['name'], c['source']) for c in found] == [('json-in v3', 'template_like')]


async def test_a_references_loader_is_the_feeds_or_the_only_one_never_a_name():
    stroom = SimpleNamespace(post=AsyncMock(return_value={'values': []}))
    with patch.object(reference, 'loader_pipelines', AsyncMock(return_value=[{'uuid': 'l1', 'name': 'Our loader'}])):
        assert await reference.resolve_loader(stroom, 'ACME-USERS') == 'l1'
    with patch.object(reference, 'loader_pipelines', AsyncMock(return_value=[{'uuid': 'a', 'name': 'A'}, {'uuid': 'b', 'name': 'B'}])), \
            pytest.raises(ToolError, match='several loaders: A, B'):
        await reference.resolve_loader(stroom, 'ACME-USERS')


async def test_stream_types_come_from_the_feed_and_the_pipeline():
    stroom = SimpleNamespace(get=AsyncMock(return_value={'uuid': 'f'}),
                             get_doc=AsyncMock(return_value={'streamType': 'Raw Reference'}),
                             pipeline_layers=AsyncMock(return_value=[{'pipelineData': {
                                 'elements': {'add': [{'id': 'streamAppender', 'type': 'StreamAppender'}]},
                                 'properties': {'add': [{'element': 'streamAppender', 'name': 'streamType',
                                                         'value': {'string': 'Records'}}]}}}]))
    assert await processing_writes.feed_stream_type(stroom, 'ACME-USERS') == 'Raw Reference'
    assert await processing_writes.output_stream_type(stroom, 'p') == 'Records'
