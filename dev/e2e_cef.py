"""End to end on the local stack: CEF for ArcSight, sent through Kafka, from Events.

    uv run python dev/e2e_cef.py

Events go to a build's feed; draft_cef_mapping drafts and saves the CEF XSLT (keys in ArcSight's dictionary only);
with no forwarding template, create_pipeline builds a pipeline of its own (XMLParser, SplitFilter one Event a record,
XSLTFilter, SchemaFilter on kafka-records:1, StandardKafkaProducer with a KafkaConfig made here for the test);
stepping it gives the CEF lines (nothing is sent: the stack has no Kafka); draft_cef_mapping reviews them, and
write_documentation documents them to the field. Then, with an environment around it:

5. A CEF pipeline written by hand in a production folder (lines of text, an unescaped | in a header, a key outside
   the dictionary): the review finds both, from the mapping its lines imply; a draft lists it among the existing CEF
   pipelines and its XSLT among those writing CEF.
6. An AGENTS doc in that folder decides: keys outside the dictionary allowed (so no question), the topic, two
   mappings (one to a key outside the dictionary), and the pipeline template, its own; a header override too.
7. Saved as lines of text, and a pipeline from the template the AGENTS doc names: stepped clean, reviewed as text.
8. The Kafka XSLT changed (one override, with its reason): pending until promotion, which writes one version line
   for both of its changes, and a version control row in each doc.
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
from tools import builds, cef, explorer, feeds, pipeline_writes, stepping, templates  # noqa: E402
from tools.pipeline_writes import PropertyValue, _set_property  # noqa: E402
from utils import versionlog, xsltversion  # noqa: E402
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

    await environment(ctx, stroom, stamp, build, events_id, xslt, pipeline, doc)


# Written by hand, as a CEF pipeline in production might be: the description's | left unescaped in the header, and a
# key outside ArcSight's dictionary.
HAND_XSLT = """<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xpath-default-namespace="event-logging:3"
    version="2.0">
  <xsl:output method="text"/>
  <xsl:template match="/Events"><xsl:apply-templates select="Event"/></xsl:template>
  <xsl:template match="Event">
    <xsl:value-of select="concat('CEF:0|Delinea|SecretServer|11.4|', EventDetail/TypeId, '|', EventDetail/Description,
                          '|3|suser=', EventSource/User/Id, ' dvchost=', EventSource/Device/HostName,
                          ' myCustomField1=', EventSource/System/Environment)"/>
    <xsl:text>&#10;</xsl:text>
  </xsl:template>
