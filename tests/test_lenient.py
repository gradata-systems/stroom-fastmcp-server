"""Unknown arguments are dropped with a note, the published schema allows them, and build_data_splitter can save."""
from unittest.mock import AsyncMock, patch

import pytest

from fastmcp import Client, FastMCP

from security.lenient import LenientArguments
from tools import generation


async def test_unknown_arguments_are_ignored_and_reported():
    server = FastMCP('t', lifespan=None, middleware=[LenientArguments()])
    server.tool(generation.build_data_splitter)
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert 'additionalProperties' not in tools['build_data_splitter'].input_schema
        result = await client.call_tool('build_data_splitter', {'sample': 'a,b\n1,2\n', 'save_text_converter': 'true'})
    text = ' '.join(c.text for c in result.content if getattr(c, 'text', None))
    assert '"inferred": true' in text or "'inferred': True" in text or 'converter' in text
    assert "Ignored unknown argument(s) ['save_text_converter']" in text and "'sample'" in text


async def test_build_data_splitter_can_save_the_converter_it_built():
    with patch.object(generation, 'resolve_build', create=True), \
            patch('tools.plan.resolve_build', return_value='onboard-fw'), \
            patch('tools.translation.create_text_converter', AsyncMock(return_value={'type': 'TextConverter', 'uuid': 'tc', 'name': 'FW'})) as save:
        result = await generation.build_data_splitter(None, sample='a,b\n1,2\n', save_as='FW')
    assert result['saved']['uuid'] == 'tc' and save.await_args.args[1:4] == ('onboard-fw', 'FW', 'DATA_SPLITTER')
    with patch('tools.plan.resolve_build', return_value='onboard-fw'), \
            patch('tools.translation.create_text_converter', AsyncMock()) as save:
        result = await generation.build_data_splitter(None, sample='a,b\nc,d,e\n', spec={'kind': 'delimited', 'header': ['x', 'y']}, save_as='FW')
    assert 'not_saved' in result and save.await_count == 0


async def test_a_choice_with_stray_punctuation_is_read_as_the_choice():
    # Seen: an agent sent converter_type ",XML_FRAGMENT" again and again, refused each time.
    from typing import Literal
    server = FastMCP('t', middleware=[LenientArguments()])

    @server.tool
    def create_text_converter(converter_type: Literal['DATA_SPLITTER', 'XML_FRAGMENT'],
                              mode: Literal['a', 'b'] | None = None) -> dict:
        return {'converter_type': converter_type, 'mode': mode}
    async with Client(server) as client:
        result = await client.call_tool('create_text_converter', {'converter_type': ',XML_FRAGMENT', 'mode': ' B'})
        assert result.structured_content == {'converter_type': 'XML_FRAGMENT', 'mode': 'b'}
        assert "Read converter_type ',XML_FRAGMENT' as 'XML_FRAGMENT'" in result.content[-1].text
        with pytest.raises(Exception, match='DATA_SPLITTER'):       # not a choice at all: still refused
            await client.call_tool('create_text_converter', {'converter_type': 'XML'})
