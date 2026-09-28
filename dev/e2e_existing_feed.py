"""Building a pipeline from a feed that already holds data, against the local Stroom stack (see dev/stroom).

    uv run python dev/e2e_existing_feed.py

A source feed gets four streams in which the kinds of event are spread out: the newest two hold only logins,
older ones add logouts, password changes and file reads. Then, as the agent would:

1. survey_feed over the newest streams finds only logins; its sample goes to a test feed in the build;
2. the translation is generated from a mapping for that shape and steps clean;
3. survey_feed further back, with the known signatures, finds the new shapes; their sample steps with the
   current translation and every new record is flagged as unmatched (not dropped silently);
4. the mapping gains rules for them, the regenerated XSLT steps both samples clean;
5. a last survey finds no older streams: the feed is covered.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_phase2 as p2  # noqa: E402
from config import Settings  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import feeds, generation, pipeline_writes, sampling, stepping, templates, translation  # noqa: E402
from tools.pipeline_writes import PropertyValue  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402
from utils.xsltgen import TranslationMapping  # noqa: E402


def events(minute: int, rows: list[tuple[str, str, str]]) -> str:
    return json.dumps([{'ts': f'2026-09-28T09:{minute + i:02d}:00Z', 'host': 'app01', 'user': user, 'action': action,
                        **({'path': extra} if extra else {})} for i, (user, action, extra) in enumerate(rows)])


# uploaded oldest first, so stream ids rise towards the newest
STREAMS = [
    events(0, [('alice', 'login', ''), ('alice', 'file_read', '/data/a.csv'), ('alice', 'logout', '')]),
    events(10, [('bob', 'login', ''), ('bob', 'passwd_change', '')]),
    events(20, [('carol', 'login', ''), ('dave', 'login', '')]),
    events(30, [('erin', 'login', ''), ('frank', 'login', ''), ('erin', 'login', '')]),
]
COMMON = [{'path': 'EventTime/TimeCreated', 'field': 'ts', 'time_format': "yyyy-MM-dd'T'HH:mm:ssX"},
          {'path': 'EventSource/System/Name', 'value': 'App'}, {'path': 'EventSource/System/Environment', 'value': 'Dev'},
          {'path': 'EventSource/Generator', 'value': 'app'}, {'path': 'EventSource/Device/HostName', 'field': 'host'},
          {'path': 'EventSource/User/Id', 'field': 'user'}, {'path': 'EventDetail/TypeId', 'field': 'action'}]


def rule(name: str, action: str, fields: list[dict]) -> dict:
    return {'name': name, 'when': [{'field': 'action', 'equals': action}], 'fields': fields}


def authenticate(verb: str) -> list[dict]:
    return [{'path': 'EventDetail/Authenticate/Action', 'value': verb},
            {'path': 'EventDetail/Authenticate/User/Id', 'field': 'user'}]


V1 = [rule('login', 'login', authenticate('Logon'))]
V2 = V1 + [rule('logout', 'logout', authenticate('Logoff')),
           rule('password change', 'passwd_change', authenticate('ChangePassword')),
           rule('file read', 'file_read', [{'path': 'EventDetail/View/File/Path', 'field': 'path'}])]


async def xslt_for(ctx, rules: list[dict]) -> str:
    result = await generation.build_translation_xslt(ctx, TranslationMapping.model_validate(
        {'input': 'json', 'common': COMMON, 'events': rules}))
    p2.check(result['ok'], f"mapping generates: {result['problems']}")
    return result['xslt']


async def main():
    local = p2.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=p2.VERSION)
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'), 'elastic': None,
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = time.strftime('%H%M%S')
    try:
        print('### a source feed that already holds data')
        source = f'SRC-APP-{stamp}'
        await p2.agreed(feeds.create_feed, ctx=ctx, build=f'src-{stamp}', name=source)
        for text in STREAMS:
            await feeds.upload_sample(ctx, source, text)
        await asyncio.sleep(2)

        print('\n### 1. survey the newest streams')
        first = await sampling.survey_feed(ctx, source, max_streams=2)
        signatures = [s['signature'] for s in first['shapes']]
        print(f"    read {first['streams_read']}; shapes: {[(s['signature'][-40:], s['count']) for s in first['shapes']]}")
        p2.check(first['format'] == 'json array' and len(first['shapes']) == 1 and first['shapes'][0]['count'] == 5,
                 'only logins in the newest two streams')
        p2.check(not first['saturated'], 'not saturated after two streams: keep looking')

        print('\n### 2. pipeline from the survey sample, in a test feed')
        build, test_feed = f'existing-{stamp}', f'SRC-APP-{stamp}-MCP-TEST'
        await p2.agreed(feeds.create_feed, ctx=ctx, build=build, name=test_feed)
        sample_1 = (await feeds.upload_sample(ctx, test_feed, first['sample']))['stream_id']
        template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                        if c['name'] == 'Event Data (JSON)')
        xslt = await translation.create_xslt(ctx, build, f'{test_feed}-Events', await xslt_for(ctx, V1))
        pipeline = await p2.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{source}-Events',
                                   template_uuid=template['uuid'], properties=[
                                       PropertyValue(element='translationFilter', name='xslt', doc_uuid=xslt['uuid'], doc_type='XSLT'),
                                       PropertyValue(element='jsonParser', name='addRootObject', value=False)])
        stepped = await stepping.step_sample(ctx, pipeline['uuid'], [sample_1])
        p2.check(stepped['verdict'] == 'clean', f"the login sample steps clean ({stepped['records_stepped']} records)")

        print('\n### 3. survey further back with the known shapes')
        second = await sampling.survey_feed(ctx, source, before_stream_id=first['oldest_stream_read'],
                                            known_signatures=signatures)
        new = [s['signature'].split('action=')[-1] for s in second['shapes'] if s['new']]
        print(f"    read {second['streams_read']}; new shapes: {new}")
        p2.check(sorted(new) == ['file_read', 'logout', 'passwd_change'], 'the older streams add three kinds of event')
        sample_2 = (await feeds.upload_sample(ctx, test_feed, second['sample']))['stream_id']
        gaps = await stepping.step_sample(ctx, pipeline['uuid'], [sample_2])
        unmatched = [g for g in gaps['groups'] if 'No event mapping matched' in str(g)]
        p2.check(unmatched and unmatched[0]['count'] == 3,
                 f"the current translation flags all 3 new records as unmatched: {[(g['class'], g['count']) for g in gaps['groups']]}")

        print('\n### 4. extend the mapping and step both samples')
        await translation.update_xslt(ctx, xslt['uuid'], await xslt_for(ctx, V2))
        both = await stepping.step_sample(ctx, pipeline['uuid'], [sample_1, sample_2])
        p2.check(both['verdict'] == 'clean' and both['records_stepped'] == len(first['sample_order']) + 3,
                 f"both samples step clean ({both['records_stepped']} records)")

        print('\n### 5. survey again: nothing older')
        third = await sampling.survey_feed(ctx, source, before_stream_id=second['oldest_stream_read'],
                                           known_signatures=signatures + [s['signature'] for s in second['shapes']])
        p2.check(third['saturated'] and third['new_shapes'] == 0 and not third['streams_read'],
                 f"the feed is covered: {third['hint']}")
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
