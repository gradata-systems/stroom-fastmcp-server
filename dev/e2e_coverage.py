"""End to end on the local stack: a kind of event the sample missed, found once the feed is processed; the rule
added; only the affected stream processed again; the CEF pipeline that follows reviewed.

    uv run python dev/e2e_coverage.py

The sample stream holds logons only; a second stream of the feed also holds views. The events pipeline (a rule for
logons, unmatched records warned) processes both; review_coverage finds the views in the second stream's Error
stream, names that stream alone, and the CEF pipeline reading the Events as a follow-on. A rule for views is added
(changes=), the second stream reprocessed, and the follow-on review proposes the CEF fields for View events.
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from security.guard import guard_from  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import cef, coverage, feeds, generation, pipeline_writes, processing_writes, stepping, templates  # noqa: E402
from tools.pipeline_writes import PropertyValue  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402
from utils.xsltversion import pending_of  # noqa: E402

HEADER = 'time,user,host,action,resource\n'
SAMPLE = HEADER + '2026-10-01T10:00:00,alice,ws01,login,\n2026-10-01T10:05:00,bob,ws02,login,\n'
LATER = HEADER + ('2026-10-02T09:00:00,carol,ws03,login,\n2026-10-02T09:01:00,carol,ws03,view,Payroll\n'
                  '2026-10-02T09:02:00,dave,ws04,view,Firewall\n')

MAPPING = {
    'input': 'data_splitter', 'unmatched': 'warn',
    'common': [{'path': 'EventTime/TimeCreated', 'field': 'time', 'time_format': "yyyy-MM-dd'T'HH:mm:ss"},
               {'path': 'EventSource/System/Name', 'value': 'Coverage'},
               {'path': 'EventSource/System/Environment', 'value': 'Test'},
               {'path': 'EventSource/Generator', 'value': 'e2e'},
               {'path': 'EventSource/Device/HostName', 'field': 'host'},
               {'path': 'EventSource/User/Id', 'field': 'user'}],
    'events': [{'name': 'login', 'when': [{'field': 'action', 'equals': 'login'}],
                'fields': [{'path': 'EventDetail/TypeId', 'value': 'Logon'},
                           {'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                           {'path': 'EventDetail/Authenticate/User/Id', 'field': 'user'}]}]}
VIEW_RULE = {'name': 'view', 'when': [{'field': 'action', 'equals': 'view'}],
             'fields': [{'path': 'EventDetail/TypeId', 'value': 'View'},
                        {'path': 'EventDetail/View/Resource/Type', 'value': 'Secret'},
                        {'path': 'EventDetail/View/Resource/Name', 'field': 'resource'}]}


async def main():
    stroom = StroomGateway(e2e.target_settings())
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    try:
        await run(ctx, stroom, e2e.STAMP)
        print('\nALL PASSED')
    finally:
        await stroom.close()


async def run(ctx, stroom: StroomGateway, stamp: str) -> None:
    build, feed = f'cov-{stamp}', f'COV-{stamp}'
    print('\n### 1. an events pipeline built from a sample of logons, processing the whole feed')
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    first = (await feeds.upload_sample(ctx, feed, SAMPLE))['stream_id']
    later = (await feeds.upload_sample(ctx, feed, LATER))['stream_id']
    converter = (await generation.build_data_splitter(ctx, stream_ids=[first], save_as=f'{feed}-CSV', build=build))['saved']
    built = await generation.build_translation_xslt(ctx, MAPPING, stream_ids=[first], build=build, name=f'{feed}-Events',
                                                    agent_model='e2e-model')
    e2e.check(built['ok'] and built.get('saved'), f"XSLT saved: {built.get('problems')}")
    xslt = built['saved']
    template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                    if c['name'] == 'Event Data (Text)')
    pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                template_uuid=template['uuid'], set_properties=[
                                    PropertyValue(element='dsParser', name='textConverter', doc_uuid=converter['uuid'],
                                                  doc_type='TextConverter'),
                                    PropertyValue(element='translationFilter', name='xslt', doc_uuid=xslt['uuid'],
                                                  doc_type='XSLT')])
    e2e.check((await stepping.step_sample(ctx, pipeline['uuid'], [first]))['verdict'] == 'clean', "the sample steps clean")
    await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                     stream_ids=[first, later])
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [first, later])
    events_first = done['streams'][0]['events']
    e2e.check(bool(events_first), f"processed: {done['streams']}")

    print('\n### 2. a CEF pipeline following it, from now on')
    saved = await cef.draft_cef_mapping(ctx, events_first, custom_keys=False, topic='arcsight', build=build,
                                        name=f'{feed}-CEF', header={'vendor': 'Acme'})
    e2e.check(bool(saved.get('saved')), f"CEF XSLT saved: {saved['problems']}")
    folder = await guard_from(ctx).build_folder(build)
    kafka = await stroom.post('/explorer/v2/create', {'docType': 'KafkaConfig', 'docName': f'COV-KAFKA-{stamp}',
                                                      'destinationFolder': {k: v for k, v in folder.items() if not k.startswith('_')},
                                                      'permissionInheritance': 'DESTINATION'})
    forwarding = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-CEF',
                                  standalone='kafka', set_properties=[
                                      PropertyValue(element='xsltFilter', name='xslt', doc_uuid=saved['saved']['uuid'], doc_type='XSLT'),
                                      PropertyValue(element='standardKafkaProducer', name='kafkaConfig',
                                                    doc_uuid=kafka['uuid'], doc_type='KafkaConfig')])
    e2e.check((await stepping.step_sample(ctx, forwarding['uuid'], events_first))['verdict'] == 'clean',
              "the CEF pipeline steps clean (sending nothing)")
    soon = (datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=forwarding['uuid'], feed=feed,
                     stream_type='Events', created_after=soon)

    print('\n### 3. the whole feed reviewed: the views the sample missed, in the later stream alone')
    review = await coverage.review_coverage(ctx, pipeline['uuid'])
    [group] = [g for g in review['missed'] if g['found_as'] == 'no rule matched']
    e2e.check(group['kind'] == {'action': 'view'} and group['records'] == 2 and group['raw_streams'] == [later],
              f"missed: {group['kind']}, {group['records']} records in {group['raw_streams']}")
    e2e.check(review['affected_raw_stream_ids'] == [later] and review['progress']['done']
              and review['tell_user'].startswith('Found untranslated events'),
              review['tell_user'])
    e2e.check(any(f['uuid'] == forwarding['uuid'] and f['kind'] == 'cef' for f in review['follow_on']),
              f"follow-on: {[(f.get('name'), f.get('kind')) for f in review['follow_on']]}")

    print('\n### 4. the rule added, only that stream processed again, the follow-on reviewed against it')
    fixed = await generation.build_translation_xslt(ctx, changes={'events': [VIEW_RULE]}, uuid=xslt['uuid'],
                                                    stream_ids=[first, later],
                                                    change='Rule for view events, found once the whole feed was processed')
    e2e.check(fixed['ok'] and fixed['changes_applied'] == ["rule 'view' added after the others"], f"{fixed.get('problems')}")
    e2e.check((await stepping.step_sample(ctx, pipeline['uuid'], [first, later]))['verdict'] == 'clean', "steps clean")
    redo = await e2e.agreed(processing_writes.reprocess_streams, ctx=ctx, pipeline_uuid=pipeline['uuid'], stream_ids=[later])
    again = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [later], filter_id=redo['filter_id'])
    new_events = again['streams'][0]['events']
    e2e.check(bool(new_events), f"reprocessed: {again['streams']}")
    after = await coverage.review_coverage(ctx, pipeline['uuid'], events_stream_ids=new_events)
    [cef_review] = [f for f in after['follow_on'] if f['uuid'] == forwarding['uuid']]
    view = [c for c in cef_review['changes'] if c.get('event_type') == 'View']
    e2e.check(bool(view) and any('View events' in m for m in cef_review['missing']),
              f"CEF changes for View events: {view} {cef_review['missing']}")
    pending = pending_of((await stroom.get_doc('XSLT', xslt['uuid'])).get('description'))
    e2e.check([e['change'] for e in pending['entries']] == ['Created from its mapping',
                                                            'Rule for view events, found once the whole feed was processed']
              and 'e2e-model' in pending['entries'][0]['by'],
              f"the XSLT's changes pending, for one version line at promotion: {[e['change'] for e in pending['entries']]}")


if __name__ == '__main__':
    asyncio.run(main())
