"""describe_feed end to end on the local stack: a feed onboarded and indexed, its pipelines and documentation
promoted, then asked about as a chat client would (OpenWebUI, Stroom's own assistant).

    uv run python dev/e2e_describe.py

The CSV feed from the translation suite, indexed on Lucene (the Lucene suite's stage 2), promoted. Then: the feed's
overview (its events and indexing pipelines, each with its generated documentation found by tag, and the index);
a field asked about by the user's name for it (user_id for UserId), traced from the index doc's row to its
event-logging path and the events doc's row, with how to search it; a field nothing names; a feed that doesn't
exist.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from e2e_lucene_indexing import index_stage  # noqa: E402
from tools import builds, describe  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402

check, agreed = e2e.check, e2e.agreed


async def main():
    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = e2e.Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                            stroom_api_key=local['STROOM_ADMIN_API_KEY'], oidc_issuer_url='-', oidc_audience='-',
                            public_base_url='-', event_logging_version=e2e.VERSION, conventions_dir=ROOT / 'conventions')
    stroom = e2e.StroomGateway(settings)
    ctx = e2e.SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': e2e.ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': e2e.AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = time.strftime('%H%M%S')
    try:
        csv = await e2e.onboard(ctx, 'csv', e2e.CASES['csv'], stamp)
        indexed = await index_stage(ctx, csv, stamp)
        folder = f'E2E Described {stamp}'
        system = next(r for r in (await stroom.post('/explorer/v2/fetchExplorerNodes', {
            'openItems': [], 'temporaryOpenedItems': [], 'minDepth': 1, 'ensureVisible': None, 'showAlerts': False,
            'filter': {'includedTypes': None, 'includedRootTypes': None, 'tags': None, 'nodeFlags': None,
                       'requiredPermissions': ['VIEW'], 'nameFilter': None, 'nameFilterChange': False,
                       'recentItems': None}}))['rootNodes'] if r['type'] == 'System')
        await stroom.post('/explorer/v2/create', {'docType': 'Folder', 'docName': folder, 'destinationFolder': system,
                                                  'permissionInheritance': 'DESTINATION'})
        dest = {t: f'System/{folder}' for t in ('Feed', 'Pipeline', 'XSLT', 'TextConverter', 'Documentation', 'Index',
                                                'Dashboard')}
        await agreed(builds.promote_build, ctx=ctx, build=csv['build'], destinations=dest)

        print('\n### the feed, asked about')
        # Asked in another case, as a user might type it.
        overview = await describe.describe_feed(ctx, csv['feed'].lower())
        print(json.dumps({k: v for k, v in overview.items() if k != 'hint'}, indent=1)[:2500])
        kinds = {p['kind']: p for p in overview['pipelines']}
        check(set(kinds) == {'events', 'indexing'}, f"its pipelines: {sorted(kinds)}")
        for kind, p in kinds.items():
            doc = p.get('documentation') or {}
            check(doc.get('generated') and 'action=open-doc' in doc.get('link', ''),
                  f"the {kind} pipeline's generated documentation, found by tag, with its link")
        check(any('translation e2e test' in p['text'] for p in overview.get('purpose') or []),
              "the overview carries the events doc's Purpose and data")
        index = overview['indexes'][0]
        check(index['name'] == indexed['index']['name'] and index['backend'] == 'lucene' and index['fields'] >= 5,
              f"the index it is indexed into: {index['name']}, {index['fields']} fields")

        print('\n### a field, by the user\'s name for it')
        user_field = next(f.name for f in indexed['plan'].fields if f.source == 'EventSource/User/Id')
        asked = await describe.describe_feed(ctx, csv['feed'], field=user_field.lower())
        about = asked['field']
        print(json.dumps(about, indent=1)[:2500])
        hit = about['in_indexes'][0]
        check(hit['field'] == user_field and hit['how_to_search'],
              f"{user_field} in the index, as {hit['type']}, with how to search it")
        check('EventSource/User/Id' in about.get('event_logging_paths', []), 'traced to its event-logging path')
        docs = {r['doc'] for r in about.get('documented') or []}
        check({csv['pipeline']['name'], indexed['pipeline']['name']} <= docs,
              f"its rows in both docs, the index's and the events pipeline's: {sorted(docs)}")
        advice = ' '.join(hit['how_to_search'])
        check("EQUALS '*x'" in advice, 'a wildcard for ends-with, as Stroom finds nothing with ENDS_WITH on Lucene')
        check('not case-sensitive' in advice, "and not case-sensitive, as the index field says (EQUALS Bob found bob)")

        print('\n### a field nothing names, and a feed that does not exist')
        missing = await describe.describe_feed(ctx, csv['feed'], field='User.Nothing')
        check(missing['field'].get('found') is False and 'similar_fields' in missing['indexes'][0],
              f"not found, with the index's fields named like it: {missing['indexes'][0].get('similar_fields')}")
        try:
            await describe.describe_feed(ctx, csv['feed'] + '-NOPE')
            refused = ''
        except Exception as e:
            refused = str(e)
        check('No feed named' in refused and csv['feed'] in refused, f"an unknown feed, with feeds named like it: {refused}")
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
