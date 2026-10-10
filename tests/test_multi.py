"""Several events from one record (for_each), repeated elements from arrays (repeat), and drop conditions, in the
translation, reference-data and indexing XSLTs."""
from pathlib import Path

import yaml
from lxml import etree

from tests.test_xsltgen import SCHEMA, VALIDATOR, transform
from utils.fieldplan import FieldPlan, PlannedField
from utils.localcheck import check_mapping, sample_records
from utils.refgen import ReferenceMapping, generate_reference
from utils.fielddoc import field_mapping_markdown
from utils.xsltgen import TranslationMapping, generate

NS = {'e': 'event-logging:3'}
CASE = yaml.safe_load((Path(__file__).resolve().parents[1] / 'dev' / 'eval' / 'cases' / '16_json_batches_items.yaml')
                      .read_text(encoding='utf-8'))
# What the JSONParser emits for the case's sample (addRootObject false).
BATCHES = """<array xmlns="http://www.w3.org/2013/XSL/json">
<map><string key="host">app01</string><string key="batch">b1</string><array key="events">
  <map><string key="ts">2026-10-01T10:00:00.000Z</string><string key="user">alice</string><string key="action">LOGIN</string><array key="roles"><string>admin</string><string>dev</string></array></map>
  <map><string key="ts">2026-10-01T10:00:30.000Z</string><string key="user">monitor</string><string key="action">HEARTBEAT</string><array key="roles"/></map>
  <map><string key="ts">2026-10-01T10:05:00.000Z</string><string key="user">bob</string><string key="action">LOGOUT</string><array key="roles"><string>dev</string></array></map>
</array></map>
<map><string key="host">qa-app9</string><string key="batch">b3</string><array key="events">
  <map><string key="ts">2026-10-01T10:20:00.000Z</string><string key="user">tester</string><string key="action">LOGIN</string><array key="roles"><string>qa</string></array></map>
</array></map>
</array>"""


def mapping(**overrides) -> TranslationMapping:
    plain = {**CASE['reference']['mapping'], 'unmatched': 'skip'}
    plain['common'] = [{k: v for k, v in e.items() if k not in ('time_format',)} for e in plain['common']]
    return TranslationMapping.model_validate({**plain, **overrides})


