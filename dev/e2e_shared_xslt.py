"""Shared XSLTs (xsl:import) against the local Stroom stack (see dev/stroom).

    uv run python dev/e2e_shared_xslt.py

An environment keeps common steps in shared XSLT documents that its translations and indexing XSLTs import by
name: here one writing Event/Meta from the stream's GUID and EventSource/Device from a MyHostName header (both read
with stroom:meta()), and one writing a "stroom" object into indexed documents. A sibling pipeline of each template
already calls them. As the agent would:

1. describe_template finds the sibling's imports, reads each shared XSLT by name, and reports each named template
   called: where (Meta, EventSource/Device, stroom), what it writes and reads, and the parameters passed.
2. A translation mapping using the shared templates and still mapping EventSource/Device is refused: Device would
   be written twice. Without it, the XSLT imports and calls both, Stroom steps it clean, and the Events are valid,
   with one Device whose HostName is the header and one Meta holding the GUID.
2b. Its mapping lost (the Documentation tab cleared): rebuild_mapping reads it back from the XSLT and the shared XSLT
   it imports, fetched by name: each template, where it is called and the parameter passed, as they were.
3. The index plan takes stroom.feed from the shared indexing XSLT itself; the indexing XSLT calls the shared
   template instead of writing it, and the documents get one stroom object.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from e2e_generator import MAPPINGS  # noqa: E402
from config import Settings  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import (feeds, generation, indexing, pipeline_writes, processing_writes, rebuild, stepping, templates,  # noqa: E402
                   translation)
from utils.mappingstore import read_mapping  # noqa: E402
from tools.pipeline_writes import PropertyValue  # noqa: E402
from tools import validation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import FieldPlan  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402
from utils.xsltgen import SharedTemplate, TranslationMapping  # noqa: E402

TEXT_TEMPLATE, ES_TEMPLATE = 'E2E Shared Text', 'E2E Shared Events to Elasticsearch'
COMMON_EVENT, COMMON_ELASTIC = 'E2E-Common-Event-V1', 'E2E-Common-Elastic-V1'

SHARED_EVENT = """<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xmlns="event-logging:3" xmlns:stroom="stroom" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <!-- The stream's GUID, for tracing an event back to what was received. -->
  <xsl:template name="eventMeta">
    <Meta ContentType="stroom-guid"><xsl:value-of select="stroom:meta('GUID')" /></Meta>
  </xsl:template>
  <!-- The device that sent the stream, from the collector's MyHostName header; its IP when the record has one. -->
  <xsl:template name="eventSourceDevice">
    <xsl:param name="ip" />
    <Device>
      <HostName><xsl:value-of select="stroom:meta('MyHostName')" /></HostName>
      <xsl:if test="$ip"><IPAddress><xsl:value-of select="$ip" /></IPAddress></xsl:if>
    </Device>
  </xsl:template>
</xsl:stylesheet>
"""
SHARED_ELASTIC = """<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xmlns="http://www.w3.org/2005/xpath-functions" xmlns:stroom="stroom"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:template name="stroomFields">
    <map key="stroom"><string key="feed"><xsl:value-of select="stroom:meta('Feed')" /></string></map>
  </xsl:template>
</xsl:stylesheet>
"""
# The siblings: written by hand, as an existing environment's would be. Only read, never run.
SIBLING_EVENT = f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="records:2" xmlns="event-logging:3" xmlns:stroom="stroom"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:import href="{COMMON_EVENT}" />
  <xsl:template match="records"><Events><xsl:apply-templates /></Events></xsl:template>
  <xsl:template match="record">
    <Event>
      <xsl:call-template name="eventMeta" />
      <EventTime><TimeCreated><xsl:value-of select="data[@name='when']/@value" /></TimeCreated></EventTime>
      <EventSource>
        <System><Name>Other</Name><Environment>Dev</Environment></System>
        <Generator>other</Generator>
        <xsl:call-template name="eventSourceDevice">
          <xsl:with-param name="ip" select="data[@name='src']/@value" />
        </xsl:call-template>
      </EventSource>
      <EventDetail><TypeId>Other</TypeId><Unknown /></EventDetail>
    </Event>
  </xsl:template>
</xsl:stylesheet>
"""
SIBLING_ELASTIC = f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="event-logging:3" xmlns="http://www.w3.org/2005/xpath-functions"
    xmlns:stroom="stroom" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:import href="{COMMON_ELASTIC}" />
  <xsl:template match="/Events"><array><xsl:apply-templates select="Event" /></array></xsl:template>
  <xsl:template match="Event">
    <map>
      <number key="StreamId"><xsl:value-of select="@StreamId" /></number>
      <xsl:call-template name="stroomFields" />
    </map>
  </xsl:template>
