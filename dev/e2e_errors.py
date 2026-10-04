"""Invalid data, and how errors are resolved, accepted and documented, against the local Stroom stack and
Elasticsearch 9 (see dev/stroom).

    cd dev/stroom && docker compose --profile elastic up -d
    uv run python dev/e2e_errors.py

1. Invalid data the agent's own content mishandles: a result value the mapping does not cover gives an event that
   fails the schema. Stepping shows it, blocking; the agent fixes its mapping and steps again, clean, before
   anything goes to the user.
2. An error the agent cannot resolve in its own content: the template's decoration step (inherited) logs an error
   for service accounts it has no HR record for. Triage classes it for review; the user says it is benign; the
   agent records that with write_documentation (the user confirms). Stepping again, and the Error streams after
   processing, report it as benign with the user's reason; the documentation's Errors section lists it.
3. Invalid data for Elasticsearch: a record whose user is a value where an earlier one's was an object. Indexing
   in the workspace runs with batch size 10; Elasticsearch rejects the record, and triage reports which document
   and why (the batch size stays small). The user chooses to leave the field out; the plan changes, the stream is
   reprocessed, every record indexes, and the template's batch size is restored.
4. Raw data that is not well-formed XML: stepping fails in the parser, blocking, and says so; there is nothing in
   the agent's content to fix, which is what it tells the user.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from e2e_discovery import fixture_template as discovery_template  # noqa: E402
from e2e_elastic_handover import ES, _request, live_cluster  # noqa: E402
from e2e_generator import MAPPINGS  # noqa: E402
from config import Settings  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import builds, feeds, generation, indexing, pipeline_writes, processing_writes, stepping, streams, translation  # noqa: E402
from tools.pipeline_writes import PropertyValue  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import Discovery, FieldPlan  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402
from utils.xsltgen import TranslationMapping  # noqa: E402

DECORATED = 'E2E Decorated Text'
DECORATE_XSLT = 'E2E-Decorate-Users'
# The environment's decoration step: events pass through as they are; a user with no HR record is logged as an error.
DECORATE = """<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xmlns="event-logging:3" xpath-default-namespace="event-logging:3" xmlns:stroom="stroom"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:template match="@*|node()"><xsl:copy><xsl:apply-templates select="@*|node()" /></xsl:copy></xsl:template>
  <xsl:template match="Event">
    <xsl:if test="starts-with(EventSource/User/Id, 'svc-')">
      <xsl:value-of select="stroom:log('ERROR', concat('No HR record for user ', EventSource/User/Id))" />
    </xsl:if>
    <xsl:copy><xsl:apply-templates select="@*|node()" /></xsl:copy>
  </xsl:template>
</xsl:stylesheet>
"""
SAMPLE = ("time,user,host,ip,result\n"
          "2026-09-28T10:00:00,alice,ws01,10.0.0.1,ok\n"
          "2026-09-28T10:05:00,svc-backup,ws02,10.0.0.2,ok\n"
          "2026-09-28T10:07:30,carol,ws03,10.0.0.3,locked\n")


def groups_of(summary: dict) -> list[dict]:
    return summary.get('groups') or [g for v in summary.values() if isinstance(v, dict) for g in v.get('groups') or []]


async def _folder(stroom: StroomGateway) -> dict:
    found = await stroom.post('/explorer/v2/find', {
        'filter': {'includedTypes': ['Folder'], 'nameFilter': 'Template Pipelines', 'requiredPermissions': ['VIEW']},
        'pageRequest': {'offset': 0, 'length': 5}})
    return found['values'][0]['docRef']


async def _named(stroom: StroomGateway, name: str, doc_type: str) -> dict | None:
    return next((v['docRef'] for v in await stroom.find_all_documents(name, [doc_type])
                 if v['docRef']['name'] == name), None)


async def decorated_template(stroom: StroomGateway) -> dict:
    """Event Data (Text) with its decoration step set, as an environment's template has it: inherited by children."""
    xslt = await _named(stroom, DECORATE_XSLT, 'XSLT')
    if xslt is None:
        node = await stroom.post('/explorer/v2/create', {'docType': 'XSLT', 'docName': DECORATE_XSLT,
                                                         'destinationFolder': await _folder(stroom),
                                                         'permissionInheritance': 'DESTINATION'})
        xslt = node.get('docRef', node)
    doc = await stroom.get_doc('XSLT', xslt['uuid'])
    if doc.get('data') != DECORATE:
        doc['data'] = DECORATE
        await stroom.put_doc(doc)
    ref = await _named(stroom, DECORATED, 'Pipeline')
    if ref is None:
        node = await stroom.post('/explorer/v2/create', {'docType': 'Pipeline', 'docName': DECORATED,
                                                         'destinationFolder': await _folder(stroom),
                                                         'permissionInheritance': 'DESTINATION'})
        ref = node.get('docRef', node)
    pipeline = await stroom.get(f"/pipeline/v1/{ref['uuid']}")
    if not (pipeline.get('pipelineData') or {}).get('properties'):
        pipeline['parentPipeline'] = await _named(stroom, 'Event Data (Text)', 'Pipeline')
        pipeline['pipelineData'] = {'properties': {'add': [{'element': 'decorationFilter', 'name': 'xslt', 'value': {
            'entity': {'type': 'XSLT', 'uuid': xslt['uuid'], 'name': DECORATE_XSLT}}}]}}
        pipeline['description'] = 'Fixture for dev/e2e_errors.py'
        await stroom.request('PUT', f"/pipeline/v1/{ref['uuid']}", pipeline)
    return {'type': 'Pipeline', 'uuid': ref['uuid'], 'name': DECORATED}