</xsl:stylesheet>
"""
TEXT_CHAIN = [('Source', 'Source'), ('xmlParser', 'XMLParser'), ('xsltFilter', 'XSLTFilter'),
              ('textWriter', 'TextWriter'), ('streamAppender', 'StreamAppender')]


async def _create(stroom: StroomGateway, doc_type: str, name: str, folder: dict) -> dict:
    node = await stroom.post('/explorer/v2/create', {'docType': doc_type, 'docName': name, 'destinationFolder': folder,
                                                     'permissionInheritance': 'DESTINATION'})
    return node.get('docRef', node)


async def environment(ctx, stroom: StroomGateway, stamp: str, build: str, events_id: int, kafka_xslt: dict,
                      kafka_pipeline: dict, kafka_doc: dict) -> None:
    print('\n### 5. a CEF pipeline written by hand in production: reviewed, and found by the draft')
    system = await guard_from(ctx).system_node()
    top = next((v['docRef'] for v in (await stroom.find_documents('E2E CEF Production', ['Folder'], 10)).get('values') or []
                if v['docRef']['name'] == 'E2E CEF Production'), None)
    top = top or await _create(stroom, 'Folder', 'E2E CEF Production', {k: v for k, v in system.items() if not k.startswith('_')})
    folder = await _create(stroom, 'Folder', f'vault-{stamp}', top)
    folder_path = f'System/E2E CEF Production/vault-{stamp}'
    hand = await stroom.get_doc('XSLT', (await _create(stroom, 'XSLT', f'Vault CEF {stamp}', folder))['uuid'])
    hand['data'] = HAND_XSLT
    hand = await stroom.put_doc(hand)
    template_name = f'E2E CEF to file {stamp}'
    fixture = await stroom.get_doc('Pipeline', (await _create(stroom, 'Pipeline', template_name, folder))['uuid'])
    data = {'elements': {'add': [{'id': e, 'type': t} for e, t in TEXT_CHAIN]},
            'links': {'add': [{'from': x, 'to': y} for (x, _), (y, _) in zip(TEXT_CHAIN, TEXT_CHAIN[1:])]}}
    _set_property(data, 'xsltFilter', 'xslt', {'entity': {'type': 'XSLT', 'uuid': hand['uuid'], 'name': hand['name']}})
    fixture['pipelineData'] = data
    fixture = await stroom.put_doc(fixture)
    reviewed = await cef.draft_cef_mapping(ctx, [events_id], custom_keys=False, pipeline_uuid=fixture['uuid'])
    found = ' '.join(f"{p}" for p in reviewed['review']['problems'])
    e2e.check(reviewed['plan_from'].startswith('the mapping its lines imply'), reviewed['plan_from'][:80])
    e2e.check('unescaped |' in found and "'myCustomField1' is not in ArcSight's CEF dictionary" in found
              and "aren't allowed" in found, f"the review finds the hand-written faults: {found[:200]}")
    drafted = await cef.draft_cef_mapping(ctx, [events_id], custom_keys=False)
    existing = [p['name'] for p in drafted['templates'].get('existing_cef_pipelines') or []]
    writing = [x['name'] for x in drafted['templates'].get('xslts_writing_cef') or []]
    e2e.check(template_name in existing and hand['name'] in writing,
              f"found by the draft: pipelines {existing[:4]}, XSLTs writing CEF {writing[:4]}")

    print('\n### 6. an AGENTS doc decides: custom keys, topic, mappings, the template')
    agents = await _create(stroom, 'Documentation', 'AGENTS', folder)
    agents_doc = await stroom.get_doc('Documentation', agents['uuid'])
    agents_doc['data'] = (f"# CEF output\n\n"
                          f"- EventDetail/View/Resource/Name -> cs4 (label: 'Secret name')\n"
                          f"- EventDetail/View/Data[@Name='folder']/@Value -> secretFolder (label: 'Folder')\n\n"
                          f"Custom CEF keys are allowed.\n"
                          f"Kafka topic: arcsight-vault-{stamp}\n"
                          f"Pipeline template: {template_name}\n")
    await stroom.put_doc(agents_doc)
    # Stroom's explorer finds a new doc once its tree is rebuilt, a moment after the change.
    from tools.instructions import applicable_instructions
    for _ in range(30):
        if (await applicable_instructions(ctx, [folder_path], [])).get('instructions'):
            break
        await asyncio.sleep(1)
    told = await cef.draft_cef_mapping(ctx, [events_id], folders=[folder_path], output='text',
                                       header={'vendor': 'Acme Vault'})
    said = told.get('from_standing_instructions') or {}
    e2e.check(told.get('status') != 'needs_guidance' and told['custom_keys'] is True and said.get('custom_keys') is True,
              "keys outside the dictionary allowed by the AGENTS doc: the user isn't asked")
    e2e.check(said.get('topic') == f'arcsight-vault-{stamp}' and len(said.get('mappings') or []) == 2,
              f"from the AGENTS doc: {said}")
    named = (told['templates'].get('from_instructions') or {})
    e2e.check([f['name'] for f in named.get('found') or []] == [template_name], f"the template it names: {named}")
    view = next(k for k in told['plan']['events'] if k.startswith('View'))
    keys = {f['path']: f['key'] for f in told['plan']['events'][view]}
    e2e.check(keys.get('EventDetail/View/Resource/Name') == 'cs4'
              and keys.get("EventDetail/View/Data[@Name='folder']/@Value") == 'secretFolder',
              f"the AGENTS mappings, one outside the dictionary: {keys}")
    e2e.check((told.get('example_line') or '').startswith('CEF:0|Acme Vault|SecretServer|'),
              f"the header override: {(told.get('example_line') or '')[:60]}")

    print('\n### 7. saved as text; a pipeline from the template the AGENTS doc names')
    saved = await cef.draft_cef_mapping(ctx, [events_id], folders=[folder_path], output='text',
                                        header={'vendor': 'Acme Vault'}, build=build, name=f'CEF-{stamp}-Text')
    e2e.check(bool(saved.get('saved')) and 'kafkaRecord' not in saved['plan'].get('output', ''),
              f"saved, as {saved['plan'].get('output')}: {saved.get('saved')}")
    text_pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'CEF-{stamp}-Text',
                                     template_uuid=fixture['uuid'], set_properties=[
                                         PropertyValue(element='xsltFilter', name='xslt', doc_uuid=saved['saved']['uuid'],
                                                       doc_type='XSLT')])
    step = await stepping.step_sample(ctx, text_pipeline['uuid'], [events_id])
    e2e.check(step['verdict'] == 'clean', f"the text pipeline steps clean: {step['verdict']} {step.get('groups', [])[:1]}")
    as_text = await cef.draft_cef_mapping(ctx, [events_id], folders=[folder_path], pipeline_uuid=text_pipeline['uuid'])
    problems = as_text['review']['problems']
    e2e.check(as_text['review']['lines'] == 2 and all('secretFolder' in str(p) for p in problems),
              f"reviewed: {as_text['review']['lines']} text lines, problems {problems}")
    text_doc = await builds.write_documentation(ctx, build, text_pipeline['uuid'],
                                                f"# CEF-{stamp}-Text\n\n## Purpose and data\n\nSecretServer Events as CEF "
                                                f"lines, for the vault team.\n", 'Created', stream_ids=[events_id])
    e2e.check(bool(text_doc.get('uuid')), 'documented')

    print('\n### 8. the Kafka XSLT changed, and promoted: one version line for its changes')
    changed = await cef.draft_cef_mapping(ctx, [events_id], custom_keys=False, uuid=kafka_xslt['uuid'],
                                          overrides=[{'path': "EventDetail/Authenticate/Data[@Name='session_id']/@Value",
                                                      'key': 'cn2', 'label': 'Session'}],
                                          change='Session id as cn2, asked for by the SOC')
    e2e.check(bool(changed.get('saved')), f"changed: {changed.get('applied')}")
    described = await explorer.describe_document(ctx, 'XSLT', kafka_xslt['uuid'])
    e2e.check(described['pending_changes'] == ['Created from its CEF plan', 'Session id as cn2, asked for by the SOC']
              and described['kept_mapping']['kind'] == 'cef', f"pending: {described['pending_changes']}")
    step = await stepping.step_sample(ctx, kafka_pipeline['uuid'], [events_id])
    e2e.check(step['verdict'] == 'clean', 'the Kafka pipeline steps clean again')
    await builds.write_documentation(ctx, build, kafka_pipeline['uuid'],
                                     f"# CEF-{stamp}-Kafka\n\n## Purpose and data\n\nSecretServer Events to ArcSight "
                                     f"as CEF.\n", 'Session id as cn2', stream_ids=[events_id])
    destination = await _create(stroom, 'Folder', f'promoted-{stamp}', folder)
    where = f'{folder_path}/promoted-{stamp}'
    result = await e2e.agreed(builds.promote_build, ctx=ctx, build=build,
                              destinations={t: where for t in ('Feed', 'Pipeline', 'XSLT', 'Documentation', 'KafkaConfig')})
    e2e.check(len(result['promoted']) >= 7, f"promoted {len(result['promoted'])} documents")
    history = xsltversion.rows((await stroom.get_doc('XSLT', kafka_xslt['uuid']))['data'])
    e2e.check(len(history) == 1 and 'Session id as cn2' in history[0]['change'] and 'Created' in history[0]['change']
              and history[0]['how'] == 'agent', f"the XSLT's version history: {history}")
    rows = versionlog.rows_of((await stroom.get_doc('Documentation', kafka_doc['uuid']))['data'])
    e2e.check(len(rows) == 1 and 'Session id as cn2' in str(rows[0]), f"the doc's version control: {rows}")


if __name__ == '__main__':
    asyncio.run(main())
