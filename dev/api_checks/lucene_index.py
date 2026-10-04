"""Stroom API checks for indexing, with Stroom's built-in Lucene index (local stack).

Runs after stroom_apis.py has produced an Events stream:

    uv run python dev/api_checks/lucene_index.py               # all steps
    uv run python dev/api_checks/lucene_index.py search        # one step; shares dev/api_checks/out/state.json

Proves the backend-neutral stage 2 flow on Lucene: index doc and fields, indexing pipeline from
the Indexing template, stepping the cooked Events, processing, then a verification dashboard and
test searches through dashboard/v1/search.
"""
import sys
import time
import uuid

from stroom_apis import _step_all, load, ref, s, save, show

INDEX = 'APICHECK-AUTH-V1-INDEX2'
FIELDS = [  # name, type; IDs and the time field are what every stage-2 build needs
    ('StreamId', 'ID'), ('EventId', 'ID'), ('EventTime', 'DATE'),
    ('UserId', 'KEYWORD'), ('HostName', 'KEYWORD'), ('Success', 'KEYWORD'), ('Description', 'TEXT'),
]

INDEX_XSLT = """<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="event-logging:3" xmlns="records:2" xmlns:stroom="stroom"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="3.0">
  <xsl:template match="/Events">
    <records xsi:schemaLocation="records:2 file://records-v2.0.xsd" version="2.0">
      <xsl:apply-templates select="Event" />
    </records>
  </xsl:template>
  <xsl:template match="Event">
    <record>
      <data name="StreamId" value="{@StreamId}" />
      <data name="EventId" value="{@EventId}" />
      <data name="EventTime" value="{EventTime/TimeCreated}" />
      <data name="UserId" value="{EventSource/User/Id}" />
      <data name="HostName" value="{EventSource/Device/HostName}" />
      <data name="Success" value="{EventDetail/Authenticate/Outcome/Success}" />
      <data name="Description" value="{EventDetail/Description}" />
    </record>
  </xsl:template>
</xsl:stylesheet>
"""


def lucene_index(st):
    """Create a Lucene index doc in the build folder and add its fields."""
    node = s.create('Index', INDEX, st['build'])
    doc = s.get(f"/index/v2/{node['uuid']}")
    doc.update(volumeGroupName='Default Volume Group', timeField='EventTime', partitionBy='MONTH',
               partitionSize=1, shardsPerPartition=1)
    doc = s.put(f"/index/v2/{node['uuid']}", doc)
    st['index'] = ref(doc)
    for name, fld_type in FIELDS:
        # Lucene has no KEYWORD field type: a keyword is TEXT with the KEYWORD analyzer (as in the
        # content pack's Example Index). A first run with fldType KEYWORD indexed nothing for them.
        lucene_type, analyzer = ('TEXT', 'KEYWORD') if fld_type == 'KEYWORD' else (
            fld_type, 'ALPHA_NUMERIC' if fld_type == 'TEXT' else 'KEYWORD')
        s.post('/index/v2/addField', {'indexDocRef': st['index'], 'indexField': {
            'fldName': name, 'fldType': lucene_type, 'indexed': True, 'stored': True,
            'analyzerType': analyzer, 'caseSensitive': False}})
    fields = s.post('/index/v2/findFields', {'dataSourceRef': st['index'], 'pageRequest': {'offset': 0, 'length': 50}})
    show('index fields', [(f['fldName'], f['fldType']) for f in fields['values']])


