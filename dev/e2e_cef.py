"""End to end on the local stack: CEF for ArcSight, sent through Kafka, from Events.

    uv run python dev/e2e_cef.py

Events go to a build's feed; draft_cef_mapping drafts and saves the CEF XSLT (keys in ArcSight's dictionary only);
with no forwarding template, create_pipeline builds a pipeline of its own (XMLParser, SplitFilter one Event a record,
XSLTFilter, SchemaFilter on kafka-records:1, StandardKafkaProducer with a KafkaConfig made here for the test);
stepping it gives the CEF lines (nothing is sent: the stack has no Kafka); draft_cef_mapping reviews them, and
write_documentation documents them to the field.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from security.guard import guard_from  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import builds, cef, feeds, pipeline_writes, stepping, templates  # noqa: E402
from tools.pipeline_writes import PropertyValue  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

EVENTS = '''<?xml version="1.1" encoding="UTF-8"?>
<Events xmlns="event-logging:3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" Version="3.5.2" xsi:schemaLocation="event-logging:3 file://event-logging-v3.5.2.xsd">
<Event><EventTime><TimeCreated>2026-09-27T23:48:10.000Z</TimeCreated></EventTime>
 <EventSource><System><Name>SecretServer</Name><Environment>Prod</Environment><Organisation>Delinea</Organisation><Version>11.4</Version></System><Generator>SecretServer Audit</Generator>
  <Device><HostName>ss-web-02</HostName><IPAddress>10.1.2.3</IPAddress></Device><Client><IPAddress>192.0.2.45</IPAddress></Client><User><Id>priya.patel</Id></User></EventSource>
 <EventDetail><TypeId>User-Login</TypeId><Description>User logged in</Description><Authenticate><Action>Logon</Action><User><Id>priya.patel</Id></User><Outcome><Success>true</Success></Outcome>
  <Data Name="session_id" Value="6857"/><Data Name="ticket" Value="CHG-3000"/></Authenticate></EventDetail></Event>
<Event><EventTime><TimeCreated>2026-09-27T23:56:15.000Z</TimeCreated></EventTime>
 <EventSource><System><Name>SecretServer</Name><Environment>Prod</Environment><Organisation>Delinea</Organisation><Version>11.4</Version></System><Generator>SecretServer Audit</Generator>
  <Device><HostName>ss-web-01</HostName></Device><Client><IPAddress>10.20.4.18</IPAddress></Client><User><Id>riley.chen</Id></User></EventSource>
 <EventDetail><TypeId>Secret-View</TypeId><Description>Secret viewed: a=b|c</Description><View><Resource><Type>Secret</Type><Name>Firewall - Edge Admin</Name><Id>10005</Id></Resource>
  <Data Name="folder" Value="Applications\\Finance"/></View></EventDetail></Event>
</Events>
'''


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
    build, feed = f'cef-{stamp}', f'CEF-EVENTS-{stamp}'
    print('\n### 1. Events in a build feed, and a KafkaConfig for the test')
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed, stream_type='Events')
    events_id = (await feeds.upload_sample(ctx, feed, EVENTS, stream_type='Events'))['stream_id']
    e2e.check(events_id is not None, f"Events stream {events_id}")
    folder = await guard_from(ctx).build_folder(build)
    node = await stroom.post('/explorer/v2/create', {'docType': 'KafkaConfig', 'docName': f'CEF-KAFKA-{stamp}',
                                                     'destinationFolder': {k: v for k, v in folder.items() if not k.startswith('_')},
                                                     'permissionInheritance': 'DESTINATION'})
    kafka = {'type': 'KafkaConfig', 'uuid': node['uuid'], 'name': node['name']}
    e2e.check(bool(kafka['uuid']), f"KafkaConfig {kafka['name']}")

    print('\n### 2. the user is asked about keys outside the dictionary; the draft keeps to it')
    asked = await cef.draft_cef_mapping(ctx, [events_id], topic='arcsight-cef')
    e2e.check(asked.get('status') == 'needs_guidance' and 'outside' in asked['question'],
              f"asked first: {asked.get('question', '')[:70]}")
    drafted = await cef.draft_cef_mapping(ctx, [events_id], custom_keys=False, topic='arcsight-cef')
    e2e.check(drafted['problems'] == [], f"no problems: {drafted['problems']}")
    line = drafted.get('example_line') or ''
    e2e.check(line.startswith('CEF:0|Delinea|SecretServer|11.4|User-Login|User logged in|3|') and 'act=Logon' in line
              and 'cn1Label=session_id' in line, f"example line: {line[:160]}")
    e2e.check('sample_check' not in drafted, f"its sample lines review clean: {drafted.get('sample_check')}")
    e2e.check('### Authenticate events' in drafted['documentation'] and 'deviceCustomNumber1' in drafted['documentation'],
              "the documentation tables name each key's ArcSight field and label")

    print('\n### 3. saved, and a pipeline of its own (no forwarding template here)')
    saved = await cef.draft_cef_mapping(ctx, [events_id], custom_keys=False, topic='arcsight-cef', build=build,
                                        name=f'CEF-{stamp}',
                                        overrides=[{'path': "EventDetail/Authenticate/Data[@Name='ticket']/@Value",
                                                    'key': 'deviceCustomString6', 'label': 'Change ticket'}])
    xslt = saved['saved']
    e2e.check(bool(xslt and xslt['uuid']), f"XSLT saved: {xslt}")
    e2e.check(any('ticket' in a and 'cs6' in a for a in saved.get('applied', [])), f"override applied: {saved.get('applied')}")
    forwarding = (await templates.find_pipeline_templates(ctx, 'forwarding'))['candidates']
    print(f"    forwarding templates here: {[c['name'] for c in forwarding]}")
    pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'CEF-{stamp}-Kafka',
                                standalone='kafka', set_properties=[
                                    PropertyValue(element='xsltFilter', name='xslt', doc_uuid=xslt['uuid'], doc_type='XSLT'),
                                    PropertyValue(element='standardKafkaProducer', name='kafkaConfig',
                                                  doc_uuid=kafka['uuid'], doc_type='KafkaConfig')])
    e2e.check(pipeline.get('chain', '').endswith('standardKafkaProducer'), f"pipeline {pipeline.get('name')}: {pipeline.get('chain')}")
    shape = await templates._shape(stroom, pipeline['uuid'])
    e2e.check(shape['stage'] == 'forwarding' and shape['backend'] == 'kafka', f"classed as {shape['stage']}/{shape['backend']}")

    print('\n### 4. stepped (nothing sent), reviewed, documented')
    step = await stepping.step_sample(ctx, pipeline['uuid'], [events_id])
    e2e.check(step['verdict'] == 'clean', f"stepped {step['records_stepped']} records: {step['verdict']} {step.get('groups', [])[:2]}")
    review = await cef.draft_cef_mapping(ctx, [events_id], custom_keys=False, pipeline_uuid=pipeline['uuid'])
    e2e.check(review['review']['lines'] == 2 and review['review']['problems'] == [],
              f"review: {review['review']['lines']} lines, problems {review['review']['problems']}")
    e2e.check(review['plan_from'].startswith('the plan kept with XSLT'), review['plan_from'])
    e2e.check(review['plan']['topic'] == 'arcsight-cef', f"topic {review['plan'].get('topic')}")
    doc = await builds.write_documentation(ctx, build, pipeline['uuid'],
                                           f"# CEF-{stamp}-Kafka\n\n## Purpose and data\n\nSecretServer Events to ArcSight as CEF.\n",
                                           'Created', stream_ids=[events_id])
    text = (await stroom.get_doc('Documentation', doc['uuid'])).get('data') or ''
    e2e.check('## Field mapping' in text and '### View events' in text and 'Change ticket' in text
              and 'CHG-3000' in text, f"documentation to the field: {len(text)} characters")


if __name__ == '__main__':
    asyncio.run(main())
