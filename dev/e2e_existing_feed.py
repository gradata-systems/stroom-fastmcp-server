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
Stepping copies nothing. 8. Then the feed's streams are processed where they are (asked for by the user): the
   Events go to a test feed the server makes in the build, through the pipeline's StreamAppender; no feed is
   made for raw samples and nothing is uploaded. 9. Promotion takes the StreamAppender's feed off, leaves
   the test feed in the workspace, and the feed's filter leaves none of the sample out.
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

import e2e_translation as e2e  # noqa: E402
from config import Settings  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from security.guard import guard_from as ctx_guard  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from tools import builds, feeds, generation, pipeline_writes, processing_writes, sampling, stepping, templates, translation  # noqa: E402
from tools.plan import status as build_status  # noqa: E402
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
    e2e.check(result['ok'], f"mapping generates: {result['problems']}")
    return result['xslt']


async def text_locations(ctx, stamp: str) -> None:
    """Survey locations point at the right record in text formats too (CSV with a header, syslog)."""
    import yaml
    for case_file, key in (('01_csv_vpn', 'user'), ('06_syslog5424_ssh', 'user')):
        print(f'\n### locations in a {case_file[3:]} feed')
        case = yaml.safe_load((ROOT / 'dev' / 'eval' / 'cases' / f'{case_file}.yaml').read_text(encoding='utf-8'))
        build, feed = f'loc-{case_file[:2]}-{stamp}', f'LOC-{case_file[:2]}-{stamp}'
        await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
        await feeds.upload_sample(ctx, feed, case['sample'])
        await asyncio.sleep(1)
        survey = await sampling.survey_feed(ctx, feed, examples_per_shape=5)
        mapping = TranslationMapping.model_validate(case['reference']['mapping'])
        xslt_text = (await generation.build_translation_xslt(ctx, mapping))['xslt']
        converter = case['reference']['converter']
        tc = await translation.create_text_converter(ctx, build, feed, 'DATA_SPLITTER',
                                                     e2e.CSV_SPLITTER if converter == 'csv_header' else converter)
        x = await translation.create_xslt(ctx, build, f'{feed}-Events', xslt_text)
        template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                        if c['name'] == 'Event Data (Text)')
        pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                   template_uuid=template['uuid'], set_properties=[
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
                e2e.check(user is not None and user in line,
                         f"location {location['record']} steps the line it names (user {user})")
                checked += 1
        e2e.check(checked == 3 and bool(examples), f'all {checked} records located')


async def main():
    settings = e2e.target_settings()
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = e2e.STAMP
    try:
        print('### a source feed that already holds data')
        source = f'SRC-APP-{stamp}'
        await e2e.agreed(feeds.create_feed, ctx=ctx, build=f'src-{stamp}', name=source)
        for text in STREAMS:
            await feeds.upload_sample(ctx, source, text)
        await asyncio.sleep(2)

        ids = sorted(m['meta']['id'] for m in (await stroom.find_meta([e2e.processing_writes._term('Feed', source)], 10))['values'])

        build = f'existing-{stamp}'
        print('\n### 1. survey two streams, spread over the feed, recorded in the build')
        # Fewer streams than the default is the user's to confirm (seen: an agent surveying a live feed 3 at a time).
        asked = await sampling.survey_feed(ctx, source, max_streams=2, build=build)
        e2e.check(asked.get('status') == 'needs_confirmation' and 'only 2 stream(s) a call' in asked.get('summary', ''),
                  f"fewer streams than the default are confirmed first: {asked.get('summary')}")
        first = await sampling.survey_feed(ctx, source, max_streams=2, build=build, confirmation_id=asked['confirmation_id'])
        e2e.check(first['survey_doc']['name'] == f'{source} - Survey', f"survey kept in {first['survey_doc']['name']}")
        signatures = [s['signature'] for s in first['shapes']]
        kinds = sorted(s['signature'].split('action=')[-1] for s in first['shapes'])
        print(f"    read {first['streams_read']} of {ids}; kinds: {kinds}")
        e2e.check(first['streams_read'] == [ids[-1], ids[0]], 'the newest and the oldest stream are read first')
        e2e.check(kinds == ['file_read', 'login', 'logout'] and not first['saturated'], 'three kinds so far, keep looking')

        print('\n### 2. pipeline for those shapes, stepped on the feed\'s own records')
        template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                        if c['name'] == 'Event Data (JSON)')
        xslt = await translation.create_xslt(ctx, build, f'{source}-Events', await xslt_for(ctx, V1))
        pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{source}-Events',
                                   template_uuid=template['uuid'], set_properties=[
                                       PropertyValue(element='translationFilter', name='xslt', doc_uuid=xslt['uuid'], doc_type='XSLT'),
                                       PropertyValue(element='jsonParser', name='addRootObject', value=False)])
        stepped = await stepping.step_records(ctx, pipeline['uuid'], first['locations'])
        e2e.check(stepped['verdict'] == 'clean' and stepped['records_stepped'] == len(first['locations']),
                 f"those records step clean in place ({stepped['records_stepped']} records)")
        for location in first['locations']:
            one = await stepping.step_pipeline(ctx, pipeline['uuid'], location['stream'], location['record'], part=location['part'])
            action = location['shape'].split('action=')[-1]
            e2e.check(f'<TypeId>{action}</TypeId>' in one['elements']['translationFilter']['output'],
                     f"location {location['stream']}:{location['record']} is a {action} record")

        print('\n### 3. survey the streams not read yet')
        second = await sampling.survey_feed(ctx, source, build=build)  # carries on from the survey doc
        new = [s['signature'].split('action=')[-1] for s in second['shapes'] if s['new']]
        print(f"    read {second['streams_read']}; new kinds: {new}")
        e2e.check(sorted(second['streams_read']) == ids[1:3] and new == ['passwd_change'],
                 'the other two streams add the password change')
        gaps = await stepping.step_records(ctx, pipeline['uuid'], second['locations'])
        unmatched = [g for g in gaps['groups'] if 'No event mapping matched' in str(g)]
        e2e.check(unmatched and unmatched[0]['count'] == 1 and len(gaps['shapes_not_clean']) == 1,
                 f"the current translation flags it: {[(g['class'], g['count']) for g in gaps['groups']]}")

        print('\n### 4. extend the mapping and step every location')
        await translation.update_xslt(ctx, xslt['uuid'], await xslt_for(ctx, V2))
        every = first['locations'] + second['locations']
        both = await stepping.step_records(ctx, pipeline['uuid'], every)
        e2e.check(both['verdict'] == 'clean' and not both['shapes_not_clean'] and both['records_stepped'] == len(every),
                 f"every kind of event steps clean ({both['records_stepped']} records)")

        print('\n### 5. survey again: every stream read')
        third = await sampling.survey_feed(ctx, source, build=build)
        e2e.check(third['saturated'] and third['new_shapes'] == 0 and not third['streams_read'],
                 f"the feed is covered: {third['hint']}")
        record = (await stroom.get_doc('Documentation', third['survey_doc']['uuid']))['data']
        state = sampling.read_state(record)
        e2e.check(sorted(state['streams_read']) == ids and len(state['shapes']) == 4 and state['saturated'],
                 f"the survey doc records all {len(ids)} streams and 4 kinds of event")
        e2e.check('## Kinds of event' in record and '"action": "passwd_change"' in record and 'Stream ' in record,
                 'the doc shows the kinds of event with example records and where they are')

        print('\n### 6. broad check: the head of each stream')
        broad = await stepping.step_sample(ctx, pipeline['uuid'], ids, records_per_stream=2)
        e2e.check(broad['verdict'] == 'clean' and broad['records_stepped'] == 2 * len(ids),
                 f"{broad['records_stepped']} records, 2 from each stream, step clean")

        print('\n### 6b. the user leaves one kind of event untranslated')
        marked = await e2e.agreed(sampling.set_shape_handling, ctx=ctx, build=build, feed=source, shapes=['action=file_read'],
                                 handling='drop', reason='file reads are audited elsewhere')
        drop_locs = marked['locations']
        e2e.check(len(marked['shapes']) == 1 and drop_locs and all(l['expect'] == 'none' for l in drop_locs),
                 f"recorded, with {len(drop_locs)} example location(s) to step expecting no Event")
        still = await stepping.step_records(ctx, pipeline['uuid'], drop_locs)
        e2e.check(still['shapes_not_clean'] == marked['shapes'] and 'left untranslated' in str(still['groups']),
                 'the current translation still writes Events for them, and is flagged')
        await translation.update_xslt(ctx, xslt['uuid'], await xslt_for(ctx, V3))
        rest = [l for l in every if l['shape'] not in marked['shapes']]
        after = await stepping.step_records(ctx, pipeline['uuid'], rest + drop_locs)
        e2e.check(after['verdict'] == 'clean' and not after['shapes_not_clean']
                 and after['left_untranslated_as_intended'] == len(drop_locs),
                 f"with a drop rule every location steps clean, {after['left_untranslated_as_intended']} left untranslated")
        record = (await stroom.get_doc('Documentation', third['survey_doc']['uuid']))['data']
        e2e.check('left untranslated: file reads are audited elsewhere' in record, 'the survey doc shows the choice and why')

        print('\n### 7. a big single-line stream is read from its head only')
        big_feed = f'SRC-BIG-{stamp}'
        await e2e.agreed(feeds.create_feed, ctx=ctx, build=f'src-{stamp}', name=big_feed)
        rows = [{'ts': '2026-09-28T10:00:00Z', 'host': 'app02', 'user': f'user{i}',
                 'action': 'logout' if i % 50 == 7 else 'login'} for i in range(30000)]
        big = json.dumps(rows)
        await feeds.upload_sample(ctx, big_feed, big)
        await asyncio.sleep(2)
        head = await sampling.survey_feed(ctx, big_feed, max_chars_per_stream=100_000)
        stream = head['per_stream'][0]
        print(f"    {len(big)} chars; read {head['records_read']} records; per stream {stream}")
        e2e.check(stream['head_only'] and 0 < head['records_read'] < 2000, 'only the head was read')
        e2e.check(sorted(s['signature'].split('action=')[-1] for s in head['shapes']) == ['login', 'logout'],
                 'both kinds found in the head')
        located = await stepping.step_records(ctx, pipeline['uuid'], head['locations'])
        e2e.check(located['verdict'] == 'clean' and located['records_stepped'] == len(head['locations']),
                 f"its locations step clean ({located['records_stepped']} records)")

        print('\n### stepping copied nothing; generated docs are tagged')
        created = [m['meta'] for m in (await stroom.find_meta([e2e.processing_writes._term('Feed', source)], 50))['values']]
        e2e.check(len(created) == len(STREAMS) and all(m['typeName'] == 'Raw Events' for m in created),
                 f"the source feed still holds only its {len(STREAMS)} raw streams")
        tags = await ctx_guard(ctx).tags({'type': 'Pipeline', 'uuid': pipeline['uuid'], 'name': pipeline['name']})
        e2e.check('mcp-generated' in tags and 'mcp-managed' in tags, f"the pipeline is tagged {tags}")

        print('\n### 8. processed where it is: the Events go to a test feed the server makes in the build')
        # Asked for by the user: no feed made for raw samples and nothing uploaded; the build's pipeline processes the
        # existing feed's streams, its StreamAppender writing to a test feed in the build.
        test_feed = f'{source}-MCP-TEST'
        try:
            await feeds.create_feed(ctx, build, f'{source}-SAMPLES')
            e2e.check(False, 'a feed for raw samples is refused in a build on an existing feed')
        except ToolError as e:
            e2e.check('works from the existing feed' in str(e) and 'user_samples=' in str(e),
                      f"a feed for raw samples is refused: {str(e)[:160]}")
        started = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                                   stream_ids=ids)
        e2e.check(started['output_feed'] == test_feed and 'not into the production feed' in started['output_note'],
                  f"the filter's Events go to {started.get('output_feed')}")
        own = (await stroom.get_doc('Pipeline', pipeline['uuid'])).get('pipelineData') or {}
        set_feed = [p_ for p_ in (own.get('properties') or {}).get('add') or [] if p_['name'] == 'feed']
        e2e.check(len(set_feed) == 1 and set_feed[0]['value']['entity']['name'] == test_feed,
                  f"the pipeline's StreamAppender writes to {test_feed}")
        try:
            await feeds.upload_sample(ctx, test_feed, '{"ts": "2026-09-28T10:00:00Z"}')
            e2e.check(False, 'nothing is uploaded to the test feed')
        except ToolError as e:
            e2e.check('upload nothing' in str(e), f"nothing is uploaded to the test feed: {str(e)[:120]}")
        done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], ids)
        outputs = {raw: await processing_writes._outputs(stroom, raw, pipeline['uuid']) for raw in ids}
        events = [m for metas in outputs.values() for m in metas if m['typeName'] == 'Events']
        e2e.check(done['finished'] and len(events) == len(ids) and all(m['feedName'] == test_feed for m in events),
                  f"one Events stream per raw stream, all in {test_feed}: {[(m['id'], m['feedName']) for m in events]}")
        in_source = [m['meta'] for m in (await stroom.find_meta([e2e.processing_writes._term('Feed', source)], 50))['values']]
        errors = [m for metas in outputs.values() for m in metas if m['typeName'] == 'Error']
        print(f"    the source feed holds {sorted({m['typeName'] for m in in_source})}; Error streams: "
              f"{[(m['id'], m['feedName']) for m in errors]}")
        e2e.check(not [m for m in in_source if m['typeName'] == 'Events'], f"no Events were written to {source}")
        status = await build_status(ctx, build)
        e2e.check(status['next']['step'] != 'feed' and 'upload_sample' not in json.dumps(status['next']),
                  f"the plan goes on from the processed Events: next {status['next']['step']}")

        print('\n### 9. promoted: the pipeline writes into its input feed again, and processes the sample again')
        await builds.write_documentation(ctx, build, pipeline['uuid'], change='Created', stream_ids=ids[:1], markdown=(
            '## Purpose and data\n\nApplication audit events.\n\n## Field mapping\n\n| Field | Path |\n| --- | --- |\n'
            '| user | User/Id |\n'))
        folder = f'System/E2E Production/existing-{stamp}'
        promoted = await e2e.agreed(builds.promote_build, ctx=ctx, build=build,
                                    destinations={'Pipeline': folder, 'XSLT': folder, 'Documentation': folder})
        own = (await stroom.get_doc('Pipeline', pipeline['uuid'])).get('pipelineData') or {}
        e2e.check(not [p_ for p_ in (own.get('properties') or {}).get('add') or [] if p_['name'] == 'feed'],
                  'the StreamAppender feed property is taken off')
        e2e.check(any('writes its Events into its input' in d for d in promoted['promoted']),
                  'promotion says so')
        e2e.check(any(test_feed in d and 'left' in d for d in promoted['promoted']),
                  f"{test_feed} is left in the workspace")
        made = promoted.get('processing_filters') or []
        e2e.check(len(made) == 1 and made[0]['feed'] == source and not made[0].get('except_processed_in_build'),
                  f"its filter on {source} leaves none of the sample out (their Events stayed in the test feed): {made}")
        await text_locations(ctx, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
