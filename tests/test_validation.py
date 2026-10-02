from types import SimpleNamespace

import httpx
import pytest
import respx

from tests.test_gateway import API, SETTINGS
from tools import validation
from utils.stroom import StroomGateway

EVENT = """<?xml version="1.1" encoding="UTF-8"?><Events xmlns="event-logging:3"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
 xsi:schemaLocation="event-logging:3 file://event-logging-v9.9.9.xsd" Version="9.9.9"><Event>
<EventTime><TimeCreated>2026-09-28T10:00:00.000Z</TimeCreated></EventTime>
<EventSource><System><Name>SPIKE</Name><Environment>Dev</Environment></System><Generator>g</Generator>
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
      <EventSource><System><Name>SPIKE</Name></System>
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


async def test_describe_translation_maps_outputs_to_inputs(ctx):
    result = await validation.describe_translation(ctx, xslt=XSLT)
    mapped = {m['output']: m['source'] for m in result['mappings']}
    assert mapped['[record] Event/EventSource/User/Id'] == "data[@name='user']/@value"
    assert mapped['[record] Event/Data/@Value'] == "{data[@name='host']/@value}"
    assert mapped['[record] Event/EventSource/System/Name'] == 'SPIKE'
    assert result['input_fields'] == ['host', 'time', 'user']
    assert (result['imports'], result['lookups']) == (['IP Lookup'], ['USER_MAP'])
