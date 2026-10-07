"""The formats sources send, each onboarded end to end against the local Stroom stack (see dev/stroom).

    uv run python dev/e2e_formats.py [case ...]

What the agent relies on the server for, for each format in dev/format_samples.py: profile_sample names the format
and parser; build_data_splitter infers the Data Splitter from the uploaded sample (never written by hand here) and
saves it; draft_translation_mapping drafts the mapping; the agent's own decisions are made (a host when none is
recognised, a regex for a free-form message, names for headerless columns); build_translation_xslt checks it
against the sample and saves it; the pipeline is a child of the template the profile names; Stroom steps every
record clean, each Event valid, with the record's user and time.

Delimited (quoted CSV with "" inside quotes, TSV, pipe, no header), syslog (RFC 3164 free-form, RFC 5424 with a
key=value body), CEF (alone, and after a syslog header), key=value with quoted values, JSON lines and a nested
JSON array, XML documents (records/record, attributes, a prefixed namespace) and XML fragments with their own
namespace.
"""
import asyncio
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from format_samples import SAMPLES  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import feeds, generation, pipeline_writes, stepping, templates, translation, validation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

EVT = '{event-logging:3}'
USERS = ['alice', 'bob', 'carol']
TEXT, JSON, XML = 'Event Data (Text)', 'Event Data (JSON)', 'Event Data (XML)'

SSH = {'xpath': None, 'field': 'message',
       'regex': r'^(Accepted|Failed) (\S+) for (\S+) from (\S+) port (\d+)', 'names': ['outcome', 'method', 'user', 'ip', 'port']}


