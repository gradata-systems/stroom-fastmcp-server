"""Replacing an index (asked for by the user, after an agent fell into a loop reading the old indexing XSLT from a
file its client spilled describe_document's output to): the old XSLT's fields by name and source, and what a new
draft leaves out of them, without reading the XSLT."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lxml import etree

from tools import indexing, validation
from utils.fieldplan import FieldPlan, PlannedField as P

# Written by hand, as the FortiOS index was: nested maps, the field names in their keys.
OLD = """<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns="http://www.w3.org/2005/xpath-functions"
    xpath-default-namespace="event-logging:3" version="3.0">
  <xsl:template match="Event">
    <map>
      <number key="StreamId"><xsl:value-of select="@StreamId"/></number>
      <xsl:variable name="dst" select="EventDetail/Network/*/Destination"/>
      <map key="Destination">
        <string key="IPAddress"><xsl:value-of select="$dst/Device/IPAddress"/></string>
        <map key="Location"><string key="Country"><xsl:value-of select="$dst/Device/Location/Country"/></string></map>
      </map>
      <string key="Rule"><xsl:value-of select="EventDetail/Network/*/Rule"/></string>
    </map>
  </xsl:template>
</xsl:stylesheet>"""


def test_an_indexing_xslt_is_described_by_its_fields():
    described = validation._describe(etree.fromstring(OLD.encode()))
    assert 'mappings' not in described      # '[Event] map/map/string' said nothing of the fields
    assert described['index_fields'] == [
        {'field': 'StreamId', 'source': '@StreamId', 'type': 'number'},
        {'field': 'Destination.IPAddress', 'source': 'EventDetail/Network/*/Destination/Device/IPAddress', 'type': 'string'},
        {'field': 'Destination.Location.Country', 'source': 'EventDetail/Network/*/Destination/Device/Location/Country',
         'type': 'string'},
        {'field': 'Rule', 'source': 'EventDetail/Network/*/Rule', 'type': 'string'}]


async def test_a_draft_says_what_the_index_it_replaces_wrote_that_it_doesnt():
    stroom = SimpleNamespace(get_doc=AsyncMock(return_value={'name': 'FORTIGATE-Index-V1.0', 'data': OLD, 'description': ''}))
    plan = FieldPlan(backend='elasticsearch', index_name='ecs-fortigate', time_field='@timestamp', convention='ecs',
                     fields=[P(name='StreamId', type='id', source='@StreamId'),
                             P(name='destination.ip', type='ip', source='EventDetail/Network/*/Destination/Device/IPAddress')])
    populated = {'EventDetail/Network/Deny/Destination/Device/Location/Country': 40.0}
    with patch.object(indexing, 'gateway_from', lambda ctx: stroom):
        replaced = await indexing._replaced(SimpleNamespace(), 'x-old', plan, populated)
    assert replaced['xslt'] == 'FORTIGATE-Index-V1.0' and replaced['fields'] == 4
    assert replaced['not_in_draft'] == [
        {'field': 'Destination.Location.Country', 'source': 'EventDetail/Network/*/Destination/Device/Location/Country',
         'populated': '40%'},
        {'field': 'Rule', 'source': 'EventDetail/Network/*/Rule', 'populated': 'not in the sample'}]
    assert 'extra_fields' in replaced['hint']
    # Kept with a plan, the old XSLT's fields are the plan's.
    kept = FieldPlan(backend='elasticsearch', index_name='old', time_field='@timestamp',
                     fields=[P(name='Rule', type='keyword', source='EventDetail/Network/*/Rule')])
    from utils.mappingstore import with_mapping
    stroom.get_doc = AsyncMock(return_value={'name': 'Old', 'data': '<x/>',
                                             'description': with_mapping('', 'index', kept.model_dump())})
    with patch.object(indexing, 'gateway_from', lambda ctx: stroom):
        replaced = await indexing._replaced(SimpleNamespace(), 'x-old', plan, populated)
    assert [m['field'] for m in replaced['not_in_draft']] == ['Rule']