</xsl:stylesheet>
"""


async def _folder(stroom: StroomGateway) -> dict:
    found = await stroom.post('/explorer/v2/find', {
        'filter': {'includedTypes': ['Folder'], 'nameFilter': 'Template Pipelines', 'requiredPermissions': ['VIEW']},
        'pageRequest': {'offset': 0, 'length': 5}})
    return found['values'][0]['docRef']


async def _named(stroom: StroomGateway, name: str, doc_type: str) -> dict | None:
    return next((v['docRef'] for v in await stroom.find_all_documents(name, [doc_type])
                 if v['docRef']['name'] == name), None)


async def shared_doc(stroom: StroomGateway, name: str, text: str) -> None:
    """A shared XSLT, outside any build, kept with this text (Stroom resolves an import by the document's name)."""
    ref = await _named(stroom, name, 'XSLT')
    if ref is None:
        node = await stroom.post('/explorer/v2/create', {'docType': 'XSLT', 'docName': name,
                                                         'destinationFolder': await _folder(stroom),
                                                         'permissionInheritance': 'DESTINATION'})
        ref = node.get('docRef', node)
    doc = await stroom.get_doc('XSLT', ref['uuid'])
    if doc.get('data') != text:
        doc['data'] = text
        await stroom.request('PUT', f"/xslt/v1/{ref['uuid']}", doc)


async def fixture_template(stroom: StroomGateway, name: str, source: str, elastic: bool = False) -> dict:
    """A template of our own (a copy of a standard one), so its children are only this suite's."""
    ref = await _named(stroom, name, 'Pipeline')
    if ref is None:
        node = await stroom.post('/explorer/v2/create', {'docType': 'Pipeline', 'docName': name,
                                                         'destinationFolder': await _folder(stroom),
                                                         'permissionInheritance': 'DESTINATION'})
        ref = node.get('docRef', node)
    doc = await stroom.get(f"/pipeline/v1/{ref['uuid']}")
    if ((doc.get('pipelineData') or {}).get('elements') or {}).get('add'):
        ref = {'type': 'Pipeline', 'uuid': ref['uuid'], 'name': name}     # filled by an earlier run
        return await _validated(stroom, ref) if elastic else ref
    data = (await stroom.get(f"/pipeline/v1/{(await _named(stroom, source, 'Pipeline'))['uuid']}"))['pipelineData']
    if elastic:
        data = json.loads(json.dumps(data).replace('"indexingFilter"', '"elasticIndexingFilter"')
                          .replace('"IndexingFilter"', '"ElasticIndexingFilter"'))
    doc['pipelineData'], doc['description'] = data, 'Fixture for dev/e2e_shared_xslt.py'
    await stroom.request('PUT', f"/pipeline/v1/{ref['uuid']}", doc)
    ref = {'type': 'Pipeline', 'uuid': ref['uuid'], 'name': name}
    return await _validated(stroom, ref) if elastic else ref


async def _validated(stroom: StroomGateway, ref: dict) -> dict:
    from e2e_elastic_handover import with_json_schema_filter
    return await with_json_schema_filter(stroom, ref)


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
        await run(ctx, stroom, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


async def run(ctx, stroom: StroomGateway, stamp: str) -> None:
    print('### shared XSLTs, and sibling pipelines that use them')
    await shared_doc(stroom, COMMON_EVENT, SHARED_EVENT)
    await shared_doc(stroom, COMMON_ELASTIC, SHARED_ELASTIC)
    text_template = await fixture_template(stroom, TEXT_TEMPLATE, 'Event Data (Text)')
    es_template = await fixture_template(stroom, ES_TEMPLATE, 'Indexing', elastic=True)
    src = f'e2e-shared-src-{stamp}'
    sibling = await translation.create_xslt(ctx, src, f'E2E-OTHER-{stamp}-Events', SIBLING_EVENT)
    sibling_tc = await translation.create_text_converter(ctx, src, f'E2E-OTHER-{stamp}', *e2e.CASES['csv']['converter'])
    await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=src, name=f'E2E-OTHER-{stamp}-Events',
                    template_uuid=text_template['uuid'],
                    set_properties=[PropertyValue(element='dsParser', name='textConverter', doc_uuid=sibling_tc['uuid'],
                                                  doc_type='TextConverter'),
                                    PropertyValue(element='translationFilter', name='xslt', doc_uuid=sibling['uuid'],
                                                  doc_type='XSLT')])
    sibling_index = await translation.create_xslt(ctx, src, f'e2e-other-{stamp}-v1-XSLT', SIBLING_ELASTIC)
    await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=src, name=f'e2e-other-{stamp}-v1 - Indexing',
                    template_uuid=es_template['uuid'],
                    set_properties=[PropertyValue(element='xsltFilter', name='xslt', doc_uuid=sibling_index['uuid'],
                                                  doc_type='XSLT'),
                                    PropertyValue(element='elasticIndexingFilter', name='indexName', value='e2e-other-v1')])

    print('\n### 1. describe_template finds what the siblings import, and reads it by name')
    described = await templates.describe_template(ctx, text_template['uuid'])
    shared = {u['template']: u for u in described.get('shared_xslt') or [] if u.get('template')}
    device, meta = shared.get('eventSourceDevice') or {}, shared.get('eventMeta') or {}
    e2e.check(device.get('at') == ['EventSource/Device'] and meta.get('at') == ['Meta']
             and device.get('href') == COMMON_EVENT, f"the calls and where: {json.dumps(described.get('shared_xslt'))[:600]}")
    e2e.check(device.get('paths') == ['Device/HostName', 'Device/IPAddress'] and device.get('reads_meta') == ['MyHostName']
             and device.get('with_params') == {'ip': "data[@name='src']/@value"} and meta.get('reads_meta') == ['GUID'],
             'what each writes and reads, and the parameter the sibling passes')
    e2e.check(any(u.get('document_contents', {}).get('name') == COMMON_EVENT for u in described['shared_xslt']),
             'the shared document itself, by name and uuid, for describe_document')

    print('\n### 2. the translation: the shared templates called, Device not written twice')
    uses = [{'href': COMMON_EVENT, 'template': 'eventMeta', 'at': 'Meta'},
            {'href': COMMON_EVENT, 'template': 'eventSourceDevice', 'at': 'EventSource/Device',
             'with_params': {'ip': "data[@name='ip']/@value"}}]
    both = TranslationMapping.model_validate({**MAPPINGS['csv'], 'shared': uses})
    refused = await generation.build_translation_xslt(ctx, both)
    e2e.check(not refused['ok'] and any('EventSource/Device is written by the shared template eventSourceDevice' in p
                                       for p in refused['problems']),
             f"mapping Device as well is refused: {[p[:120] for p in refused['problems']]}")
    mapping = TranslationMapping.model_validate({
        **MAPPINGS['csv'], 'shared': uses,
        'common': [f for f in MAPPINGS['csv']['common'] if not f['path'].startswith('EventSource/Device')]})
    build, feed = f'e2e-shared-{stamp}', f'E2E-SHARED-{stamp}'
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, e2e.CASES['csv']['sample'],
                                     headers={'MyHostName': 'collector-01', 'GUID': f'guid-{stamp}'}))['stream_id']
    saved = await generation.build_translation_xslt(ctx, mapping, build=build, name=f'{feed}-Events', include_xslt=True)
    e2e.check(saved['ok'] and saved.get('saved'), f"saved: {saved.get('problems')}")
    sheet = etree.fromstring(saved['xslt'].encode())
    ns = {'xsl': 'http://www.w3.org/1999/XSL/Transform', 'e': 'event-logging:3'}
    e2e.check(etree.QName(sheet[0]).localname == 'import' and sheet[0].get('href') == COMMON_EVENT
             and sheet.find('.//e:Device', ns) is None and sheet.find('.//e:Meta', ns) is None,
             'the XSLT imports the shared one and writes neither Device nor Meta itself')
    tc = await translation.create_text_converter(ctx, build, feed, *e2e.CASES['csv']['converter'])
    pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                               template_uuid=text_template['uuid'], set_properties=[
                                   PropertyValue(element='dsParser', name='textConverter', doc_uuid=tc['uuid'],
                                                 doc_type='TextConverter'),
                                   PropertyValue(element='translationFilter', name='xslt',
                                                 doc_uuid=saved['saved']['uuid'], doc_type='XSLT')])
    sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    e2e.check(sample['verdict'] == 'clean', f"stepped {sample['records_stepped']} records clean: "
                                           f"{[(g['class'], g.get('message')) for g in sample['groups']]}")
    output = (await stepping.step_pipeline(ctx, pipeline['uuid'], raw, 0))['elements']['translationFilter']['output']
    events = etree.fromstring(output.encode())
    event = events.find('e:Event', ns)
    valid = await validation.validate_events(ctx, output)
    e2e.check(valid['valid'], f"valid against {valid['schema']}: {valid.get('errors')}")
    e2e.check(len(event.findall('e:EventSource/e:Device', ns)) == 1
             and event.findtext('e:EventSource/e:Device/e:HostName', namespaces=ns) == 'collector-01'
             and event.findtext('e:EventSource/e:Device/e:IPAddress', namespaces=ns) == '10.0.0.1',
             "one Device, from the shared template: the header's host name and the record's IP")
    e2e.check(event.findtext('e:Meta', namespaces=ns) == f'guid-{stamp}' and len(event.findall('e:Meta', ns)) == 1,
             "one Meta, holding the stream's GUID")

    print('\n### 2b. its mapping lost: rebuilt from the XSLT and the shared XSLT it imports, read by name')
    cleared = await stroom.get_doc('XSLT', saved['saved']['uuid'])
    cleared['description'] = ''
    await stroom.put_doc(cleared)
    restored = await e2e.agreed(rebuild.rebuild_mapping, ctx=ctx, uuid=saved['saved']['uuid'])
    # The mapping's own xpath entry (TypeId, a concat) is the only one kept as xpath.
    e2e.check(bool(restored.get('saved')) and restored['mode'] == 'mapping lost' and 'differences' not in restored
              and all('TypeId' in x for x in restored['rebuilt'].get('kept_as_xpath') or []),
              f"rebuilt, proven on {restored['proven_on']['records']} records of {restored['proven_on']['which']}")
    back = read_mapping((await stroom.get_doc('XSLT', saved['saved']['uuid']))['description'])[1]['mapping']
    e2e.check([{k: v for k, v in s.items() if v} for s in back.get('shared') or []] == uses,
              f"each shared template, where it is called and the parameter passed: {back.get('shared')}")
    again = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    e2e.check(again['verdict'] == 'clean', 'the XSLT saved from the rebuilt mapping steps clean')

    print('\n### 3. the index: the shared indexing template called, its fields planned from it')
    await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'], stream_ids=[raw])
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [raw])
    e2e.check(done['gate'] == 'pass', f"one Events stream: {done['streams']}")
    events_ids = done['streams'][0]['events']
    described = await templates.describe_template(ctx, es_template['uuid'])
    use = next(u for u in described['shared_xslt'] if u.get('template') == 'stroomFields')
    e2e.check(use['at'] == ['stroom'] and use['paths'] == ['stroom/feed'], f"the sibling's call: {use}")
    index = f'e2e-shared-{stamp}-v1'
    draft = await e2e.agreed(indexing.draft_index_mapping, ctx=ctx, backend='elasticsearch', index_name=index,
                             convention='ecs', events_stream_ids=events_ids, without_example=True,
                             shared=[SharedTemplate(href=use['href'], template='stroomFields', at='stroom')])
    plan = FieldPlan.model_validate(draft['plan'])
    e2e.check(any(f.name == 'stroom.feed' and f.type == 'keyword' for f in plan.fields) and plan.required() == [],
             "stroom.feed planned from the shared XSLT's own text")
    e2e.check('<xsl:call-template name="stroomFields" />' in plan.xslt() and 'key="stroom"' not in plan.xslt(),
             'the indexing XSLT calls it instead of writing the field')
    xslt = await translation.save_xslt(ctx, build, f'{index}-XSLT', index_plan=plan)
    indexer = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=build, name=f'{index} - Indexing',
                              template_uuid=es_template['uuid'], xslt_uuid=xslt['uuid'], index_name=index,
                              cluster_uuid=(await _named(stroom, 'E2E_LOCAL_ES', 'ElasticCluster'))['uuid'])
    stepped = await stepping.step_sample(ctx, indexer['uuid'], events_ids)
    e2e.check(stepped['verdict'] == 'clean', f"the indexing pipeline steps clean: {stepped['verdict']}")
    documents = await indexing._documents(ctx, indexer['uuid'], events_ids, 5)
    e2e.check(all(d.get('stroom') == {'feed': ('string', feed)} for d in documents) and documents,
             f"each document gets one stroom object from the shared template: {json.dumps(documents[:1], default=str)[:300]}")
    template = plan.elastic_template(index)['body']['template']['mappings']['properties']
    e2e.check(template['stroom']['properties']['feed'] == {'type': 'keyword'}, 'and the index template maps it')


if __name__ == '__main__':
    asyncio.run(main())
