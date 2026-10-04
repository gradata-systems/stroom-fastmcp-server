"""A discovery index against the local Stroom stack and Elasticsearch 9 (see dev/stroom).

    cd dev/stroom && docker compose --profile elastic up -d
    uv run python dev/e2e_discovery.py

Raw JSON indexed as it is, for exploration, with no event-logging translation and nothing surveyed up front:
Elasticsearch maps the source's fields dynamically, and only StreamId, EventId and @timestamp are mapped
explicitly. The sample holds nested objects, arrays, a message that is sometimes JSON text and sometimes not, and
numbers, so the dynamic mapping has something to do.

1. A raw JSON sample uploaded to a new workspace feed. The local stack has no discovery template, so this adds a
   fixture one ('E2E Raw to Elasticsearch': JSONParser, one record per split, XSLTFilter, ElasticIndexingFilter).
2. The discovery plan from what the user confirms (the timestamp field, stream meta to add, a field to drop):
   the XSLT copies each record, unpacking JSON held in a string into a sibling <field>_json object.
3. The indexing pipeline from the template, stepped over the raw stream: the documents show the source's fields.
4. The index template: permissive, with the settings and component templates of the user's example, agreed by
   the user; indexing is refused before that, and the approval asks whether it is committed.
5. The cluster admin applies it; indexing on the raw stream; Elasticsearch has mapped every field dynamically
   (strings as keywords), the JSON message unpacked, the dropped field absent; Stroom's searches find the
   documents; the pipeline is documented with the fields the sample documents held.

Then for an existing raw feed (outside the build, already holding three streams sent over time, whose data
drifts: an older stream's latency_ms is "n/a", only the newest has a geo object):

6. The feed's Raw Events streams are found; one record is read to confirm the time field, nothing more.
7. The discovery pipeline steps the two newest streams. The template is not offered to agree until the user
   gives an example: this time a standalone template already on the cluster, with no component templates (the
   first scenario's has one), as GET returns it. Built from it, the template is agreed and committed.
8. The existing streams are indexed by id, and a feed-wide filter from now on indexes what is sent next: a
   fourth stream sent afterwards is indexed with no change. Every record of the four streams is in the index;
   the drift is absorbed (malformed numbers ignored, new fields mapped as they arrive); Stroom's searches find
   them all.

With --shapes only (and last in a full run), awkward shapes: arrays of objects (indexed flattened, and documented
so), arrays of numbers and of arrays, null and empty values, deep nesting, keys with spaces, a JSON array held in a
string (left as text), a mixed array (its odd value ignored), keys Elasticsearch refuses or Stroom drops (_id, other
_ keys at any depth, an empty key, a..b) or that repeat ours (@timestamp): renamed or repaired. One record has a
value where an earlier one had an object: Elasticsearch rejects it, and that is reported as an Error stream that
triage explains (which document, and why), while the others are indexed.
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from e2e_elastic_handover import ES, _request, live_cluster  # noqa: E402
from searching import paired  # noqa: E402
from config import Settings  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import builds, feeds, indexing, processing_writes, stepping, streams, templates, translation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import Discovery, FieldPlan  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

FIXTURE_TEMPLATE = 'E2E Raw to Elasticsearch'

SAMPLE = json.dumps([
    {'ts': '2026-10-01T09:00:00Z', 'host': 'web01', 'user': {'name': 'alice', 'roles': ['admin', 'ops']},
     'event': 'login', 'status': 200, 'latency_ms': 12.5, 'session_token': 'tok-1',
     'message': json.dumps({'action': 'login', 'client': {'ip': '10.1.1.1', 'agent': 'curl/8.9'}, 'ok': True})},
    {'ts': '2026-10-01T09:01:30Z', 'host': 'web02', 'user': {'name': 'bob'}, 'event': 'logout', 'status': 200,
     'session_token': 'tok-2', 'message': 'session closed by user'},
    {'ts': '2026-10-01T09:05:00Z', 'host': 'web01', 'user': {'name': 'carol'}, 'event': 'upload', 'status': 500,
     'tags': ['bulk', 'retry'], 'session_token': 'tok-3',
     'message': json.dumps({'action': 'upload', 'bytes': 1048576, 'error': {'code': 'E42', 'detail': 'disk full'}})},
], indent=1)

# The user's example: a sibling discovery index's template, for its settings and components (not its fields:
# a discovery index keeps the source's names).
EXAMPLE = """PUT _index_template/stroom-discovery-proxy-v1
{"index_patterns": ["stroom-discovery-proxy-v1*"], "priority": 250, "composed_of": ["e2e-discovery-base"],
 "template": {"settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
              "mappings": {"dynamic": true, "properties": {"proxy": {"properties": {"name": {"type": "keyword"}}}}}}}"""
COMPONENT = """PUT _component_template/e2e-discovery-base
{"template": {"mappings": {"properties": {"StreamId": {"type": "long"}, "EventId": {"type": "long"}}}}}"""


PARSERS = {FIXTURE_TEMPLATE: ('jsonParser', 'JSONParser'),
           'E2E Raw Text to Elasticsearch': ('dsParser', 'DSParser'),
           'E2E Raw XML to Elasticsearch': ('xmlParser', 'XMLParser')}


async def fixture_template(stroom: StroomGateway, name: str = FIXTURE_TEMPLATE) -> dict:
    """A discovery template: a parser (JSON, a Data Splitter for delimited text, or XML) and one-record splits, then
    an XSLT and the Elasticsearch indexing filter."""
    found = {v['docRef']['name']: v['docRef'] for v in
             (await stroom.find_documents('E2E*', ['Pipeline'], 50)).get('values') or []}
    from e2e_elastic_handover import with_json_schema_filter
    if name in found:
        return await with_json_schema_filter(stroom, found[name])
    parent = await stroom.post('/explorer/v2/find', {
        'filter': {'includedTypes': ['Folder'], 'nameFilter': 'Template Pipelines', 'requiredPermissions': ['VIEW']},
        'pageRequest': {'offset': 0, 'length': 5}})
    node = await stroom.post('/explorer/v2/create', {
        'docType': 'Pipeline', 'docName': name, 'permissionInheritance': 'DESTINATION',
        'destinationFolder': parent['values'][0]['docRef'] if parent.get('values') else None})
    ref = node.get('docRef', node)
    doc = await stroom.get(f"/pipeline/v1/{ref['uuid']}")
    parser = PARSERS[name]
    elements = [parser, ('readRecordCountFilter', 'RecordCountFilter'), ('splitFilter', 'SplitFilter'),
                ('xsltFilter', 'XSLTFilter'), ('elasticIndexingFilter', 'ElasticIndexingFilter')]
    properties = [
        {'element': 'readRecordCountFilter', 'name': 'countRead', 'value': {'boolean': True}},
        # One record per split, so the record number (EventId) finds exactly one record again.
        {'element': 'splitFilter', 'name': 'splitDepth', 'value': {'integer': 1}},
        {'element': 'splitFilter', 'name': 'splitCount', 'value': {'integer': 1}}]
    if parser[1] == 'JSONParser':
        properties.insert(0, {'element': 'jsonParser', 'name': 'addRootObject', 'value': {'boolean': False}})
    doc['pipelineData'] = {
        'elements': {'add': [{'id': i, 'type': t} for i, t in elements]},
        'links': {'add': [{'from': a[0], 'to': b[0]} for a, b in zip(elements, elements[1:])]},
        'properties': {'add': properties}}
    doc['description'] = 'Fixture for dev/e2e_discovery.py'
    await stroom.request('PUT', f"/pipeline/v1/{ref['uuid']}", doc)
    return await with_json_schema_filter(stroom, {'type': 'Pipeline', 'uuid': ref['uuid'], 'name': name})


CSV_SAMPLE = ('time,user,host,status,latency_ms,msg,secret\n'
              '2026-10-02T10:00:00Z,alice,web01,200,12.5,{"action":"login"},s1\n'
              '2026-10-02T10:01:00Z,bob,web02,404,n/a,page missing,s2\n'
              '2026-10-02T10:02:00Z,carol,web01,500,30,{"action":"upload"},s3\n')
XML_SAMPLE = ('<logons xmlns="urn:example:logons">'
              '<logon id="1"><when>2026-10-02T11:00:00Z</when><who>dave</who><roles><role>admin</role><role>ops</role></roles>'
              '<client ip="10.2.2.1">laptop</client><status>200</status></logon>'
              '<logon id="2"><when>2026-10-02T11:05:00Z</when><who>erin</who><roles><role>ops</role></roles>'
              '<client ip="10.2.2.2">phone</client><status>401</status></logon>'
              '<logon id="3"><when>2026-10-02T11:09:00Z</when><who>frank</who><roles><role>ops</role></roles>'
              '<client ip="10.2.2.3">laptop</client><status>500</status><detail><code>E42</code></detail></logon>'
              '</logons>')


# The fields each format's documentation must list, and a value from the sample each must show.
DOCUMENTED = {
    'csv': (['user', 'host', 'status', 'latency_ms', 'msg', 'msg_json.action'],
            {'user': 'alice', 'host': 'web01', 'status': '200', 'msg_json.action': 'login'}),
    'xml': (['who', 'roles.role', 'client.ip', 'client.value', 'status', 'detail.code'],
            {'who': 'dave', 'roles.role': 'admin', 'client.ip': '10.2.2.1', 'client.value': 'laptop',
             'detail.code': 'E42'}),
}


async def text_formats(ctx, stroom: StroomGateway, es: httpx.AsyncClient, stamp: str) -> None:
    """Delimited text and XML, indexed as they are."""
    from e2e_translation import CSV_SPLITTER
    for kind, sample, template_name, discovery, checks in (
        ('csv', CSV_SAMPLE, 'E2E Raw Text to Elasticsearch',
         Discovery(input='delimited', timestamp_field='time', drop=['secret']), [
             ('user', 'EQUALS', 'bob', 1, {'term': {'user': 'bob'}}),
             ('status', 'GREATER_THAN', '400', 2, {'range': {'status': {'gt': 400}}}),      # a number, from text
             ('latency_ms', 'LESS_THAN', '20', 1, {'range': {'latency_ms': {'lt': 20}}}),   # its 'n/a' ignored
             ('msg_json.action', 'EQUALS', 'upload', 1, {'term': {'msg_json.action': 'upload'}}),
             ('msg', 'EQUALS', 'page*', 1, {'wildcard': {'msg': 'page*'}}),
         ]),
        ('xml', XML_SAMPLE, 'E2E Raw XML to Elasticsearch',
         Discovery(input='xml', record='logon', timestamp_field='when'), [
             ('who', 'EQUALS', 'erin', 1, {'term': {'who': 'erin'}}),
             ('roles.role', 'EQUALS', 'ops', 3, {'term': {'roles.role': 'ops'}}),        # repeated elements
             ('client.ip', 'EQUALS', '10.2.2.1', 1, {'term': {'client.ip': '10.2.2.1'}}),  # an attribute
             ('client.value', 'EQUALS', 'laptop', 2, {'term': {'client.value': 'laptop'}}),
             ('status', 'GREATER_THAN', '400', 2, {'range': {'status': {'gt': 400}}}),
             ('detail.code', 'EQUALS', 'E42', 1, {'term': {'detail.code': 'E42'}}),
         ]),
    ):
        build, feed = f'e2e-discovery-{kind}-{stamp}', f'E2E-{kind.upper()}-RAW-{stamp}'
        index = f'stroom-discovery-{kind}-{stamp}-v1'
        print(f'\n### {kind}: indexed as it is')
        await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
        raw = (await feeds.upload_sample(ctx, feed, sample))['stream_id']
        if kind == 'csv':
            await translation.create_text_converter(ctx, build, feed, 'DATA_SPLITTER', CSV_SPLITTER)
        template = await fixture_template(stroom, template_name)
        cluster = await live_cluster(stroom)
        draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', index, discovery=discovery)
        plan = FieldPlan.model_validate(draft['plan'])
        xslt = await translation.save_xslt(ctx, build, f'{index}-XSLT', index_plan=plan)
        pipeline = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=build, name=f'{index} - Discovery',
                                    template_uuid=template['uuid'], xslt_uuid=xslt['uuid'], index_name=index,
                                    cluster_uuid=cluster['uuid'])
        sample_step = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
        e2e.check(sample_step['verdict'] == 'clean' and sample_step['records_stepped'] == 3,
                  f"stepped {sample_step['records_stepped']} {kind} records clean: "
                  f"{[(g['class'], g.get('examples', [{}])[0].get('message', '')[:100]) for g in sample_step['groups']]}")
        documents = await indexing._documents(ctx, pipeline['uuid'], [raw], 5)
        e2e.check([d['EventId'][1] for d in documents] == ['1', '2', '3'], 'one document per record, EventId the record number')
        final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], plan=plan,
                                 events_stream_ids=[raw],
                                 example_template='PUT _index_template/e2e-discovery-legacy-v1\n' + json.dumps(STANDALONE))
        e2e.check(final['template']['template']['mappings'].get('numeric_detection') is True,
                  'agreed: text holds numbers as text, so numeric detection is on')
        path, request = _request(final['dev_tools'])
        e2e.check((await es.put(f'/{path}', json=request)).status_code == 200, f'PUT {path}')
        started = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                                   stream_ids=[raw])
        done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [raw], expect_events=False,
                                                           filter_id=started['filter_id'])
        e2e.check(done.get('gate') == 'pass', f"indexed with no Error stream: {done.get('streams')}")
        await es.post(f'/{index}/_refresh')
        mapped = (await es.get(f'/{index}/_mapping')).json()[index]['mappings']['properties']
        e2e.check('secret' not in mapped and 'time' in mapped or kind == 'xml', 'the dropped column was never indexed')
        doc = await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=build, backend='elasticsearch', name=index,
                               time_field='@timestamp', index_name=index, cluster_uuid=cluster['uuid'])
        await paired(ctx, es, build, index, doc['uuid'], [raw], 3, ['StreamId', 'EventId', '@timestamp'], checks,
                     pipeline_uuid=pipeline['uuid'])
        written = await builds.write_documentation(ctx, build, pipeline['uuid'],
                                                   f'## Purpose and data\n\n{kind.upper()} records, indexed as they are '
                                                   f'for exploration.\n', 'Created', stream_ids=[raw])
        fields, values = DOCUMENTED[kind]
        await e2e.documented_to_the_field(stroom, written, ['StreamId', 'EventId', '@timestamp'] + fields, values,
                                          f'the {kind} discovery pipeline')
        e2e.check('secret' not in e2e.field_rows(written.get('field_mapping') or ''), 'the dropped column is not documented')


async def main():
    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=e2e.VERSION)
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = time.strftime('%H%M%S')
    try:
        async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
            try:
                version = (await es.get('/')).json()['version']['number']
            except httpx.HTTPError:
                raise SystemExit(f"No Elasticsearch at {ES}: cd dev/stroom && docker compose --profile elastic up -d")
            e2e.check(version.startswith('9.'), f'Elasticsearch {version}')
            only = next((a for a in sys.argv[1:] if a.startswith('--')), None)
            if only in (None, '--json'):
                await run(ctx, stroom, es, stamp)
                await existing_feed(ctx, stroom, es, stamp)
            if only in (None, '--formats'):
                await text_formats(ctx, stroom, es, stamp)
            if only in (None, '--shapes'):
                await shapes(ctx, stroom, es, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


async def run(ctx, stroom: StroomGateway, es: httpx.AsyncClient, stamp: str) -> str:
    build, feed = f'e2e-discovery-{stamp}', f'E2E-WEB-{stamp}'
    index = f'stroom-discovery-web-{stamp}-v1'
    print('\n### the raw sample')
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, SAMPLE))['stream_id']
    e2e.check(raw is not None, f'raw JSON uploaded as stream {raw}')
    template = await fixture_template(stroom)
    candidates = (await templates.find_pipeline_templates(ctx, 'discovery'))['candidates']
    found = next((c for c in candidates if c['name'] == FIXTURE_TEMPLATE), None)
    e2e.check(found is not None and found['backend'] == 'elasticsearch', 'the fixture reads as a discovery template')
    cluster = await live_cluster(stroom)

    print('\n### the discovery plan: nothing surveyed, only what the user confirms')
    discovery = Discovery(timestamp_field='ts', meta={'stroom.feed': 'Feed'}, drop=['session_token'])
    draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', index, discovery=discovery)
    plan = FieldPlan.model_validate(draft['plan'])
    e2e.check([f.name for f in plan.fields] == ['StreamId', 'EventId', '@timestamp'],
             'only StreamId, EventId and @timestamp are mapped explicitly')
    xslt = await translation.save_xslt(ctx, build, f'{index}-XSLT', index_plan=plan)
    pipeline = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=build, name=f'{index} - Discovery',
                               template_uuid=template['uuid'], xslt_uuid=xslt['uuid'], index_name=index,
                               cluster_uuid=cluster['uuid'])
    sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    if sample['verdict'] != 'clean':
        print(json.dumps(sample, indent=1)[:3000])
    e2e.check(sample['verdict'] == 'clean', f"stepped {sample.get('records_stepped')} records clean")
    documents = await indexing._documents(ctx, pipeline['uuid'], [raw], 10)
    first = documents[0]
    e2e.check([d['EventId'][1] for d in documents] == ['1', '2', '3'] and first['StreamId'][1] == str(raw),
             'StreamId is the stream, EventId the record number')
    e2e.check(first['@timestamp'][1] == '2026-10-01T09:00:00Z' and first['stroom.feed'][1] == feed
             and first['user']['roles'] == [('string', 'admin'), ('string', 'ops')],
             'each record as it is, with @timestamp from ts and the feed from stream meta')
    e2e.check(first['message_json']['client']['ip'] == ('string', '10.1.1.1') and 'message_json' not in documents[1]
             and documents[1]['message'] == ('string', 'session closed by user'),
             'a JSON message is unpacked beside its text; a plain one is left as it is')
    e2e.check(not any('session_token' in d for d in documents), 'the dropped field is left out')

    print('\n### the index template: permissive, with the example\'s settings, agreed by the user')
    try:
        await processing_writes.create_processor_filter(ctx, pipeline['uuid'], stream_ids=[raw])
        refused = ''
    except ToolError as e:
        refused = str(e)
    e2e.check(refused.startswith('No Elasticsearch index template has been agreed'), f'refused before: {refused[:80]}')
    final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], plan=plan,
                            events_stream_ids=[raw], example_template=EXAMPLE, component_templates=[COMPONENT])
    body, mappings = final['template'], final['template']['template']['mappings']
    for note in final.get('from_example') or []:
        print(f'    {note}')
    e2e.check(final.get('agreed') and mappings['dynamic'] is True and mappings['date_detection'] is False
             and mappings['dynamic_templates'][0]['strings_as_keywords']['mapping']['type'] == 'keyword',
             'agreed: dynamic mapping, strings as keywords, no date guessing')
    index_settings = body['template']['settings']['index']
    e2e.check(index_settings['number_of_shards'] == 1 and index_settings['mapping']['ignore_malformed'] is True
             and index_settings['mapping']['total_fields']['limit'] == 2000 and body['composed_of'] == ['e2e-discovery-base'],
             "the example's settings and components, with the discovery guardrails")
    e2e.check(mappings['properties'] == {'@timestamp': {'type': 'date'}} and 'proxy' not in json.dumps(mappings),
             "StreamId and EventId left to the component, @timestamp explicit, the example's own fields not copied")

    print('\n### the cluster admin applies it')
    for text in (COMPONENT, final['dev_tools']):
        path, request = _request(text)
        response = await es.put(f'/{path}', json=request)
        e2e.check(response.status_code == 200, f'PUT {path}: {response.status_code} {response.text[:200]}')

    print('\n### indexing the raw stream')
    started = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                              stream_ids=[raw])
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [raw], expect_events=False,
                                                       filter_id=started['filter_id'])
    e2e.check(done.get('gate') == 'pass', f"processed with no Error stream: {done.get('streams')}")
    await es.post(f'/{index}/_refresh')
    count = (await es.get(f'/{index}/_count')).json().get('count')
    e2e.check(count == 3, f'the index holds the 3 records: {count}')
    mapped = (await es.get(f'/{index}/_mapping')).json()[index]['mappings']['properties']

    def kind(path: str) -> str | None:
        node = {'properties': mapped}
        for part in path.split('.'):
            node = (node.get('properties') or {}).get(part)
            if node is None:
                return None
        return node.get('type', 'object')
    expected = {'StreamId': 'long', 'EventId': 'long', '@timestamp': 'date', 'ts': 'keyword', 'user.name': 'keyword',
                'user.roles': 'keyword', 'status': 'long', 'latency_ms': 'float', 'tags': 'keyword',
                'message': 'keyword', 'message_json.client.ip': 'keyword', 'message_json.ok': 'boolean',
                'message_json.bytes': 'long', 'message_json.error.code': 'keyword', 'stroom.feed': 'keyword'}
    actual = {path: kind(path) for path in expected}
    e2e.check(actual == expected, f'Elasticsearch mapped the fields dynamically, strings as keywords: {actual}')
    e2e.check(kind('session_token') is None, 'the dropped field was never indexed')
    hit = (await es.post(f'/{index}/_search', json={'query': {'term': {'message_json.error.code': 'E42'}}})).json()
    e2e.check(hit['hits']['total']['value'] == 1 and hit['hits']['hits'][0]['_source']['user']['name'] == 'carol',
             'a field from inside the JSON message is searchable')

    print('\n### searched through Stroom and in Elasticsearch, each hit traced to its record; documented')
    doc = await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=build, backend='elasticsearch', name=index,
                          time_field='@timestamp', index_name=index, cluster_uuid=cluster['uuid'])
    await paired(ctx, es, build, index, doc['uuid'], [raw], 3,
                 ['StreamId', 'EventId', '@timestamp', 'user.name', 'event'], [
                     ('user.name', 'EQUALS', 'alice', 1, {'term': {'user.name': 'alice'}}),
                     ('user.name', 'EQUALS', 'Alice', 0, {'term': {'user.name': 'Alice'}}),      # keywords: exact case
                     ('user.name', 'EQUALS', '*aro*', 1, {'wildcard': {'user.name': '*aro*'}}),
                     ('user.name', 'IN', 'alice,bob', 2, {'terms': {'user.name': ['alice', 'bob']}}),
                     ('user.name', 'MATCHES_REGEX', 'ca.*', 1, {'regexp': {'user.name': 'ca.*'}}),
                     ('user.roles', 'EQUALS', 'ops', 1, {'term': {'user.roles': 'ops'}}),           # in an array
                     ('tags', 'EQUALS', 'retry', 1, {'term': {'tags': 'retry'}}),
                     ('status', 'GREATER_THAN', '200', 1, {'range': {'status': {'gt': 200}}}),
                     ('status', 'BETWEEN', '100,300', 2, {'range': {'status': {'gte': 100, 'lte': 300}}}),
                     ('latency_ms', 'LESS_THAN', '20', 1, {'range': {'latency_ms': {'lt': 20}}}),
                     ('message', 'EQUALS', '*session*', 1, {'wildcard': {'message': '*session*'}}),
                     ('message_json.client.ip', 'EQUALS', '10.1.1.1', 1, {'term': {'message_json.client.ip': '10.1.1.1'}}),
                     ('message_json.ok', 'EQUALS', 'true', 1, {'term': {'message_json.ok': True}}),
                     ('message_json.bytes', 'GREATER_THAN', '1000000', 1, {'range': {'message_json.bytes': {'gt': 1000000}}}),
                     ('message_json.error.code', 'EQUALS', '*', 1, {'exists': {'field': 'message_json.error.code'}}),
                     ('@timestamp', 'BETWEEN', '2026-10-01T09:00:30.000Z,2026-10-01T09:10:00.000Z', 2,
                      {'range': {'@timestamp': {'gte': '2026-10-01T09:00:30.000Z', 'lte': '2026-10-01T09:10:00.000Z'}}}),
                 ], pipeline_uuid=pipeline['uuid'])
    try:
        await indexing.verify_index(ctx, build, doc['uuid'], 'elasticsearch', [raw], 3, ['StreamId'],
                                    searches=[indexing.SearchCheck(field='user.name', condition='STARTS_WITH', value='al')])
        refused = ''
    except ToolError as e:
        refused = str(e)
    e2e.check("use EQUALS 'al*'" in refused, 'a search Stroom would answer wrongly on Elasticsearch is refused, with what to use')
    written = await builds.write_documentation(ctx, build, pipeline['uuid'],
                                               '## Purpose and data\n\nWeb access logs, indexed as they are for '
                                               'exploration.\n', 'Created', stream_ids=[raw])
    section = written.get('field_mapping') or ''
    print('    ' + '\n    '.join(section.splitlines()[:14]))
    rows = e2e.field_rows(section)
    e2e.check('Elasticsearch, discovery' in section and rows['@timestamp'][1:3] == ['date', '`ts`']
             and rows['message_json.client.ip'][1:] == ['33% of documents', '`10.1.1.1`']
             and f'Elasticsearch index template `{index}`, agreed with the user' in section,
             'documented: the explicit fields, then the fields the sample held, and the agreed template')
    await e2e.documented_to_the_field(stroom, written, ['StreamId', 'EventId', '@timestamp', 'message_json.client.ip'],
                                      {'message_json.client.ip': '10.1.1.1'}, 'the JSON discovery pipeline')
    return index


# An existing feed's streams, oldest first, as a sending system sent them over time. The data drifts: the
# oldest has latency_ms as "n/a" (later ones are numbers), the newest adds a geo object.
FEED_STREAMS = [
    [{'time': '2026-09-01T08:00:00Z', 'app': 'billing', 'level': 'INFO', 'latency_ms': 'n/a', 'msg': 'started'},
     {'time': '2026-09-01T08:00:05Z', 'app': 'billing', 'level': 'WARN', 'latency_ms': 'n/a', 'msg': 'slow disk'}],
    [{'time': '2026-09-15T10:00:00Z', 'app': 'billing', 'level': 'INFO', 'latency_ms': 31, 'msg': 'invoice 1001'},
     {'time': '2026-09-15T10:00:09Z', 'app': 'billing', 'level': 'ERROR', 'latency_ms': 950,
      'msg': json.dumps({'error': 'timeout', 'upstream': 'payments'})}],
    [{'time': '2026-10-01T12:00:00Z', 'app': 'billing', 'level': 'INFO', 'latency_ms': 12, 'msg': 'invoice 1002',
      'geo': {'country': 'NZ', 'city': 'Wellington'}}],
]
LATER = [{'time': '2026-10-04T07:30:00Z', 'app': 'billing', 'level': 'INFO', 'latency_ms': 8, 'msg': 'invoice 1003',
          'geo': {'country': 'AU', 'city': 'Sydney'}, 'retry': True}]


# A sibling discovery index's template already on the cluster: standalone, with no component templates, so it
# maps StreamId, EventId and @timestamp itself.
STANDALONE = {'index_patterns': ['e2e-discovery-legacy-v1*'], 'priority': 150, 'template': {
    'settings': {'index': {'number_of_shards': 1, 'number_of_replicas': 0, 'refresh_interval': '5s'}},
    'mappings': {'dynamic': True, 'properties': {'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'},
                                                 '@timestamp': {'type': 'date'}, 'proxy': {'type': 'keyword'}}}}}


async def existing_feed(ctx, stroom: StroomGateway, es: httpx.AsyncClient, stamp: str) -> None:
    source, build = f'SRC-BILLING-{stamp}', f'e2e-discovery-existing-{stamp}'
    index = f'stroom-discovery-billing-{stamp}-v1'
    print('\n### an existing raw feed, outside the build, already holding data')
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=f'src-{stamp}', name=source)
    for records in FEED_STREAMS:
        await feeds.upload_sample(ctx, source, json.dumps(records))
    await asyncio.sleep(2)

    print('\n### its raw streams; one record read to confirm the time field, no survey')
    found = await streams.find_streams(ctx, feed=source, stream_type='Raw Events')
    ids = sorted(s['id'] for s in found['streams'])
    e2e.check(len(ids) == 3, f'the feed holds three raw streams: {ids}')
    record = (await streams.read_stream(ctx, ids[-1], 0, 1))['records'][0]
    e2e.check('"time": "2026-10-01T12:00:00Z"' in record, 'the newest record shows the time field the user named')

    print('\n### the discovery pipeline, stepped on the two newest streams')
    template = await fixture_template(stroom)
    cluster = await live_cluster(stroom)
    draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', index,
                                               discovery=Discovery(timestamp_field='time'))
    plan = FieldPlan.model_validate(draft['plan'])
    xslt = await translation.save_xslt(ctx, build, f'{index}-XSLT', index_plan=plan)
    pipeline = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=build, name=f'{index} - Discovery',
                               template_uuid=template['uuid'], xslt_uuid=xslt['uuid'], index_name=index,
                               cluster_uuid=cluster['uuid'])
    newest = ids[-2:]
    sample = await stepping.step_sample(ctx, pipeline['uuid'], newest)
    e2e.check(sample['verdict'] == 'clean', f"stepped {sample.get('records_stepped')} records of {newest} clean, in place")

    print("\n### the template waits for the user's example, then is built from it and agreed")
    unasked = await indexing.propose_index_template(ctx, pipeline['uuid'], plan, newest)
    e2e.check('status' not in unasked and unasked['hint'].startswith('No example was given: ask the user')
             and 'sibling discovery index' in unasked['hint'], 'without an example, the user is asked for one first')
    # What the user pastes: the sibling discovery index's template, as GET returns it. It has no components.
    response = await es.put('/_index_template/e2e-discovery-legacy-v1', json=STANDALONE)
    e2e.check(response.status_code == 200, 'a standalone sibling template on the cluster (set up as the admin had)')
    example = json.dumps((await es.get('/_index_template/e2e-discovery-legacy-v1')).json())
    final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], plan=plan,
                            events_stream_ids=newest, example_template=example)
    body = final['template']
    for note in final.get('from_example') or []:
        print(f'    {note}')
    e2e.check(final.get('agreed') and body['index_patterns'] == [f'{index}*'] and body['priority'] == 150
             and 'composed_of' not in body and body['template']['settings']['index']['refresh_interval'] == '5s',
             "agreed with no component templates: the new index's pattern, the sibling's priority and settings")
    e2e.check(body['template']['mappings']['dynamic'] is True and body['template']['mappings']['properties'] ==
             {'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}, '@timestamp': {'type': 'date'}}
             and 'proxy' not in json.dumps(body) and not any('composed_of' in n for n in final.get('from_example') or []),
             'StreamId, EventId and @timestamp mapped in the template itself; nothing of the sibling index copied')
    path, request = _request(final['dev_tools'])
    response = await es.put(f'/{path}', json=request)
    e2e.check(response.status_code == 200, f'PUT {path}: {response.status_code}')

    print('\n### the existing streams by id, and what is sent from now on by feed')
    backfill = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                               stream_ids=ids)
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], ids, expect_events=False,
                                                       filter_id=backfill['filter_id'])
    e2e.check(done.get('gate') == 'pass', f"the existing streams indexed with no Error stream: {done.get('streams')}")
    since = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec='seconds').replace('+00:00', 'Z')
    ongoing = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                              feed=source, stream_type='Raw Events', created_after=since)
    later = (await feeds.upload_sample(ctx, source, json.dumps(LATER)))['stream_id']
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [later], expect_events=False,
                                                       filter_id=ongoing['filter_id'])
    e2e.check(done.get('gate') == 'pass', f'stream {later}, sent afterwards, indexed by the feed filter')
    # Local only: stop the feed filter again, so nothing else sent to this feed is indexed in the background.
    await e2e.agreed(processing_writes.set_processor_filter_enabled, ctx=ctx, filter_id=ongoing['filter_id'],
                    enabled=False)

    print('\n### every record, the drift absorbed')
    await es.post(f'/{index}/_refresh')
    total = sum(len(r) for r in FEED_STREAMS) + len(LATER)
    count = (await es.get(f'/{index}/_count')).json().get('count')
    e2e.check(count == total, f'the index holds all {total} records of the four streams: {count}')
    per_stream = (await es.post(f'/{index}/_search', json={'size': 0, 'aggs': {'s': {'terms': {'field': 'StreamId'}}}})).json()
    by_stream = {int(b['key']): b['doc_count'] for b in per_stream['aggregations']['s']['buckets']}
    e2e.check(by_stream == {**{i: len(r) for i, r in zip(ids, FEED_STREAMS)}, later: len(LATER)},
             f'each stream complete: {by_stream}')
    props = (await es.get(f'/{index}/_mapping')).json()[index]['mappings']['properties']
    e2e.check(props['geo']['properties']['country']['type'] == 'keyword' and props['retry']['type'] == 'boolean'
             and 'msg_json' in props, 'fields that appeared later were mapped as they arrived, the JSON message unpacked')
    latency = props['latency_ms']['type']
    if latency == 'long':
        ignored = (await es.post(f'/{index}/_count', json={'query': {'term': {'_ignored': 'latency_ms'}}})).json()['count']
        e2e.check(ignored == 2, f'latency_ms mapped as a number; the two "n/a" values ignored, their records kept: {ignored}')
    else:
        e2e.check(latency == 'keyword', f'latency_ms mapped from "n/a" first, as a keyword; the numbers kept as text')

    print('\n### searched through Stroom and in Elasticsearch across the four streams; documented')
    doc = await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=build, backend='elasticsearch', name=index,
                          time_field='@timestamp', index_name=index, cluster_uuid=cluster['uuid'])
    await paired(ctx, es, build, index, doc['uuid'], ids + [later], total,
                 ['StreamId', 'EventId', '@timestamp', 'app', 'level'], [
                     ('level', 'EQUALS', 'ERROR', 1, {'term': {'level': 'ERROR'}}),
                     ('level', 'IN', 'WARN,ERROR', 2, {'terms': {'level': ['WARN', 'ERROR']}}),
                     ('app', 'EQUALS', 'billing', total, {'term': {'app': 'billing'}}),
                     ('geo.country', 'EQUALS', 'NZ', 1, {'term': {'geo.country': 'NZ'}}),          # only in a later stream
                     ('retry', 'EQUALS', 'true', 1, {'term': {'retry': True}}),                 # only in the last
                     ('msg_json.upstream', 'EQUALS', 'payments', 1, {'term': {'msg_json.upstream': 'payments'}}),
                     ('@timestamp', 'GREATER_THAN', '2026-10-01T00:00:00.000Z', 2,
                      {'range': {'@timestamp': {'gt': '2026-10-01T00:00:00.000Z'}}}),
                 ], pipeline_uuid=pipeline['uuid'])
    written = await builds.write_documentation(ctx, build, pipeline['uuid'],
                                               f'## Purpose and data\n\nThe {source} feed, indexed as it is for '
                                               f'exploration.\n', 'Created', stream_ids=newest)
    section = written.get('field_mapping') or ''
    rows = e2e.field_rows(section)
    e2e.check('geo.country' in rows and rows['@timestamp'][1:3] == ['date', '`time`'],
             'documented from the streams stepped')


SHAPES = [
    {'ts': '2026-10-02T08:00:00Z', 'kind': 'order', 'user': {'name': 'alice'},
     'items': [{'sku': 'A1', 'qty': 2}, {'sku': 'B2', 'qty': 1}], 'scores': [3, 5, 8], 'matrix': [[1, 2], [3, 4]],
     'nothing': None, 'empty_list': [], 'empty_obj': {}, 'deep': {'l1': {'l2': {'l3': {'l4': 'bottom'}}}, '_inner': 'in'},
     'key with space': 'spaced', 'embedded_list': '[1, 2, 3]', '_id': 'src-1', '_other': 'kept', '@timestamp': 'theirs',
     'long_text': 'x' * 1100,
     '': 'blank',
     'a..b': 'two dots'},
    # user was an object above: Elasticsearch must reject this one, and that must not go unnoticed.
    {'ts': '2026-10-02T08:01:00Z', 'kind': 'order', 'user': 'bob'},
    {'ts': '2026-10-02T08:02:00Z', 'kind': 'misc', 'mixed': [1, 'two', 3], 'user': {'name': 'carol'}},
]


async def shapes(ctx, stroom: StroomGateway, es: httpx.AsyncClient, stamp: str) -> None:
    build, feed = f'e2e-discovery-shapes-{stamp}', f'E2E-SHAPES-{stamp}'
    index = f'stroom-discovery-shapes-{stamp}-v1'
    print('\n### awkward shapes, indexed as they are')
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, json.dumps(SHAPES)))['stream_id']
    template = await fixture_template(stroom)
    cluster = await live_cluster(stroom)
    plan = FieldPlan.model_validate((await indexing.draft_index_mapping(
        ctx, 'elasticsearch', index, discovery=Discovery(timestamp_field='ts')))['plan'])
    xslt = await translation.save_xslt(ctx, build, f'{index}-XSLT', index_plan=plan)
    pipeline = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=build, name=f'{index} - Discovery',
                               template_uuid=template['uuid'], xslt_uuid=xslt['uuid'], index_name=index,
                               cluster_uuid=cluster['uuid'])
    sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    e2e.check(sample['verdict'] == 'clean', f"stepped {sample.get('records_stepped')} records clean")
    first = (await indexing._documents(ctx, pipeline['uuid'], [raw], 5))[0]
    e2e.check(first.get('id_original') == ('string', 'src-1') and first.get('other_original') == ('string', 'kept')
             and first.get('@timestamp_original') == ('string', 'theirs')
             and first.get('@timestamp') == ('string', '2026-10-02T08:00:00Z') and first.get('empty_key') == ('string', 'blank')
             and first.get('a.b') == ('string', 'two dots') and '_id' not in first and '' not in first,
             'keys Elasticsearch refuses, or that repeat ours, renamed; keys it cannot take repaired')
    final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], plan=plan,
                            events_stream_ids=[raw], example_template='PUT _index_template/e2e-discovery-legacy-v1\n'
                            + json.dumps(STANDALONE))
    path, request = _request(final['dev_tools'])
    e2e.check((await es.put(f'/{path}', json=request)).status_code == 200, f'PUT {path}')
    started = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                              stream_ids=[raw])
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [raw], expect_events=False,
                                                       filter_id=started['filter_id'])
    e2e.check(done.get('gate') == 'fail' and done['streams'][0]['errors'],
             f"the rejected record is reported (an Error stream), not lost silently: {done.get('problems')}")
    e2e.check((done.get('next') or {}).get('step') != 'translation',
             f"a discovery build is not sent to write a translation: next is {(done.get('next') or {}).get('step')}")
    errors = await streams.summarise_streams(ctx, [raw], kind='errors')
    groups = [g for s in errors.get('streams', {}).values() for g in (s.get('groups') or [])] if isinstance(
        errors.get('streams'), dict) else [g for s in errors.get('streams') or [] for g in (s.get('groups') or [])]
    groups = groups or errors.get('groups') or [g for v in errors.values() if isinstance(v, dict)
                                                for g in v.get('groups') or []]
    why = json.dumps(groups)
    e2e.check('Elasticsearch rejected document 2 of 3' in why and 'A field is an object in some records' in why,
             f"triage names the document and why: {why[:400]}")
    await es.post(f'/{index}/_refresh')
    count = (await es.get(f'/{index}/_count')).json().get('count')
    e2e.check(count == 2, f'the other two records are indexed: {count}')
    mapped = (await es.get(f'/{index}/_mapping')).json()[index]['mappings']['properties']

    def kind(path: str) -> str | None:
        node = {'properties': mapped}
        for part in path.split('.'):
            node = (node.get('properties') or {}).get(part)
            if node is None:
                return None
        return node.get('type', 'object')
    expected = {'items.sku': 'keyword', 'items.qty': 'long', 'scores': 'long', 'matrix': 'long',
                'deep.l1.l2.l3.l4': 'keyword', 'key with space': 'keyword', 'embedded_list': 'keyword',
                'id_original': 'keyword', 'other_original': 'keyword', 'deep.inner_original': 'keyword',
                '@timestamp_original': 'keyword', 'empty_key': 'keyword', 'a.b': 'keyword',
                'mixed': 'long', 'user.name': 'keyword', '@timestamp': 'date'}
    actual = {path: kind(path) for path in expected}
    e2e.check(actual == expected, f'each shape mapped as expected: {actual}')
    e2e.check(kind('nothing') is None and kind('empty_list') is None,
             'null and an empty array map nothing, and their records still index')
    ignored = (await es.post(f'/{index}/_count', json={'query': {'term': {'_ignored': 'mixed'}}})).json()['count']
    e2e.check(ignored == 1, f'the string in a number array was ignored, its record kept: {ignored}')
    written = await builds.write_documentation(ctx, build, pipeline['uuid'],
                                               '## Purpose and data\n\nShapes, indexed as they are.\n', 'Created',
                                               stream_ids=[raw])
    section = written.get('field_mapping') or ''
    e2e.check('Arrays of objects are indexed flattened' in section and '`items`' in section
             and 'kept as `<field>_original`' in section, 'documented: arrays of objects flattened, renamed keys')

    print('\n### the shapes searched through Stroom and in Elasticsearch')
    doc = await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=build, backend='elasticsearch', name=index,
                          time_field='@timestamp', index_name=index, cluster_uuid=cluster['uuid'])
    await paired(ctx, es, build, index, doc['uuid'], [raw], 2, ['StreamId', 'EventId', '@timestamp', 'kind'], [
        ('items.sku', 'EQUALS', 'B2', 1, {'term': {'items.sku': 'B2'}}),                  # inside an array of objects
        ('items.qty', 'GREATER_THAN', '1', 1, {'range': {'items.qty': {'gt': 1}}}),
        ('scores', 'EQUALS', '5', 1, {'term': {'scores': 5}}),                             # an array of numbers
        ('matrix', 'EQUALS', '4', 1, {'term': {'matrix': 4}}),                             # an array of arrays
        ('deep.l1.l2.l3.l4', 'EQUALS', 'bottom', 1, {'term': {'deep.l1.l2.l3.l4': 'bottom'}}),
        ('id_original', 'EQUALS', 'src-1', 1, {'term': {'id_original': 'src-1'}}),         # was _id
        ('deep.inner_original', 'EQUALS', 'in', 1, {'term': {'deep.inner_original': 'in'}}),
        ('a.b', 'EQUALS', 'two dots', 1, {'term': {'a.b': 'two dots'}}),                   # was a..b
        ('embedded_list', 'EQUALS', '[1, 2, 3]', 1, {'term': {'embedded_list': '[1, 2, 3]'}}),
        ('mixed', 'EQUALS', '3', 1, {'term': {'mixed': 3}}),                               # its odd value ignored
        ('long_text', 'EQUALS', '*', 0, {'exists': {'field': 'long_text'}}),               # over ignore_above
    ], pipeline_uuid=pipeline['uuid'])
    kept = (await es.post(f'/{index}/_search', json={'query': {'term': {'kind': 'order'}}})).json()['hits']['hits']
    e2e.check(len(kept[0]['_source'].get('long_text', '')) == 1100,
             'the over-long string is in the stored document, though not searchable')
    crossed = (await es.post(f'/{index}/_count', json={'query': {'bool': {'must': [
        {'term': {'items.sku': 'A1'}}, {'term': {'items.qty': 1}}]}}})).json()['count']
    e2e.check(crossed == 1, 'flattened: sku A1 with qty 1 matches, though A1 had qty 2 (as the documentation warns)')


if __name__ == '__main__':
    asyncio.run(main())
