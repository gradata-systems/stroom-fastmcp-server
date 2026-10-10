"""Indexing XSLTs written in the Events translation's style (asked for by the user: "indexing translations should also
use templates, for readability and maintainability ... and variables too - in fact, they should follow the Events
XSLT styles generally"): a template a top-level object, in its own mode (or named, or inline: style.layout), named in
the style's naming, with a comment saying what it writes; an input a template reads often read into a variable."""
import re

from saxonche import PySaxonProcessor

from utils.fieldplan import FieldPlan, PlannedField as P
from utils.xsltgen import XsltStyle

FIELDS = [P(name='StreamId', type='id', source='@StreamId'), P(name='EventId', type='id', source='@EventId'),
          P(name='@timestamp', type='date', source='EventTime/TimeCreated'),
          P(name='user.name', type='keyword', source='EventSource/User/Id'),
          P(name='http.request.method', type='keyword', source='EventDetail/*/Resource/HTTPMethod'),
          P(name='http.request.body.bytes', type='long', source='EventDetail/*/Resource/InboundSize'),
          P(name='event.outcome', type='keyword', source='EventDetail/*/Outcome/Success', transform='outcome')]
EVENTS = """<Events xmlns="event-logging:3"><Event StreamId="7" EventId="1">
<EventTime><TimeCreated>2026-10-11T09:00:00.000Z</TimeCreated></EventTime><EventSource><User><Id>alice</Id></User></EventSource>
<EventDetail><View><Resource><HTTPMethod>GET</HTTPMethod><InboundSize>512</InboundSize></Resource>
<Outcome><Success>false</Success></Outcome></View></EventDetail></Event></Events>"""
EXPECTED = ('<map><number key="StreamId">7</number><number key="EventId">1</number>'
            '<string key="@timestamp">2026-10-11T09:00:00.000Z</string><map key="user"><string key="name">alice</string>'
            '</map><map key="http"><map key="request"><string key="method">GET</string><map key="body"><number '
            'key="bytes">512</number></map></map></map><map key="event"><string key="outcome">failure</string></map></map>')


def plan(**style) -> FieldPlan:
    return FieldPlan(backend='elasticsearch', index_name='ecs-acme-v1', time_field='@timestamp', fields=FIELDS,
                     style=XsltStyle(**style))


def run(xslt: str) -> str:
    with PySaxonProcessor(license=False) as proc:
        out = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=xslt).transform_to_string(
            xdm_node=proc.parse_xml(xml_text=EVENTS))
    return re.sub(r'>\s+<', '><', re.sub(r'<\?xml[^>]*\?>|\s+(xmlns(:\w+)?|xsi:schemaLocation)="[^"]*"', '',
                                         out)).strip()


def test_a_template_a_top_level_object_each_with_a_comment_and_its_inputs_read_once():
    xslt = plan().xslt()
    for part in ('user', 'http', 'event'):
        assert f'<xsl:apply-templates select="." mode="{part}"/>' in xslt
        assert f'<xsl:template match="Event" mode="{part}">' in xslt
    assert '<!-- http: http.request.method from EventDetail/*/Resource/HTTPMethod; http.request.body.bytes from' in xslt
    # Read three times (the object's guard, the field's guard and its value): a variable, declared before its first use.
    assert '<xsl:variable name="http_request_method" select="EventDetail/*/Resource/HTTPMethod"/>' in xslt
    assert '<xsl:if test="$http_request_method or $http_request_body_bytes">' in xslt
    # Read twice: inline, as the Events translation writes it.
    assert 'name="user_name"' not in xslt and '<xsl:value-of select="EventSource/User/Id"/>' in xslt
    assert run(xslt) == f'<array>{EXPECTED}</array>'


def test_the_layout_naming_and_variables_follow_the_style():
    named = plan(layout='named', naming='camelCase', variables='top').xslt()
    assert '<xsl:call-template name="http"/>' in named and '<xsl:template name="http">' in named
    assert '<xsl:variable name="httpRequestMethod"' in named
    top = named.split('<xsl:template name="http">')[1]
    assert top.lstrip().startswith('<xsl:variable name="httpRequestMethod"')
    inline = plan(layout='inline').xslt()
    assert 'mode="http"' not in inline and inline.count('<xsl:template') == 2
    always = plan(variable_min_reads=1).xslt()
    assert '<xsl:variable name="user_name" select="EventSource/User/Id"/>' in always
    for xslt in (named, inline, always):
        assert run(xslt) == f'<array>{EXPECTED}</array>'


def test_a_lucene_record_is_grouped_as_the_event_is():
    lucene = FieldPlan(backend='lucene', index_name='ACME', time_field='EventTime', fields=[
        P(name='StreamId', type='id', source='@StreamId'), P(name='EventTime', type='date', source='EventTime/TimeCreated'),
        P(name='UserId', type='keyword', source='EventSource/User/Id')]).xslt()
    assert '<xsl:apply-templates select="." mode="event_source"/>' in lucene
    assert '<!-- event_source: UserId from EventSource/User/Id -->' in lucene
    assert '<data name="UserId" value="{EventSource/User/Id}"/>' in lucene
