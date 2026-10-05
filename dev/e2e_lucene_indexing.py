"""Indexing on the Lucene backend end to end against the local Docker stack, driving the real tools.

    uv run python dev/e2e_lucene_indexing.py

Stage 1 comes from the translation suite (a CSV build). Then: convention guidance, field plan, Lucene index doc
and fields, indexing XSLT and pipeline from the Indexing template, stepping the Events, processing,
verification dashboard and test searches, documentation; then a v2 copy adding one field, a diff limited
to that field, and verification of v2 beside v1; then promotion.
"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import e2e_translation as e2e  # noqa: E402
from tools import builds, indexing, pipeline_writes, processing_writes, stepping, templates, translation  # noqa: E402
from tools.pipeline_writes import PropertyValue  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import FieldPlan, PlannedField  # noqa: E402

check, agreed = e2e.check, e2e.agreed


async def index_stage(ctx, csv: dict, stamp: str):
    print('\n### stage 2 on Lucene')
    stroom = ctx.lifespan_context['stroom']
    events = (await e2e.processing_writes.wait_for_processing(ctx, csv['pipeline']['uuid'], [csv['raw']]))['streams'][0]['events']
    guidance = await indexing.get_field_conventions(ctx)
    check(guidance['status'] == 'needs_guidance', 'no convention chosen: the tool asks instead of assuming one')
    conv = await indexing.get_field_conventions(ctx, 'stroom-flat')
    check('field_map' in conv['profile'], "user chose 'stroom-flat'")
    candidates = (await templates.find_pipeline_templates(ctx, 'indexing'))['candidates']
    template = next(c for c in candidates if c['backend'] == 'lucene')
    check(template['name'] == 'Indexing', f"indexing template {template['name']} ({template['backend']})")
    index_name = f'E2E-CSV-{stamp}-INDEX-V1'
    draft = await indexing.draft_index_mapping(ctx, 'lucene', index_name, 'stroom-flat', events)
    plan = FieldPlan.model_validate(draft['plan'])
    print(f"    plan: {[(f.name, f.type) for f in plan.fields]}")
    check(not draft['problems'], 'field plan has StreamId, EventId and the time field')
    index = await agreed(indexing.create_index_doc, ctx=ctx, build=csv['build'], backend='lucene', name=index_name,
                         time_field=plan.time_field)
    added = await indexing.set_index_fields(ctx, index['uuid'], plan)
    check(len(added['added']) == len(plan.fields), f"index fields added: {added['added']}")
    xslt = await translation.create_xslt(ctx, csv['build'], f'{index_name}-XSLT', plan.xslt())
    pipeline = await agreed(indexing.create_indexing_pipeline, ctx=ctx, build=csv['build'], name=f'{index_name} - Indexing',
                            template_uuid=template['uuid'], xslt_uuid=xslt['uuid'], index_uuid=index['uuid'])
    sample = await stepping.step_sample(ctx, pipeline['uuid'], events)
    check(sample['verdict'] == 'clean', f"stepped {sample['records_stepped']} events through the indexing pipeline clean")
    await agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'], stream_ids=events,
                 source_pipeline_uuid=csv['pipeline']['uuid'])
    gate = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], events, expect_events=False)
    check(gate['gate'] == 'pass', f"indexing finished with no Error stream: {gate['streams']}")
    dash = await indexing.create_verification_dashboard(ctx, csv['build'], f'{index_name}-VERIFY', index['uuid'], 'lucene',
                                                        ['StreamId', 'EventId', 'EventTime', 'UserId', 'HostName'])
    # Lucene is searched only through Stroom; each hit is traced back to its event by stepping the pipeline.
    S = indexing.SearchCheck
    result = await indexing.run_test_searches(
        ctx, dash['uuid'], events, 3, exact=[{'field': 'UserId', 'value': 'bob'}, {'field': 'HostName', 'value': 'ws03'}],
        time_range={'field': 'EventTime', 'from': '2026-09-28T10:04:00.000Z', 'to': '2026-09-28T10:08:00.000Z', 'expected': 2},
        searches=[S(field='UserId', value='Bob', expected=1),          # the plan's Lucene fields are case-insensitive
                  S(field='UserId', condition='IN', value='alice,carol', expected=2),
                  S(field='UserId', value='a*', expected=1),
                  S(field='HostName', value='ws0*', expected=3),
                  S(field='UserId', condition='NOT_EQUALS', value='bob', expected=2),
                  S(field='EventTime', condition='GREATER_THAN', value='2026-09-28T10:04:00.000Z', expected=2)],
        pipeline_uuid=pipeline['uuid'])
    for c in result['checks']:
        trace = c.get('trace')
        print(f"    {'ok ' if c['pass'] else 'BAD'} {c['check']}: {c['returned']}"
              + (f" (traced to record {trace['event']})" if trace and trace['traced'] else f" (trace: {trace})" if trace else ''))
    check(result['passed'], 'verification searches pass, each hit traced to its event')
    try:
        await builds.write_documentation(ctx, csv['build'], pipeline['uuid'],
                                         f"# {pipeline['name']}\n\n## Output\n\nLucene index {index_name}.\n", 'Created')
        refused = ''
    except Exception as e:
        refused = str(e)
    check('Give stream_ids' in refused and 'values the sample gave' in refused,
          'documentation without the sample is refused: it goes down to the field, with sample values')
    written = await builds.write_documentation(ctx, csv['build'], pipeline['uuid'],
                                               f"# {pipeline['name']}\n\n## Output\n\nLucene index {index_name}.\n",
                                               'Created', stream_ids=events)
    user_field = next(f.name for f in plan.fields if f.source == 'EventSource/User/Id')
    await e2e.documented_to_the_field(ctx.lifespan_context['stroom'], written, [f.name for f in plan.fields],
                                      {user_field: 'alice'})
    return {'events': events, 'template': template, 'index': index, 'pipeline': pipeline, 'plan': plan}


async def version_two(ctx, csv: dict, v1: dict, stamp: str):
    print('\n### v2: index the logon outcome as well')
    name = f'E2E-CSV-{stamp}-INDEX-V2'
    extra = [PlannedField(name='Success', type='keyword', source='EventDetail/Authenticate/Outcome/Success')]
    draft = await indexing.draft_index_mapping(ctx, 'lucene', name, 'stroom-flat', v1['events'], extra_fields=extra)
    plan = FieldPlan.model_validate(draft['plan'])
    index = await agreed(indexing.create_index_doc, ctx=ctx, build=csv['build'], backend='lucene', name=name,
                         time_field=plan.time_field)
    await indexing.set_index_fields(ctx, index['uuid'], plan)
    xslt = await translation.create_xslt(ctx, csv['build'], f'{name}-XSLT', plan.xslt())
    copy = await agreed(pipeline_writes.copy_pipeline, ctx=ctx, build=csv['build'], source_uuid=v1['pipeline']['uuid'],
                        new_name=f'{name} - Indexing', rename={'V1': 'V2'},
                        set_properties=[PropertyValue(element='xsltFilter', name='xslt', doc_uuid=xslt['uuid'], doc_type='XSLT'),
                                        PropertyValue(element='indexingFilter', name='index', doc_uuid=index['uuid'], doc_type='Index')])
    diff = await stepping.compare_outputs(ctx, v1['pipeline']['uuid'], v1['events'], other_pipeline_uuid=copy['uuid'])
    paths = [f['path'] for f in diff['fields_changed']]
    print(f"    v1 -> v2 changed paths: {paths}")
    check(paths == ['record/data[Success]/@value'], 'v2 differs from v1 only by the added field')
    # The copy's code changed, so it is stepped clean before it processes, as for any pipeline.
    stepped = await stepping.step_sample(ctx, copy['uuid'], v1['events'])
    check(stepped['verdict'] == 'clean', f"v2 steps clean: {stepped['verdict']}")
    await agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=copy['uuid'], stream_ids=v1['events'],
                 source_pipeline_uuid=csv['pipeline']['uuid'])
    gate = await processing_writes.wait_for_processing(ctx, copy['uuid'], v1['events'], expect_events=False)
    check(gate['gate'] == 'pass', 'v2 indexing finished with no Error stream')
    dash = await indexing.create_verification_dashboard(ctx, csv['build'], f'{name}-VERIFY', index['uuid'], 'lucene',
                                                        ['StreamId', 'EventId', 'EventTime', 'UserId', 'Success'])
    result = await indexing.run_test_searches(ctx, dash['uuid'], v1['events'], 3,
                                              exact=[{'field': 'Success', 'value': 'false'}])
    check(result['passed'] and result['checks'][1]['returned'] == 1, 'v2 finds the one failed logon by the new field')
    v1_still = await indexing.run_test_searches(ctx, (await indexing.create_verification_dashboard(
        ctx, csv['build'], f"{v1['index']['name']}-RECHECK", v1['index']['uuid'], 'lucene', ['StreamId', 'UserId']))['uuid'],
        v1['events'], 3)
    check(v1_still['passed'], 'v1 index is untouched beside v2')


async def main():
    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = e2e.Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True, stroom_api_key=local['STROOM_ADMIN_API_KEY'],
                           oidc_issuer_url='-', oidc_audience='-', public_base_url='-',
                           event_logging_version=e2e.VERSION, conventions_dir=ROOT / 'conventions')
    stroom = e2e.StroomGateway(settings)
    ctx = e2e.SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': e2e.ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': e2e.AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = time.strftime('%H%M%S')
    try:
        csv = await e2e.onboard(ctx, 'csv', e2e.CASES['csv'], stamp)
        v1 = await index_stage(ctx, csv, stamp)
        await version_two(ctx, csv, v1, stamp)
        print('\n### promotion')
        folder = f'E2E Indexed {stamp}'
        system = next(r for r in (await stroom.post('/explorer/v2/fetchExplorerNodes', {
            'openItems': [], 'temporaryOpenedItems': [], 'minDepth': 1, 'ensureVisible': None, 'showAlerts': False,
            'filter': {'includedTypes': None, 'includedRootTypes': None, 'tags': None, 'nodeFlags': None,
                       'requiredPermissions': ['VIEW'], 'nameFilter': None, 'nameFilterChange': False,
                       'recentItems': None}}))['rootNodes'] if r['type'] == 'System')
        await stroom.post('/explorer/v2/create', {'docType': 'Folder', 'docName': folder, 'destinationFolder': system,
                                                  'permissionInheritance': 'DESTINATION'})
        dest = {t: f'System/{folder}' for t in ('Feed', 'Pipeline', 'XSLT', 'TextConverter', 'Documentation', 'Index',
                                                'Dashboard')}
        result = await agreed(builds.promote_build, ctx=ctx, build=csv['build'], destinations=dest)
        check(len(result['promoted']) >= 12, f"promoted {len(result['promoted'])} documents")
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
