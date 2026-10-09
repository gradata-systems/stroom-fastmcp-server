import json
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from tests.test_gateway import API, SETTINGS
from tools import validation
from utils.stroom import StroomGateway

EVENT = """<?xml version="1.1" encoding="UTF-8"?><Events xmlns="event-logging:3"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
 xsi:schemaLocation="event-logging:3 file://event-logging-v9.9.9.xsd" Version="9.9.9"><Event>
<EventTime><TimeCreated>2026-09-28T10:00:00.000Z</TimeCreated></EventTime>
<EventSource><System><Name>ACME</Name><Environment>Dev</Environment></System><Generator>g</Generator>
<Device><HostName>ws01</HostName></Device></EventSource>
<EventDetail><TypeId>Logon</TypeId><Authenticate><Action>Logon</Action></Authenticate></EventDetail>
</Event></Events>"""

# A cut-down stand-in for the event-logging XSD: just enough structure to prove validation.
XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="event-logging:3"
 xmlns="event-logging:3" elementFormDefault="qualified">
 <xs:element name="Events"><xs:complexType><xs:sequence>
  <xs:element name="Event" maxOccurs="unbounded"><xs:complexType><xs:sequence>
   <xs:element name="EventTime"><xs:complexType><xs:sequence><xs:element name="TimeCreated" type="xs:dateTime"/>
   </xs:sequence></xs:complexType></xs:element>
   <xs:any processContents="skip" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType></xs:element>
 </xs:sequence><xs:anyAttribute processContents="skip"/></xs:complexType></xs:element>
</xs:schema>"""

XSLT = """<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns="event-logging:3"
 xmlns:stroom="stroom" xpath-default-namespace="records:2" version="3.0">
  <xsl:import href="IP Lookup"/>
  <xsl:template match="record">
    <Event>
      <EventTime><TimeCreated><xsl:value-of select="stroom:format-date(data[@name='time']/@value)"/></TimeCreated></EventTime>
      <EventSource><System><Name>ACME</Name></System>
        <User><Id><xsl:value-of select="data[@name='user']/@value"/></Id></User></EventSource>
      <Data Name="host" Value="{data[@name='host']/@value}"/>
      <xsl:variable name="m" select="stroom:lookup('USER_MAP', data[@name='user']/@value)"/>
    </Event>
  </xsl:template>
