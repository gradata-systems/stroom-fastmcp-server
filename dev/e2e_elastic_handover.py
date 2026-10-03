"""Elasticsearch template hand-over against the local Stroom stack (see dev/stroom), without Elasticsearch,
or with it (--live).

    uv run python dev/e2e_elastic_handover.py
    uv run python dev/e2e_elastic_handover.py --live    # needs: docker compose --profile elastic up -d

The local stack has no Elasticsearch indexing template, so this adds a fixture one (the local 'Indexing'
template with an ElasticIndexingFilter in place of the Lucene one) and an Elastic Cluster doc pointing
nowhere. The documents come from stepping the indexing XSLT, so no Elasticsearch server is needed.

1. Stage 1 on the CSV sample (as in the Phase 2 test).
2. An ES indexing pipeline from the fixture template; the agent's template proposal, self-checked against
   the documents the pipeline writes.
3. A user's changed template (a renamed field, a stricter type, dynamic strict) is checked: not compatible,
   with the pipeline changes it needs.
4. Indexing is refused until an index template is agreed. The user's example (a sibling source's index
   template and its component templates): the template for the new index follows its conventions, and the user
   confirms it. Then they correct it (a priority), and the correction is confirmed and kept instead.
5. Once the user says the admin has committed it, the approval starts the indexing filter (here disabled again
   afterwards, as there is no Elasticsearch).

--live then runs the whole workflow against Elasticsearch 9 (the local stack's elastic profile): the user's
example is a sibling index's template in Stroom-style PascalCase with explicit objects (User.Id, TypeId) and a
component template, holding fields this source lacks and lacking some it has. The plan is named from it, the
built template agreed, then applied as the cluster admin would (component templates first); Elasticsearch's own
composition (_simulate_index) is compared with ours; indexing starts; the index holds every event, and its
mapping is the template's, with nothing added dynamically; and Stroom searches it.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_phase2 as p2  # noqa: E402
from config import Settings  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from utils.mappingstore import read_agreed_template  # noqa: E402
from tools import builds, indexing, processing_writes, stepping, templates, translation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import FieldPlan  # noqa: E402
from utils.templatecheck import compose  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

FIXTURE_TEMPLATE = 'E2E Events to Elasticsearch'
FIXTURE_CLUSTER = 'E2E_LOCAL_ES'
LIVE_CLUSTER = 'E2E_LIVE_ES'
ES = 'http://127.0.0.1:19200'          # the elastic profile's node, from here; Stroom reaches it as elasticsearch:9200

# A sibling source's index template, as the user would paste it from Dev Tools: Stroom-style names with explicit
# objects, a field this source lacks (Keyfob.Serial, User.Name) and none for some it has (Description).
LIVE_EXAMPLE = """PUT _index_template/stroom-keyfob-v1
{"index_patterns": ["stroom-keyfob-v1*"], "priority": 300, "composed_of": ["e2e-stroom-base"],
 "template": {"settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
  "aliases": {"keyfob": {}},
  "mappings": {"dynamic": "strict", "properties": {
   "TypeId": {"type": "keyword", "ignore_above": 512},
   "User": {"type": "object", "properties": {"Id": {"type": "keyword", "ignore_above": 512},
                                             "Name": {"type": "keyword", "ignore_above": 512}}},
   "Device": {"type": "object", "properties": {"HostName": {"type": "keyword", "ignore_above": 512},
                                               "IPAddress": {"type": "ip"}}},
   "Keyfob": {"type": "object", "properties": {"Serial": {"type": "keyword", "ignore_above": 512}}}}}}}"""
LIVE_COMPONENT = """PUT _component_template/e2e-stroom-base
{"template": {"mappings": {"properties": {"StreamId": {"type": "long"}, "EventId": {"type": "long"},
                                          "@timestamp": {"type": "date"}}}}}"""


async def fixtures(stroom: StroomGateway) -> tuple[dict, dict]:
    """The fixture ES indexing template (beside 'Indexing') and a cluster doc, created once."""
    found = (await stroom.find_documents('Indexing', ['Pipeline'], 500))['values']
    lucene = next(v for v in found if v['docRef']['name'] == 'Indexing')
    existing = {v['docRef']['name']: v['docRef'] for v in
                (await stroom.find_documents('E2E*', ['Pipeline', 'ElasticCluster'], 20))['values']}
    parent = await stroom.post('/explorer/v2/find', {
        'filter': {'includedTypes': ['Folder'], 'nameFilter': 'Template Pipelines', 'requiredPermissions': ['VIEW']},
        'pageRequest': {'offset': 0, 'length': 5}})
    destination = parent['values'][0]['docRef'] if parent.get('values') else None
    if FIXTURE_TEMPLATE not in existing:
        node = await stroom.post('/explorer/v2/create', {'docType': 'Pipeline', 'docName': FIXTURE_TEMPLATE,
                                                         'destinationFolder': destination, 'permissionInheritance': 'DESTINATION'})
        doc = await stroom.get(f"/pipeline/v1/{node['docRef']['uuid'] if 'docRef' in node else node['uuid']}")
        source = (await stroom.get(f"/pipeline/v1/{lucene['docRef']['uuid']}"))['pipelineData']
        data = json.loads(json.dumps(source).replace('"indexingFilter"', '"elasticIndexingFilter"')
                          .replace('"IndexingFilter"', '"ElasticIndexingFilter"'))
        doc['pipelineData'] = data
        doc['description'] = 'Fixture for dev/e2e_elastic_handover.py'
        await stroom.request('PUT', f"/pipeline/v1/{doc['uuid']}", doc)
        existing[FIXTURE_TEMPLATE] = {'type': 'Pipeline', 'uuid': doc['uuid'], 'name': FIXTURE_TEMPLATE}
    if FIXTURE_CLUSTER not in existing:
        node = await stroom.post('/explorer/v2/create', {'docType': 'ElasticCluster', 'docName': FIXTURE_CLUSTER,
                                                         'destinationFolder': destination, 'permissionInheritance': 'DESTINATION'})
        ref = node.get('docRef', node)
        existing[FIXTURE_CLUSTER] = {'type': 'ElasticCluster', 'uuid': ref['uuid'], 'name': FIXTURE_CLUSTER}
    return existing[FIXTURE_TEMPLATE], existing[FIXTURE_CLUSTER]


def change_template(body: dict) -> dict:
    """What a user might send back: user.name renamed to user.id, host.name made an ip, dynamic strict."""
    changed = json.loads(json.dumps(body))
    props = changed['template']['mappings']['properties']
    props['user']['properties']['id'] = props['user']['properties'].pop('name')
    props['host']['properties']['name'] = {'type': 'ip'}
    changed['template']['mappings']['dynamic'] = 'strict'
    return changed


async def main():
    local = p2.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=p2.VERSION)
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = time.strftime('%H%M%S')
    try:
        template_ref, cluster = await fixtures(stroom)
        csv = await p2.onboard(ctx, 'csv', p2.CASES['csv'], stamp)
        events = (await processing_writes.wait_for_processing(ctx, csv['pipeline']['uuid'], [csv['raw']]))['streams'][0]['events']

        print('\n### Elasticsearch indexing pipeline')
        candidates = (await templates.find_pipeline_templates(ctx, 'indexing'))['candidates']
        es_template = next(c for c in candidates if c['name'] == FIXTURE_TEMPLATE)
        p2.check(es_template['backend'] == 'elasticsearch', 'fixture template reads as an Elasticsearch template')
        index = f'e2e-acme-{stamp}-v1'
        draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', index, 'ecs', events)
        plan = FieldPlan.model_validate(draft['plan'])
        # As the agent does: the indexing XSLT saved from its plan, which is kept with it for the documentation.
        xslt = await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=plan)
        pipeline = await p2.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=csv['build'], name=f'{index} - Indexing',
                                   template_uuid=es_template['uuid'],
                                   xslt_uuid=xslt['uuid'], index_name=index,
                                   cluster_uuid=cluster['uuid'])
        sample = await stepping.step_sample(ctx, pipeline['uuid'], events)
        print(f"    stepping: {sample['verdict']}; groups: {[(g['class'], g['element'], g['count']) for g in sample['groups']]}")

        print('\n### index field documentation, with the values from the sample')
        doc = await builds.write_documentation(ctx, csv['build'], pipeline['uuid'],
                                               '## Purpose and data\n\nCSV events indexed into Elasticsearch.\n',
                                               'Created', stream_ids=events)
        section = doc.get('field_mapping') or ''
        print('    ' + '\n    '.join(section.splitlines()[:8]))
        p2.check('| Index field | Type | From (event-logging path) | In sample | Sample values |' in section,
                 'the index fields, each with its event-logging path, how often it is populated and its sampled values')
        p2.check('Sample values are what the 3 documents written from the sample got' in section
                 and '`alice`' in section and '`bob`' in section, 'sampled values from the documents the pipeline writes')

        print('\n### the proposed template')
        proposal = await indexing.propose_index_template(ctx, pipeline['uuid'], plan, events)
        print('    ' + proposal['dev_tools'].splitlines()[0])
        p2.check(proposal['template']['index_patterns'] == [f'{index}*'], "template covers the pipeline's index")
        p2.check(proposal['self_check']['compatible'] and proposal['self_check']['documents_checked'] == 3,
                 f"proposal fits the {proposal['self_check']['documents_checked']} documents the pipeline writes: "
                 f"{proposal['self_check']['blocking']}")

        print("\n### the user's changed template")
        changed = change_template(proposal['template'])
        check = await indexing.check_index_template(ctx, pipeline['uuid'],
                                                    f"PUT _index_template/{proposal['template_name']}\n{json.dumps(changed)}", events)
        for change in check['pipeline_changes']:
            print(f"    change: {change['field']}: {change['change']}")
        fields = {c['field'] for c in check['pipeline_changes']}
        p2.check(not check['compatible'] and {'user.name', 'host.name'} <= fields,
                 f"not compatible, with the rename and the type change flagged: {check['blocking']}")
        same = await indexing.check_index_template(ctx, pipeline['uuid'], json.dumps(proposal['template']), events)
        p2.check(same['compatible'], 'the unchanged proposal checks as compatible')

        print('\n### no template agreed yet: indexing is refused')
        try:
            await processing_writes.create_processor_filter(ctx, pipeline['uuid'], stream_ids=events,
                                                            source_pipeline_uuid=csv['pipeline']['uuid'])
            refused = ''
        except ToolError as e:
            refused = str(e)
        p2.check(refused.startswith('No Elasticsearch index template has been agreed'), f"refused: {refused[:90]}")

        print("\n### the template from the user's example, confirmed by the user")
        example = ('PUT _index_template/ecs-sibling-v1\n' + json.dumps({
            'index_patterns': ['ecs-sibling-v1*'], 'priority': 300, 'composed_of': ['ecs-base'],
            'template': {'settings': {'index': {'number_of_shards': 2}}, 'aliases': {'sibling': {}},
                         'mappings': {'dynamic': 'strict', 'properties': {'user': {'properties': {
                             'name': {'type': 'keyword', 'ignore_above': 256}}}}}}}))
        base = 'PUT _component_template/ecs-base\n' + json.dumps({'template': {'mappings': {'properties': {
            '@timestamp': {'type': 'date'}}}}})
        asked = await indexing.propose_index_template(ctx, pipeline['uuid'], plan, events, example_template=example,
                                                      component_templates=[base])
        p2.check(asked.get('status') == 'needs_confirmation' and asked['details']['index template'].startswith(
                 f'PUT _index_template/{index}\n'), f"the user is asked to confirm it as shown: {asked.get('summary')}")
        final = await p2.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], plan=plan,
                                events_stream_ids=events, example_template=example, component_templates=[base])
        body = final['template']
        print('    ' + '; '.join(final['from_example']))
        p2.check(body['index_patterns'] == [f'{index}*'] and body['composed_of'] == ['ecs-base']
                 and body['template']['settings']['index']['number_of_shards'] == 2 and 'aliases' not in body['template'],
                 "the new index's pattern, with the example's components and settings, without its aliases")
        p2.check(body['template']['mappings']['dynamic'] == 'strict' and '@timestamp' not in body['template']['mappings']['properties'],
                 "the example's mapping parameters, and fields its components map left to them")
        p2.check(final['self_check']['compatible'], f"the final template fits the documents: {final['self_check']['blocking']}")
        kept = read_agreed_template((await stroom.get_doc('Pipeline', pipeline['uuid'])).get('description'))
        p2.check(final.get('agreed') and kept and kept['dev_tools'] == final['dev_tools'], 'agreed, and kept with the pipeline')

        print("\n### the user's correction, confirmed instead")
        corrected = f"PUT _index_template/{index}\n{json.dumps({**body, 'priority': 400})}"
        fixed = await p2.agreed(indexing.check_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], template=corrected,
                                events_stream_ids=events, component_templates=[base])
        kept = read_agreed_template((await stroom.get_doc('Pipeline', pipeline['uuid'])).get('description'))
        p2.check(fixed.get('agreed') and '"priority": 400' in kept['dev_tools'], 'the correction is the agreed template now')

        print('\n### the admin has committed it: indexing starts')
        first = await processing_writes.create_processor_filter(ctx, pipeline['uuid'], stream_ids=events,
                                                                source_pipeline_uuid=csv['pipeline']['uuid'])
        p2.check(first.get('status') == 'needs_approval' and first['summary'].startswith(
                 f"The agreed index template '{index}' for Elasticsearch index '{index}' is committed to cluster "
                 f"{cluster['name']}: start indexing"), f"the approval asks whether it is committed: {first.get('summary')}")
        started = await processing_writes.create_processor_filter(ctx, pipeline['uuid'], stream_ids=events,
                                                                  source_pipeline_uuid=csv['pipeline']['uuid'],
                                                                  approval_id=first['approval_id'])
        stored = await stroom.get(f"/processorFilter/v1/{started['filter_id']}")
        p2.check(stored.get('enabled') is True, f"filter {started['filter_id']} created enabled")
        # No Elasticsearch here: stop it again, so it doesn't fail in the background.
        await p2.agreed(processing_writes.set_processor_filter_enabled, ctx=ctx, filter_id=started['filter_id'], enabled=False)
        if '--live' in sys.argv:
            await live(ctx, stroom, csv, events, es_template, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


def _request(text: str) -> tuple[str, dict]:
    """A Dev Tools request: ('_index_template/name', body)."""
    line, body = text.split('\n', 1)
    return line.split(' ', 1)[1].strip(), json.loads(body)


def _properties(node: dict) -> dict:
    """A mapping's properties with object nodes reduced to their properties, for comparing what Elasticsearch
    holds (it omits "type": "object" once applied) with what was built."""
    out = {}
    for name, spec in (node or {}).items():
        if 'properties' in spec:
            out[name] = {k: v for k, v in spec.items() if k not in ('type', 'properties')}
            out[name]['properties'] = _properties(spec['properties'])
        else:
            out[name] = spec
    return out


async def live_cluster(stroom: StroomGateway) -> dict:
    found = (await stroom.find_documents(LIVE_CLUSTER, ['ElasticCluster'], 5)).get('values') or []
    if found:
        ref = found[0]['docRef']
    else:
        parent = await stroom.post('/explorer/v2/find', {
            'filter': {'includedTypes': ['Folder'], 'nameFilter': 'Template Pipelines', 'requiredPermissions': ['VIEW']},
            'pageRequest': {'offset': 0, 'length': 5}})
        node = await stroom.post('/explorer/v2/create', {
            'docType': 'ElasticCluster', 'docName': LIVE_CLUSTER, 'permissionInheritance': 'DESTINATION',
            'destinationFolder': parent['values'][0]['docRef'] if parent.get('values') else None})
        ref = node.get('docRef', node)
    doc = await stroom.get_doc('ElasticCluster', ref['uuid'])
    doc['connection'] = {**(doc.get('connection') or {}), 'connectionUrls': ['http://elasticsearch:9200'],
                         'useAuthentication': False}
    await stroom.request('PUT', f"/elasticCluster/v1/{ref['uuid']}", doc)
    return {'type': 'ElasticCluster', 'uuid': ref['uuid'], 'name': LIVE_CLUSTER}


async def live(ctx, stroom: StroomGateway, csv: dict, events: list[int], es_template: dict, stamp: str) -> None:
    import httpx
    async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
        version = (await es.get('/')).json()['version']['number']
        print(f'\n### live: Elasticsearch {version}')
        p2.check(version.startswith('9.'), f'Elasticsearch 9 at {ES}')
        cluster = await live_cluster(stroom)
        index = f'stroom-door-{stamp}-v1'

        print("\n### the plan, named from the user's example")
        draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', index, 'ecs', events,
                                                   example_template=LIVE_EXAMPLE, component_templates=[LIVE_COMPONENT])
        plan = FieldPlan.model_validate(draft['plan'])
        names = {f.source: f.name for f in plan.fields}
        for note in draft['from_example']:
            print(f'    {note}')
        p2.check(names.get('EventSource/User/Id') == 'User.Id' and names.get('EventDetail/TypeId') == 'TypeId'
                 and names.get('EventSource/Device/HostName') == 'Device.HostName',
                 f"the example's names: User.Id for the user, TypeId for the event type: {sorted(names.values())}")
        p2.check(all(n in ('StreamId', 'EventId', '@timestamp') or n[:1].isupper() for n in names.values()),
                 "every other field named in the example's PascalCase")
        xslt = await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=plan)
        pipeline = await p2.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=csv['build'],
                                   name=f'{index} - Indexing', template_uuid=es_template['uuid'],
                                   xslt_uuid=xslt['uuid'], index_name=index, cluster_uuid=cluster['uuid'])
        sample = await stepping.step_sample(ctx, pipeline['uuid'], events)
        p2.check(sample['verdict'] == 'clean', f"stepped clean: {sample['verdict']}")

        print('\n### the template, built and agreed')
        final = await p2.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], plan=plan,
                                events_stream_ids=events, example_template=LIVE_EXAMPLE,
                                component_templates=[LIVE_COMPONENT])
        props = final['template']['template']['mappings']['properties']
        print('    ' + json.dumps(props))
        p2.check(final.get('agreed') and props['User'].get('type') == 'object'
                 and props['User']['properties']['Id'] == {'type': 'keyword', 'ignore_above': 512}
                 and props['TypeId'] == {'type': 'keyword', 'ignore_above': 512},
                 "agreed: the example's objects and field types for User.Id and TypeId")
        p2.check('Keyfob' not in props and 'StreamId' not in props, "the example's own fields and the component's not repeated")

        print('\n### the cluster admin applies it')
        for text in (LIVE_COMPONENT, final['dev_tools']):
            path, body = _request(text)
            response = await es.put(f'/{path}', json=body)
            p2.check(response.status_code == 200, f'PUT {path}: {response.status_code} {response.text[:200]}')
        simulated = (await es.post(f'/_index_template/_simulate_index/{index}')).json()['template']['mappings']
        ours, _ = compose(final['template'], {'e2e-stroom-base': _request(LIVE_COMPONENT)[1]})
        p2.check(_properties(simulated['properties']) == _properties(ours['template']['mappings']['properties'])
                 and simulated.get('dynamic') == 'strict',
                 "Elasticsearch composes the templates as check_index_template does")

        print('\n### indexing')
        started = await p2.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                                  stream_ids=events, source_pipeline_uuid=csv['pipeline']['uuid'])
        done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], events, expect_events=False,
                                                           filter_id=started['filter_id'])
        print(f"    {done.get('status') or done.get('state')}: {json.dumps(done)[:300]}")
        await es.post(f'/{index}/_refresh')
        count = (await es.get(f'/{index}/_count')).json().get('count')
        p2.check(count == 3, f'the index holds the 3 events: {count}')
        actual = (await es.get(f'/{index}/_mapping')).json()[index]['mappings']
        p2.check(_properties(actual['properties']) == _properties(simulated['properties']),
                 "the index's mapping is the template's: nothing added dynamically")
        hit = (await es.post(f'/{index}/_search', json={'query': {'term': {'User.Id': 'alice'}}})).json()
        p2.check(hit['hits']['total']['value'] == 1 and hit['hits']['hits'][0]['_source']['TypeId'] == 'Logon',
                 'User.Id and TypeId searchable as indexed')

        print('\n### verified through Stroom')
        doc = await p2.agreed(indexing.create_index_doc, ctx=ctx, build=csv['build'], backend='elasticsearch',
                              name=index, time_field=plan.time_field, index_name=index, cluster_uuid=cluster['uuid'])
        verified = await indexing.verify_index(ctx, csv['build'], doc['uuid'], 'elasticsearch', events, 3,
                                               ['StreamId', 'EventId', '@timestamp', 'User.Id', 'TypeId'],
                                               exact=[{'field': 'User.Id', 'value': 'bob'}])
        p2.check(verified.get('passed', verified.get('ok')) is True, f"Stroom's searches: {json.dumps(verified)[:400]}")

        print('\n### documented')
        written = await builds.write_documentation(ctx, csv['build'], pipeline['uuid'],
                                                   '## Purpose and data\n\nCSV logons indexed into Elasticsearch.\n',
                                                   'Created', stream_ids=events)
        section = written.get('field_mapping') or ''
        p2.check(f"Elasticsearch index template `{index}`, agreed with the user" in section
                 and '| `User.Id` | keyword | `EventSource/User/Id` |' in section and '`alice`' in section,
                 'the field mapping names the agreed template, with each field and its sampled values')


if __name__ == '__main__':
    asyncio.run(main())
