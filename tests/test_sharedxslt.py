from saxonche import PySaxonProcessor

from utils.fielddoc import index_field_mapping_markdown
from utils.fieldplan import FieldPlan, PlannedField
from utils.sharedxslt import describe, imports_of, usage
from utils.xsltgen import SharedTemplate

SHARED = """<xsl:stylesheet xmlns="event-logging:3" xmlns:stroom="stroom" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:template name="eventSourceDevice">
    <xsl:param name="ip" />
    <Device><HostName><xsl:value-of select="stroom:meta('MyHostName')" /></HostName>
      <xsl:if test="$ip"><IPAddress><xsl:value-of select="$ip" /></IPAddress></xsl:if></Device>
  </xsl:template>
  <xsl:template name="eventMeta"><Meta><Source><Id><xsl:value-of select="stroom:meta('GUID')" /></Id></Source></Meta></xsl:template>
  <xsl:template name="unused"><xsl:param name="x" required="yes" /><Data Name="x" /></xsl:template>
  <xsl:template match="Event" mode="decorate"><xsl:copy-of select="." /></xsl:template>
</xsl:stylesheet>"""
CALLER = """<xsl:stylesheet xmlns="event-logging:3" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:import href="Common-Event-V1" />
  <xsl:import href="Missing-V1" />
  <xsl:template match="record">
    <Event>
      <xsl:call-template name="eventMeta" />
      <EventSource><System><Name>Acme</Name></System>
        <xsl:call-template name="eventSourceDevice"><xsl:with-param name="ip" select="data[@name='ip']/@value" /></xsl:call-template>
      </EventSource>
    </Event>
  </xsl:template>
</xsl:stylesheet>"""


def test_a_shared_xslt_is_read_for_what_each_template_writes_reads_and_takes():
    found = describe(SHARED)
    device = found['templates']['eventSourceDevice']
    assert device == {'writes': ['Device'], 'paths': ['Device/HostName', 'Device/IPAddress'],
                      'reads_meta': ['MyHostName'], 'params': {'ip': 'optional'}}
    assert found['templates']['eventMeta']['paths'] == ['Meta/Source/Id']
    assert found['templates']['unused']['params'] == {'x': 'required'}
    assert found['template_rules'] == [{'match': 'Event', 'mode': 'decorate', 'writes': []}]


def test_calls_are_placed_by_the_elements_round_them_with_the_parameters_passed():
    assert imports_of(CALLER) == ['Common-Event-V1', 'Missing-V1']
    found = usage(CALLER, {'Common-Event-V1': SHARED, 'Missing-V1': None})
    by = {u['template']: u for u in found}
    assert by['eventSourceDevice']['at'] == ['EventSource/Device'] and by['eventMeta']['at'] == ['Meta']
    assert by['eventSourceDevice']['with_params'] == {'ip': "data[@name='ip']/@value"}
    assert by['eventSourceDevice']['reads_meta'] == ['MyHostName']
    assert by[None] == {'href': 'Missing-V1', 'template': None, 'note': 'not found as an XSLT document'}


SHARED_JSON = """<xsl:stylesheet xmlns="http://www.w3.org/2005/xpath-functions" xmlns:stroom="stroom"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:template name="guid"><string key="guid">g-1</string></xsl:template>
  <xsl:template name="stroomMeta"><map key="stroom"><string key="feed">F</string></map></xsl:template>
</xsl:stylesheet>"""
EVENTS = """<Events xmlns="event-logging:3"><Event StreamId="7" EventId="2">
<EventTime><TimeCreated>2026-09-28T10:00:00.000Z</TimeCreated></EventTime>
<EventSource><User><Id>alice</Id></User></EventSource></Event></Events>"""


