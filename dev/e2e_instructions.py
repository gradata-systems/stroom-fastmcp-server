"""Standing instructions (AGENTS docs) against the local Stroom stack (see dev/stroom).

    uv run python dev/e2e_instructions.py

Creates three AGENTS Documentation docs: one directly under System (applies everywhere), one in a build's
folder (applies to the feed in it), and one in another build's folder (does not). get_instructions for the
feed returns the first two, general first, and lists the third. The docs are deleted afterwards.
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from config import Settings  # noqa: E402
from security.guard import guard_from  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import builds, feeds, generation, instructions, pipeline_writes, stepping, templates, translation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402


async def agents_doc(stroom: StroomGateway, folder: dict, text: str) -> dict:
    node = await stroom.post('/explorer/v2/create', {'docType': 'Documentation', 'docName': 'AGENTS',
                                                     'destinationFolder': folder, 'permissionInheritance': 'DESTINATION'})
    ref = node.get('docRef', node)
    doc = await stroom.get_doc('Documentation', ref['uuid'])
    doc['data'] = text  # the body, as typed in the Stroom UI
    await stroom.put_doc(doc)
    return {'type': 'Documentation', 'uuid': ref['uuid'], 'name': 'AGENTS'}


async def main():
    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=e2e.VERSION)
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = e2e.run_stamp()
    created = []
    try:
        guard = guard_from(ctx)
        build, other = f'instr-{stamp}', f'instr-other-{stamp}'
        feed = f'INSTR-{stamp}'
        await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
        build_folder = {k: v for k, v in (await guard.build_folder(build)).items() if not k.startswith('_')}
        other_folder = {k: v for k, v in (await guard.build_folder(other)).items() if not k.startswith('_')}
        roots = await stroom.post('/explorer/v2/fetchExplorerNodes', {
            'openItems': [], 'temporaryOpenedItems': [], 'minDepth': 1, 'ensureVisible': None, 'showAlerts': False,
            'filter': {'includedTypes': None, 'includedRootTypes': None, 'tags': None, 'nodeFlags': None,
                       'requiredPermissions': ['VIEW'], 'nameFilter': None, 'nameFilterChange': False, 'recentItems': None}})
        system = next(r for r in roots['rootNodes'] if r['type'] == 'System')
        created.append(await agents_doc(stroom, system, f'Global {stamp}: map the acting user to EventSource/User/Id.'))
        shared = f'E2E-Stream-Meta-{stamp}'
        created.append(await agents_doc(stroom, build_folder, f'Build {stamp}: TypeId is the vendor event code.\n\n'
                                                              f'## Required imports\n- translation: {shared}\n'))
        created.append(await agents_doc(stroom, other_folder, f'Other {stamp}: not for this feed.'))

        result = None
        for _ in range(15):  # explorer search indexes new docs after a short delay
            result = await instructions.get_instructions(ctx, feeds=[feed])
            texts = [i['instructions'] for i in result['instructions']]
            listed = any(o['uuid'] == created[2]['uuid'] for o in result['other_instruction_docs'])
            if len([t for t in texts if stamp in t]) >= 2 and listed:     # all three found, not just the two
                break
            await asyncio.sleep(2)
        mine = [i for i in result['instructions'] if stamp in i['instructions']]
        print(f"    applying: {[(i['folder'], i['applies_to']) for i in mine]}")
        e2e.check([i['instructions'].split(':')[0] for i in mine] == [f'Global {stamp}', f'Build {stamp}'],
                 'the root doc and the build folder doc apply, general first')
        e2e.check(mine[0]['applies_to'] == 'everything' and mine[1]['applies_to'].endswith(f'{build} and below'),
                 f"scopes: {mine[0]['applies_to']}; {mine[1]['applies_to']}")
        e2e.check(any(o['uuid'] == created[2]['uuid'] for o in result['other_instruction_docs']),
                 "the other build's doc is listed, not applied")
        started = await builds.start_build(ctx, build, feeds=[feed])
        handed = [i['instructions'].split(':')[0] for i in started['standing_instructions']['instructions']
                  if stamp in i['instructions']]
        e2e.check(handed == [f'Global {stamp}', f'Build {stamp}'], 'start_build hands back the same standing instructions')

        print('\n### required imports (asked for by the user: every translation imports a Stream-Meta XSLT)')
        # The shared XSLT the build's AGENTS doc requires, as an environment keeps one: a named template.
        node = await stroom.post('/explorer/v2/create', {'docType': 'XSLT', 'docName': shared,
                                                          'destinationFolder': system, 'permissionInheritance': 'DESTINATION'})
        lib = {'type': 'XSLT', 'uuid': node['uuid'], 'name': shared}
        created.append(lib)
        doc = await stroom.get_doc('XSLT', lib['uuid'])
        doc['data'] = ('<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns="event-logging:3" '
                       'xmlns:stroom="stroom" version="3.0"><xsl:template name="streamFeed">'
                       '<Data Name="Feed" Value="{stroom:feed-name()}"/></xsl:template></xsl:stylesheet>')
        await stroom.put_doc(doc)
        raw = (await feeds.upload_sample(ctx, feed, '{"ts": "2026-10-01T08:00:00Z", "u": "alice"}\n'
                                                    '{"ts": "2026-10-01T08:05:00Z", "u": "bob"}\n'))['stream_id']
        mapping = {'input': 'json', 'json_layout': 'lines',
                   'common': [{'path': 'EventTime/TimeCreated', 'field': 'ts', 'time_format': "yyyy-MM-dd'T'HH:mm:ssX"},
                              {'path': 'EventSource/System/Name', 'value': 'Instr'},
                              {'path': 'EventSource/System/Environment', 'value': 'Test'},
                              {'path': 'EventSource/Generator', 'value': 'e2e'},
                              {'path': 'EventSource/Device/HostName', 'value': 'h1'},
                              {'path': 'EventSource/User/Id', 'field': 'u'},
                              {'path': 'EventDetail/TypeId', 'value': 'Logon'}],
                   'events': [{'name': 'logon', 'fields': [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                                                           {'path': 'EventDetail/Authenticate/User/Id', 'field': 'u'}]}]}
        built = await e2e.agreed(generation.build_translation_xslt, ctx=ctx, mapping=mapping, stream_ids=[raw],
                                 build=build, name=f'{feed}-Events', feeds=[feed])
        saved_code = (await stroom.get_doc('XSLT', built['saved']['uuid'])).get('data') or '' if built.get('saved') else ''
        e2e.check(f'<xsl:import href="{shared}"' in saved_code,
                  f"the translation imports {shared}, as the build's AGENTS doc requires: {built.get('problems')}")
        offered = (built.get('required_imports') or [{}])[0]
        e2e.check('streamFeed' in (offered.get('named_templates') or {}),
                  f"the reply names what it offers: {offered}")
        placed = next((d for d in await guard.folder_contents(build) if d['uuid'] == built['saved']['uuid']), {})
        e2e.check(placed.get('role') == 'Events', f"the XSLT is in the build's Events folder: {placed.get('path')}")
        template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                        if c['name'] == 'Event Data (JSON)')
        pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                    template_uuid=template['uuid'])
        stepped = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
        e2e.check(stepped['verdict'] == 'clean', f"stepped clean in Stroom, the import resolved: {stepped['verdict']}")
        hand = ('<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns="event-logging:3" '
                'version="3.0"><xsl:template match="/"><Events/></xsl:template></xsl:stylesheet>')
        try:
            await translation.save_xslt(ctx, build, f'{feed}-Hand', code=hand)
            e2e.check(False, 'a hand-written translation without the import is refused')
        except Exception as e:
            e2e.check(f"import '{shared}'" in str(e), f"a hand-written translation without it is refused: {str(e)[:160]}")
        print('\nALL PASSED')
    finally:
        if created:
            await stroom.request('DELETE', '/explorer/v2/delete', {'docRefs': created})
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
