"""Samples whose records come in different shapes, and fields holding JSON text. Seen in a test environment: two JSON
structures in one sample, a whole JSON line written into TypeId, and the second shape's events without time or kind."""
import json

from pathlib import Path

from lxml import etree
from saxonche import PySaxonProcessor

from utils.draftmap import draft_mapping
from utils.eventschema import EventSchema
from utils.localcheck import rule_of, sample_records
from utils.xpathcheck import json_xml
from utils.xsltgen import TranslationMapping, generate

SCHEMA = EventSchema.parse((Path(__file__).parent / 'fixtures' / 'event-logging-v3.5.2.xsd').read_bytes())
NS = {'e': 'event-logging:3'}
FLAT = [{'timestamp': f'2026-10-06T09:0{i}:00Z', 'event_type': t, 'user': u, 'src_ip': f'10.0.0.{i}', 'host': 'fw1'}
        for i, (t, u) in enumerate([('login', 'alice'), ('login', 'bob'), ('logout', 'alice')])]
HELD = [{'@timestamp': f'2026-10-06T10:0{i}:00Z', 'host': 'app01', 'level': 'INFO',
         'message': json.dumps({'timestamp': f'2026-10-06T10:0{i}:00Z', 'event_type': t, 'user': u})}
        for i, (t, u) in enumerate([('user_authentication_check', 'carol'), ('user_authentication_check', 'dave'),
                                    ('password_change', 'erin')])]


def run(xslt: str, records: list[dict]) -> etree._Element:
    # Stroom's format-date isn't available here: one that passes the (ISO) time through stands in for it.
    stand_in = ''.join(f'<xsl:function name="stroom:format-date">' + ''.join(f'<xsl:param name="p{i}"/>' for i in range(n))
                       + '<xsl:sequence select="string($p0)"/></xsl:function>' for n in (2, 3, 4))
    xslt = xslt.replace('</xsl:stylesheet>', stand_in + '</xsl:stylesheet>')
    proc = PySaxonProcessor(license=False)
    doc = proc.parse_xml(xml_text='<array xmlns="http://www.w3.org/2013/XSL/json">'
                                  + ''.join(json_xml(r) for r in records) + '</array>')
    out = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=xslt).transform_to_string(xdm_node=doc)
    return etree.fromstring(out.encode())


def test_every_shape_is_read_and_json_held_in_a_field_is_read_inside():
    draft = draft_mapping({'a.json': json.dumps(FLAT), 'b.json': json.dumps(HELD)}, 'App', 'App', 'Prod')
    mapping = draft['mapping']
    common = {e['path']: e for e in mapping['common']}
    assert mapping['json_fields'] == ['message']
    assert common['EventTime/TimeCreated']['any_of'] == ['timestamp', '@timestamp']
    assert common['EventDetail/TypeId']['any_of'] == ['event_type', 'message.event_type']
    assert common['EventSource/User/Id']['any_of'] == ['user', 'message.user']
    assert 'EventDetail/Description' not in common          # the JSON text itself isn't written into an element
    assert {r['name'] for r in mapping['events']} >= {'login', 'logout', 'user_authentication_check', 'password_change'}
    assert any('2 shapes' in n for n in draft['notes']) and any("'message' holds JSON" in n for n in draft['notes'])
    # The local checks read the held keys as the XSLT does.
    model = TranslationMapping.model_validate(mapping)
    records, _ = sample_records(model, [json.dumps(FLAT), json.dumps(HELD)])
    assert [rule_of(model, r) for r in records] == ['login', 'login', 'logout', 'user_authentication_check',
                                                    'user_authentication_check', 'password_change']
    generated = generate(model, SCHEMA, '3.5.2')
    assert generated['ok'], generated['problems']
    empty = {'@timestamp': '2026-10-06T10:09:00Z', 'host': 'app01', 'level': 'INFO', 'message': ''}
    events = run(generated['xslt'], FLAT + HELD + [empty])          # the empty one: no fatal error
    got = [(e.findtext('e:EventTime/e:TimeCreated', namespaces=NS), e.findtext('e:EventDetail/e:TypeId', namespaces=NS),
            e.findtext('e:EventSource/e:User/e:Id', namespaces=NS)) for e in events.findall('e:Event', NS)]
    assert got[0] == ('2026-10-06T09:00:00Z', 'login', 'alice')
    assert got[3] == ('2026-10-06T10:00:00Z', 'user_authentication_check', 'carol')
    assert got[5] == ('2026-10-06T10:02:00Z', 'password_change', 'erin') and len(got) == 7


def test_one_shape_drafts_as_before():
    mapping = draft_mapping({'a.json': json.dumps(FLAT)}, 'App', 'App', 'Prod')['mapping']
    common = {e['path']: e for e in mapping['common']}
    assert common['EventTime/TimeCreated']['field'] == 'timestamp' and 'json_fields' not in mapping
    assert common['EventDetail/TypeId']['field'] == 'event_type'
