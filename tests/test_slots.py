"""create_pipeline fills the template's open slots from the build, and refuses a parser with no converter to run."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tools import pipeline_writes
from tools.pipeline_writes import PropertyValue, fill_open_slots, open_slots
from tools.pipelines import merge_layers

TEXT_TEMPLATE = [{'pipelineData': {
    'elements': {'add': [{'id': 'dsParser', 'type': 'DSParser'}, {'id': 'translationFilter', 'type': 'XSLTFilter'},
                         {'id': 'decorationFilter', 'type': 'XSLTFilter'}, {'id': 'schemaFilter', 'type': 'SchemaFilter'}]},
    'links': {'add': [{'from': 'dsParser', 'to': 'translationFilter'}, {'from': 'translationFilter', 'to': 'decorationFilter'},
                      {'from': 'decorationFilter', 'to': 'schemaFilter'}]}}}]


async def test_open_slots_are_the_parsers_converter_and_the_first_xslt():
    slots = await open_slots(None, merge_layers(TEXT_TEMPLATE))
    assert [(s['element'], s['property']) for s in slots] == [('dsParser', 'textConverter'), ('translationFilter', 'xslt')]
    swapped = await open_slots(None, merge_layers([{'pipelineData': {
        'elements': {'add': [{'id': 'xmlParser', 'type': 'XMLParser'}, {'id': 'translationFilter', 'type': 'XSLTFilter'}]},
        'links': {'add': [{'from': 'xmlParser', 'to': 'translationFilter'}]}}}]), replace_parser='XMLFragmentParser')
    assert [(s['element'], s['property']) for s in swapped] == [('translationFilter', 'xslt'), ('xmlFragmentParser', 'textConverter')]


async def test_json_and_xml_parsers_need_no_converter():
    for parser in ('JSONParser', 'XMLParser'):
        merged = merge_layers([{'pipelineData': {
            'elements': {'add': [{'id': 'parser', 'type': parser}, {'id': 'translationFilter', 'type': 'XSLTFilter'}]},
            'links': {'add': [{'from': 'parser', 'to': 'translationFilter'}]}}}])
        assert [(s['element'], s['property']) for s in await open_slots(None, merged)] == [('translationFilter', 'xslt')]
        guard = SimpleNamespace(folder_contents=AsyncMock(return_value=[]))   # a build with no converter at all
        with patch.object(pipeline_writes, 'guard_from', lambda c: guard):
            props, filled, still_open = await fill_open_slots(SimpleNamespace(lifespan_context={'stroom': None}), 'b', merged, None, [])
        assert props == [] and filled == [] and still_open == ['translationFilter.xslt']


async def test_slots_are_filled_from_the_builds_only_candidates_or_refused():
    docs = [{'type': 'TextConverter', 'uuid': 'tc', 'name': 'FW'}, {'type': 'XSLT', 'uuid': 'x1', 'name': 'FW-Events'},
            {'type': 'XSLT', 'uuid': 'x2', 'name': 'FW-INDEX-XSLT'}]
    descriptions = {'x1': '--- stroom-mcp translation mapping (generated) ---\n{"mapping": {}}\n--- end of stroom-mcp mapping ---', 'x2': ''}
    stroom = SimpleNamespace(get_doc=AsyncMock(side_effect=lambda t, u: {'description': descriptions[u]}))
    guard = SimpleNamespace(folder_contents=AsyncMock(return_value=docs))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    merged = merge_layers(TEXT_TEMPLATE)
    with patch.object(pipeline_writes, 'guard_from', lambda c: guard):
        props, filled, still_open = await fill_open_slots(ctx, 'b', merged, None, [])
    assert [(p.element, p.name, p.doc_uuid) for p in props] == [('dsParser', 'textConverter', 'tc'), ('translationFilter', 'xslt', 'x1')]
    assert filled == ['dsParser.textConverter = FW (the build\'s only TextConverter)', 'translationFilter.xslt = FW-Events (the build\'s only XSLT)']
    assert still_open == []
    # Given properties are kept; the XSLT with the mapping wins over the indexing one; no converter at all is refused.
    with patch.object(pipeline_writes, 'guard_from', lambda c: guard):
        props, filled, _ = await fill_open_slots(ctx, 'b', merged, None, [PropertyValue(element='dsParser', name='textConverter', doc_uuid='other', doc_type='TextConverter')])
    assert [p.doc_uuid for p in props] == ['other', 'x1'] and len(filled) == 1
    guard.folder_contents = AsyncMock(return_value=[d for d in docs if d['type'] != 'TextConverter'])
    with patch.object(pipeline_writes, 'guard_from', lambda c: guard):
        with pytest.raises(ToolError, match='needs a text converter and build .b. has none'):
            await fill_open_slots(ctx, 'b', merged, None, [])
    # No XSLT yet: the pipeline may be created, with the slot reported as still open.
    guard.folder_contents = AsyncMock(return_value=[docs[0]])
    with patch.object(pipeline_writes, 'guard_from', lambda c: guard):
        props, filled, still_open = await fill_open_slots(ctx, 'b', merged, None, [])
    assert still_open == ['translationFilter.xslt'] and [p.name for p in props] == ['textConverter']
    # Two converters: the model must choose.
    guard.folder_contents = AsyncMock(return_value=docs + [{'type': 'TextConverter', 'uuid': 'tc2', 'name': 'FW2'}])
    with patch.object(pipeline_writes, 'guard_from', lambda c: guard):
        with pytest.raises(ToolError, match='has 2 TextConverter documents'):
            await fill_open_slots(ctx, 'b', merged, None, [])
