"""An events pipeline in a build brought up to the generator's current style, against the local Stroom stack, as asked
of an agent in production ("update the firewall Events pipeline that's still in staging to conform with your default
XSLT style").

    uv run python dev/e2e_restyle.py

A FortiGate-like source: JSON records whose body is key="value" pairs, in three streams, the VPN records only in the
last (past the 300th record, as in production, where they began at the 557th of 3,933). Then:

1. The mapping saved over all three streams: every record is checked, so the VPN rule's keys are found; over the
   first stream alone, a key it lacks is named as such, not "matches as far as ``".
2. An earlier generator simulated: the XSLT written with variables at the top and xsl:attribute values, and the
   mapping kept as an earlier server kept it (no style, then the defaults). build_status says it was written by an
   earlier generator and is unchanged, not "edited by hand", and names the call that regenerates it.
3. describe_document on the XSLT summarises the kept mapping (its rules, the calls that change or regenerate it)
   rather than returning it whole.
4. Regenerated with uuid alone (no mapping sent): one function a key=value shape, the parts the Network rules share
   written once, variables just in time, Data values interpolated; smaller; it steps clean, and every record's Event,
   the VPN ones included, is what the old XSLT wrote. build_status has nothing to say about it.
4b. A hand edit (a Data element added, the Rule no longer written): build_status names both lines and
   rebuild_mapping; rebuild_mapping carries them into the mapping, proven on 200 records stepped in Stroom, and the
   Events are those the hand edit wrote. 4c. The XSLT's Documentation tab cleared: the loss named everywhere, and
   rebuild_mapping reads the mapping back from the XSLT alone (its key=value extractions too). 4d. An edit no mapping
   can express (a Data element written even when empty): refused with the differences, saved once accepted.
5. An edit made by hand (a call to a function Stroom doesn't have): build_status says edited by hand; stepping it is
   blocking, naming the missing function. Regenerated again, it is clean.
6. A function call given as a field is refused, with the extract list named instead.
"""
import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from e2e_xslt_style import canonical  # noqa: E402
from lxml import etree  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import explorer, feeds, generation, pipeline_writes, plan, rebuild, stepping, templates  # noqa: E402
from tools.pipeline_writes import PropertyValue  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.mappingstore import read_mapping, with_mapping  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

check, agreed = e2e.check, e2e.agreed
NS = {'e': 'event-logging:3'}


def traffic(n: int, action: str) -> dict:
    body = (f'date=2026-10-01 time=10:{n // 60 % 60:02d}:{n % 60:02d} eventtime=17909317{n:010d} tz="+1000" '
            f'logid="000000{n % 3}013" type="traffic" subtype="{"forward" if n % 2 else "local"}" level="notice" '
            f'vd="root" srcip=10.0.{n % 7}.{n % 250 + 1} srcport={1024 + n} srcintf="port{n % 3}" srcintfrole="lan" '
            f'dstip=192.0.2.{n % 200 + 1} dstport={(443, 53, 22)[n % 3]} dstintf="wan1" dstintfrole="wan" '
            f'policyid={n % 5} proto={(6, 17, 1)[n % 3]} action="{action}" service="{("HTTPS", "DNS", "SSH")[n % 3]}"')
    return {'priority': 189, 'timestamp': f'2026-10-01T10:{n // 60 % 60:02d}:{n % 60:02d}Z', 'hostname': 'fw01',
            'body': body}


def vpn(n: int, action: str) -> dict:
    body = (f'date=2026-10-01 time=11:00:{n:02d} eventtime=17909400{n:010d} tz="+1000" logid="0101037124" '
            f'type="event" subtype="vpn" level="error" vd="root" logdesc="IPsec phase 1 error" action="{action}" '
            f'remip=203.0.113.{n + 1} locip=198.51.100.2 remport=500 locport=500 outintf="wan1" vpntunnel="hq-{n}" '
            f'status="{"failure" if action == "negotiate" else "success"}"')
    return {'priority': 187, 'timestamp': f'2026-10-01T11:00:{n:02d}Z', 'hostname': 'fw01', 'body': body}