def test_an_indexing_xslt_calls_shared_templates_for_their_fields_and_writes_each_once(tmp_path):
    assert describe(SHARED_JSON)['templates']['stroomMeta']['paths'] == ['stroom/feed']
    fields = [PlannedField(name='StreamId', type='id', source='@StreamId'),
              PlannedField(name='EventId', type='id', source='@EventId'),
              PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
              PlannedField(name='user.id', type='keyword', source='EventSource/User/Id'),
              PlannedField(name='guid', type='keyword', source='shared:guid')]
    plan = FieldPlan(backend='elasticsearch', index_name='x', time_field='@timestamp', fields=fields,
                     shared=[SharedTemplate(href='Common-Elastic-V1', template='guid', at='guid')])
    assert plan.required() == [] and plan.written_by('guid').template == 'guid'
    xslt = plan.xslt()
    assert '<xsl:import href="Common-Elastic-V1" />' in xslt and '<xsl:call-template name="guid" />' in xslt
    assert 'key="guid"' not in xslt
    (tmp_path / 'Common-Elastic-V1').write_text(SHARED_JSON, encoding='utf-8')
    (tmp_path / 'main.xsl').write_text(xslt, encoding='utf-8')
    with PySaxonProcessor(license=False) as proc:
        exe = proc.new_xslt30_processor().compile_stylesheet(stylesheet_file=str(tmp_path / 'main.xsl'))
        output = exe.transform_to_string(xdm_node=proc.parse_xml(xml_text=EVENTS))
    assert output.count('key="guid"') == 1 and '<map key="user"><string key="id">alice</string></map>' in output
    # The index still maps the field; the documentation says where it comes from.
    assert 'guid' in plan.elastic_template('x')['body']['template']['mappings']['properties']
    section = index_field_mapping_markdown(plan, {'EventSource/User/Id': 100.0})
    assert ('| `guid` | Written by the shared template `guid`. | keyword | shared template `guid` of '
            '`Common-Elastic-V1` | always |') in section


def test_a_shared_object_in_an_indexing_xslt_and_a_lucene_one():
    fields = [PlannedField(name='StreamId', type='id', source='@StreamId'),
              PlannedField(name='EventId', type='id', source='@EventId'),
              PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
              PlannedField(name='stroom.feed', type='keyword', source='shared:stroomMeta')]
    use = SharedTemplate(href='Common-Elastic-V1', template='stroomMeta', at='stroom')
    plan = FieldPlan(backend='elasticsearch', index_name='x', time_field='@timestamp', fields=fields, shared=[use])
    xslt = plan.xslt()
    assert '<xsl:call-template name="stroomMeta" />' in xslt and 'key="stroom"' not in xslt and 'key="feed"' not in xslt
    lucene = FieldPlan(backend='lucene', index_name='x', time_field='@timestamp', fields=fields[:3] + [
        PlannedField(name='Guid', type='keyword', source='shared:guid')],
        shared=[SharedTemplate(href='Common-Lucene-V1', template='guid', at='Guid', with_params={'n': '1'})])
    text = lucene.xslt()
    assert '<xsl:import href="Common-Lucene-V1" />' in text and 'name="Guid"' not in text
    assert '<xsl:call-template name="guid"><xsl:with-param name="n" select="1" /></xsl:call-template>' in text



def test_shared_functions_are_described_with_their_namespace_and_how_importers_call_them():
    from utils.sharedxslt import describe, usage
    shared = ('<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:gs="urn:gs" '
              'xmlns:xs="http://www.w3.org/2001/XMLSchema" version="3.0">'
              '<xsl:function name="gs:isLocalIpAddress" as="xs:boolean"><xsl:param name="ipAddress" as="xs:string"/>'
              '<xsl:sequence select="starts-with($ipAddress, \'10.\')"/></xsl:function></xsl:stylesheet>')
    assert describe(shared)['functions'] == {'gs:isLocalIpAddress': {
        'prefix': 'gs', 'namespace': 'urn:gs', 'params': ['ipAddress as xs:string'], 'returns': 'xs:boolean'}}
    caller = ('<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:gs="urn:gs" version="3.0">'
              '<xsl:import href="IP Lookup"/><xsl:template match="record">'
              '<xsl:if test="not(gs:isLocalIpAddress($sourceIp))"/></xsl:template></xsl:stylesheet>')
    [use] = usage(caller, {'IP Lookup': shared})
    assert use['function'] == 'gs:isLocalIpAddress' and use['namespace'] == 'urn:gs' and use['calls'] == 1
    assert use['example'] == 'not(gs:isLocalIpAddress($sourceIp))' and 'own_copy' not in use



def test_a_call_in_a_part_template_is_placed_where_that_template_is_applied():
    # Seen live: the house style calls shared templates inside a networkSource mode, applied in Network/Connect/Source.
    from utils.sharedxslt import calls_of
    caller = ('<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns="event-logging:3" version="3.0">'
              '<xsl:template match="node()" mode="event"><Event><EventDetail><Network><Connect><Source>'
              '<xsl:apply-templates select="." mode="networkSource"/></Source></Connect></Network></EventDetail></Event>'
              '</xsl:template><xsl:template match="node()" mode="networkSource"><Device>'
              '<xsl:call-template name="ipAddressToLocation"/></Device></xsl:template></xsl:stylesheet>')
    [call] = calls_of(caller, {'ipAddressToLocation': ['Location']})
    assert call['within'] == 'EventDetail/Network/Connect/Source/Device'
    assert call['at'] == ['EventDetail/Network/Connect/Source/Device/Location']