async def own_and_accepted(ctx, stroom: StroomGateway, stamp: str) -> None:
    build, feed = f'e2e-errors-{stamp}', f'E2E-ERRORS-{stamp}'
    print('\n### 1. invalid data its own mapping mishandles: the agent resolves it first')
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, SAMPLE))['stream_id']
    template = await decorated_template(stroom)
    await translation.create_text_converter(ctx, build, feed, *e2e.CASES['csv'][ 'converter'])
    mapping = json.loads(json.dumps(MAPPINGS['csv']))
    logon = next(r for r in mapping['events'] if r['name'] == 'logon')
    success = next(f for f in logon['fields'] if f['path'].endswith('Outcome/Success'))
    success.pop('map')                      # the agent's mistake: the result column as it is, not a boolean
    saved = await generation.build_translation_xslt(ctx, TranslationMapping.model_validate(mapping), build=build,
                                                    name=f'{feed}-Events')
    e2e.check(saved['ok'], f"the translation saved from its mapping: {saved.get('problems')}")
    pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                template_uuid=template['uuid'])
    first = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    blocking = [g for g in first['groups'] if g['class'] == 'blocking']
    print('    ' + json.dumps([(g['element'], g['reason'], g['examples'][0]['message'][:120]) for g in first['groups']]))
    e2e.check(first['verdict'] == 'blocking' and blocking and all(g['element'] == 'schemaFilter' for g in blocking),
              "the logons' Outcome/Success ('ok', 'locked') fails the schema: blocking, and caused by the mapping")
    success['map'] = {'ok': 'true', 'locked': 'false'}       # the agent's fix, in its own mapping
    resaved = await generation.build_translation_xslt(ctx, TranslationMapping.model_validate(mapping), build=build,
                                                      uuid=saved['saved']['uuid'])
    e2e.check(resaved['ok'], 'the fixed mapping saved over the XSLT')
    second = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    left = [(g['class'], g['element'], g['own_element']) for g in second['groups']]
    e2e.check(not any(g['class'] == 'blocking' for g in second['groups']), f"after the fix nothing blocks: {left}")

    print('\n### 2. an error it cannot resolve: inherited; the user accepts it as benign')
    review = [g for g in second['groups'] if g['class'] == 'review']
    e2e.check(len(review) == 1 and review[0]['element'] == 'decorationFilter' and not review[0]['own_element']
              and 'No HR record for user svc-backup' in review[0]['examples'][0]['message'],
              "left for the user: the template's decoration step has no HR record for svc-backup")
    example = review[0]['examples'][0]['message']
    reason = 'Service accounts have no HR record; expected for svc- accounts'
    asked = await builds.write_documentation(ctx, build, pipeline['uuid'], '## Purpose and data\n\nCSV logons.\n\n'
                                             '## Field mapping\n\n(see the mapping)\n', 'Created',
                                             stream_ids=[raw], accept_errors=[{'element': 'decorationFilter', 'example': example, 'reason': reason,
                                                             'matches': 'No HR record for user svc-*'}])
    e2e.check(asked.get('status') == 'needs_confirmation' and 'benign' in asked['summary'] and reason in json.dumps(asked['details']),
              f"the user confirms what is recorded: {asked.get('summary')}")
    await e2e.agreed(builds.write_documentation, ctx=ctx, build=build, pipeline_uuid=pipeline['uuid'],
                     markdown='## Purpose and data\n\nCSV logons.\n\n## Field mapping\n\n(see the mapping)\n',
                     change='Created', stream_ids=[raw],
                     accept_errors=[{'element': 'decorationFilter', 'example': example, 'reason': reason,
                                     'matches': 'No HR record for user svc-*'}])
    third = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    hr = [g for g in third['groups'] if g['element'] == 'decorationFilter']
    e2e.check(third['verdict'] == 'clean' and hr and hr[0]['class'] == 'benign' and hr[0].get('accepted')
              and reason in hr[0]['reason'], f"stepped again: reported as benign, with the user's reason: {hr[0]['reason'] if hr else hr}")

    print('\n### processed: the Error stream says the same, and the documentation lists it')
    await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'], stream_ids=[raw])
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [raw])
    e2e.check(done['streams'][0]['events'] and done['streams'][0]['errors'], f"Events, and an Error stream: {done['streams']}")
    summary = await streams.summarise_errors(ctx, raw)
    groups = groups_of(summary)
    e2e.check(groups and all(g['class'] == 'benign' and g.get('accepted') for g in groups),
              f"its errors are the accepted kind, reported as benign: {[(g['class'], g['reason'][:60]) for g in groups]}")
    written = await builds.write_documentation(ctx, build, pipeline['uuid'], '## Purpose and data\n\nCSV logons.\n\n'
                                               '## Field mapping\n\n(see the mapping)\n', 'Processed the sample',
                                               stream_ids=[raw])
    doc = await stroom.get_doc('Documentation', written['uuid'])
    text = doc.get('data') or ''
    errors = text.split('## Errors')[1].split('\n## ')[0] if '## Errors' in text else ''
    print('    ' + errors.strip().replace('\n', '\n    ')[:900])
    e2e.check('| benign | `decorationFilter` |' in errors and 'No HR record for user svc-backup' in errors
              and reason in errors and 'stroom-mcp accepted errors' in errors,
              "the Errors section lists the error, as accepted, with the reason; kept for the next review")


