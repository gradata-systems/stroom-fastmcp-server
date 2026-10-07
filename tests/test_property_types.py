"""Pipeline property values written as the types Stroom declares, and pipelines Stroom can't build or run."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from security.policy import AccessPolicy
from tools import pipeline_writes, stepping, templates
from tools.pipeline_writes import PropertyValue

DECLARED = {('JSONParser', 'addRootObject'): 'boolean', ('SplitFilter', 'splitCount'): 'int',
            ('StreamAppender', 'streamType'): 'String', ('XSLTFilter', 'xslt'): 'XSLT'}
TYPES = {'jsonParser': 'JSONParser', 'splitFilter': 'SplitFilter', 'streamAppender': 'StreamAppender',
         'translationFilter': 'XSLTFilter'}


def gateway(declared=DECLARED):
    return SimpleNamespace(property_types=AsyncMock(return_value=declared))


async def test_a_string_for_a_boolean_is_written_as_a_boolean():
    # Seen: addRootObject given as "false" and written {'string': 'false'}; every step of the pipeline then outlasted
    # Stroom's wait, on any stream.
    props = [PropertyValue(element='jsonParser', name='addRootObject', value='false'),
             PropertyValue(element='splitFilter', name='splitCount', value='100'),
             PropertyValue(element='streamAppender', name='streamType', value='Records')]
    keys = await pipeline_writes.typed_properties(gateway(), TYPES, props)
    assert [p.value for p in props] == [False, 100, 'Records']
    assert [await pipeline_writes._value(None, p, keys.get((p.element, p.name))) for p in props] == [
        {'boolean': False}, {'integer': 100}, {'string': 'Records'}]


async def test_a_value_not_of_its_type_or_a_property_the_element_lacks_is_refused():
    with pytest.raises(ToolError, match='jsonParser.addRootObject is true or false'):
        await pipeline_writes.typed_properties(gateway(), TYPES, [PropertyValue(element='jsonParser', name='addRootObject', value='no')])
    with pytest.raises(ToolError, match="has no property 'addRootObjects'; its properties are \\['addRootObject'\\]"):
        await pipeline_writes.typed_properties(gateway(), TYPES, [PropertyValue(element='jsonParser', name='addRootObjects', value=False)])
    # A Stroom with no property types resource: values are written as they were given.
    assert await pipeline_writes.typed_properties(gateway({}), TYPES, [PropertyValue(element='jsonParser', name='addRootObject', value='x')]) == {}


async def test_stepping_refuses_a_pipeline_with_a_property_stroom_cant_take():
    layers = [{'pipelineData': {'elements': {'add': [{'id': 'jsonParser', 'type': 'JSONParser'}]},
                                'properties': {'add': [{'element': 'jsonParser', 'name': 'addRootObject',
                                                        'value': {'string': 'false'}}]}}}]
    stroom = SimpleNamespace(get=AsyncMock(return_value={'name': 'FortiOS-Events'}),
                             pipeline_layers=AsyncMock(return_value=layers), property_types=AsyncMock(return_value=DECLARED))
    pipeline = await stepping._Pipeline.load(stroom, 'p')
    with pytest.raises(ToolError, match="jsonParser.addRootObject is 'false', where Stroom takes true or false.*update_pipeline"):
        pipeline.refuse_mistyped()
    layers[0]['pipelineData']['properties']['add'][0]['value'] = {'boolean': False}
    (await stepping._Pipeline.load(stroom, 'p')).refuse_mistyped()      # written right: steps


async def test_a_pipeline_stroom_cant_build_is_left_out_of_the_template_search():
    # Seen live: a State Store loader with an element type this Stroom doesn't have failed the whole search.
    index = {'t': {'uuid': 't', 'name': 'json-in v3', 'path': 'Acme/Bases', 'parent_uuid': None},
             'b': {'uuid': 'b', 'name': 'State Store Loader', 'path': 'System/Standard Pipelines', 'parent_uuid': None}}

    async def shape(stroom, uuid, markers=None):
        if uuid == 'b':
            raise ToolError('Stroom rejected the request (500): Element type "StateFilter" is unknown')
        return {'stage': 'translation', 'backend': None, 'parser': 'JSONParser', 'properties': {},
                'child_must_supply': [{'element': 'translationFilter', 'property': 'xslt'}]}
    ctx = SimpleNamespace(lifespan_context={'policy': AccessPolicy()})
    with patch.object(templates, '_pipeline_index', AsyncMock(return_value=index)), \
            patch.object(templates, 'gateway_from', lambda c: None), patch.object(templates, '_shape', shape):
        found = await templates.find_pipeline_templates(ctx, 'translation')
    assert [c['name'] for c in found['candidates']] == ['json-in v3']
    assert found['unreadable'][0].startswith('System/Standard Pipelines/State Store Loader: ')