ACTIONS = ('deny', 'accept', 'close')
STREAMS = [[traffic(n, ACTIONS[n % 3]) for n in range(150)],
           [traffic(n, ACTIONS[n % 3]) for n in range(150, 270)],
           [traffic(n, ACTIONS[n % 3]) for n in range(270, 320)] + [vpn(n, ('negotiate', 'tunnel-stats')[n % 2])
                                                                       for n in range(6)]]
QUOTED = ['logid', 'subtype', 'level', 'vd', 'srcintf', 'srcintfrole', 'dstintf', 'dstintfrole', 'action', 'service',
          'logdesc', 'outintf', 'vpntunnel', 'status']
BARE = ['srcip', 'srcport', 'dstip', 'dstport', 'policyid', 'proto', 'remip', 'locip', 'remport', 'locport']
PROTO = {'map': {'1': 'ICMP', '6': 'TCP', '17': 'UDP'}, 'default': 'Other'}


def network(name: str, actions: list[str], element: str, success: str) -> dict:
    at = f'EventDetail/Network/{element}'
    return {'name': name, 'when': [{'field': 'action', 'one_of': actions}],
            'fields': [{'path': 'EventDetail/TypeId', 'value': name},
                       {'path': f'{at}/Source/Device/IPAddress', 'field': 'srcip'},
                       {'path': f'{at}/Source/Port', 'field': 'srcport'},
                       {'path': f'{at}/Source/TransportProtocol', 'field': 'proto', **PROTO},
                       {'path': f'{at}/Destination/Device/IPAddress', 'field': 'dstip'},
                       {'path': f'{at}/Destination/Port', 'field': 'dstport'},
                       {'path': f'{at}/Destination/TransportProtocol', 'field': 'proto', **PROTO},
                       {'path': f'{at}/Destination/ApplicationProtocol', 'field': 'service'},
                       {'path': f'{at}/Rule', 'field': 'policyid'},
                       {'path': f'{at}/Outcome/Success', 'value': success}]
            + [{'path': f'{at}/Data', 'data_name': k, 'field': k}
               for k in ('logid', 'subtype', 'level', 'vd', 'srcintf', 'srcintfrole', 'dstintf', 'dstintfrole')]}


MAPPING = {
    'input': 'json', 'json_layout': 'array', 'unmatched': 'warn',
    'extract': [{'field': 'body', 'regex': f'(?:^|\\s){k}="([^"]*)"', 'names': [k]} for k in QUOTED]
    + [{'field': 'body', 'regex': f'(?:^|\\s){k}=(\\S+)', 'names': [k]} for k in BARE],
    'common': [{'path': 'EventTime/TimeCreated', 'field': 'timestamp', 'time_format': "yyyy-MM-dd'T'HH:mm:ssX"},
               {'path': 'EventSource/System/Name', 'value': 'FortiOS'},
               {'path': 'EventSource/System/Environment', 'value': 'Test'},
               {'path': 'EventSource/Generator', 'value': 'FortiOS'},
               {'path': 'EventSource/Device/HostName', 'field': 'hostname'}],
    'events': [network('traffic-deny', ['deny'], 'Deny', 'false'),
               network('traffic-permit', ['accept'], 'Permit', 'true'),
               network('traffic-close', ['close'], 'Close', 'true'),
               {'name': 'vpn', 'when': [{'field': 'action', 'one_of': ['negotiate', 'tunnel-stats']}],
                'fields': [{'path': 'EventDetail/TypeId', 'value': 'VpnConnect'},
                           {'path': 'EventDetail/Description', 'field': 'logdesc'},
                           {'path': 'EventDetail/Network/Connect/Source/Device/IPAddress', 'field': 'locip'},
                           {'path': 'EventDetail/Network/Connect/Source/Port', 'field': 'locport'},
                           {'path': 'EventDetail/Network/Connect/Destination/Device/IPAddress', 'field': 'remip'},
                           {'path': 'EventDetail/Network/Connect/Destination/Port', 'field': 'remport'},
                           {'path': 'EventDetail/Network/Connect/Outcome/Success', 'field': 'action',
                            'map': {'negotiate': 'false', 'tunnel-stats': 'true'}}]
                + [{'path': 'EventDetail/Network/Connect/Data', 'data_name': k, 'field': k}
                   for k in ('logid', 'vpntunnel', 'status', 'outintf')]}]}