async def elastic(ctx, stroom: StroomGateway, es: httpx.AsyncClient, stamp: str) -> None:
    print('\n### 3. invalid data for Elasticsearch: small batches, the rejection reported, fixed, batch size restored')
    build, feed, index = f'e2e-errors-es-{stamp}', f'E2E-ERRORS-ES-{stamp}', f'e2e-errors-{stamp}-v1'
    records = [{'ts': '2026-10-02T08:00:00Z', 'user': {'name': 'alice'}, 'action': 'login'},
               {'ts': '2026-10-02T08:01:00Z', 'user': 'bob', 'action': 'login'},
               {'ts': '2026-10-02T08:02:00Z', 'user': {'name': 'carol'}, 'action': 'logout'}]
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, json.dumps(records)))['stream_id']
    template, cluster = await discovery_template(stroom), await live_cluster(stroom)
    plan = FieldPlan.model_validate((await indexing.draft_index_mapping(
        ctx, 'elasticsearch', index, discovery=Discovery(timestamp_field='ts')))['plan'])
    xslt = await translation.save_xslt(ctx, build, f'{index}-XSLT', index_plan=plan)
    pipeline = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=build, name=f'{index} - Discovery',
                                template_uuid=template['uuid'], xslt_uuid=xslt['uuid'], index_name=index,
                                cluster_uuid=cluster['uuid'])
    e2e.check((await stepping.step_sample(ctx, pipeline['uuid'], [raw]))['verdict'] == 'clean',
              'stepping cannot see it: the documents are fine until Elasticsearch maps them')
    final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], plan=plan,
                             events_stream_ids=[raw], example_template='PUT _index_template/e2e-errors-sibling\n' + json.dumps(
                                 {'index_patterns': ['e2e-errors-sibling*'], 'template': {'mappings': {'dynamic': True}}}))
    path, body = _request(final['dev_tools'])
    e2e.check((await es.put(f'/{path}', json=body)).status_code == 200, f'PUT {path}')

    async def batch_size() -> int | None:
        doc = await stroom.get_doc('Pipeline', pipeline['uuid'])
        own = [p for p in ((doc.get('pipelineData') or {}).get('properties') or {}).get('add') or []
               if p.get('name') == 'batchSize']
        return (own[0].get('value') or {}).get('integer') if own else None
    started = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                               stream_ids=[raw])
    e2e.check(started.get('batch_size') and await batch_size() == 10, f"started with batch size 10: {started.get('batch_size')}")
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [raw], expect_events=False,
                                                       filter_id=started['filter_id'])
    e2e.check(done['gate'] == 'fail' and await batch_size() == 10, 'an Error stream: the batch size stays small')
    groups = groups_of(await streams.summarise_errors(ctx, raw))
    message = json.dumps(groups)
    e2e.check('Elasticsearch rejected document 2 of 3' in message and 'A field is an object in some records' in message,
              'triage names the document Elasticsearch rejected, and why')
    print('\n### the user chooses to leave the field out: plan changed, reprocessed')
    fixed = FieldPlan.model_validate((await indexing.draft_index_mapping(
        ctx, 'elasticsearch', index, discovery=Discovery(timestamp_field='ts', drop=['user'])))['plan'])
    await translation.save_xslt(ctx, build, f'{index}-XSLT', index_plan=fixed, uuid=xslt['uuid'])
    e2e.check((await stepping.step_sample(ctx, pipeline['uuid'], [raw]))['verdict'] == 'clean', 'steps clean')
    try:
        await processing_writes.reprocess_streams(ctx, pipeline['uuid'], [raw])
        refused = ''
    except Exception as e:
        refused = str(e)
    e2e.check('The indexing XSLT changed since index template' in refused,
              'the XSLT changed, so the agreed template is checked again before reprocessing')
    rechecked = await e2e.agreed(indexing.check_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                                 template=final['dev_tools'], events_stream_ids=[raw])
    e2e.check(rechecked.get('agreed'), 'the same template still fits; the user confirms it again')
    asked = await processing_writes.reprocess_streams(ctx, pipeline['uuid'], [raw])
    note = asked['details']['already indexed']
    e2e.check(asked.get('status') == 'needs_approval' and f'POST {index}/_delete_by_query' in note,
              "the approval says Stroom will not remove the stream's earlier documents, and gives the request to")
    request = note[note.index('POST '):]
    path, body = request.split(' ', 2)[1], json.loads(request.split(' ', 2)[2])
    deleted = (await es.post(f'/{path}?refresh=true', json=body)).json().get('deleted')
    e2e.check(deleted == 2, f'the cluster admin deletes them first: {deleted} documents')
    again = await processing_writes.reprocess_streams(ctx, pipeline['uuid'], [raw], approval_id=asked['approval_id'])
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [raw], expect_events=False,
                                                       filter_id=again['filter_id'])
    e2e.check(done['gate'] == 'pass' and done.get('batch_size', '').startswith('restored') and await batch_size() is None,
              f"indexed without errors, and the template's batch size restored: {done.get('batch_size')}")
    await es.post(f'/{index}/_refresh')
    e2e.check((await es.get(f'/{index}/_count')).json()['count'] == 3, 'every record indexed')


