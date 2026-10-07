"""build_translation_xslt answers fast and precisely: what is wrong and where, before any slow work.

Seen in a test environment: about 80 seconds a call (the pipeline stepped for a field mapping preview on every
attempt), vague "not a translation mapping" errors, and regexes escaped again on every attempt.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tests.test_xsltgen import SCHEMA
from tools import generation

GOOD = {'input': 'json', 'common': [{'path': 'EventTime/TimeCreated', 'field': 'ts'},
                                    {'path': 'EventSource/System/Name', 'value': 'Acme'},
                                    {'path': 'EventSource/System/Environment', 'value': 'Test'},
                                    {'path': 'EventSource/Generator', 'value': 'acme'},
                                    {'path': 'EventSource/Device/HostName', 'field': 'host'}],
        'events': [{'name': 'logon', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'Logon'},
                                                {'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                                                {'path': 'EventDetail/Authenticate/User/Id', 'field': 'user'}]}]}


def patched(read=None, stepped=None):
    read = read or AsyncMock(return_value=({'stream 5': '{"ts": "2026-10-01T00:00:00.000Z"}\n'}, []))
    stepped = stepped or AsyncMock(return_value={})
    return (patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)),
            patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})),
            patch.object(generation, 'gateway_from', lambda ctx: SimpleNamespace(
                settings=SimpleNamespace(event_logging_version='4.1.0'))),
            patch.object(generation, 'read_sample_streams', read),
            patch.object(generation._Pipeline, 'load', AsyncMock(return_value=SimpleNamespace(
                default_outputs=lambda: ['translationFilter']))),
            patch.object(generation, '_outputs', stepped)), read, stepped


async def build(mapping, **kwargs):
    patches, read, stepped = patched()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = await generation.build_translation_xslt(SimpleNamespace(lifespan_context={}), mapping, **kwargs)
    return result, read, stepped


async def test_text_that_is_not_json_says_where_and_how_backslashes_travel():
    with pytest.raises(ToolError) as raised:
        await build('{"input": "json", "extract": [{"regex": "\\[\\[AppServer"}]}')
    message = str(raised.value)
    assert 'Invalid \\escape at character' in message and 'around `' in message
    assert 'only the json of your call doubles each backslash' in message.lower()


async def test_an_invalid_mapping_says_where_each_error_is_with_the_value_given():
    bad = {**GOOD, 'rules': [], 'events': [{'fields': [{'path': 'EventDetail/TypeId', 'value': 'Logon'}]},
                                          {'name': 'x', 'fields': 'all of them'}]}
    with pytest.raises(ToolError) as raised:
        await build(bad, stream_ids=[5])
    message = str(raised.value)
    assert 'events[0].name: Field required' in message
    assert 'events[1].fields: Input should be a valid list (given "all of them")' in message
    assert "keys that are not part of a mapping: ['rules']" in message


async def test_schema_problems_come_back_before_the_sample_is_read():
    result, read, _ = await build({**GOOD, 'common': GOOD['common'] + [{'path': 'EventSource/Nope', 'value': 'x'}]},
                                  stream_ids=[5])
    assert not result['ok'] and result['sample_check'] == 'not run: fix the problems first'
    read.assert_not_awaited()


async def test_the_pipeline_is_stepped_only_when_the_field_mapping_preview_is_asked_for():
    result, read, stepped = await build(GOOD, stream_ids=[5], pipeline_uuid='p')
    assert result['ok'] and result['field_mapping'] is None and 'write_documentation makes the section' in result['field_mapping_needs']
    stepped.assert_not_awaited()
    result, _, stepped = await build(GOOD, stream_ids=[5], pipeline_uuid='p', field_mapping=True)
    stepped.assert_awaited_once()
    assert stepped.await_args.args[-1] == 50        # a preview: 50 records unless asked


async def test_keys_a_mapping_has_no_place_for_are_reported_when_it_is_otherwise_fine():
    result, _, _ = await build({**GOOD, 'extracts': []}, stream_ids=[5])
    assert any("mapping keys ignored, as a mapping has no such key: ['extracts']" in w for w in result['warnings'])
