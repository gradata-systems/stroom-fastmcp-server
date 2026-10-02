"""Guards against the mistakes a model makes when it writes pipeline content by hand: a text converter for JSON,
an XSLT with no input namespace, non-existent stroom: functions, elements the event-logging schema has no place for,
and an indexing pipeline before there are any Events to index."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tests.test_xsltgen import SCHEMA, mapping
from tools import generation, indexing, stepping, translation, validation
from utils.profile import profile
from utils.xsltgen import generate

HANDWRITTEN = """<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns="event-logging:3"
 xmlns:stroom="stroom" version="3.0">
  <xsl:template match="/map">
    <Events Version="4.1.0"><xsl:apply-templates select="map"/></Events>
  </xsl:template>
  <xsl:template match="map">
    <Event>
      <EventTime><TimeCreated><xsl:value-of select="stroom:format-date(string[@key='ts'])"/></TimeCreated></EventTime>
      <EventSource><System><Name>x</Name></System><Generator>g</Generator><Device><HostName>h</HostName></Device></EventSource>
      <EventDetail><TypeId>t</TypeId>
        <ServerEvent><Message><xsl:value-of select="stroom:json-parse(string[@key='message'])"/></Message></ServerEvent>
      </EventDetail>
    </Event>
  </xsl:template>
</xsl:stylesheet>"""


@pytest.fixture
def ctx():
    return SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))})


def with_schema():
    return patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA))


async def test_check_xslt_names_the_three_handwriting_mistakes(ctx):
    with with_schema():
        result = await validation.check_xslt(ctx, HANDWRITTEN)
    assert not result['ok']
    [function, namespace, element] = result['errors']
    assert function.startswith('stroom:json-parse() is not a Stroom function') and 'json-to-xml(text)' in function
    assert namespace.startswith('match="/map", select="map", match="map"')
    assert 'xpath-default-namespace="http://www.w3.org/2013/XSL/json"' in namespace
    assert element.startswith("Event/EventDetail/ServerEvent is not in the event-logging schema")
    assert "'Authenticate'" in element   # what EventDetail does allow


async def test_check_xslt_passes_generated_xslt_and_an_explicit_empty_namespace(ctx):
    with with_schema():
        assert (await validation.check_xslt(ctx, generate(mapping(), SCHEMA, '4.1.0')['xslt']))['ok']
        fixed = HANDWRITTEN.replace('version="3.0"', 'version="3.0" xpath-default-namespace=""') \
            .replace('stroom:json-parse(', 'json-to-xml(').replace('ServerEvent', 'Unknown').replace('Message', 'Data')
        result = await validation.check_xslt(ctx, fixed)
    assert result['errors'] == [], result['errors']


async def test_a_fragment_in_a_named_template_is_checked_where_the_schema_allows_it(ctx):
    sheet = """<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns="event-logging:3" version="3.0"
     xpath-default-namespace="records:2">
      <xsl:template name="who"><User><Id>a</Id><Nickname>b</Nickname></User></xsl:template>
    </xsl:stylesheet>"""
    with with_schema():
        result = await validation.check_xslt(ctx, sheet)
    [error] = result['errors']
    assert error.startswith("Nickname is not allowed below any of ['Event/EventSource/User', 'Event/EventDetail/Authenticate/User'")
    assert error.endswith("] in the event-logging schema")


async def test_draft_xslt_is_checked_before_it_is_stepped(ctx):
    with with_schema(), pytest.raises(ToolError, match="draft_code\\['translationFilter'\\] is not stepped: stroom:json-parse"):
        await stepping.step_pipeline(ctx, 'p-1', 7, draft_code={'translationFilter': HANDWRITTEN})


def test_a_text_converter_is_a_data_splitter_never_a_json_parser():
    with pytest.raises(ToolError, match='cannot parse JSON'):
        translation._check_converter('DATA_SPLITTER', '<textConverter><jsonParser/></textConverter>')
    with pytest.raises(ToolError, match='root element is <dataSplitter'):
        translation._check_converter('DATA_SPLITTER', '<records xmlns="records:2"/>')
    translation._check_converter('DATA_SPLITTER', '<dataSplitter xmlns="data-splitter:3" version="3.0"/>')
    translation._check_converter('XML_FRAGMENT', '<!DOCTYPE records [<!ENTITY fragment SYSTEM "fragment">]><records>&fragment;</records>')


def test_profile_says_how_json_reaches_the_xslt():
    lines = profile('{"a": 1}\n{"a": 2}\n')
    assert lines['text_converter'].startswith('none')
    assert lines['parser_properties'] == {'jsonParser.addRootObject': True}
    assert lines['xslt_input']['root'] == '/map' and lines['xslt_input']['mapping']['json_layout'] == 'lines'
    array = profile('[{"a": 1}, {"a": 2}]')
    assert array['parser_properties'] == {'jsonParser.addRootObject': False} and array['xslt_input']['root'] == '/array'


async def test_build_translation_xslt_tells_the_parser_setting(ctx):
    with with_schema(), patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})):
        result = await generation.build_translation_xslt(ctx, mapping(input='json', json_layout='lines'))
    assert result['pipeline_properties']['jsonParser.addRootObject'] is True


async def test_an_indexing_pipeline_needs_events_first():
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace()})
    folder = AsyncMock(return_value=[{'type': 'Pipeline', 'uuid': 'x', 'name': 'x', 'tags': [], 'path': 'p'}])
    guard = SimpleNamespace(folder_contents=folder)
    with patch.object(indexing, 'guard_from', lambda c: guard), \
            patch.object(indexing, '_shape', AsyncMock(return_value={'stage': 'indexing'})), \
            patch.object(indexing, '_meta', AsyncMock(return_value={'typeName': 'Raw Events'})):
        with pytest.raises(ToolError, match='build the events pipeline first'):
            await indexing._events_available(ctx, 'b', [])
        with pytest.raises(ToolError, match="is 'Raw Events', not Events"):
            await indexing._events_available(ctx, 'b', [7])
    with patch.object(indexing, 'guard_from', lambda c: guard), \
            patch.object(indexing, '_shape', AsyncMock(return_value={'stage': 'translation'})):
        await indexing._events_available(ctx, 'b', [])    # the build's own events pipeline will produce them
    folder.return_value = []
    with patch.object(indexing, 'guard_from', lambda c: guard), \
            patch.object(indexing, '_meta', AsyncMock(return_value={'typeName': 'Events'})):
        await indexing._events_available(ctx, 'b', [7])   # existing Events from another pipeline
