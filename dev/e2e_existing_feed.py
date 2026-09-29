"""Building a pipeline from a feed that already holds data, against the local Stroom stack (see dev/stroom).

    uv run python dev/e2e_existing_feed.py

A source feed gets four streams in which the kinds of event are spread out: every stream has logins, the oldest
also has a file read and a logout, the second oldest a password change. Then, as the agent would:

1. survey_feed with two streams reads the newest and the oldest (spread over the feed's lifetime) and finds
   logins, file reads and logouts;
2. the translation is generated from a mapping for those shapes and steps clean on the feed's own records, at
   the survey's locations (each checked to be the record it names);
3. survey_feed again, skipping the streams read, finds the password change; stepping it in place with the
   current translation flags it as unmatched (not dropped silently);
4. the mapping gains a rule for it, and every location steps clean;
5. a last survey finds every stream read: the feed is covered;
6. the broad check steps the head of each stream (records_per_stream) clean;
7. a single-line JSON stream of about 3 MB is surveyed from its head only (max_chars_per_stream): a bounded
   read, complete records only, and its locations step clean.
Nothing is copied or processed: no test feed, no processor filter, no new streams.
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
from security.guard import guard_from as ctx_guard  # noqa: E402
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


V1 = [rule('login', 'login', authenticate('Logon')), rule('logout', 'logout', authenticate('Logoff')),
      rule('file read', 'file_read', [{'path': 'EventDetail/View/File/Path', 'field': 'path'}])]
V2 = V1 + [rule('password change', 'passwd_change', authenticate('ChangePassword'))]
# The user chooses to leave file reads untranslated.
V3 = [r for r in V2 if r['name'] != 'file read'] + [
    {'name': 'file read', 'drop': True, 'when': [{'field': 'action', 'equals': 'file_read'}]}]


async def xslt_for(ctx, rules: list[dict]) -> str:
    result = await generation.build_translation_xslt(ctx, TranslationMapping.model_validate(
        {'input': 'json', 'common': COMMON, 'events': rules}))
    p2.check(result['ok'], f"mapping generates: {result['problems']}")
    return result['xslt']


async def text_locations(ctx, stamp: str) -> None:
    """Survey locations point at the right record in text formats too (CSV with a header, syslog)."""
    import yaml
    for case_file, key in (('01_csv_vpn', 'user'), ('06_syslog5424_ssh', 'user')):
        print(f'\n### locations in a {case_file[3:]} feed')
        case = yaml.safe_load((ROOT / 'dev' / 'eval' / 'cases' / f'{case_file}.yaml').read_text(encoding='utf-8'))
        build, feed = f'loc-{case_file[:2]}-{stamp}', f'LOC-{case_file[:2]}-{stamp}'
        await p2.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
        await feeds.upload_sample(ctx, feed, case['sample'])
        await asyncio.sleep(1)
        survey = await sampling.survey_feed(ctx, feed, examples_per_shape=5)
        mapping = TranslationMapping.model_validate(case['reference']['mapping'])
        xslt_text = (await generation.build_translation_xslt(ctx, mapping))['xslt']
        converter = case['reference']['converter']
        tc = await translation.create_text_converter(ctx, build, feed, 'DATA_SPLITTER',
                                                     p2.CSV_SPLITTER if converter == 'csv_header' else converter)
        x = await translation.create_xslt(ctx, build, f'{feed}-Events', xslt_text)
        template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                        if c['name'] == 'Event Data (Text)')
        pipeline = await p2.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                   template_uuid=template['uuid'], properties=[
                                       PropertyValue(element='dsParser', name='textConverter', doc_uuid=tc['uuid'], doc_type='TextConverter'),
                                       PropertyValue(element='translationFilter', name='xslt', doc_uuid=x['uuid'], doc_type='XSLT')])
        examples = [(e, loc) for s in survey['shapes'] for e, loc in zip([s['example']], s['locations'][:1])]
        checked = 0
        for shape in survey['shapes']:
            for location in shape['locations']:
                one = await stepping.step_pipeline(ctx, pipeline['uuid'], location['stream'], location['record'],
                                                   part=location['part'])
                output = one['elements']['translationFilter']['output']
                user = output.split('<User><Id>')[1].split('<')[0] if '<User><Id>' in output else None
                line = case['sample'].strip().splitlines()[location['record'] + (1 if converter == 'csv_header' else 0)]
                p2.check(user is not None and user in line,
                         f"location {location['record']} steps the line it names (user {user})")
                checked += 1
        p2.check(checked == 3 and bool(examples), f'all {checked} records located')


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

        ids = sorted(m['meta']['id'] for m in (await stroom.find_meta([p2.processing_writes._term('Feed', source)], 10))['values'])

        build = f'existing-{stamp}'
        print('\n### 1. survey two streams, spread over the feed, recorded in the build')
        first = await sampling.survey_feed(ctx, source, max_streams=2, build=build)
        p2.check(first['survey_doc']['name'] == f'{source} - Survey', f"survey kept in {first['survey_doc']['name']}")
        signatures = [s['signature'] for s in first['shapes']]
        kinds = sorted(s['signature'].split('action=')[-1] for s in first['shapes'])
        print(f"    read {first['streams_read']} of {ids}; kinds: {kinds}")
        p2.check(first['streams_read'] == [ids[-1], ids[0]], 'the newest and the oldest stream are read first')
        p2.check(kinds == ['file_read', 'login', 'logout'] and not first['saturated'], 'three kinds so far, keep looking')

        print('\n### 2. pipeline for those shapes, stepped on the feed\'s own records')
        template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                        if c['name'] == 'Event Data (JSON)')
        xslt = await translation.create_xslt(ctx, build, f'{source}-Events', await xslt_for(ctx, V1))
        pipeline = await p2.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{source}-Events',
                                   template_uuid=template['uuid'], properties=[
                                       PropertyValue(element='translationFilter', name='xslt', doc_uuid=xslt['uuid'], doc_type='XSLT'),
                                       PropertyValue(element='jsonParser', name='addRootObject', value=False)])
        stepped = await stepping.step_records(ctx, pipeline['uuid'], first['locations'])
        p2.check(stepped['verdict'] == 'clean' and stepped['records_stepped'] == len(first['locations']),
                 f"those records step clean in place ({stepped['records_stepped']} records)")
        for location in first['locations']:
            one = await stepping.step_pipeline(ctx, pipeline['uuid'], location['stream'], location['record'], part=location['part'])
            action = location['shape'].split('action=')[-1]
            p2.check(f'<TypeId>{action}</TypeId>' in one['elements']['translationFilter']['output'],
                     f"location {location['stream']}:{location['record']} is a {action} record")

        print('\n### 3. survey the streams not read yet')
        second = await sampling.survey_feed(ctx, source, build=build)  # carries on from the survey doc
        new = [s['signature'].split('action=')[-1] for s in second['shapes'] if s['new']]
        print(f"    read {second['streams_read']}; new kinds: {new}")
        p2.check(sorted(second['streams_read']) == ids[1:3] and new == ['passwd_change'],
                 'the other two streams add the password change')
        gaps = await stepping.step_records(ctx, pipeline['uuid'], second['locations'])
        unmatched = [g for g in gaps['groups'] if 'No event mapping matched' in str(g)]
        p2.check(unmatched and unmatched[0]['count'] == 1 and len(gaps['shapes_not_clean']) == 1,
                 f"the current translation flags it: {[(g['class'], g['count']) for g in gaps['groups']]}")

        print('\n### 4. extend the mapping and step every location')
        await translation.update_xslt(ctx, xslt['uuid'], await xslt_for(ctx, V2))
        every = first['locations'] + second['locations']
        both = await stepping.step_records(ctx, pipeline['uuid'], every)
        p2.check(both['verdict'] == 'clean' and not both['shapes_not_clean'] and both['records_stepped'] == len(every),
                 f"every kind of event steps clean ({both['records_stepped']} records)")

        print('\n### 5. survey again: every stream read')
        third = await sampling.survey_feed(ctx, source, build=build)
        p2.check(third['saturated'] and third['new_shapes'] == 0 and not third['streams_read'],
                 f"the feed is covered: {third['hint']}")
        record = (await stroom.get_doc('Documentation', third['survey_doc']['uuid']))['documentation']
        state = sampling.read_state(record)
        p2.check(sorted(state['streams_read']) == ids and len(state['shapes']) == 4 and state['saturated'],
                 f"the survey doc records all {len(ids)} streams and 4 kinds of event")
        p2.check('## Kinds of event' in record and '"action": "passwd_change"' in record and 'Stream ' in record,
                 'the doc shows the kinds of event with example records and where they are')

        print('\n### 6. broad check: the head of each stream')
        broad = await stepping.step_sample(ctx, pipeline['uuid'], ids, records_per_stream=2)
        p2.check(broad['verdict'] == 'clean' and broad['records_stepped'] == 2 * len(ids),
                 f"{broad['records_stepped']} records, 2 from each stream, step clean")

        print('\n### 6b. the user leaves one kind of event untranslated')
        marked = await p2.agreed(sampling.set_shape_handling, ctx=ctx, build=build, feed=source, shapes=['action=file_read'],
                                 handling='drop', reason='file reads are audited elsewhere')
        drop_locs = marked['locations']
        p2.check(len(marked['shapes']) == 1 and drop_locs and all(l['expect'] == 'none' for l in drop_locs),
                 f"recorded, with {len(drop_locs)} example location(s) to step expecting no Event")
        still = await stepping.step_records(ctx, pipeline['uuid'], drop_locs)
        p2.check(still['shapes_not_clean'] == marked['shapes'] and 'left untranslated' in str(still['groups']),
                 'the current translation still writes Events for them, and is flagged')
        await translation.update_xslt(ctx, xslt['uuid'], await xslt_for(ctx, V3))
        rest = [l for l in every if l['shape'] not in marked['shapes']]
        after = await stepping.step_records(ctx, pipeline['uuid'], rest + drop_locs)
        p2.check(after['verdict'] == 'clean' and not after['shapes_not_clean']
                 and after['left_untranslated_as_intended'] == len(drop_locs),
                 f"with a drop rule every location steps clean, {after['left_untranslated_as_intended']} left untranslated")
        record = (await stroom.get_doc('Documentation', third['survey_doc']['uuid']))['documentation']
        p2.check('left untranslated: file reads are audited elsewhere' in record, 'the survey doc shows the choice and why')

        print('\n### 7. a big single-line stream is read from its head only')
        big_feed = f'SRC-BIG-{stamp}'
        await p2.agreed(feeds.create_feed, ctx=ctx, build=f'src-{stamp}', name=big_feed)
        rows = [{'ts': '2026-09-28T10:00:00Z', 'host': 'app02', 'user': f'user{i}',
                 'action': 'logout' if i % 50 == 7 else 'login'} for i in range(30000)]
        big = json.dumps(rows)
        await feeds.upload_sample(ctx, big_feed, big)
        await asyncio.sleep(2)
        head = await sampling.survey_feed(ctx, big_feed, max_chars_per_stream=100_000)
        stream = head['per_stream'][0]
        print(f"    {len(big)} chars; read {head['records_read']} records; per stream {stream}")
        p2.check(stream['head_only'] and 0 < head['records_read'] < 2000, 'only the head was read')
        p2.check(sorted(s['signature'].split('action=')[-1] for s in head['shapes']) == ['login', 'logout'],
                 'both kinds found in the head')
        located = await stepping.step_records(ctx, pipeline['uuid'], head['locations'])
        p2.check(located['verdict'] == 'clean' and located['records_stepped'] == len(head['locations']),
                 f"its locations step clean ({located['records_stepped']} records)")

        print('\n### nothing copied or processed; generated docs are tagged')
        created = [m['meta'] for m in (await stroom.find_meta([p2.processing_writes._term('Feed', source)], 50))['values']]
        p2.check(len(created) == len(STREAMS) and all(m['typeName'] == 'Raw Events' for m in created),
                 f"the source feed still holds only its {len(STREAMS)} raw streams")
        filters = [r for r in (await stroom.post('/processorFilter/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': []}})).get('values') or []
                   if (r.get('processorFilter') or {}).get('pipelineUuid') == pipeline['uuid']]
        p2.check(not filters, 'no processor filter was created')
        tags = await ctx_guard(ctx).tags({'type': 'Pipeline', 'uuid': pipeline['uuid'], 'name': pipeline['name']})
        p2.check('mcp-generated' in tags and 'mcp-managed' in tags, f"the pipeline is tagged {tags}")
        await text_locations(ctx, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
