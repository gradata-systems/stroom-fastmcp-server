"""An XML fragments source end to end against the local Stroom stack (see dev/stroom), as seen in a test environment.

    uv run python dev/e2e_fragments.py

The source sends <Event> elements, one per line with no root, whose EventData/Data holds a message: a time, then
"User: domain.com\\Bloggs, Joe – [[AppServer]] Event: [User] Action: [Login] By User: domain.com\\joe.bloggs (Item
Id: 219)", with en dashes between the parts. As the agent would, driven by the tools' guidance:

1. profile_sample names XML fragments, the XMLFragmentParser and its wrapper converter.
2. The pipeline is a child of Event Data (XML) with its parser replaced (replace_parser): the XMLFragmentParser is
   fed from Source, so the UI shows it and Stroom steps it (seen: it was linked onwards only, nothing fed it).
3. A regex written with '-' where the text has an en dash: refused at once, saying where it stops matching and
   which character the text has there. Fixed, it saves; the user and action come out of every record.
4. Each build_translation_xslt call is quick: no pipeline stepping unless the field mapping preview is asked for.
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import feeds, generation, pipeline_writes, pipelines, stepping, templates, translation, validation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

ACTIONS = ['Login', 'Logout', 'Login', 'Update']
LINE = ('<Event><System><Provider Name="AppAudit"/><EventID>{id}</EventID><Computer>app01.domain.com</Computer></System>'
        '<EventData><Data>Sep 27 2026 23:48:{s:02d} – User: domain.com\\Bloggs, Joe – [[AppServer]] Event: [User] '
        'Action: [{action}] By User: domain.com\\joe.bloggs (Item Id: {id})</Data></EventData></Event>')
SAMPLE = '\n'.join(LINE.format(id=219 + n, s=10 + n, action=action) for n, action in enumerate(ACTIONS)) + '\n'
DASHED = (r'^(\S+ \d+ \d+ \S+) - User: (.+?) - \[\[(\w+)\]\] Event: \[(\w+)\] Action: \[(\w+)\] '
          r'By User: (\S+) \(Item Id: (\d+)\)$')


NAMESPACE = 'records:2'   # the wrapper's, as profile_sample gives it: the environment's own wrapper may differ


def mapping(regex: str) -> dict:
    return {
        'input': 'xml_fragments', 'record': 'Event', 'xml_namespace': NAMESPACE,
        'extract': [{'xpath': 'EventData/Data', 'regex': regex,
                     'names': ['when', 'subject', 'server', 'kind', 'action', 'user', 'item']}],
        'common': [{'path': 'EventTime/TimeCreated', 'field': 'when', 'time_format': 'MMM dd yyyy HH:mm:ss'},
                   {'path': 'EventSource/System/Name', 'value': 'AppAudit'},
                   {'path': 'EventSource/System/Environment', 'value': 'Test'},
                   {'path': 'EventSource/Generator', 'xpath': 'System/Provider/@Name'},
                   {'path': 'EventSource/Device/HostName', 'xpath': 'System/Computer'},
                   {'path': 'EventSource/User/Id', 'field': 'user', 'transform': 'strip_domain'}],
        'events': [
            {'name': 'logon', 'when': [{'field': 'action', 'equals': 'Login'}], 'fields': [
                {'path': 'EventDetail/TypeId', 'value': 'Logon'},
                {'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                {'path': 'EventDetail/Authenticate/User/Id', 'field': 'user', 'transform': 'strip_domain'},
                {'path': 'EventDetail/Authenticate/Data', 'data_name': 'server', 'field': 'server'}]},
            {'name': 'logoff', 'when': [{'field': 'action', 'equals': 'Logout'}], 'fields': [
                {'path': 'EventDetail/TypeId', 'value': 'Logoff'},
                {'path': 'EventDetail/Authenticate/Action', 'value': 'Logoff'},
                {'path': 'EventDetail/Authenticate/User/Id', 'field': 'user', 'transform': 'strip_domain'}]},
            {'name': 'update', 'when': [{'field': 'action', 'equals': 'Update'}], 'fields': [
                {'path': 'EventDetail/TypeId', 'value': 'Update'},
                {'path': 'EventDetail/Update/After/Object/Id', 'field': 'item'},
                {'path': 'EventDetail/Update/Data', 'data_name': 'subject', 'field': 'subject'}]}]}


async def main():
    stroom = StroomGateway(e2e.target_settings())
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    try:
        await run(ctx, stroom, e2e.STAMP)
        print('\nALL PASSED')
    finally:
        await stroom.close()


async def run(ctx, stroom: StroomGateway, stamp: str) -> None:
    build, feed = f'frag-{stamp}', f'FRAG-{stamp}'
    print('\n### 1. the sample: XML fragments, and what reads them')
    profile = await feeds.profile_sample(ctx, SAMPLE)
    setup = profile
    e2e.check(profile['format'] == 'xml fragments' and setup.get('text_converter', {}).get('type') == 'XML_FRAGMENT',
              f"profiled as {profile['format']}, with the XMLFragmentParser's wrapper: {profile.get('suggested_parser', '')[:80]}")
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, SAMPLE))['stream_id']
    global NAMESPACE
    NAMESPACE = setup['xslt_input']['namespace']
    tc = await translation.create_text_converter(ctx, build, feed, 'XML_FRAGMENT', setup['text_converter']['code'])

    print('\n### 3. the regex, tried on the sample (before the pipeline, as an agent drafting would)')
    started = time.perf_counter()
    refused = await generation.build_translation_xslt(ctx, mapping(DASHED), stream_ids=[raw])
    took = time.perf_counter() - started
    problem = next((p for p in refused.get('problems') or [] if 'extract[0]' in p), '')
    e2e.check(not refused['ok'] and 'matches none of the 4 sample texts' in problem and 'EN DASH (U+2013)' in problem,
              f"a regex with '-' for the en dash is refused, saying where and why: {problem[:260]}")
    e2e.check(took < 15, f"answered in {took:.1f}s")
    fixed = DASHED.replace(' - ', ' – ')
    started = time.perf_counter()
    saved = await e2e.agreed(generation.build_translation_xslt, ctx=ctx, mapping=mapping(fixed), stream_ids=[raw],
                             build=build, name=f'{feed}-Events')
    e2e.check(saved['ok'] and saved.get('saved'), f"with the en dash, saved in {time.perf_counter() - started:.1f}s: "
                                                  f"{saved.get('problems')}")

    print('\n### 2. the pipeline: Event Data (XML), its parser replaced')
    template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                    if c['name'] == 'Event Data (XML)')
    pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                template_uuid=template['uuid'], replace_parser='XMLFragmentParser')
    layers = await stroom.pipeline_layers(pipeline['uuid'])
    merged = pipelines.merge_layers(layers)
    chain = pipelines.chain_order(merged['elements'], merged['links'])
    types = {e['id']: e['type'] for e in merged['elements']}
    # Source is implicit here (the chain starts at the element nothing links to); where it is stored, it links on.
    first = [t for t in (types[e] for e in chain) if t != 'Source'][0]
    e2e.check(first == 'XMLFragmentParser' and 'XMLParser' not in types.values()
              and all(link['to'] != 'xmlParser' and link['from'] != 'xmlParser' for link in merged['links']),
              f"the XMLFragmentParser starts the chain, in the XMLParser's place: {[types[e] for e in chain]}")
    converter = next((p.get('value') for p in merged['properties']
                      if types.get(p['element']) == 'XMLFragmentParser' and p['name'] == 'textConverter'), None)
    e2e.check((converter or {}).get('uuid') == tc['uuid'], "with the build's wrapper converter set on it")

    print('\n### 3. stepped clean, with the user and action from every record')
    sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    e2e.check(sample['verdict'] == 'clean', f"{sample['records_stepped']} records stepped clean: "
                                            f"{[(g['class'], g.get('reason')) for g in sample['groups']]}")
    users, kinds = [], []
    for record in range(sample['records_stepped']):
        output = (await stepping.step_pipeline(ctx, pipeline['uuid'], raw, record))['elements']['translationFilter']['output']
        valid = await validation.validate_events(ctx, output)
        e2e.check(valid['valid'], f"record {record} valid: {valid.get('errors')}")
        event = etree.fromstring(output.encode()).find('{event-logging:3}Event')
        e2e.check(event is not None, f"record {record} gave an Event")
        users.append(event.findtext('.//{event-logging:3}EventSource/{event-logging:3}User/{event-logging:3}Id'))
        kinds.append(event.findtext('.//{event-logging:3}TypeId'))
    e2e.check(users == ['joe.bloggs'] * 4 and kinds == ['Logon', 'Logoff', 'Logon', 'Update'],
              f"the login's user from every record: {users}, {kinds}")

    print('\n### 4. quick while iterating: the pipeline is stepped only when the preview is asked for')
    started = time.perf_counter()
    again = await generation.build_translation_xslt(ctx, mapping(fixed), stream_ids=[raw], pipeline_uuid=pipeline['uuid'])
    took = time.perf_counter() - started
    e2e.check(again['ok'] and again['field_mapping'] is None and took < 15,
              f"with pipeline_uuid but no field_mapping: not stepped, {took:.1f}s")
    started = time.perf_counter()
    preview = await generation.build_translation_xslt(ctx, mapping(fixed), stream_ids=[raw], pipeline_uuid=pipeline['uuid'],
                                                      field_mapping=True)
    e2e.check(bool(preview.get('field_mapping')) and preview['field_mapping_sample']['records'] == 4,
              f"asked for, the preview steps the sample ({time.perf_counter() - started:.1f}s)")


if __name__ == '__main__':
    asyncio.run(main())
