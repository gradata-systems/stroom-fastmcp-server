"""One bad item from a production Stroom (a pipeline it can no longer load, a document deleted since a search listed
it, a stream on a lost volume) is said or passed over; the rest of the answer is still given (asked for by the user:
describe_feed failed outright on one pipeline whose stored structure set a property its element type no longer has).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tools.pipelines import merge_layers
from utils.mappingstore import read_block
from utils.schemas import SchemaCache
from utils.stroom import refused_reason

UNLOADABLE = ToolError('Stroom rejected the request (500): Attempt to set property "compressionLevel" on element '
                       '"rollingFileAppender" but property is unknown. (stroom.pipeline.factory.PipelineFactoryException: x)')


def test_strooms_reason_is_kept_without_the_java_around_it():
    assert refused_reason(UNLOADABLE) == ('Attempt to set property "compressionLevel" on element "rollingFileAppender" '
                                          'but property is unknown.')


async def test_one_unreadable_schema_neither_fails_nor_sticks():
    # Shared by every user: an index left half-built after one failed read said "Stroom has no XML schema" to all.
    stroom = SimpleNamespace(
        find_documents=AsyncMock(return_value={'values': [{'docRef': {'type': 'XMLSchema', 'uuid': 'gone'}},
                                                          {'docRef': {'type': 'XMLSchema', 'uuid': 'ok'}}]}),
        get=AsyncMock(side_effect=lambda path: (_ for _ in ()).throw(ToolError('404')) if path.endswith('gone')
                      else {'systemId': 'file://event-logging-v3.5.2.xsd'}))
    cache = SchemaCache(stroom)
    assert await cache.system_ids() == ['file://event-logging-v3.5.2.xsd']


def test_malformed_layer_entries_are_passed_over():
    layers = [{'pipelineData': {'elements': {'add': [{'id': 'parser', 'type': 'DSParser'}, {'type': 'XSLTFilter'}]},
                                'links': {'add': [{'from': 'Source', 'to': 'parser'}, {'from': 'parser'}]},
                                'properties': {'add': [{'element': 'parser', 'name': 'textConverter', 'value': {}},
                                                       {'name': 'orphan'}]},
                                'pipelineReferences': {'add': [{'name': 'x'}]}}}]
    merged = merge_layers(layers)
    assert merged['elements'] == [{'id': 'parser', 'type': 'DSParser'}]
    assert [(p['element'], p['name']) for p in merged['properties']] == [('parser', 'textConverter')]


def test_a_shared_xslt_that_doesnt_parse_is_not_a_keyerror():
    from utils.sharedxslt import usage
    xslt = ('<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">'
            '<xsl:import href="Shared-V1"/><xsl:template match="/"><xsl:call-template name="x"/></xsl:template>'
            '</xsl:stylesheet>')
    assert isinstance(usage(xslt, {'Shared-V1': '<xsl:stylesheet not well formed'}), list)


def test_a_block_is_only_ever_an_object():
    assert read_block('<!-- stroom-mcp agreed index template: kept by the server, do not edit or delete\n[1, 2]\n-->',
                      'agreed index template') is None


async def test_an_unreadable_agents_doc_is_said_and_the_others_followed():
    from tools.instructions import applicable_instructions
    docs = {'a': '## Component templates\n- include: org-base\n'}

    async def get_doc(kind, uuid):
        if uuid == 'gone':
            raise ToolError('Stroom rejected the request (404): Document not found (stroom.DocumentNotFoundException: x)')
        return {'data': docs[uuid]}
    stroom = SimpleNamespace(settings=SimpleNamespace(instructions_doc_name='AGENTS'), get_doc=get_doc,
                             find_documents=AsyncMock(return_value={'values': [
                                 {'docRef': {'type': 'Documentation', 'uuid': 'a', 'name': 'AGENTS'}, 'path': 'System'},
                                 {'docRef': {'type': 'Documentation', 'uuid': 'gone', 'name': 'AGENTS'}, 'path': 'System'},
                                 {'docRef': {'type': 'Documentation', 'uuid': 'x', 'name': None}, 'path': 'System'}]}))
    found = await applicable_instructions(SimpleNamespace(lifespan_context={'stroom': stroom}))
    assert [d['uuid'] for d in found['instructions']] == ['a']
    assert found['unreadable_instruction_docs'] == [{'folder': 'System', 'uuid': 'gone', 'reason': 'Document not found'}]


async def test_one_unreadable_stream_leaves_the_others_summarised():
    from tools import streams

    async def summarise(ctx, stream_id):
        if stream_id == 2:
            raise ToolError('Stroom rejected the request (500): volume not found')
        return {'stream_id': stream_id, 'groups': []}
    with patch.object(streams, 'summarise_errors', summarise):
        out = await streams.summarise_streams(SimpleNamespace(lifespan_context={}), [1, 2, 3], 'errors')
    assert out['streams'][1]['groups'] == [] and out['streams'][3]['groups'] == []
    assert 'volume not found' in out['streams'][2]['error']


async def test_error_streams_from_a_pipeline_stroom_cant_load_are_still_triaged():
    from tools import streams
    stroom = SimpleNamespace(
        find_meta=AsyncMock(return_value={'values': [{'meta': {'id': 9, 'pipelineUuid': 'bad'}},
                                                     {'meta': {'id': 10, 'pipelineUuid': 'bad'}}]}),
        fetch_data=AsyncMock(side_effect=lambda sid, *a: (_ for _ in ()).throw(ToolError('500 volume gone'))
                             if sid == 10 else {'markers': []}),
        pipeline_layers=AsyncMock(side_effect=UNLOADABLE))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom, 'rules': SimpleNamespace()})
    with patch.object(streams, '_meta', AsyncMock(return_value={'id': 5, 'typeName': 'Raw Events'})), \
            patch.object(streams, 'accepted_for', AsyncMock(return_value=[])), \
            patch.object(streams, 'triage', lambda markers, rules, own, accepted=(): {'groups': [], 'own': sorted(own)}):
        out = await streams.summarise_errors(ctx, 5)
    assert out['error_streams'] == [9, 10] and out['own'] == []
    assert out['unread'] == ['Error stream 10: 500 volume gone']


@pytest.mark.parametrize('stage', [None])
async def test_a_pipeline_stroom_cant_load_has_no_stage_for_a_choice(stage):
    from tools.builds import _stage_or_none
    with patch('tools.templates._shape', AsyncMock(side_effect=UNLOADABLE)):
        assert await _stage_or_none(None, 'bad') is stage
