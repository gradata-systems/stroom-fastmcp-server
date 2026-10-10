"""Stream types beyond Raw Events and Events, and templates found by what they are, against the local Stroom stack.

    uv run python dev/e2e_stream_types.py

Templates here are fixtures with names no standard template has, in a folder of their own (System/E2E Bases), with
no children: found only by what they are.

1. Templates: a JSON translation template ('json-in v3') and a records template ('csv records v1') are found for
   their stage; start_onboarding names 'json-in v3' for a JSON sample; a source is onboarded through it.
2. Raw Reference: the reference-data template and the loaders are found by what they are; a Raw Reference feed's
   whole-feed filter takes the feed's stream type; the wait takes the pipeline's output type (Reference); an events
   pipeline's reference names no loader, and the lookup fills the events. Its mapping lost, rebuild_mapping reads the
   lookup back from the XSLT as the entry that wrote it, and the events are filled as before.
3. Records: a pipeline writing Records, from 'csv records v1'; its wait takes Records from the pipeline; the Records
   stream is refused as Events to plan an index from, and indexed as records (records:2 data) into Elasticsearch.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from e2e_elastic_handover import ES, _request, live_cluster  # noqa: E402
from e2e_formats import decide  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from format_samples import SAMPLES  # noqa: E402
from security.guard import guard_from  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import (feeds, generation, indexing, pipeline_writes, plan, processing_writes, rebuild, reference, stepping,  # noqa: E402
                   templates, translation)
from tools.pipeline_writes import PipelineReference  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import Discovery, FieldPlan  # noqa: E402
from utils.mappingstore import read_mapping  # noqa: E402
from utils.refgen import ReferenceMapping  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

BASES = 'E2E Bases'
JSON_TEMPLATE, RECORDS_TEMPLATE = 'json-in v3', 'csv records v1'
RECORDS_DATA = {
    'elements': {'add': [{'id': 'dsParser', 'type': 'DSParser'}, {'id': 'xmlWriter', 'type': 'XMLWriter'},
                         {'id': 'streamAppender', 'type': 'StreamAppender'}]},
    'links': {'add': [{'from': 'dsParser', 'to': 'xmlWriter'}, {'from': 'xmlWriter', 'to': 'streamAppender'}]},
    'properties': {'add': [{'element': 'streamAppender', 'name': 'streamType', 'value': {'string': 'Records'}}]}}
DIRECTORY = ('user,name,department,site\nalice,Alice Anderson,Operations,HQ\nbob,Bob Brown,Finance,HQ\n'
             'carol,Carol Clark,Engineering,Annex\n')
BADGES = ('time,badge_user,door,result\n2026-10-01T07:58:00Z,ALICE,Main entrance,granted\n'
          '2026-10-01T08:02:00Z,BOB,Server room,denied\n2026-10-01T08:10:00Z,ALICE,Server room,granted\n')
REFERENCE_MAPPING = {'input': 'data_splitter', 'maps': [{'name': 'USER_DIRECTORY', 'key': 'user', 'values': [
    {'element': 'name', 'field': 'name'}, {'element': 'department', 'field': 'department'}]}]}
LOOKUP = "lower-case(data[@name='badge_user']/@value)"
BADGE_MAPPING = {
    'input': 'data_splitter',
    'common': [{'path': 'EventTime/TimeCreated', 'field': 'time', 'time_format': "yyyy-MM-dd'T'HH:mm:ssX"},
               {'path': 'EventSource/System/Name', 'value': 'Door Control'},
               {'path': 'EventSource/System/Environment', 'value': 'Test'},
               {'path': 'EventSource/Generator', 'value': 'badge-controller'},
               {'path': 'EventSource/Device/Name', 'field': 'door'},
               {'path': 'EventSource/User/Id', 'field': 'badge_user', 'transform': 'lower'},
               {'path': 'EventSource/User/Name', 'lookup': {'map': 'USER_DIRECTORY', 'xpath': LOOKUP, 'path': 'name'}}],
    'events': [{'name': 'badge', 'fields': [
        {'path': 'EventDetail/TypeId', 'value': 'Badge-Access'},
        {'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
        {'path': 'EventDetail/Authenticate/User/Id', 'field': 'badge_user', 'transform': 'lower'},
        {'path': 'EventDetail/Authenticate/Outcome/Success', 'field': 'result',
         'map': {'granted': 'true', 'denied': 'false'}}]}]}


async def fixture(stroom: StroomGateway, ctx, name: str, data: dict) -> dict:
    """A template of our own, named as no standard template is, in a folder of its own, with no children."""
    guard = guard_from(ctx)
    system = await guard.system_node()
    folder = await guard.find_child_folder(system, BASES) or await guard.create_folder(system, BASES, [])
    found = [v['docRef'] for v in await stroom.find_all_documents(name, ['Pipeline']) if v['docRef']['name'] == name]
    if found:
        ref = found[0]
    else:
        node = await stroom.post('/explorer/v2/create', {
            'docType': 'Pipeline', 'docName': name, 'permissionInheritance': 'DESTINATION',
            'destinationFolder': {k: v for k, v in folder.items() if not k.startswith('_')}})
        ref = node.get('docRef', node)
    doc = await stroom.get(f"/pipeline/v1/{ref['uuid']}")
    doc['pipelineData'], doc['description'] = data, 'Fixture for dev/e2e_stream_types.py'
    await stroom.request('PUT', f"/pipeline/v1/{ref['uuid']}", doc)
    return {'type': 'Pipeline', 'uuid': ref['uuid'], 'name': name}


async def templates_found(ctx, stroom: StroomGateway, stamp: str) -> dict:
    print('\n### 1. templates found by what they are')
    standard_json = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                         if c['parser'] == 'JSONParser')
    json_data = (await stroom.get(f"/pipeline/v1/{standard_json['uuid']}"))['pipelineData']
    mine = await fixture(stroom, ctx, JSON_TEMPLATE, json_data)
    records = await fixture(stroom, ctx, RECORDS_TEMPLATE, RECORDS_DATA)
    translation_candidates = (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
    found = next((c for c in translation_candidates if c['uuid'] == mine['uuid']), {})
    # Neither configured nor in Stroom's standard folder: what it is (or, after a run, its children) finds it.
    e2e.check(found.get('source') in ('template_like', 'inherited_by_others') and found.get('path') == f'System/{BASES}'
              and found['parser'] == 'JSONParser',
              f"'{JSON_TEMPLATE}' found as a translation template by what it is: {found.get('source')}, {found.get('path')}")
    records_candidates = (await templates.find_pipeline_templates(ctx, 'records'))['candidates']
    e2e.check(any(c['uuid'] == records['uuid'] for c in records_candidates),
              f"'{RECORDS_TEMPLATE}' found as a records template: {[c['name'] for c in records_candidates]}")
    started = await plan.start_onboarding(ctx, 'Acme JSON app', {'app.jsonl': SAMPLES['json_lines']}, build=f'st-json-{stamp}')
    e2e.check(JSON_TEMPLATE in started['template'], f"start_onboarding names the templates there are: {started['template']}")
    build, feed = f'st-json-{stamp}', f'ST-JSON-{stamp}'
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, SAMPLES['json_lines']))['stream_id']
    draft = await generation.draft_translation_mapping(ctx, stream_ids=[raw], source_name='Acme', system_name='Acme',
                                                       environment='Test')
    saved = await e2e.agreed(generation.build_translation_xslt, ctx=ctx, mapping=decide(draft['mapping'], {'user': 'user.name'}),
                             stream_ids=[raw], build=build, name=f'{feed}-Events')
    e2e.check(saved['ok'] and saved.get('saved'), f"saved: {saved.get('problems')}")
    pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                template_uuid=mine['uuid'])
    stepped = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    e2e.check(stepped['verdict'] == 'clean', f"a source onboarded through '{JSON_TEMPLATE}': {stepped['records_stepped']} "
                                             f"records clean")
    return records


async def reference_data(ctx, stroom: StroomGateway, stamp: str) -> None:
    print('\n### 2. Raw Reference: a reference pipeline, its filter and its output, from what the environment has')
    found = await reference.find_reference_data(ctx)
    template = found.get('reference_data_template')
    e2e.check(bool(template) and bool(found['loaders']),
              f"the reference-data template and the loaders, by what they are: {template and template['name']}, "
              f"{[l['name'] for l in found['loaders']]}")
    build, feed = f'st-ref-{stamp}', f'ST-USERS-{stamp}'
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed, stream_type='Raw Reference')
    raw = (await feeds.upload_sample(ctx, feed, DIRECTORY, stream_type='Raw Reference',
                                     effective_time='2000-01-01T00:00:00.000Z'))['stream_id']
    await generation.build_data_splitter(ctx, stream_ids=[raw], save_as=feed, build=build)
    code = await generation.build_reference_xslt(ctx, ReferenceMapping.model_validate(REFERENCE_MAPPING))
    e2e.check(code['ok'], f"reference XSLT: {code.get('problems')}")
    await translation.create_xslt(ctx, build, f'{feed}-Reference', code['xslt'])
    loader = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Reference',
                              template_uuid=template['uuid'])
    stepped = await stepping.step_sample(ctx, loader['uuid'], [raw])
    e2e.check(stepped['verdict'] == 'clean', f"the reference pipeline steps clean: {stepped['verdict']}")
    started = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=loader['uuid'], feed=feed,
                               created_after='2000-01-01T00:00:00.000Z')
    e2e.check('(Raw Reference)' in started['scope'], f"the whole-feed filter takes the feed's stream type: {started['scope']}")
    done = await processing_writes.wait_for_processing(ctx, loader['uuid'], [raw])
    e2e.check(done['gate'] == 'pass' and done['streams'][0].get('output_type') == 'Reference',
              f"the wait takes the pipeline's output type: {done['streams']} {done['problems']}")
    events_build, events_feed = f'st-badge-{stamp}', f'ST-BADGE-{stamp}'
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=events_build, name=events_feed)
    events_raw = (await feeds.upload_sample(ctx, events_feed, BADGES))['stream_id']
    await generation.build_data_splitter(ctx, stream_ids=[events_raw], save_as=events_feed, build=events_build)
    saved = await e2e.agreed(generation.build_translation_xslt, ctx=ctx, mapping=BADGE_MAPPING, stream_ids=[events_raw],
                             build=events_build, name=f'{events_feed}-Events')
    e2e.check(saved['ok'], f"the events mapping with its lookup: {saved.get('problems')}")
    text = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                if c['parser'] == 'DSParser')
    events = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=events_build, name=f'{events_feed}-Events',
                              template_uuid=text['uuid'], references=[PipelineReference(feed=feed)])
    output = (await stepping.step_pipeline(ctx, events['uuid'], events_raw, 0))['elements']['translationFilter']['output']
    name = etree.fromstring(output.encode()).findtext('.//{event-logging:3}EventSource/{event-logging:3}User/{event-logging:3}Name')
    e2e.check(name == 'Alice Anderson', f"the lookup, through the loader resolved with none named, fills the event: {name}")
    # Its mapping lost: the lookup read back from the XSLT (asked for by the user: reference data as generation has it).
    cleared = await stroom.get_doc('XSLT', saved['saved']['uuid'])
    cleared['description'] = ''
    await stroom.put_doc(cleared)
    restored = await e2e.agreed(rebuild.rebuild_mapping, ctx=ctx, uuid=saved['saved']['uuid'])
    back = read_mapping((await stroom.get_doc('XSLT', saved['saved']['uuid']))['description'])[1]['mapping']
    lookups = [f for f in back.get('common', []) + [f for r in back['events'] for f in r.get('fields', [])] if f.get('lookup')]
    e2e.check(bool(restored.get('saved')) and 'differences' not in restored
              and [f['lookup'] for f in lookups] == [BADGE_MAPPING['common'][-1]['lookup']],
              f"its mapping lost, rebuild_mapping reads the lookup back as written: {[f['lookup'] for f in lookups]}")
    output = (await stepping.step_pipeline(ctx, events['uuid'], events_raw, 0))['elements']['translationFilter']['output']
    name = etree.fromstring(output.encode()).findtext('.//{event-logging:3}EventSource/{event-logging:3}User/{event-logging:3}Name')
    e2e.check(name == 'Alice Anderson', f"the XSLT saved from the rebuilt mapping fills the event the same: {name}")


async def records(ctx, stroom: StroomGateway, template: dict, stamp: str) -> None:
    print('\n### 3. Records: a pipeline writing Records, its output, and the Records indexed as records')
    build, feed = f'st-rec-{stamp}', f'ST-REC-{stamp}'
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, SAMPLES['csv_quoted']))['stream_id']
    await generation.build_data_splitter(ctx, stream_ids=[raw], save_as=feed, build=build)
    writer = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Records',
                              template_uuid=template['uuid'])
    stepped = await stepping.step_sample(ctx, writer['uuid'], [raw])
    e2e.check(stepped['verdict'] == 'clean', f"the records pipeline steps clean: {stepped['verdict']} "
                                             f"{[(g['class'], g.get('reason')) for g in stepped['groups']]}")
    await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=writer['uuid'], stream_ids=[raw])
    done = await processing_writes.wait_for_processing(ctx, writer['uuid'], [raw])
    e2e.check(done['gate'] == 'pass' and done['streams'][0].get('output_type') == 'Records',
              f"the wait takes Records from the pipeline: {done['streams']} {done['problems']}")
    produced = done['streams'][0]['events']
    index = f'st-records-{stamp}'
    try:
        await indexing.draft_index_mapping(ctx, 'elasticsearch', index, events_stream_ids=produced, convention='ecs')
        refused = ''
    except ToolError as e:
        refused = str(e)
    e2e.check('is a Records stream' in refused, f"the Records stream is refused as Events to plan from: {refused[:110]}")
    plan_ = FieldPlan.model_validate((await indexing.draft_index_mapping(
        ctx, 'elasticsearch', index, discovery=Discovery(input='delimited', timestamp_field='time')))['plan'])
    xslt = await translation.save_xslt(ctx, build, f'{index}-XSLT', index_plan=plan_)
    es_template = next(c for c in (await templates.find_pipeline_templates(ctx, 'indexing'))['candidates']
                       if c['backend'] == 'elasticsearch' and c['parser'] == 'XMLParser')
    cluster = await live_cluster(stroom)
    indexer = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=build, name=f'{index} - Indexing',
                               template_uuid=es_template['uuid'], xslt_uuid=xslt['uuid'], index_name=index,
                               cluster_uuid=cluster['uuid'])
    stepped = await stepping.step_sample(ctx, indexer['uuid'], produced)
    e2e.check(stepped['verdict'] == 'clean', f"the indexing pipeline steps the Records clean: {stepped['verdict']}")
    final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=indexer['uuid'], plan=plan_,
                             events_stream_ids=produced, example_template=f'PUT _index_template/{index}-example\n'
                             + json.dumps({'index_patterns': [f'{index}-example*'], 'template': {'mappings': {'dynamic': True}}}))
    path, body = _request(final['dev_tools'])
    async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
        e2e.check((await es.put(f'/{path}', json=body)).status_code == 200, f'the agreed template applied: PUT {path}')
        await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=indexer['uuid'],
                         stream_ids=produced)
        gate = await processing_writes.wait_for_processing(ctx, indexer['uuid'], produced, expect_events=False)
        e2e.check(gate['gate'] == 'pass', f"indexed with no Error stream: {gate['streams']} {gate['problems']}")
        await es.post(f'/{index}/_refresh')
        count = (await es.get(f'/{index}/_count')).json().get('count')
        users = sorted(h['_source'].get('user') for h in (await es.get(f'/{index}/_search')).json()['hits']['hits'])
    e2e.check(count == 3 and users == ['alice', 'carol', "o'brien, pat"], f"every record indexed, as records: {count}, {users}")


async def main():
    stroom = StroomGateway(e2e.target_settings())
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    try:
        template = await templates_found(ctx, stroom, e2e.STAMP)
        await reference_data(ctx, stroom, e2e.STAMP)
        await records(ctx, stroom, template, e2e.STAMP)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