async def events_of(ctx, pipeline_uuid: str, raw: int, records: range) -> list[bytes]:
    """Each record's Event as Stroom writes it, canonical, to compare two XSLTs' output."""
    out = []
    for record in records:
        output = (await stepping.step_pipeline(ctx, pipeline_uuid, raw, record))['elements']['translationFilter']['output']
        out += [canonical(e) for e in etree.fromstring(output.encode()).findall('e:Event', NS)]
    return out


def just_in_time(code: str) -> bool:
    """Whether any variable is declared after other content of its template, or inside it, rather than at its start."""
    xsl = '{http://www.w3.org/1999/XSL/Transform}'
    for variable in etree.fromstring(code.encode()).iter(f'{xsl}variable'):
        parent = variable.getparent()
        if parent.tag != f'{xsl}template':
            if parent.tag != f'{xsl}stylesheet':
                return True
            continue
        if any(isinstance(s.tag, str) and s.tag != f'{xsl}variable' for s in variable.itersiblings(preceding=True)):
            return True
    return False


async def drift_of(ctx, build: str, pipeline_name: str) -> str:
    status = await plan.build_status(ctx, build)
    return ' '.join(p for p in status.get('before_promotion') or [] if pipeline_name in p and 'XSLT' in p)


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
    build, feed = f'restyle-{stamp}', f'FW-JSON-{stamp}'
    print('\n### 1. a FortiGate-like feed, its VPN records past the 300th; the mapping checked against every record')
    await agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raws = [(await feeds.upload_sample(ctx, feed, json.dumps(records)))['stream_id'] for records in STREAMS]
    check(all(raws), f"streams {raws}: {[len(s) for s in STREAMS]} records")
    first_only = await generation.build_translation_xslt(ctx, MAPPING, stream_ids=[raws[0]])
    [lacking] = [p for p in first_only['problems'] if "['vpntunnel']" in p]
    check('None of these texts has `vpntunnel="`' in lacking and 'its records may be elsewhere' in lacking,
          f"the first stream alone lacks the VPN keys, and says so: {lacking[:150]}")
    old_style = {**MAPPING, 'style': {'variables': 'top', 'data_values': 'attribute', 'data_entries': 'guarded',
                                      'data_run_min': 99}}
    built = await generation.build_translation_xslt(ctx, old_style, stream_ids=raws, build=build, name=f'{feed}-Events',
                                                    agent_model='e2e-model', include_xslt=False)
    check(built['ok'] and not any('vpntunnel' in p for p in built['problems']),
          f"over all {sum(len(s) for s in STREAMS)} records, the VPN keys are found: {built['problems'][:2]}")
    xslt = built['saved']
    template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                    if c['name'] == 'Event Data (JSON)')
    pipeline = await agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                            template_uuid=template['uuid'], set_properties=[
                                PropertyValue(element='jsonParser', name='addRootObject', value=False),
                                PropertyValue(element='translationFilter', name='xslt', doc_uuid=xslt['uuid'],
                                              doc_type='XSLT')])

    print('\n### 2. as an earlier generator left it: build_status names a generator upgrade, not a hand edit')
    doc = await stroom.get_doc('XSLT', xslt['uuid'])
    old_code = doc['data']
    kind, payload = read_mapping(doc['description'])
    payload['mapping'].pop('style')      # an earlier server kept no style: those were its defaults
    doc['description'] = with_mapping(doc['description'], kind, payload)
    await stroom.put_doc(doc)
    drift = await drift_of(ctx, build, pipeline['name'])
    check('written by an earlier generator' in drift and 'unchanged since' in drift and 'by hand' not in drift
          and f"uuid='{xslt['uuid']}' (no mapping) regenerates it" in drift, f"build_status: {drift[:220]}")
    stepped = await stepping.step_sample(ctx, pipeline['uuid'], raws)
    check(stepped['verdict'] == 'clean', f"the old XSLT steps clean: {stepped['verdict']}")
    checked = [(raws[0], range(6)), (raws[2], range(44, 56))]       # traffic, and the VPN records at the end
    before = [e for raw, records in checked for e in await events_of(ctx, pipeline['uuid'], raw, records)]
    check(len(before) == 18 and sum(b'VpnConnect' in e for e in before) == 6, f"{len(before)} Events, 6 of them VPN")

    print('\n### 3. describe_document summarises the kept mapping instead of returning it')
    described = await explorer.describe_document(ctx, 'XSLT', xslt['uuid'])
    summary = described['kept_mapping']
    check(summary['kind'] == 'translation' and len(summary['rules']) == 4 and summary['extractions'] == 24
          and summary['style'] == "the generator's current defaults" and '"extract"' not in described['description'],
          f"summary: {len(summary['rules'])} rules, {summary['extractions']} extractions, style {summary['style']}")
    check(f"uuid='{xslt['uuid']}' alone regenerates it" in summary['how'], summary['how'][:120])
    whole = await explorer.describe_document(ctx, 'XSLT', xslt['uuid'], mapping=True)
    check(len(whole['kept_mapping']['mapping']['extract']) == 24, 'mapping=true gives it whole, to read')

    print('\n### 4. regenerated with uuid alone: the current style, the same Events')
    regenerated = await generation.build_translation_xslt(ctx, uuid=xslt['uuid'], stream_ids=raws, include_xslt=False)
    check(regenerated['ok'] and regenerated.get('saved'), f"regenerated and saved: {regenerated['problems'][:2]}")
    new_code = (await stroom.get_doc('XSLT', xslt['uuid']))['data']
    check('<xsl:function name="mcp:quoted_value"' in new_code and "mcp:quoted_value($body, 'dstintfrole')" in new_code
          and '<xsl:function name="mcp:value"' in new_code, 'one function a key=value shape, called with the key')
    check('mode="source"' in new_code and 'mode="destination"' in new_code,
          "the Network rules' Source and Destination written once, whatever their action element")
    check("<xsl:sequence select=\"mcp:data('dstintfrole', mcp:quoted_value($body, 'dstintfrole'))\"/>" in new_code
          and '<xsl:attribute name="Value"' not in new_code and '<Data Name="dstintfrole"' not in new_code,
          'Data entries one line each, through mcp:data')
    runs = sorted(set(re.findall(r'mode="(data_[^"]+)"', new_code)))
    check(bool(runs) and all(new_code.count(f'mode="{r}"/>') >= 2 for r in runs),
          f"the Data runs the Network rules share, written once: {runs}")
    check(not just_in_time(old_code) and just_in_time(new_code) and len(new_code) < len(old_code),
          f"variables declared where first used, not at each template's start; {len(old_code):,} -> "
          f"{len(new_code):,} characters")
    pending = (await explorer.describe_document(ctx, 'XSLT', xslt['uuid']))['pending_changes']
    check(pending[-1] == 'Regenerated from its mapping', f"pending changes: {pending}")
    shown = (await explorer.describe_document(ctx, 'XSLT', xslt['uuid']))['description']
    preview = [line.strip() for line in shown.splitlines() if line.strip().startswith('| Unreleased |')]
    check(len(preview) == 1 and 'Created from its mapping; Regenerated from its mapping' in preview[0]
          and 'version history' not in new_code,
          f"the build's changes previewed in its Documentation, Unreleased, the code left alone: {preview}")
    stepped = await stepping.step_sample(ctx, pipeline['uuid'], raws)
    check(stepped['verdict'] == 'clean', f"steps clean: {stepped['verdict']}")
    # Seen in VS Code: promotion said no clean step of the regenerated code was recorded, though it had stepped clean.
    pipeline_doc = next(d for d in (await plan.build_status(ctx, build))['pipelines'] if d['uuid'] == pipeline['uuid'])
    check(pipeline_doc['stepped'], 'the clean step of the regenerated code is recorded, as promotion checks it')
    after = [e for raw, records in checked for e in await events_of(ctx, pipeline['uuid'], raw, records)]
    check(after == before, f"every Event the same as the old XSLT wrote ({len(after)}, the VPN ones included)")
    check(await drift_of(ctx, build, pipeline['name']) == '', 'build_status: nothing to say about the XSLT')

    print('\n### 4b. a hand edit: a new Data element, and the Rule no longer written; carried into the mapping')
    # Asked for by the user: someone changes the XSLT in Stroom's editor, to write a new field or to stop writing one.
    edited = await stroom.get_doc('XSLT', xslt['uuid'])
    deny = edited['data'].index('mode="event_type_traffic_deny"')
    end = edited['data'].index('</Deny>', deny)
    body = edited['data'][deny:end]
    rule_call = '<xsl:apply-templates select="." mode="rule"/>'
    check(rule_call in body, 'the Deny rule writes its Rule through the shared template')
    body = body.replace(rule_call, '', 1) + '<Data Name="collector" Value="{*[@key=\'hostname\']}"/>\n          '
    edited['data'] = edited['data'][:deny] + body + edited['data'][end:]
    await stroom.put_doc(edited)
    drift = await drift_of(ctx, build, pipeline['name'])
    check('edited by hand since the server saved it' in drift and '+ <Data Name="collector"' in drift
          and f'- {rule_call}' in drift, f"build_status names both changes: {drift[drift.find('What differs'):][:260]}")
    check((await stepping.step_sample(ctx, pipeline['uuid'], raws))['verdict'] == 'clean', 'the hand edit steps clean')
    by_hand = [e for raw, records in checked for e in await events_of(ctx, pipeline['uuid'], raw, records)]
    denied = [e for e in by_hand if b'<TypeId>traffic-deny</TypeId>' in e]
    check(bool(denied) and all(b'Name="collector"' in e and b'<Rule>' not in e for e in denied),
          f"its Deny events: the collector written, no Rule ({len(denied)} of them)")
    check('rebuild_mapping' in drift, 'build_status names rebuild_mapping to carry it in')
    redone = await agreed(rebuild.rebuild_mapping, ctx=ctx, uuid=xslt['uuid'], stream_ids=raws,
                          change='Collector for denied traffic, no rule id: a hand edit carried in')
    summary = redone['rebuilt']
    check(redone.get('saved') and summary['entries_new'] == ['rule traffic-deny: EventDetail/Network/Deny/Data Data collector']
          and summary['entries_removed'] == ['rule traffic-deny: EventDetail/Network/Deny/Rule']
          and summary['entries_kept'] > 50 and 'kept_as_xpath' not in summary,
          f"rebuild_mapping carried the edit in: {summary.get('entries_new')}, removed {summary.get('entries_removed')}, "
          f"{summary['entries_kept']} entries kept as they were")
    check(redone['proven_on']['records'] == 200 and 'differences' not in redone,
          f"proven on {redone['proven_on']['records']} records stepped in Stroom: the same output")
    kept = read_mapping((await stroom.get_doc('XSLT', xslt['uuid']))['description'])[1]['mapping']
    deny = next(r for r in kept['events'] if r['name'] == 'traffic-deny')
    check({'path': 'EventDetail/Network/Deny/Data', 'data_name': 'collector', 'field': 'hostname'} in deny['fields']
          and not any(f['path'].endswith('/Rule') for f in deny['fields']),
          'the kept mapping has the collector, read as a field, and no Rule for denied traffic')
    check(await drift_of(ctx, build, pipeline['name']) == '', 'build_status: the mapping and the XSLT agree again')
    check((await stepping.step_sample(ctx, pipeline['uuid'], raws))['verdict'] == 'clean', 'regenerated, it steps clean')
    carried_events = [e for raw, records in checked for e in await events_of(ctx, pipeline['uuid'], raw, records)]
    check(carried_events == by_hand, f"the same Events as the hand edit wrote ({len(carried_events)})")

    print("\n### 4c. the XSLT's Documentation tab cleared: the loss is named, and the mapping rebuilt from the XSLT")
    cleared = await stroom.get_doc('XSLT', xslt['uuid'])
    cleared['description'] = ''
    await stroom.put_doc(cleared)
    drift = await drift_of(ctx, build, pipeline['name'])
    check('was saved by stroom-mcp with the mapping (or plan) it is generated from' in drift and 'it is gone' in drift,
          f"build_status says it was lost: {drift[:160]}")
    try:
        await generation.build_translation_xslt(ctx, uuid=xslt['uuid'], stream_ids=raws)
        said = ''
    except Exception as e:
        said = str(e)
    check('it is gone' in said and 'rebuild_mapping' in said, f"regenerating is refused, saying why: {said[:140]}")
    lost = (await explorer.describe_document(ctx, 'XSLT', xslt['uuid']))['kept_mapping']
    check('lost' in lost, 'describe_document says so too')
    restored = await agreed(rebuild.rebuild_mapping, ctx=ctx, uuid=xslt['uuid'])      # no streams: it finds them
    summary = restored['rebuilt']
    check(restored.get('saved') and restored['mode'] == 'mapping lost' and summary['entries_kept'] == 0
          and 'kept_as_xpath' not in summary and 'differences' not in restored,
          f"rebuilt from the XSLT alone ({len(summary['entries_new'])} entries, none kept as xpath), proven on "
          f"{restored['proven_on']['records']} records")
    check(sorted(restored['proven_on']['streams']) == sorted(raws) and 'newest raw streams' in restored['proven_on']['which'],
          f"proven on {restored['proven_on']['which']} (stepped, never processed: no sample filters)")
    back = read_mapping((await stroom.get_doc('XSLT', xslt['uuid']))['description'])[1]['mapping']
    check(len(back['extract']) == len(MAPPING['extract']) and back['input'] == 'json'
          and {e['names'][0] for e in back['extract']} == {e['names'][0] for e in MAPPING['extract']},
          'its key=value extractions read back from the functions that write them')
    check(await drift_of(ctx, build, pipeline['name']) == '', 'build_status has nothing to say')
    after_loss = [e for raw, records in checked for e in await events_of(ctx, pipeline['uuid'], raw, records)]
    check(after_loss == by_hand, f"the same Events as before the loss ({len(after_loss)})")

    print("\n### 4d. an edit no mapping can express: refused with the differences, saved only once accepted")
    edited = await stroom.get_doc('XSLT', xslt['uuid'])
    deny = edited['data'].index('mode="event_type_traffic_deny"')
    end = edited['data'].index('</Deny>', deny)
    edited['data'] = edited['data'][:end] + '<Data Name="tunnel" Value="{*[@key=\'tunnel\']}"/>\n          ' + edited['data'][end:]
    await stroom.put_doc(edited)
    refused = await rebuild.rebuild_mapping(ctx, uuid=xslt['uuid'], stream_ids=raws)
    paths = [d['path'] for d in refused.get('differences') or []]
    check(refused.get('saved') is None and any('tunnel' in p for p in paths) and 'accept_differences' in refused['hint'],
          f"refused: no record has a tunnel, so the XSLT writes it empty and the mapping wouldn't: {paths[:2]}")
    accepted = await agreed(rebuild.rebuild_mapping, ctx=ctx, uuid=xslt['uuid'], stream_ids=raws, accept_differences=True)
    check(bool(accepted.get('saved')) and await drift_of(ctx, build, pipeline['name']) == '',
          'accepted: saved, the mapping and the XSLT in step')

    print('\n### 5. an edit made by hand is still called one, and stepping it names the missing function')
    edited = await stroom.get_doc('XSLT', xslt['uuid'])
    edited['data'] = edited['data'].replace("mcp:quoted_value($body, 'logid')", "stroom:extract($body, 'logid')", 1)
    await stroom.put_doc(edited)
    drift = await drift_of(ctx, build, pipeline['name'])
    check('edited by hand since the server saved it' in drift, f"build_status: {drift[:160]}")
    stepped = await stepping.step_sample(ctx, pipeline['uuid'], raws[:1])
    blocking = json.dumps(stepped.get('groups') or [])
    check(stepped['verdict'] == 'blocking' and 'function named' in blocking,
          f"stepping it: {stepped['verdict']}, {blocking[:160]}")
    again = await generation.build_translation_xslt(ctx, uuid=xslt['uuid'], stream_ids=raws, include_xslt=False,
                                                    change='Regenerated over a hand edit')
    check(again['ok'] and await drift_of(ctx, build, pipeline['name']) == '', 'regenerated again: clean')

    print('\n### 6. a function call given as a field is refused')
    called = {**MAPPING, 'common': MAPPING['common'] + [{'path': 'EventSource/User/Id', 'field': "stroom:extract(body, 'user')"}]}
    try:
        await generation.build_translation_xslt(ctx, called, stream_ids=raws[:1])
        said = ''
    except Exception as e:
        said = str(e)
    check('is a function call, not an input field' in said and 'extract' in said, f"refused: {said[:200]}")


if __name__ == '__main__':
    asyncio.run(main())
