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

import e2e_phase2 as p2  # noqa: E402
from config import Settings  # noqa: E402
from security.guard import guard_from  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import builds, feeds, instructions  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402


async def agents_doc(stroom: StroomGateway, folder: dict, text: str) -> dict:
    node = await stroom.post('/explorer/v2/create', {'docType': 'Documentation', 'docName': 'AGENTS',
                                                     'destinationFolder': folder, 'permissionInheritance': 'DESTINATION'})
    ref = node.get('docRef', node)
    doc = await stroom.get_doc('Documentation', ref['uuid'])
    doc['documentation'] = text
    await stroom.put_doc(doc)
    return {'type': 'Documentation', 'uuid': ref['uuid'], 'name': 'AGENTS'}


async def main():
    local = p2.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=p2.VERSION)
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = time.strftime('%H%M%S')
    created = []
    try:
        guard = guard_from(ctx)
        build, other = f'instr-{stamp}', f'instr-other-{stamp}'
        feed = f'INSTR-{stamp}'
        await p2.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
        build_folder = {k: v for k, v in (await guard.build_folder(build)).items() if not k.startswith('_')}
        other_folder = {k: v for k, v in (await guard.build_folder(other)).items() if not k.startswith('_')}
        roots = await stroom.post('/explorer/v2/fetchExplorerNodes', {
            'openItems': [], 'temporaryOpenedItems': [], 'minDepth': 1, 'ensureVisible': None, 'showAlerts': False,
            'filter': {'includedTypes': None, 'includedRootTypes': None, 'tags': None, 'nodeFlags': None,
                       'requiredPermissions': ['VIEW'], 'nameFilter': None, 'nameFilterChange': False, 'recentItems': None}})
        system = next(r for r in roots['rootNodes'] if r['type'] == 'System')
        created.append(await agents_doc(stroom, system, f'Global {stamp}: map the acting user to EventSource/User/Id.'))
        created.append(await agents_doc(stroom, build_folder, f'Build {stamp}: TypeId is the vendor event code.'))
        created.append(await agents_doc(stroom, other_folder, f'Other {stamp}: not for this feed.'))

        result = None
        for _ in range(15):  # explorer search indexes new docs after a short delay
            result = await instructions.get_instructions(ctx, feeds=[feed])
            texts = [i['instructions'] for i in result['instructions']]
            if any(stamp in t for t in texts) and len([t for t in texts if stamp in t]) >= 2:
                break
            await asyncio.sleep(2)
        mine = [i for i in result['instructions'] if stamp in i['instructions']]
        print(f"    applying: {[(i['folder'], i['applies_to']) for i in mine]}")
        p2.check([i['instructions'].split(':')[0] for i in mine] == [f'Global {stamp}', f'Build {stamp}'],
                 'the root doc and the build folder doc apply, general first')
        p2.check(mine[0]['applies_to'] == 'everything' and mine[1]['applies_to'].endswith(f'{build} and below'),
                 f"scopes: {mine[0]['applies_to']}; {mine[1]['applies_to']}")
        p2.check(any(o['uuid'] == created[2]['uuid'] for o in result['other_instruction_docs']),
                 "the other build's doc is listed, not applied")
        started = await builds.start_build(ctx, build, feeds=[feed])
        handed = [i['instructions'].split(':')[0] for i in started['standing_instructions']['instructions']
                  if stamp in i['instructions']]
        p2.check(handed == [f'Global {stamp}', f'Build {stamp}'], 'start_build hands back the same standing instructions')
        print('\nALL PASSED')
    finally:
        if created:
            await stroom.request('DELETE', '/explorer/v2/delete', {'docRefs': created})
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
