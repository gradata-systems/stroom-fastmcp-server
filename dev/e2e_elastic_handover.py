"""Elasticsearch template hand-over against the local Stroom stack (see dev/stroom), without Elasticsearch.

    uv run python dev/e2e_elastic_handover.py

The local stack has no Elasticsearch indexing template, so this adds a fixture one (the local 'Indexing'
template with an ElasticIndexingFilter in place of the Lucene one) and an Elastic Cluster doc pointing
nowhere. The documents come from stepping the indexing XSLT, so no Elasticsearch server is needed.

1. Stage 1 on the CSV sample (as in the Phase 2 test).
2. An ES indexing pipeline from the fixture template; the agent's template proposal, self-checked against
   the documents the pipeline writes.
3. A user's changed template (a renamed field, a stricter type, dynamic strict) is checked: not compatible,
   with the pipeline changes it needs.
4. Once the user confirms the template is committed, the indexing filter is pre-created disabled, with a
   direct link to the pipeline.
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
from tools import indexing, processing_writes, stepping, templates, translation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import FieldPlan  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

FIXTURE_TEMPLATE = 'E2E Events to Elasticsearch'
FIXTURE_CLUSTER = 'E2E_LOCAL_ES'


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
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'), 'elastic': None,
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
        xslt = await translation.create_xslt(ctx, csv['build'], f'{index}-XSLT', draft['xslt'])
        pipeline = await p2.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=csv['build'], name=f'{index} - Indexing',
                                   template_uuid=es_template['uuid'],
                                   xslt_uuid=xslt['uuid'], index_name=index,
                                   cluster_uuid=cluster['uuid'])
        sample = await stepping.step_sample(ctx, pipeline['uuid'], events)
        print(f"    stepping: {sample['verdict']}; groups: {[(g['class'], g['element'], g['count']) for g in sample['groups']]}")

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

        print('\n### hand-over: filter pre-created disabled')
        first = await processing_writes.create_processor_filter(ctx, pipeline['uuid'], stream_ids=events,
                                                                source_pipeline_uuid=csv['pipeline']['uuid'])
        p2.check(first.get('status') == 'needs_confirmation' and f"index '{index}'" in first['summary'],
                 f"asks whether the template is committed: {first.get('summary')}")
        ready = await processing_writes.create_processor_filter(ctx, pipeline['uuid'], stream_ids=events,
                                                                source_pipeline_uuid=csv['pipeline']['uuid'],
                                                                confirmation_id=first['confirmation_id'])
        stored = await stroom.get(f"/processorFilter/v1/{ready['filter_id']}")
        p2.check(ready['enabled'] is False and stored.get('enabled') is False, f"filter {ready['filter_id']} exists, disabled")
        p2.check(ready['pipeline_link'] == f"http://127.0.0.1:18080/?action=open-doc&docType=Pipeline&docUuid={pipeline['uuid']}",
                 f"link to review it: {ready['pipeline_link']}")
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
