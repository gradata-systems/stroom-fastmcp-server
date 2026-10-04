"""Updating an indexing pipeline as a new version, beside the one in production, on Elasticsearch 9.

    cd dev/stroom && docker compose --profile elastic up -d
    uv run python dev/e2e_index_versions.py

v1 is production: a CSV source's events pipeline and its v1 Elasticsearch indexing pipeline, built, their index
template agreed and committed, indexed, documented and promoted; the new-data filters promotion creates are
switched on, as the user would. Then the user asks for the logon outcome to be indexed as well, and the agent,
as update_indexing_pipeline asks:

1. locates v1 (its XSLT, index name, agreed template) and works out the next version, v2;
2. copies the pipeline as a new version (not a working copy), its index name bumped, and drafts the v2 plan with
   the added field, from v1's agreed template;
3. steps v2 and compares it with v1 on the same Events: only the added field differs;
4. has the v2 template agreed (v1's, with the new index's pattern and the added field) and committed;
5. starts v2 on new Events only, from a create time (backfilling older streams is the user's);
6. new data arrives through the production events pipeline: v1 keeps indexing it as before, v2 indexes it with
   the added field, and holds nothing older;
7. searches v2 (and v1) through Stroom and in Elasticsearch, hits traced to their events; documents v2, saying
   what changed; promotes it beside v1, which is unchanged and still running.
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
from e2e_elastic_handover import ES, _request, fixtures, live_cluster  # noqa: E402
from searching import paired  # noqa: E402
from config import Settings  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import builds, explorer, indexing, pipeline_writes, processing_writes, stepping, translation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import FieldPlan, PlannedField  # noqa: E402
from utils.mappingstore import read_agreed_template  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

SIBLING = 'PUT _index_template/ecs-sibling-v1\n' + json.dumps({
    'index_patterns': ['ecs-sibling-v1*'], 'priority': 200,
    'template': {'settings': {'index': {'number_of_shards': 1, 'number_of_replicas': 0}},
                 'mappings': {'dynamic': 'strict', 'properties': {
                     'user': {'properties': {'name': {'type': 'keyword', 'ignore_above': 256}}}}}}})
NEW_DATA = ("time,user,host,ip,result\n"
            "2026-10-03T08:00:00,gina,ws07,10.0.0.7,fail\n"
            "2026-10-03T08:05:00,hank,ws08,10.0.0.8,ok\n")
OUTCOME = PlannedField(name='event.outcome', type='boolean', source='EventDetail/Authenticate/Outcome/Success')


async def production(ctx, stroom: StroomGateway, es: httpx.AsyncClient, stamp: str) -> dict:
    """v1: the events pipeline and the v1 indexing pipeline, built, indexed, documented and promoted."""
    csv = await e2e.onboard(ctx, 'csv', e2e.CASES['csv'], stamp)
    events = (await processing_writes.wait_for_processing(ctx, csv['pipeline']['uuid'], [csv['raw']]))['streams'][0]['events']
    es_template, _ = await fixtures(stroom)
    cluster = await live_cluster(stroom)
    index = f'e2e-door-{stamp}-v1'
    print(f'\n### v1: {index}, indexed, then promoted')
    draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', index, 'ecs', events, example_template=SIBLING)
    plan = FieldPlan.model_validate(draft['plan'])
    xslt = await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=plan)
    v1 = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=csv['build'], name=f'{index} - Indexing',
                          template_uuid=es_template['uuid'], xslt_uuid=xslt['uuid'], index_name=index,
                          cluster_uuid=cluster['uuid'])
    e2e.check((await stepping.step_sample(ctx, v1['uuid'], events))['verdict'] == 'clean', 'v1 steps clean')
    agreed = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=v1['uuid'], plan=plan,
                              events_stream_ids=events, example_template=SIBLING)
    path, body = _request(agreed['dev_tools'])
    e2e.check((await es.put(f'/{path}', json=body)).status_code == 200, f'v1 template committed: PUT {path}')
    started = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=v1['uuid'],
                               stream_ids=events, source_pipeline_uuid=csv['pipeline']['uuid'])
    await processing_writes.wait_for_processing(ctx, v1['uuid'], events, expect_events=False, filter_id=started['filter_id'])
    index_doc = await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=csv['build'], backend='elasticsearch',
                                 name=index, time_field=plan.time_field, index_name=index, cluster_uuid=cluster['uuid'])
    await builds.write_documentation(ctx, csv['build'], v1['uuid'], '## Purpose and data\n\nCSV logons, indexed.\n',
                                     'Created', stream_ids=events)
    folder = f'System/E2E Production/door-{stamp}'
    result = await e2e.agreed(builds.promote_build, ctx=ctx, build=csv['build'], destinations={
        t: folder for t in ('Feed', 'Pipeline', 'XSLT', 'TextConverter', 'Documentation', 'ElasticIndex')})
    made = {f['pipeline']: f for f in result.get('processing_filters') or []}
    e2e.check(set(made) == {csv['pipeline']['name'], v1['name']}, f"promoted, with new-data filters for both: {sorted(made)}")
    # The user switches production on, in Stroom: the server leaves promoted pipelines alone.
    try:
        await processing_writes.set_processor_filter_enabled(ctx, made[v1['name']]['filter_id'], True)
        refused = ''
    except Exception as e:
        refused = str(e)
    e2e.check('not created by this server' in refused, "the server will not switch on a promoted pipeline's filter")
    for f in made.values():
        await stroom.request('PUT', f"/processorFilter/v1/{f['filter_id']}/enabled", True)
    return {'csv': csv, 'events': events, 'v1': v1, 'v1_index': index, 'v1_doc': index_doc, 'cluster': cluster,
            'filters': made, 'folder': folder, 'stamp': stamp}


async def update(ctx, stroom: StroomGateway, es: httpx.AsyncClient, prod: dict) -> None:
    stamp, events, csv = prod['stamp'], prod['events'], prod['csv']
    print('\n### 1. locate v1 and work out the next version')
    v1_doc = await explorer.describe_document(ctx, 'Pipeline', prod['v1']['uuid'])
    v1_name, v1_index = v1_doc['name'], prod['v1_index']
    agreed_v1 = read_agreed_template(v1_doc.get('description'))
    e2e.check(agreed_v1 and agreed_v1['index'] == v1_index and v1_index in json.dumps(v1_doc['pipeline']),
              "v1's index name and its agreed template, from the pipeline")
    v2_index, v2_name = v1_index.replace('-v1', '-v2'), v1_name.replace('-v1', '-v2')
    versions = {u: (await stroom.get_doc(t, u)).get('version') for u, t in ((prod['v1']['uuid'], 'Pipeline'),)}

    print(f'\n### 2. {v2_name}: a new version, beside v1, with the logon outcome')
    build = f'e2e-v2-{stamp}'
    copy = await e2e.agreed(pipeline_writes.copy_pipeline, ctx=ctx, build=build, source_uuid=prod['v1']['uuid'],
                            new_name=v2_name, rename={'-v1': '-v2'}, set_properties=[pipeline_writes.PropertyValue(
                                element='elasticIndexingFilter', name='indexName', value=v2_index)])
    xslt_copy = next(d for d in copy['copied_documents'] if d['type'] == 'XSLT')
    e2e.check(xslt_copy['name'].endswith('-v2-XSLT'), f"the copy owns its XSLT, renamed: {xslt_copy['name']}")
    draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', v2_index, 'ecs', events, extra_fields=[OUTCOME],
                                               example_template=agreed_v1['dev_tools'])
    plan = FieldPlan.model_validate(draft['plan'])
    e2e.check(any(f.name == 'event.outcome' for f in plan.fields), "the v2 plan adds event.outcome")
    await translation.save_xslt(ctx, build, xslt_copy['name'], index_plan=plan, uuid=xslt_copy['uuid'])

    print('\n### 3. step v2, and compare it with v1 on the same Events')
    e2e.check((await stepping.step_sample(ctx, copy['uuid'], events))['verdict'] == 'clean', 'v2 steps clean')
    diff = await stepping.compare_outputs(ctx, prod['v1']['uuid'], events, other_pipeline_uuid=copy['uuid'])
    paths = [f['path'] for f in diff['fields_changed']]
    print(f'    v1 -> v2 changed paths: {paths}')
    e2e.check(len(paths) == 1 and 'outcome' in paths[0] and diff['records_changed'] == 3,
              'only the added field differs, on every record')

    print('\n### 4. the v2 template: v1\'s, for the new index, with the added field; agreed and committed')
    final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=copy['uuid'], plan=plan,
                             events_stream_ids=events, example_template=agreed_v1['dev_tools'])
    body = final['template']
    v1_body = _request(agreed_v1['dev_tools'])[1]
    e2e.check(body['index_patterns'] == [f'{v2_index}*'] and body['priority'] == v1_body['priority']
              and body['template']['mappings']['properties']['event']['properties']['outcome'] == {'type': 'boolean'}
              and body['template']['mappings']['properties']['user'] == v1_body['template']['mappings']['properties']['user'],
              "v2's pattern and the added field; everything else as v1's")
    path, request = _request(final['dev_tools'])
    e2e.check((await es.put(f'/{path}', json=request)).status_code == 200, f'committed: PUT {path}')

    print('\n### 5. v2 on new Events only, from now')
    since = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec='seconds').replace('+00:00', 'Z')
    v2_filter = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=copy['uuid'],
                                 feed=csv['feed'], stream_type='Events', created_after=since,
                                 source_pipeline_uuid=csv['pipeline']['uuid'])

    print('\n### 6. new data, through the production events pipeline: v1 and v2 both index it')
    response = await stroom.datafeed(csv['feed'], NEW_DATA.encode('utf-8'), {'Type': 'Raw Events'})
    e2e.check(response.is_success, 'new raw data sent to the production feed')
    await asyncio.sleep(2)
    new_raw = max(m['meta']['id'] for m in (await stroom.find_meta(
        [processing_writes._term('Feed', csv['feed']), processing_writes._term('Type', 'Raw Events')], 10))['values'])
    done = await processing_writes.wait_for_processing(ctx, csv['pipeline']['uuid'], [new_raw],
                                                       filter_id=prod['filters'][csv['pipeline']['name']]['filter_id'])
    new_events = done['streams'][0]['events']
    for pipeline, filter_id in ((copy['uuid'], v2_filter['filter_id']),
                                (prod['v1']['uuid'], prod['filters'][prod['v1']['name']]['filter_id'])):
        gate = await processing_writes.wait_for_processing(ctx, pipeline, new_events, expect_events=False, filter_id=filter_id)
        e2e.check(gate.get('gate') == 'pass', f"indexed with no Error stream: {gate.get('streams')}")
    for index in (v1_index, v2_index):
        await es.post(f'/{index}/_refresh')
    v1_count = (await es.get(f'/{v1_index}/_count')).json()['count']
    v2_count = (await es.get(f'/{v2_index}/_count')).json()['count']
    e2e.check((v1_count, v2_count) == (5, 2), f'v1 holds old and new ({v1_count}); v2 only the new ({v2_count}): '
                                              f'backfilling older streams is the user\'s')
    v1_mapping = (await es.get(f'/{v1_index}/_mapping')).json()[v1_index]['mappings']['properties']
    v2_mapping = (await es.get(f'/{v2_index}/_mapping')).json()[v2_index]['mappings']['properties']
    e2e.check('outcome' not in v1_mapping['event']['properties'] and v2_mapping['event']['properties']['outcome']['type'] == 'boolean',
              'v1 unchanged, without the field; v2 maps it')

    print('\n### 7. searched both ways, documented, promoted beside v1')
    v2_doc = await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=build, backend='elasticsearch', name=v2_index,
                              time_field=plan.time_field, index_name=v2_index, cluster_uuid=prod['cluster']['uuid'])
    await paired(ctx, es, build, v2_index, v2_doc['uuid'], new_events, 2, ['StreamId', 'EventId', '@timestamp', 'user.name'], [
        ('event.outcome', 'EQUALS', 'false', 1, {'term': {'event.outcome': False}}),
        ('user.name', 'IN', 'gina,hank', 2, {'terms': {'user.name': ['gina', 'hank']}}),
        ('user.name', 'EQUALS', 'alice', 0, {'term': {'user.name': 'alice'}}),     # older: not backfilled
    ], pipeline_uuid=copy['uuid'])
    await paired(ctx, es, build, v1_index, prod['v1_doc']['uuid'], events + new_events, 5,
                 ['StreamId', 'EventId', '@timestamp', 'user.name'], [
                     ('user.name', 'EQUALS', 'hank', 1, {'term': {'user.name': 'hank'}}),        # v1 still running
                 ], pipeline_uuid=prod['v1']['uuid'])
    written = await builds.write_documentation(
        ctx, build, copy['uuid'], f'## Purpose and data\n\nCSV logons, indexed.\n\n## Changes from v1\n\n'
        f'Adds `event.outcome` (whether the logon succeeded). `{v1_index}` keeps running; moving readers to '
        f'`{v2_index}` and retiring v1 are the user\'s.\n', 'Created from v1: adds event.outcome', stream_ids=new_events)
    e2e.check('`event.outcome`' in (written.get('field_mapping') or ''), "the v2 field mapping, generated, has the added field")
    await e2e.documented_to_the_field(stroom, written, [f.name for f in plan.fields], {'event.outcome': 'true'},
                                      'v2')
    result = await e2e.agreed(builds.promote_build, ctx=ctx, build=build, destinations={
        t: prod['folder'] for t in ('Pipeline', 'XSLT', 'Documentation', 'ElasticIndex', 'Dashboard')})
    print(f"    {result['promoted']}")
    e2e.check(any(f"moved Pipeline '{v2_name}' to {prod['folder']}" == p for p in result['promoted']),
              'v2 promoted beside v1')
    after = {u: (await stroom.get_doc(t, u)).get('version') for u, t in ((prod['v1']['uuid'], 'Pipeline'),)}
    v1_filter = await stroom.get(f"/processorFilter/v1/{prod['filters'][prod['v1']['name']]['filter_id']}")
    e2e.check(after == versions and v1_filter.get('enabled') is True, 'v1 unchanged and still running')


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
            prod = await production(ctx, stroom, es, stamp)
            await update(ctx, stroom, es, prod)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