def test_each_item_is_an_event_with_record_fields_repeats_and_drops():
    result = generate(mapping(), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    assert result['items'] == 'events'
    xslt = result['xslt']
    assert '<xsl:apply-templates select="*[@key=\'events\']/*" mode="item">' in xslt
    assert '<xsl:with-param name="record" select="." tunnel="yes"/>' in xslt
    assert '<xsl:param name="record" tunnel="yes"/>' in xslt
    assert "$record/*[@key='host']" in xslt
    assert '<xsl:for-each select="*[@key=\'roles\']/*">' in xslt
    assert [e['event'] for e in result['events']] == ['drop: QA traffic', 'drop: heartbeats are not events', 'logon', 'logoff']
    events = transform(xslt, BATCHES)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    alice, bob = events.findall('e:Event', NS)   # the heartbeat and the QA batch are dropped
    assert alice.findtext('e:EventSource/e:Device/e:HostName', namespaces=NS) == 'app01'
    assert alice.findtext('e:EventSource/e:User/e:Id', namespaces=NS) == 'alice'
    assert [g.findtext('e:Name', namespaces=NS) for g in alice.findall('e:EventSource/e:User/e:Groups/e:Group', NS)] == ['admin', 'dev']
    assert alice.find(".//e:Authenticate/e:Data[@Name='batch']", NS).get('Value') == 'b1'
    assert alice.findtext('.//e:Authenticate/e:Action', namespaces=NS) == 'Logon'
    assert [g.findtext('e:Name', namespaces=NS) for g in bob.findall('e:EventSource/e:User/e:Groups/e:Group', NS)] == ['dev']
    assert bob.findtext('.//e:Authenticate/e:Action', namespaces=NS) == 'Logoff'
    doc = field_mapping_markdown(mapping(), SCHEMA)
    assert doc.startswith('Each record holds several events: one per `events` item.')
    assert '- `host` (of the record) matches `^qa-`: QA traffic' in doc and 'one per value' in doc


def test_repeated_data_elements_and_repeat_rules():
    tags = [{'path': 'EventDetail/TypeId', 'value': 'x'},
            {'path': 'EventDetail/Unknown/Data', 'data_name': 'role', 'field': 'roles', 'repeat': True, 'transform': 'upper'}]
    result = generate(mapping(common=[e for e in mapping().model_dump(exclude_none=True)['common']
                                      if not e['path'].startswith('EventDetail/')],
                              events=[{'name': 'any', 'fields': tags}]), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    events = transform(result['xslt'], BATCHES)
    alice = events.findall('e:Event', NS)[0]
    assert [d.get('Value') for d in alice.findall(".//e:Unknown/e:Data[@Name='role']", NS)] == ['ADMIN', 'DEV']
    # A repeat needs an element that may repeat, and nothing else mapped below it.
    bad = generate(mapping(common=[{'path': 'EventSource/User/Id', 'field': 'roles', 'repeat': True}]), SCHEMA, '4.1.0')
    assert any('repeat needs an element on the path the schema lets repeat' in p for p in bad['problems'])
    crowded = generate(mapping(common=mapping().model_dump(exclude_none=True)['common'] + [
        {'path': 'EventSource/User/Groups/Group/Id', 'field': 'user'}]), SCHEMA, '4.1.0')
    assert any('map nothing else below it' in p for p in crowded['problems'])


def test_the_sample_check_looks_in_items_and_records_by_scope():
    m = mapping(common=mapping().model_dump(exclude_none=True)['common'] + [{'path': 'EventSource/User/Name', 'field': 'nickname'}])
    records, _ = sample_records(m, CASE['sample'])
    check = check_mapping(m, records)
    assert check['records'] == 3
    [warning] = [w for w in check['warnings'] if not w.startswith('fields in the sample that nothing reads')]
    assert warning.startswith("field 'nickname' (used for EventSource/User/Name) is in none of the 5 sample items")
    assert 'roles' in check['fields_seen'] and 'host' in check['fields_seen']


def test_reference_data_and_indexing_xslts_can_leave_records_out():
    ref = generate_reference(ReferenceMapping.model_validate({'input': 'data_splitter', 'maps': [
        {'name': 'USERS', 'key': 'user', 'values': [{'field': 'name'}]}],
        'drop_when': [{'when': [{'field': 'status', 'equals': 'disabled'}], 'reason': 'disabled accounts'}]}))
    assert ref['ok'], ref['problems']
    assert "<xsl:if test=\"not((data[@name='status']/@value = 'disabled'))\">" in ref['xslt']
    out = transform(ref['xslt'], '<records xmlns="records:2"><record><data name="user" value="a"/><data name="name" value="A"/>'
                                 '<data name="status" value="disabled"/></record><record><data name="user" value="b"/>'
                                 '<data name="name" value="B"/><data name="status" value="active"/></record></records>')
    assert [k.text for k in out.iter('{reference-data:2}key')] == ['b']
    plan = FieldPlan(backend='lucene', index_name='x', time_field='EventTime', drop_when=["EventDetail/TypeId = 'Heartbeat'", "EventSource/User/Id = 'monitor'"],
                     fields=[PlannedField(name='StreamId', type='id', source='@StreamId'), PlannedField(name='EventId', type='id', source='@EventId'),
                             PlannedField(name='EventTime', type='date', source='EventTime/TimeCreated')])
    assert "<xsl:apply-templates select=\"Event[not((EventDetail/TypeId = 'Heartbeat') or (EventSource/User/Id = 'monitor'))]\"/>" in plan.xslt()
    assert etree.fromstring(plan.xslt().encode()) is not None
