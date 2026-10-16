"""A schema location naming a system id Stroom doesn't hold. Seen on Stroom 7.13: file:///event-logging-v4.1.0.xsd
(three slashes) was not refused as unknown; the schema filter validated against event-logging 3.2.3's schema instead,
refusing Version 4.1.0, and the error hid the cause."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from tools import validation
from utils.schemas import SchemaCache

JUDGE = SchemaCache.location_problems      # the real one: conftest has it judge nothing for the other tests
HELD = {'a': ('file://event-logging-v3.2.3.xsd', 'event-logging:3'), 'b': ('file://event-logging-v4.1.0.xsd', 'event-logging:3'),
        'c': ('file://records-v2.0.xsd', 'records:2')}
XSD = (Path(__file__).parent / 'fixtures' / 'event-logging-v4.1.0.xsd').read_text(encoding='utf-8')


def _stroom():
    found = {'values': [{'docRef': {'type': 'XMLSchema', 'uuid': u}} for u in HELD]}

    async def get(path):
        system_id, namespace = HELD[path.rsplit('/', 1)[-1]]
        return {'systemId': system_id, 'namespaceURI': namespace, 'data': XSD}
    return SimpleNamespace(find_documents=AsyncMock(return_value=found), get=AsyncMock(side_effect=get),
                           settings=SimpleNamespace())


def _xslt(location: str) -> str:
    return ('<xsl:stylesheet version="2.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns="event-logging:3">'
            f'<xsl:template match="/"><Events xsi:schemaLocation="event-logging:3 {location}" Version="4.1.0"/>'
            '</xsl:template></xsl:stylesheet>')


async def _problems(location: str) -> list[str]:
    return await JUDGE(SchemaCache(_stroom()), etree.fromstring(_xslt(location).encode()))


async def test_three_slashes_are_refused_with_the_id_stroom_holds():
    [problem] = await _problems('file:///event-logging-v4.1.0.xsd')
    assert "write 'file://event-logging-v4.1.0.xsd' (file: and two slashes)" in problem
    assert "event-logging 3.2.3's, refusing Version 4.1.0" in problem


async def test_a_bare_file_name_is_refused_too():
    [problem] = await _problems('event-logging-v4.1.0.xsd')
    assert "write 'file://event-logging-v4.1.0.xsd'" in problem


async def test_a_version_stroom_doesnt_hold_lists_those_it_does_newest_first():
    [problem] = await _problems('file://event-logging-v3.5.2.xsd')
    assert 'it holds file://event-logging-v4.1.0.xsd, file://event-logging-v3.2.3.xsd.' in problem


async def test_held_ids_unknown_namespaces_and_templates_are_not_judged():
    assert await _problems('file://event-logging-v4.1.0.xsd') == []
    assert await JUDGE(SchemaCache(_stroom()), etree.fromstring(
        b'<x xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="acme:1 file:///acme.xsd"/>')) == []
    assert await _problems('file://event-logging-v{$version}.xsd') == []


async def test_check_xslt_refuses_it(monkeypatch):
    monkeypatch.setattr(SchemaCache, 'location_problems', JUDGE)
    ctx = SimpleNamespace(lifespan_context={'stroom': _stroom()})
    checked = await validation.check_xslt(ctx, _xslt('file:///event-logging-v4.1.0.xsd'))
    assert not checked['ok'] and any('two slashes' in e for e in checked['errors'])


async def test_validating_a_document_that_names_it_says_what_to_write():
    with pytest.raises(ToolError, match=r"write 'file://event-logging-v4\.1\.0\.xsd' \(file: and two slashes\)"):
        await SchemaCache(_stroom()).source('file:///event-logging-v4.1.0.xsd')