def lucene_pipeline(st):
    """XSLT to records:2 and a child of the Indexing template pointing at the index."""
    x = s.create('XSLT', f'{INDEX}-XSLT', st['build'])
    doc = s.get(f"/xslt/v1/{x['uuid']}")
    doc['data'] = INDEX_XSLT
    st['index_xslt'] = ref(s.put(f"/xslt/v1/{x['uuid']}", doc))
    template = next(d for d in s.find('Indexing', ['Pipeline'])
                    if d['name'] == 'Indexing' and 'Template Pipelines' in (d['path'] or ''))
    p = s.create('Pipeline', f'{INDEX} - Indexing', st['build'])
    doc = s.get(f"/pipeline/v1/{p['uuid']}")
    doc['parentPipeline'] = ref(template)
    doc['pipelineData'] = {'properties': {'add': [
        {'element': 'xsltFilter', 'name': 'xslt', 'value': {'entity': st['index_xslt']}},
        {'element': 'indexingFilter', 'name': 'index', 'value': {'entity': st['index']}},
    ]}}
    st['index_pipeline'] = ref(s.put(f"/pipeline/v1/{p['uuid']}", doc))
    show('indexing pipeline', st['index_pipeline'])


def lucene_step(st):
    """Step the cooked Events through the indexing pipeline; show the document for the first event."""
    records = _step_all({'pipeline': st['index_pipeline'], 'raw_ids': st['events_ids']}, label='index')
    show(f'stepped {len(records)} events; errors per event', [r['errors'] for r in records])
    doc = s.get(f"/pipeline/v1/{st['index_pipeline']['uuid']}")
    # _step_all captures translationFilter; re-step once for xsltFilter output
    result = s.post('/stepping/v1/step', {
        'pipelineDoc': doc, 'stepType': 'FIRST', 'stepSize': 1, 'timeout': 30000, 'code': {},
        'criteria': {'expression': {'type': 'operator', 'op': 'AND', 'children': [
            {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': str(st['events_ids'][0])}]}}})
    show('first indexed document (xsltFilter output)',
         ((result.get('stepData') or {}).get('elementMap') or {}).get('xsltFilter', {}).get('output'), 1200)


def lucene_process(st):
    """Process the Events stream into the index; wait for the task to complete."""
    f = s.post('/processorFilter/v1', {
        'pipeline': st['index_pipeline'], 'processorType': 'PIPELINE', 'enabled': True, 'priority': 10,
        'autoPriority': False, 'reprocess': False, 'export': False, 'maxProcessingTasks': 0,
        'queryData': {'dataSource': {'type': 'StreamStore', 'uuid': '0', 'name': 'StreamStore'},
                      'expression': {'type': 'operator', 'op': 'OR', 'children': [
                          {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': str(i)}
                          for i in st['events_ids']]}}})
    st['index_filter_id'] = f['id']

    def done():
        # Match tasks to this filter by id: a server-side Pipeline term also matched other pipelines'
        # tasks, and content-pack filters (Example Index) process the same Events stream.
        rows = s.post('/processorTask/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': []},
                                                 'pageRequest': {'offset': 0, 'length': 200}})
        statuses = [t['status'] for t in rows.get('values', [])
                    if (t.get('processorFilter') or {}).get('id') == st['index_filter_id']]
        return statuses if statuses and all(x in ('COMPLETE', 'FAILED') for x in statuses) else None
    show('indexing task statuses', s.wait('indexing tasks', done, timeout=300))
    shards = s.post('/index/v2/shard/find', {'pageRequest': {'offset': 0, 'length': 10}, 'indexUuidSet': None})
    show('index shards', [(x.get('indexUuid') == st['index']['uuid'], x.get('documentCount'), x.get('status'))
                          for x in shards.get('values', [])])


def _column(name):
    return {'id': str(uuid.uuid4()), 'name': name, 'expression': '${' + name + '}', 'visible': True,
            'width': 150, 'format': {'type': 'GENERAL'}}


def lucene_dashboard(st):
    """Verification dashboard: a query on the index and a table of the minimal field set."""
    query_id, table_id = 'query-SPK01', 'table-SPK01'
    columns = [_column(n) for n in ('StreamId', 'EventId', 'EventTime', 'UserId', 'HostName', 'Success')]
    table = {'type': 'table', 'queryId': query_id, 'fields': columns, 'extractValues': False,
             'maxResults': [1000], 'pageSize': 100}
    config = {'components': [
        {'type': 'query', 'id': query_id, 'name': 'Query', 'settings': {
            'type': 'query', 'dataSource': st['index'],
            'expression': {'type': 'operator', 'op': 'AND', 'children': []},
            'automate': {'open': False, 'refresh': False}}},
        {'type': 'table', 'id': table_id, 'name': 'Table', 'settings': table}],
        'layout': {'type': 'splitLayout', 'dimension': 1, 'children': [
            {'type': 'tabLayout', 'tabs': [{'id': query_id, 'visible': True}], 'selected': 0},
            {'type': 'tabLayout', 'tabs': [{'id': table_id, 'visible': True}], 'selected': 0}]}}
    node = s.create('Dashboard', f'{INDEX}-VERIFY', st['build'])
    doc = s.get(f"/dashboard/v1/{node['uuid']}")
    doc['dashboardConfig'] = config
    st['verify_dashboard'] = ref(s.put(f"/dashboard/v1/{node['uuid']}", doc))
    st['verify'] = {'query_id': query_id, 'table_id': table_id, 'table': table}
    show('verification dashboard', st['verify_dashboard'])


def _search(st, expression, label):
    v = st['verify']
    request = {
        'searchRequestSource': {'sourceType': 'DASHBOARD_UI', 'ownerDocRef': st['verify_dashboard'],
                                'componentId': v['query_id']},
        'search': {'dataSourceRef': st['index'], 'expression': expression, 'incremental': True,
                   'componentSettingsMap': {v['table_id']: v['table']}},
        'componentResultRequests': [{'type': 'table', 'componentId': v['table_id'], 'fetch': 'ALL',
                                     'requestedRange': {'offset': 0, 'length': 100}, 'tableName': 'Table',
                                     'tableSettings': {k: val for k, val in v['table'].items() if k not in ('type', 'pageSize')}}],
        'dateTimeSettings': {'localZoneId': 'UTC', 'referenceTime': int(time.time() * 1000)},
        'storeHistory': False, 'timeout': 5000}
    started = time.time()
    while True:
        r = s.post('/dashboard/v1/search', request)
        if r.get('complete') or time.time() - started > 60:
            break
        request['queryKey'] = r.get('queryKey')
        time.sleep(0.5)
    if r.get('queryKey'):
        s.post('/dashboard/v1/destroy', r['queryKey']) if False else None
    table = next((x for x in r.get('results') or [] if x.get('componentId') == v['table_id']), {})
    rows = [row['values'] for row in table.get('rows') or []]
    show(f"{label}: {table.get('totalResults')} rows, errors {r.get('errors') or table.get('errors')}", rows)
    return rows


def search(st):
    """Test searches: all docs for the sample streams, exact match on a key field, a time range."""
    term = lambda field, cond, value: {'type': 'term', 'field': field, 'condition': cond, 'value': value}
    all_rows = _search(st, {'type': 'operator', 'op': 'OR', 'children': [
        term('StreamId', 'EQUALS', str(i)) for i in st['events_ids']]}, 'all docs for the sample streams')
    exact = _search(st, {'type': 'operator', 'op': 'AND', 'children': [term('UserId', 'EQUALS', 'bob')]},
                    "UserId = 'bob'")
    window = _search(st, {'type': 'operator', 'op': 'AND', 'children': [
        term('EventTime', 'BETWEEN', '2026-09-28T10:04:00.000Z,2026-09-28T10:08:00.000Z')]}, 'EventTime 10:04 to 10:08')
    st['search_checks'] = {'all': len(all_rows) == 3, 'exact': len(exact) == 1, 'window': len(window) == 2}
    show('checks (expected 3, 1, 2 rows)', st['search_checks'])


STEPS = [lucene_index, lucene_pipeline, lucene_step, lucene_process, lucene_dashboard, search]

if __name__ == '__main__':
    wanted = sys.argv[1:] or [f.__name__ for f in STEPS]
    state = load()
    for fn in STEPS:
        if fn.__name__ in wanted:
            print(f'\n##### {fn.__name__}: {fn.__doc__.strip().splitlines()[0]}')
            try:
                fn(state)
            finally:
                save(state)
