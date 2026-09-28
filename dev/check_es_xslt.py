"""Check the Elasticsearch indexing XSLT that draft_index_mapping generates, without Elasticsearch.

    uv run python dev/check_es_xslt.py

Runs the generated XSLT with Saxon over a real Events record from the local stack and validates the output
against the local Stroom's xpath-functions.xsd, which the 'Events to Elasticsearch' template's SchemaFilter
(schema group JSON) validates against before the ElasticIndexingFilter.
"""
import json
import sys
from pathlib import Path

import httpx
from lxml import etree
from saxonche import PySaxonProcessor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.fieldplan import FieldPlan, PlannedField  # noqa: E402

env = dict(line.split('=', 1) for line in (ROOT / 'dev' / 'stroom' / '.env').read_text().splitlines() if '=' in line)
stroom = httpx.Client(base_url='http://127.0.0.1:18080/api', headers={'Authorization': f"Bearer {env['STROOM_ADMIN_API_KEY']}"})


def events_record() -> str:
    metas = stroom.post('/meta/v1/find', json={'expression': {'type': 'operator', 'op': 'AND', 'children': [
        {'type': 'term', 'field': 'Type', 'condition': 'EQUALS', 'value': 'Events'}]},
        'pageRequest': {'offset': 0, 'length': 1}}).json()['values']
    meta = metas[0]['meta']['id']
    data = stroom.post('/data/v1/fetch', json={'sourceLocation': {'metaId': meta, 'partIndex': 0, 'recordIndex': 0,
                                                                  'childType': None}, 'displayMode': 'TEXT', 'recordCount': 1}).json()['data']
    # IdEnrichmentFilter adds these before the XSLT in an indexing pipeline.
    return data.replace('<Event>', f'<Event StreamId="{meta}" EventId="1">', 1)


def xpath_functions_schema() -> etree.XMLSchema:
    found = stroom.post('/explorer/v2/find', json={'filter': {'includedTypes': ['XMLSchema'], 'requiredPermissions': ['VIEW'],
                                                              'nameFilter': 'xpath-functions'},
                                                   'pageRequest': {'offset': 0, 'length': 5}}).json()['values']
    uuid = next(v['docRef']['uuid'] for v in found if v['docRef']['type'] == 'XMLSchema')
    return etree.XMLSchema(etree.fromstring(stroom.get(f'/xmlSchema/v1/{uuid}').json()['data'].encode()))


def main():
    plan = FieldPlan(backend='elasticsearch', index_name='stroom-e2e-v1', time_field='@timestamp', fields=[
        PlannedField(name='StreamId', type='id', source='@StreamId'),
        PlannedField(name='EventId', type='id', source='@EventId'),
        PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
        PlannedField(name='user.name', type='keyword', source='EventSource/User/Id'),
        PlannedField(name='host.name', type='keyword', source='EventSource/Device/HostName'),
        PlannedField(name='event.outcome', type='boolean', source='EventDetail/Authenticate/Outcome/Success'),
        PlannedField(name='source.ip', type='ip', source='EventSource/Client/IPAddress')])
    with PySaxonProcessor(license=False) as proc:
        exe = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=plan.xslt())
        output = exe.transform_to_string(xdm_node=proc.parse_xml(xml_text=events_record()))
    print(output)
    schema = xpath_functions_schema()
    ok = schema.validate(etree.fromstring(output.encode()))
    print('valid against xpath-functions.xsd:', ok, [e.message for e in schema.error_log][:3])
    print(json.dumps(plan.elastic_template('stroom-e2e-v1'), indent=1))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