</xsl:stylesheet>"""


@pytest.fixture
async def ctx():
    gw = StroomGateway(SETTINGS)
    yield SimpleNamespace(lifespan_context={'stroom': gw})
    await gw.close()


def mock_schemas():
    respx.post(f'{API}/explorer/v2/find').mock(return_value=httpx.Response(200, json={'values': [
        {'docRef': {'type': 'XMLSchema', 'uuid': 's-1', 'name': 'event-logging v9.9.9'}}]}))
    respx.get(f'{API}/xmlSchema/v1/s-1').mock(return_value=httpx.Response(200, json={
        'systemId': 'file://event-logging-v9.9.9.xsd', 'data': XSD}))


@respx.mock
async def test_validate_events_uses_the_declared_schema_from_stroom(ctx):
    mock_schemas()
    assert (await validation.validate_events(ctx, EVENT))['valid'] is True
    bad = await validation.validate_events(ctx, EVENT.replace('2026-09-28T10:00:00.000Z', 'not-a-date'))
    assert bad['valid'] is False and bad['schema'] == 'file://event-logging-v9.9.9.xsd'
    assert 'TimeCreated' in bad['errors'][0]['message']


@respx.mock
async def test_unknown_schema_version_lists_what_stroom_has(ctx):
    mock_schemas()
    with pytest.raises(Exception, match='event-logging-v9.9.9'):
        await validation.validate_events(ctx, EVENT, schema_version='3.5.2')


async def test_quality_rules_pass_a_complete_event_and_flag_gaps(ctx):
    assert (await validation.check_event_quality(ctx, EVENT))['ok'] is True
    gappy = EVENT.replace('<Generator>g</Generator>', '').replace('2026-09-28T10:00:00.000Z', '2026-09-28 10:00')
    rules = (await validation.check_event_quality(ctx, gappy))['rules']
    assert set(rules) == {'generator', 'time_created'}


async def test_unknown_events_are_noted_without_failing(ctx):
    assert 'notes' not in await validation.check_event_quality(ctx, EVENT)
    unknown = EVENT.replace('<Authenticate><Action>Logon</Action></Authenticate>', '<Unknown><Data Name="a" Value="b"/></Unknown>')
    quality = await validation.check_event_quality(ctx, unknown)
    assert quality['ok'] is True and 'EventDetail/Unknown' in quality['notes'][0]


@respx.mock
async def test_check_xslt_reports_missing_imports_and_unknown_functions(ctx):
    respx.post(f'{API}/explorer/v2/find').mock(return_value=httpx.Response(200, json={'values': []}))
    result = await validation.check_xslt(ctx, XSLT.replace('stroom:lookup', 'stroom:lookp'))
    assert result['ok'] is False
    # An unknown function is an error (it would not compile), with the nearest real name.
    assert result['errors'] == ["stroom:lookp() is not a Stroom function and will not compile; did you mean stroom:lookup()?",
                                "xsl:import/include targets not found as XSLT documents: IP Lookup"]
    # Stroom here has no event-logging schema, so element names go unchecked and the check says so.
    assert result['warnings'] == ["Event-logging element names not checked: Stroom has no XML schema "
                                  "'file://event-logging-v3.5.2.xsd'. Event-logging schemas available: none"]


async def test_check_xslt_rejects_malformed_xml(ctx):
    result = await validation.check_xslt(ctx, '<xsl:stylesheet')
    assert result['ok'] is False and 'Not well-formed' in result['errors'][0]


@respx.mock
async def test_xml_sent_html_escaped_is_read_unescaped_and_said_so(ctx):
    # Qwen in VS Code sent &lt;?xml ... and was told only "Start tag expected, '<' not found".
    import html
    respx.post(f'{API}/explorer/v2/find').mock(return_value=httpx.Response(200, json={'values': []}))
    plain = await validation.check_xslt(ctx, XSLT)
    escaped = await validation.check_xslt(ctx, html.escape(XSLT, quote=False))
    assert escaped['errors'] == plain['errors'] and escaped['warnings'][0] == validation._ESCAPED
    assert validation._unescaped('<a>&lt;b&gt;</a>') == ('<a>&lt;b&gt;</a>', [])     # escaped text inside real XML is left


async def test_describe_translation_maps_outputs_to_inputs(ctx):
    result = await validation.describe_translation(ctx, xslt=XSLT)
    mapped = {m['output']: m['source'] for m in result['mappings']}
    assert mapped['[record] Event/EventSource/User/Id'] == "data[@name='user']/@value"
    assert mapped['[record] Event/Data/@Value'] == "{data[@name='host']/@value}"
    assert mapped['[record] Event/EventSource/System/Name'] == 'ACME'
    assert result['input_fields'] == ['host', 'time', 'user']
    assert (result['imports'], result['lookups']) == (['IP Lookup'], ['USER_MAP'])


def test_the_generated_errors_section_leaves_the_agents_own_error_sections_alone():
    from utils.mappingstore import replace_section
    report = '# P\n\n## Errors and schema conformance\n\nOne event fails the schema.\n\n## Suggestions\n\n1. Fix it.\n'
    text = replace_section(report, 'Errors', 'Generated.', exact=True)
    assert 'One event fails the schema.' in text and '## Errors\n\nGenerated.' in text
    assert replace_section(text, 'Errors', 'Again.', exact=True).count('## Errors\n') == 1


def test_an_indexing_xslt_without_a_plan_is_documented_from_the_documents_it_writes():
    from utils.fielddoc import written_fields_markdown
    section = written_fields_markdown([{'UserId': ['alice'], 'Host': ['ws01'], 'Empty': ['']},
                                       {'UserId': ['bob'], 'Host': ['']}])
    assert '| `UserId` | Not described: no plan or schema covers it. | 100% of documents | `alice`, `bob` |' in section
    assert ('| `Host` | Not described: no plan or schema covers it. | 50% of documents | `ws01` |' in section
            and '`Empty`' not in section)
    assert 'keeps no index plan' in section


@respx.mock
async def test_check_events_reads_the_events_streams_itself(ctx):
    # Qwen in VS Code read 50 processed events back 23 at a time, 47 s a read, to send them to check_events.
    mock_schemas()
    bad = EVENT.replace('2026-09-28T10:00:00.000Z', 'not-a-date')
    records = [EVENT, EVENT, bad]

    def fetch(request):
        index = json.loads(request.content)['sourceLocation']['recordIndex']
        return httpx.Response(200, json={'data': records[index], 'dataType': 'SEGMENTED', 'streamTypeName': 'Events',
                                          'totalItemCount': {'count': len(records)}})
    respx.post(f'{API}/data/v1/fetch').mock(side_effect=fetch)
    result = await validation.check_events(ctx, stream_ids=[42])
    assert result['read'] == {42: {'checked': 3, 'records': 3}}
    assert not result['schema']['valid'] and result['schema']['error_count'] == 1
    with pytest.raises(ToolError, match='not both'):
        await validation.check_events(ctx, EVENT, stream_ids=[42])


async def test_describe_event_element_says_what_an_element_takes():
    # Qwen in VS Code spent twelve minutes writing PowerShell to read the action elements and their children out of
    # the XSD.
    from pathlib import Path
    from unittest.mock import AsyncMock, patch
    from utils.eventschema import EventSchema
    schema = EventSchema.parse((Path(__file__).parent / 'fixtures' / 'event-logging-v4.1.0.xsd').read_bytes())
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))})
    with patch('tools.generation.event_schema', AsyncMock(return_value=schema)):
        detail = await validation.describe_event_element(ctx, 'EventDetail')
        from tools import explorer
        assert (await explorer.describe_document(ctx, 'XMLSchema', element='EventDetail')) == detail
        actions = next(c for c in detail['choices'] if 'Authenticate' in c['one_of'])
        assert actions['one_required'] and {'Process', 'Unknown', 'Network'} <= set(actions['one_of'])
        process = {c['name']: c for c in (await validation.describe_event_element(ctx, 'EventDetail/Process'))['children']}
        assert process['Action']['required'] and 'Execute' in process['Action']['values']
        assert process['Type']['values'] == ['OS', 'Service', 'Application'] and process['Command']['required']
        leaf = await validation.describe_event_element(ctx, 'EventDetail/Authenticate/Action')
        assert leaf['path'] == 'Event/EventDetail/Authenticate/Action' and 'Logon' in leaf['values']
        with pytest.raises(ToolError, match="has no child 'Acess'"):
            await validation.describe_event_element(ctx, 'EventDetail/Acess')