def logon_rules(kind_field: str, logon: str | list[str], logoff: str | None, user: str) -> list[dict]:
    rules = [{'name': 'logon', 'when': [{'field': kind_field, **({'one_of': logon} if isinstance(logon, list) else {'equals': logon})}],
              'fields': [{'path': 'EventDetail/TypeId', 'value': 'Logon'},
                         {'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                         {'path': 'EventDetail/Authenticate/User/Id', 'field': user}]}]
    if logoff:
        rules.append({'name': 'logoff', 'when': [{'field': kind_field, 'equals': logoff}],
                      'fields': [{'path': 'EventDetail/TypeId', 'value': 'Logoff'},
                                 {'path': 'EventDetail/Authenticate/Action', 'value': 'Logoff'},
                                 {'path': 'EventDetail/Authenticate/User/Id', 'field': user}]})
    return rules


# Each case: the format the profile names, the template, the user field (the agent maps it where the draft didn't),
# the users expected in order, and any decision the agent makes beyond the draft.
CASES = {
    'csv_quoted': {'format': 'delimited', 'template': TEXT, 'user': 'user', 'users': ['alice', "o'brien, pat", 'carol'],
                   'values': {'Description': ['Logged in, from the VPN', 'Said "bye" and left', 'Plain message']}},
    'tsv': {'format': 'delimited', 'template': TEXT, 'user': 'user', 'users': USERS},
    'pipe': {'format': 'delimited', 'template': TEXT, 'user': 'user', 'users': USERS},
    # No header: the agent names the columns, and writes the rules on the named action column.
    'csv_noheader': {'format': 'delimited', 'template': TEXT, 'user': 'user', 'users': USERS,
                     'spec': {'kind': 'delimited', 'delimiter': ',', 'header': ['time', 'user', 'src_ip', 'action']},
                     'rules': logon_rules('action', 'login', 'logout', 'user')},
    # A free-form message: the agent writes the regex.
    'syslog3164_freeform': {'format': 'syslog rfc3164', 'template': TEXT, 'user': 'user', 'users': USERS,
                            'extract': [SSH], 'rules': logon_rules('outcome', ['Accepted', 'Failed'], None, 'user')},
    'syslog5424_kv': {'format': 'syslog rfc5424', 'template': TEXT, 'user': 'user', 'users': ['alice', None, 'carol']},
    'cef': {'format': 'cef', 'template': TEXT, 'user': 'suser', 'users': USERS},
    'syslog_cef': {'format': 'cef', 'template': TEXT, 'user': 'suser', 'users': USERS},
    'kv_quoted': {'format': 'key=value', 'template': TEXT, 'user': 'user', 'users': ['alice', 'bob smith', 'carol']},
    'json_lines': {'format': 'json lines', 'template': JSON, 'user': 'user.name', 'users': USERS},
    'json_array_nested': {'format': 'json array', 'template': JSON, 'user': 'actor.user', 'users': USERS},
    'xml_records': {'format': 'xml', 'template': XML, 'user': 'user', 'users': USERS},
    'xml_attributes': {'format': 'xml', 'template': XML, 'user': '@user', 'users': USERS},
    'xml_namespaced': {'format': 'xml', 'template': XML, 'user': 'User', 'users': USERS},
    'xml_fragments_ns': {'format': 'xml fragments', 'template': XML, 'replace_parser': 'XMLFragmentParser',
                         'user': "EventData/Data[@Name='TargetUserName']", 'users': USERS, 'user_is_xpath': True},
}


def decide(mapping: dict, case: dict) -> dict:
    """The agent's decisions beyond the draft: what only it (or the user) can say."""
    m = copy.deepcopy(mapping)
    common = m.setdefault('common', [])
    paths = {e['path'] for e in common}
    if not any(p.startswith('EventSource/Device/') for p in paths):
        common.append({'path': 'EventSource/Device/HostName', 'value': 'acme-gw01'})
    if 'EventSource/User/Id' not in paths:
        common.append({'path': 'EventSource/User/Id', **({'xpath': case['user']} if case.get('user_is_xpath')
                                                         else {'field': case['user']})})
    if case.get('extract'):
        m['extract'] = [{k: v for k, v in x.items() if v is not None} for x in case['extract']]
    if case.get('rules'):
        m['events'] = case['rules']
    return m


async def onboard(ctx, name: str, case: dict, stamp: str) -> None:
    print(f'\n### {name}')
    sample = SAMPLES[name]
    build, feed = f"fmt-{name.replace('_', '-')}-{stamp}", f"FMT-{name.upper().replace('_', '-')}-{stamp}"
    profile = await feeds.profile_sample(ctx, sample)
    e2e.check(profile['format'] == case['format'], f"profiled as {profile['format']}: {profile.get('suggested_parser', '')[:90]}")
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, sample))['stream_id']
    splitter = None
    if case['template'] == TEXT:
        ds = await generation.build_data_splitter(ctx, stream_ids=[raw], spec=case.get('spec'), save_as=feed, build=build)
        e2e.check(ds.get('records') == 3 and not ds.get('unmatched_count') and ds.get('saved'),
                  f"the Data Splitter ({ds['spec'].get('kind')}{', inferred' if ds.get('inferred') else ''}), saved, "
                  f"parses every line: fields {[f.get('field', f) if isinstance(f, dict) else f for f in ds.get('fields') or []][:14]}")
        splitter = ds['spec']
    elif case.get('replace_parser'):
        await translation.create_text_converter(ctx, build, feed, 'XML_FRAGMENT', profile['text_converter']['code'])
    draft = await generation.draft_translation_mapping(ctx, stream_ids=[raw], source_name='Acme', system_name='Acme',
                                                       environment='Test', splitter=splitter)
    mapping = decide(draft['mapping'], case)
    saved = await e2e.agreed(generation.build_translation_xslt, ctx=ctx, mapping=mapping, stream_ids=[raw],
                             **({'splitter': splitter} if splitter else {}), build=build, name=f'{feed}-Events')
    e2e.check(saved['ok'] and saved.get('saved'), f"the mapping (kinds {[r['name'] for r in mapping['events']]}) checked "
                                                   f"against the sample and saved: {saved.get('problems')}")
    template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                    if c['name'] == case['template'])
    pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                template_uuid=template['uuid'],
                                **({'replace_parser': case['replace_parser']} if case.get('replace_parser') else {}))
    stepped = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    e2e.check(stepped['verdict'] == 'clean' and stepped['records_stepped'] == 3,
              f"{stepped['records_stepped']} records stepped in Stroom: {stepped['verdict']} "
              f"{[(g['class'], g.get('reason'), (g.get('examples') or [{}])[0].get('message', '')[:160]) for g in stepped['groups']]}")
    users, values = [], {}
    for record in range(stepped['records_stepped']):
        output = (await stepping.step_pipeline(ctx, pipeline['uuid'], raw, record))['elements']['translationFilter']['output']
        valid = await validation.validate_events(ctx, output)
        e2e.check(valid['valid'], f"record {record} valid: {valid.get('errors')}")
        event = etree.fromstring(output.encode()).find(f'{EVT}Event')
        e2e.check(event is not None and event.findtext(f'{EVT}EventTime/{EVT}TimeCreated'),
                  f"record {record}: an Event with its time {event.findtext(f'{EVT}EventTime/{EVT}TimeCreated') if event is not None else None}")
        users.append(event.findtext(f'{EVT}EventSource/{EVT}User/{EVT}Id'))
        for element in case.get('values') or {}:
            values.setdefault(element, []).append(event.findtext(f'.//{EVT}{element}'))
    e2e.check(users == case['users'], f"each record's user: {users}")
    for element, expected in (case.get('values') or {}).items():
        e2e.check(values[element] == expected, f"{element} as the source wrote it: {values[element]}")


async def main():
    stroom = StroomGateway(e2e.target_settings())
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    failed = []
    try:
        for name in sys.argv[1:] or list(CASES):
            try:
                await onboard(ctx, name, CASES[name], e2e.STAMP)
            except SystemExit:
                failed.append(name)
            except Exception as e:      # a tool refusing is a finding too: report it and go on to the next format
                print(f'  FAIL {type(e).__name__}: {str(e)[:400]}')
                failed.append(name)
        print(f"\n{'ALL PASSED' if not failed else f'FAILED: {failed}'}")
    finally:
        await stroom.close()
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
