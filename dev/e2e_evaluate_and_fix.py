"""Evaluating an existing events pipeline, and fixing a reported issue in it, against the local Stroom stack.

    uv run python dev/e2e_evaluate_and_fix.py

The pipeline is production content the server did not build: a feed, text converter, XSLT and pipeline made
directly in Stroom outside any workspace, processing two raw streams of CSV logons through its own processor
filter. It has flaws an evaluation should surface and a fix should correct: a record whose IP is 'n/a' is written into
IPAddress as it is, which fails the schema (an error worth worrying about); a locked account's logon is written as a
success (the issue a user reports); the CSV's agent column is never used; a record with no IP leaves IPAddress out.

Evaluate (a health check, read-only), as evaluate_events_pipeline asks:
1. describe the pipeline (template, elements, its XSLT and text converter) and the feeds its filters cover;
2. errors first: the Error streams' groups (the schema failure, blocking, with an example), and stepping showing
   the same error now. Stroom does not store an event that fails the schema: the 5 stored events are valid, and
   the 6th record's event is missing, which only comparing records with events shows;
3. map the translation: the raw data's fields it never reads (agent);
4. inventory the events (types, TypeId, how often each path is populated);
5. a suggested fix, proven when the user asks: IPAddress written only for an address; only that field changes, on
   one record, and the schema error is gone;
6. the report saved as Documentation in a build, promoted beside the pipeline; nothing else changed.

Fix, as fix_pipeline_issue asks, from the user's report "event 3 of <Events stream> shows a locked account's logon
as successful":
1. locate_event: the raw stream, part and record, the stored event and the one stepping gives now;
2. step it: the issue reproduces; an event the user did not report is as expected;
3. a draft that also changes another field is not ready; the right one is: only Outcome/Success changes, on
   the locked records only, with a diff and manual steps. The unrelated schema error in one of the streams it is
   proven on is reported as there before too, not in the fix's way;
4. applied in place: a working copy, compared with production (only that field differs), stepped clean,
   documented and promoted after approval: the production XSLT now has the fix and a backup was kept.
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
from security.guard import guard_from  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import (builds, diagnosis, explorer, pipeline_writes, processing, processing_writes, stepping, streams,  # noqa: E402
                   translation, validation)
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

STREAMS = [
    "time,user,host,ip,result,agent\n"
    "2026-09-28T10:00:00,alice,ws01,10.0.0.1,ok,curl\n"
    "2026-09-28T10:05:00,bob,ws02,10.0.0.2,fail,firefox\n"
    "2026-09-28T10:07:30,carol,ws03,10.0.0.3,locked,edge\n",
    "time,user,host,ip,result,agent\n"
    "2026-09-29T09:00:00,dave,ws04,,ok,curl\n"
    "2026-09-29T09:10:00,erin,ws05,10.0.0.5,locked,chrome\n"
    "2026-09-29T09:20:00,frank,ws06,n/a,ok,curl\n",
]
IP = "data[@name='ip']/@value"
IP_FIX = f"matches({IP}, '^[0-9.]+$|:')"         # an address (IPv4, or IPv6 with colons), not n/a
BUG = "data[@name='result']/@value = ('ok', 'locked')"
FIX = "data[@name='result']/@value = 'ok'"
SUCCESS = 'Event/EventDetail/Authenticate/Outcome/Success'


def production_xslt() -> str:
    """The translation as someone wrote it: locked logons counted as successes, the IP left out when empty."""
    code = e2e.xslt('records:2', 'records', 'record',
                    "stroom:format-date(data[@name='time']/@value, 'yyyy-MM-dd''T''HH:mm:ss')",
                    "data[@name='host']/@value", "data[@name='user']/@value", "data[@name='ip']/@value", BUG)
    ip = """<IPAddress><xsl:value-of select="data[@name='ip']/@value" /></IPAddress>"""
    assert code.count(ip) == 1
    return code.replace(ip, f"""<xsl:if test="{IP} != ''">{ip}</xsl:if>""")


async def _create(stroom: StroomGateway, doc_type: str, name: str, folder: dict) -> dict:
    node = await stroom.post('/explorer/v2/create', {'docType': doc_type, 'docName': name, 'destinationFolder': folder,
                                                     'permissionInheritance': 'DESTINATION'})
    return node.get('docRef', node)


async def production(ctx, stroom: StroomGateway, stamp: str) -> dict:
    """The existing pipeline, made directly in Stroom, outside any workspace, and its processed data."""
    system = await guard_from(ctx).system_node()
    top = next((v['docRef'] for v in (await stroom.find_documents('E2E Production', ['Folder'], 10)).get('values') or []
                if v['docRef']['name'] == 'E2E Production'), None)
    top = top or await _create(stroom, 'Folder', 'E2E Production', {k: v for k, v in system.items() if not k.startswith('_')})
    folder = await _create(stroom, 'Folder', f'logins-{stamp}', top)
    feed_name = f'E2E-LOGINS-{stamp}'
    feed = await stroom.get_doc('Feed', (await _create(stroom, 'Feed', feed_name, folder))['uuid'])
    feed.update(encoding='UTF-8', streamType='Raw Events')
    await stroom.put_doc(feed)
    tc = await stroom.get_doc('TextConverter', (await _create(stroom, 'TextConverter', f'{feed_name}-Splitter', folder))['uuid'])
    tc.update(converterType='DATA_SPLITTER', data=e2e.CSV_SPLITTER)
    tc = await stroom.put_doc(tc)
    xslt = await stroom.get_doc('XSLT', (await _create(stroom, 'XSLT', f'{feed_name}-Events', folder))['uuid'])
    xslt['data'] = production_xslt()
    xslt = await stroom.put_doc(xslt)
    template = next(v['docRef'] for v in (await stroom.find_documents('Event Data (Text)', ['Pipeline'], 50))['values']
                    if v['docRef']['name'] == 'Event Data (Text)')
    pipeline = await stroom.get_doc('Pipeline', (await _create(stroom, 'Pipeline', f'{feed_name}-Events', folder))['uuid'])
    entity = lambda d: {'entity': {'type': d['type'], 'uuid': d['uuid'], 'name': d['name']}}
    pipeline['parentPipeline'] = template
    pipeline['pipelineData'] = {'properties': {'add': [
        {'element': 'dsParser', 'name': 'textConverter', 'value': entity(tc)},
        {'element': 'translationFilter', 'name': 'xslt', 'value': entity(xslt)}]}}
    pipeline = await stroom.put_doc(pipeline)
    for text in STREAMS:
        response = await stroom.datafeed(feed_name, text.encode('utf-8'), {'Type': 'Raw Events'})
        assert response.is_success, response.text
    await asyncio.sleep(2)
    raw = sorted(m['meta']['id'] for m in (await stroom.find_meta(
        [processing_writes._term('Feed', feed_name), processing_writes._term('Type', 'Raw Events')], 10))['values'])
    # Its own processor filter on its feed, as production would have.
    await processing_writes._create_filter(stroom, pipeline, {'type': 'operator', 'op': 'AND', 'children': [
        processing_writes._term('Feed', feed_name), processing_writes._term('Type', 'Raw Events')]}, 10, 2, None)
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], raw)
    events = [s['events'][0] for s in done['streams']]
    e2e.check(len(raw) == 2 and len(events) == 2, f'production: raw streams {raw}, Events {events}')
    by_raw = {s['input']: s['events'][0] for s in done['streams']}
    return {'feed': feed_name, 'pipeline': pipeline, 'xslt': xslt, 'tc': tc, 'raw': raw,
            'events': [by_raw[r] for r in raw], 'folder': f"System/E2E Production/logins-{stamp}"}


async def evaluate(ctx, stroom: StroomGateway, prod: dict, stamp: str) -> None:
    pipeline = prod['pipeline']
    versions = {d['uuid']: (await stroom.get_doc(d['type'], d['uuid'])).get('version')
                for d in (pipeline, prod['xslt'], prod['tc'])}
    print('\n### evaluate: 1. describe the pipeline and what it processes')
    for _ in range(15):     # Stroom's search index takes a moment to see a new document
        found = json.dumps(await explorer.find_documents(ctx, pipeline['name'], types=['Pipeline']))
        if pipeline['uuid'] in found:
            break
        await asyncio.sleep(2)
    e2e.check(pipeline['uuid'] in found, 'found by name')
    described = json.dumps(await explorer.describe_document(ctx, 'Pipeline', pipeline['uuid']))
    e2e.check('Event Data (Text)' in described and 'translationFilter' in described and prod['xslt']['name'] in described,
              'its template, elements and own XSLT')
    status = await processing.processing_status(ctx, pipeline['uuid'])
    e2e.check(prod['feed'] in json.dumps(status), 'the feed its processor filter covers')

    print('\n### 2. errors worth worrying about, schema compliance first')
    errors = await streams.summarise_streams(ctx, prod['raw'], kind='errors')
    groups = [g for g in json.loads(json.dumps(errors, default=str)).get('streams', {}).values()
              for g in (g.get('groups') or [])] if isinstance(errors.get('streams'), dict) else []
    groups = groups or [g for v in errors.values() if isinstance(v, dict) for g in (v.get('groups') or [])]
    blocking = [g for g in groups if g['class'] == 'blocking']
    print('    ' + json.dumps([(g['reason'], g['count'], g['examples'][0]['message'][:120]) for g in blocking]))
    # Stroom reports one schema failure as two messages (the pattern facet, then the element's value).
    e2e.check(blocking and all("'n/a'" in g['examples'][0]['message'] and g['reason'] == 'Output fails schema validation'
                               and g['count'] == 1 for g in blocking) and 'IPAddress' in json.dumps(blocking),
              "the Error streams' blocking errors are one schema failure: IPAddress 'n/a'")
    valid = total = 0
    failing = []
    for stream in prod['events']:
        records = (await streams.read_stream(ctx, stream, 0, 20))['records']
        for record in records:
            checked = await validation.check_events(ctx, record)
            total += 1
            valid += checked['schema']['valid']
            if not checked['schema']['valid']:
                failing.append(json.dumps(checked['schema'].get('errors'))[:200])
    sample = await stepping.step_sample(ctx, pipeline['uuid'], prod['raw'])
    e2e.check(sample['verdict'] == 'blocking' and sample['records_stepped'] == 6,
              f"stepping the current code gives the same error now: {sample['verdict']}")
    # The event that failed the schema was not stored: the stored events are all valid, and one record is missing.
    e2e.check((valid, total) == (5, 5) and sample['records_stepped'] - total == 1,
              f"{valid} of {total} stored events valid, but {sample['records_stepped']} records: one produced no event")

    print('\n### 3-4. the translation: input it never reads; the events')
    xslt = await explorer.describe_document(ctx, 'XSLT', prod['xslt']['uuid'])
    read = set((xslt.get('translation') or {}).get('input_fields') or [])
    header = (await streams.read_stream(ctx, prod['raw'][0], 0, 1))['records'][0].splitlines()[0].split(',')
    unused = sorted(set(header) - read)
    e2e.check(unused == ['agent'], f"the raw data's fields it never reads: {unused} (it reads {sorted(read)})")
    inventory = await streams.summarise_streams(ctx, prod['events'], kind='events')
    e2e.check(inventory['type_ids'] == {'Logon': 5} and inventory['path_population']['EventSource/Device/IPAddress'] == 80,
              f"5 logons; IPAddress in {inventory['path_population']['EventSource/Device/IPAddress']:.0f}% of events")

    print('\n### 5. the user asks for a fix for the schema error: suggested and proven')
    code = prod['xslt']['data']
    ip_line = f'<xsl:if test="{IP} != \'\'">'
    assert code.count(ip_line) == 1
    draft = code.replace(ip_line, f'<xsl:if test="{IP_FIX}">')
    proven = await diagnosis.summarise_fix(ctx, pipeline['uuid'], 'translationFilter', draft, prod['raw'],
                                           ['Event/EventSource/Device/IPAddress'],
                                           issue="IPAddress 'n/a' fails the event-logging schema")
    print(f"    ready {proven['ready']}, changed {[f['path'] for f in proven['fields_changed']]} on "
          f"{proven['records_changed']} record(s); resolved {proven.get('errors_resolved')}")
    e2e.check(proven['ready'] and [f['path'] for f in proven['fields_changed']] == ['Event/EventSource/Device/IPAddress']
              and proven['records_changed'] == 1 and proven.get('errors_resolved') and not proven.get('errors_before_too'),
              'ready: only IPAddress changes, on the one record, and the schema error is gone')
    e2e.check(proven['diff'] and proven['manual_steps'], 'with the diff and the steps to apply it by hand')

    print('\n### 6. the report, saved and promoted beside the pipeline; nothing else changed')
    build = f'e2e-evaluate-{stamp}'
    report = (f"# {pipeline['name']}: evaluation\n\n## Purpose and data\n\nCSV logons from {prod['feed']}.\n\n"
              f"## Processing\n\nEvent Data (Text), its own Data Splitter and XSLT.\n\n"
              f"## Errors and schema conformance\n\n6 records, {total} events: one record's event fails the schema "
              f"(IPAddress 'n/a') and is not stored. The {valid} stored events are valid.\n\n"
              f"## Field mapping\n\n| From | To |\n| --- | --- |\n"
              f"| user | EventSource/User/Id |\n\nNever read: agent.\n\n## Event types\n\nAuthenticate/Logon: 5.\n\n"
              f"## Suggestions\n\n1. Write IPAddress only for an address (proven: one record changes).\n")
    await builds.write_documentation(ctx, build, pipeline['uuid'], report, 'Evaluation')
    promoted = await e2e.agreed(builds.promote_build, ctx=ctx, build=build, destinations={'Documentation': prod['folder']})
    print('    ' + json.dumps(promoted.get('promoted'), default=str)[:400])
    beside = [v['docRef'] for v in (await stroom.find_documents(pipeline['name'], ['Documentation'], 10)).get('values') or []]
    for _ in range(10):
        if beside:
            break
        await asyncio.sleep(2)
        beside = [v['docRef'] for v in (await stroom.find_documents(pipeline['name'], ['Documentation'], 10)).get('values') or []]
    text = (await stroom.get_doc('Documentation', beside[0]['uuid'])).get('data') or '' if beside else ''
    e2e.check('Errors and schema conformance' in text, 'the report is beside the pipeline')
    after = {u: (await stroom.get_doc(t, u)).get('version') for u, t in
             ((pipeline['uuid'], 'Pipeline'), (prod['xslt']['uuid'], 'XSLT'), (prod['tc']['uuid'], 'TextConverter'))}
    e2e.check(after == versions, 'the pipeline, its XSLT and its text converter are unchanged')


async def fix(ctx, stroom: StroomGateway, prod: dict, stamp: str) -> None:
    pipeline, events = prod['pipeline'], prod['events'][0]
    print(f"\n### fix: the user reports event 3 of Events stream {events}: a locked account's logon shows as successful")
    located = await diagnosis.locate_event(ctx, events, 3)
    where = located['location']
    print(f"    raw stream {located['raw_stream']}, part {where['part']}, record {where['record']}")
    e2e.check(located['raw_stream'] == prod['raw'][0] and where['record'] == 2 and 'carol' in located['stored_event']
              and '<Success>true</Success>' in located['stored_event'] and located['same_as_stored'] is True,
              'located: raw stream, part and record 3 (carol); the stored event is the one stepping gives now')
    stepped = await stepping.step_pipeline(ctx, pipeline['uuid'], located['raw_stream'], where['record'], where['part'])
    output = stepped['elements']['translationFilter']['output']
    e2e.check('<Id>carol</Id>' in output and '<Success>true</Success>' in output,
              'reproduced: stepping the record gives Success true for a locked account')
    first = (await stepping.step_pipeline(ctx, pipeline['uuid'], prod['raw'][0], 0))['elements']['translationFilter']['output']
    e2e.check('<Id>alice</Id>' in first and '<Success>true</Success>' in first, "alice's ok logon is as expected")

    print('\n### a draft that changes more than it should is not ready; the right one is')
    code = prod['xslt']['data']
    assert code.count(BUG) == 1
    sloppy = code.replace(BUG, FIX).replace('<Description>User logon</Description>', '<Description>Logon</Description>')
    loose = await diagnosis.summarise_fix(ctx, pipeline['uuid'], 'translationFilter', sloppy, prod['raw'], [SUCCESS])
    e2e.check(not loose['ready'] and 'Event/EventDetail/Description' in json.dumps(loose['problems']),
              f"not ready: {loose['problems']}")
    draft = code.replace(BUG, FIX)
    proven = await diagnosis.summarise_fix(ctx, pipeline['uuid'], 'translationFilter', draft, prod['raw'], [SUCCESS],
                                           issue="a locked account's logon shows as successful")
    e2e.check(proven['ready'] and [f['path'] for f in proven['fields_changed']] == [SUCCESS]
              and proven['records_changed'] == 2, f"ready: only Success changes, on the 2 locked records: "
                                                  f"{proven['records_changed']} {proven['problems']}")
    e2e.check('IPAddress' in json.dumps(proven.get('errors_before_too')),
              "the unrelated schema error is reported as there before too, not in the fix's way")

    print('\n### applied in place: a working copy, compared, documented, promoted after approval')
    build = f'e2e-fix-{stamp}'
    copy = await e2e.agreed(pipeline_writes.copy_pipeline, ctx=ctx, build=build, source_uuid=pipeline['uuid'],
                            new_name=f"{pipeline['name']}-WORKING", working_copy=True)
    xslt_copy = next(d for d in copy['copied_documents'] if d['type'] == 'XSLT')
    await translation.update_xslt(ctx, xslt_copy['uuid'], draft)
    diff = await stepping.compare_outputs(ctx, pipeline['uuid'], prod['raw'], other_pipeline_uuid=copy['uuid'])
    e2e.check([f['path'] for f in diff['fields_changed']] == [SUCCESS], 'the working copy differs from production only in Success')
    await builds.write_documentation(ctx, build, copy['uuid'],
                                     f"# {pipeline['name']}\n\n## Purpose and data\n\nCSV logons.\n\n## Field mapping\n\n"
                                     f"| From | To |\n| --- | --- |\n| result | {SUCCESS} (ok only) |\n",
                                     "Locked accounts' logons are no longer successes")
    await e2e.agreed(builds.promote_build, ctx=ctx, build=build, destinations={})
    now = (await stroom.get_doc('XSLT', prod['xslt']['uuid']))['data']
    e2e.check(FIX in now and BUG not in now, 'the production XSLT now has the fix')
    backups = []
    for _ in range(10):
        backups = [v['docRef'] for v in (await stroom.find_documents(f"{prod['xslt']['name']} backup*", ['XSLT'], 10)).get('values') or []]
        if backups:
            break
        await asyncio.sleep(2)
    e2e.check(bool(backups), f"the original was backed up first: {[b['name'] for b in backups]}")
    docs = [v['docRef'] for v in (await stroom.find_documents(pipeline['name'], ['Documentation'], 10)).get('values') or []
            if v['docRef']['name'] == pipeline['name']]
    text = (await stroom.get_doc('Documentation', docs[0]['uuid'])).get('data') or '' if len(docs) == 1 else ''
    e2e.check("Locked accounts' logons are no longer successes" in text and '- ' in text.split('## Change log')[-1],
              f"the pipeline's own documentation (one doc, beside it) has the change in its change log: {len(docs)} doc(s)")


async def run(ctx, stroom: StroomGateway, stamp: str) -> None:
    print('### production content the server did not build')
    prod = await production(ctx, stroom, stamp)
    prod['xslt'] = await stroom.get_doc('XSLT', prod['xslt']['uuid'])
    await evaluate(ctx, stroom, prod, stamp)
    await fix(ctx, stroom, prod, stamp)


async def main():
    stroom = StroomGateway(e2e.target_settings())
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    try:
        await run(ctx, stroom, time.strftime('%H%M%S'))
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
