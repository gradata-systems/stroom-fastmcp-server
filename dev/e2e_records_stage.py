"""A pipeline writing Records, built in a build, against the local Stroom stack (see dev/stroom).

    uv run python dev/e2e_records_stage.py

Asked for by the user: a translation's build subfolder is named for what it writes, Events or Records. Live, the feed
STROOM_PROXY-RECEIVE-EVENTS receives Raw Events, which 'Stroom Proxy Receive Events' (a child of 'Stroom Family App
Events Base', itself a child of 'Event Data (Text)') writes as Records: the feed's own stream type is Raw Events all
the same, so only the pipeline says. That template is mirrored here as a fixture, made once under Template Pipelines:
a child of 'Event Data (Text)' with the RECORDS schema group and the Records stream type.

1. The fixture is found as a records-stage template.
2. A build: a Raw Events feed and a CSV sample; its Data Splitter, saved before any pipeline, goes in Events.
3. An XSLT writing records:2 goes in Records.
4. The pipeline from the fixture goes in Records, and takes the Data Splitter with it.
5. Stepped in Stroom: clean, its records valid against the records schema.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from config import Settings  # noqa: E402
from security.guard import guard_from  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import feeds, generation, pipeline_writes, stepping, templates, translation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

FIXTURE = 'E2E Records (Text)'
SAMPLE = ('time,user,host,action\n'
          '2026-10-01T08:00:00Z,alice,ws01,start\n'
          '2026-10-01T08:05:00Z,bob,ws02,stop\n'
          '2026-10-01T08:07:00Z,carol,ws03,start\n')
# Records in, records out: the source's own fields as records:2 data, as a Records-writing translation does.
RECORDS_XSLT = '''<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns="records:2"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xpath-default-namespace="records:2" version="3.0">
  <xsl:template match="records">
    <records xsi:schemaLocation="records:2 file://records-v2.0.xsd" version="2.0">
      <xsl:apply-templates select="record" />
    </records>
  </xsl:template>
  <xsl:template match="record">
    <record>
      <xsl:for-each select="data[normalize-space(@value)]">
        <data name="{@name}" value="{@value}" />
      </xsl:for-each>
    </record>
  </xsl:template>
</xsl:stylesheet>
'''


async def fixture(stroom: StroomGateway) -> dict:
    """The Records template, as live's 'Stroom Family App Events Base' is: made once, beside the stock templates."""
    found = await stroom.find_documents(FIXTURE, ['Pipeline'], 5)
    existing = next((v['docRef'] for v in found.get('values') or [] if v['docRef']['name'] == FIXTURE), None)
    if existing:
        return existing
    text = next(v['docRef'] for v in (await stroom.find_documents('Event Data (Text)', ['Pipeline'], 5))['values']
                if v['docRef']['name'] == 'Event Data (Text)')
    parent = await stroom.post('/explorer/v2/find', {
        'filter': {'includedTypes': ['Folder'], 'nameFilter': 'Template Pipelines', 'requiredPermissions': ['VIEW']},
        'pageRequest': {'offset': 0, 'length': 5}})
    folder = parent['values'][0]['docRef'] if parent.get('values') else None
    node = await stroom.post('/explorer/v2/create', {'docType': 'Pipeline', 'docName': FIXTURE, 'destinationFolder': folder,
                                                     'permissionInheritance': 'DESTINATION'})
    ref = node.get('docRef', node)
    doc = await stroom.get_doc('Pipeline', ref['uuid'])
    doc['parentPipeline'] = {'type': 'Pipeline', 'uuid': text['uuid'], 'name': text['name']}
    doc['description'] = 'Fixture for dev/e2e_records_stage.py: writes Records, as live Stroom Family App Events Base'
    doc['pipelineData'] = {'properties': {'add': [
        {'element': 'schemaFilter', 'name': 'schemaGroup', 'value': {'string': 'RECORDS'}},
        {'element': 'streamAppender', 'name': 'streamType', 'value': {'string': 'Records'}}]}}
    await stroom.put_doc(doc)
    return {'type': 'Pipeline', 'uuid': ref['uuid'], 'name': FIXTURE}


async def main():
    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'])
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = e2e.run_stamp()
    try:
        print('\n### the Records template')
        template = await fixture(stroom)
        candidates = []
        for _ in range(15):     # the explorer search lists a new template after a short delay
            candidates = (await templates.find_pipeline_templates(ctx, 'records'))['candidates']
            if any(c['uuid'] == template['uuid'] for c in candidates):
                break
            await asyncio.sleep(2)
        e2e.check(any(c['uuid'] == template['uuid'] for c in candidates),
                  f"found as a records-stage template: {[c['name'] for c in candidates]}")

        print('\n### a build writing Records')
        build, feed = f'records-stage-{stamp}', f'RECORDS-STAGE-{stamp}'
        await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
        raw = (await feeds.upload_sample(ctx, feed, SAMPLE))['stream_id']
        ds = await generation.build_data_splitter(ctx, stream_ids=[raw], save_as=feed, build=build)
        e2e.check(ds.get('records') == 3 and ds.get('saved'), f"the Data Splitter, saved: {ds.get('records')} records, {ds.get('unmatched_count')} unmatched, spec {ds.get('spec')}")
        guard = guard_from(ctx)
        role_of = lambda contents, uuid: next((d.get('role') for d in contents if d['uuid'] == uuid), 'missing')
        converter = ds['saved']['uuid']
        e2e.check(role_of(await guard.folder_contents(build), converter) == 'Events',
                  'made before any pipeline, the Data Splitter is in Events')
        xslt = await translation.save_xslt(ctx, build, f'{feed}-Records', code=RECORDS_XSLT)
        e2e.check(role_of(await guard.folder_contents(build), xslt['uuid']) == 'Records',
                  'the XSLT writing records:2 is in Records')
        pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Records',
                                    template_uuid=template['uuid'])
        contents = await guard.folder_contents(build)
        e2e.check(role_of(contents, pipeline['uuid']) == 'Records', 'the pipeline writing Records is in Records')
        e2e.check(role_of(contents, converter) == 'Records',
                  f"and the Data Splitter it runs went with it: {role_of(contents, converter)}")

        print('\n### stepped in Stroom')
        stepped = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
        e2e.check(stepped['verdict'] == 'clean' and stepped['records_stepped'] == 3,
                  f"{stepped['records_stepped']} records stepped: {stepped['verdict']} "
                  f"{[(g['class'], (g.get('examples') or [{}])[0].get('message', '')[:160]) for g in stepped['groups']]}")
        output = (await stepping.step_pipeline(ctx, pipeline['uuid'], raw, 0))['elements']['translationFilter']['output']
        e2e.check('<records' in output and 'name="user" value="alice"' in output,
                  f"its output is records:2, the source's fields kept: {output[:200]}")
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