async def broken_xml(ctx, stroom: StroomGateway, stamp: str) -> None:
    print('\n### 4. raw data that is not well-formed XML')
    build, feed = f'e2e-errors-xml-{stamp}', f'E2E-ERRORS-XML-{stamp}'
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, '<logons><logon><when>2026-09-28 12:00:00</when><who>frank</who>'
                                               '<host>ws06</host><result>SUCCESS</logon></logons>'))['stream_id']
    saved = await generation.build_translation_xslt(ctx, TranslationMapping.model_validate(MAPPINGS['xml']),
                                                    build=build, name=f'{feed}-Events')
    template = await _named(stroom, 'Event Data (XML)', 'Pipeline')
    props = [PropertyValue(element='translationFilter', name='xslt', doc_uuid=saved['saved']['uuid'], doc_type='XSLT')]
    try:
        await pipeline_writes.create_pipeline(ctx, f'{feed}-Events', template['uuid'], props, build=build)
        refused = ''
    except Exception as e:
        refused = str(e)
    e2e.check('XMLParser cannot read' in refused, f"caught first: the sample does not read as XML: {refused[:110]}")
    # The user insists the feed is XML: stepping then shows where it breaks.
    pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                template_uuid=template['uuid'], set_properties=props, accept_parser_mismatch=True)
    stepped = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    parser = [g for g in stepped['groups'] if g['class'] == 'blocking' and 'parser' in (g['element'] or '').lower()]
    print('    ' + json.dumps([(g['class'], g['element'], g['examples'][0]['message'][:120]) for g in stepped['groups']]))
    e2e.check(stepped['verdict'] == 'blocking' and parser and not parser[0]['own_element'],
              "blocking, in the template's XML parser: the data, not the agent's content, is at fault")


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
        await own_and_accepted(ctx, stroom, stamp)
        async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
            await elastic(ctx, stroom, es, stamp)
        await broken_xml(ctx, stroom, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
