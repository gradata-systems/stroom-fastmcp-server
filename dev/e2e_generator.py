"""Mapping-to-XSLT check against the local Stroom stack (see dev/stroom).

    uv run python dev/e2e_generator.py [firewall csv json xml syslog]

For each of the translation suite's samples, builds the translation the way a model would with build_translation_xslt: a
field mapping (time patterns from profile_sample), no hand-written XSLT. Then creates the pipeline in a
build, steps every record and validates the output against the instance's event-logging schema. The XML
sample carries a record no rule matches, which must come back as the generator's warning marker.
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
from security.policy import AccessPolicy  # noqa: E402
from tools import feeds, generation, pipeline_writes, stepping, templates, translation, validation  # noqa: E402
from tools.pipeline_writes import PropertyValue  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402
from utils.xsltgen import TranslationMapping  # noqa: E402

SYSTEM = [{'path': 'EventSource/System/Name', 'value': 'E2E'},
          {'path': 'EventSource/System/Environment', 'value': 'Dev'},
          {'path': 'EventSource/Generator', 'value': 'e2e-generator'}]


def logon(user: str, success: dict | None = None) -> list[dict]:
    fields = [{'path': 'EventDetail/TypeId', 'value': 'Logon'},
              {'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
              {'path': 'EventDetail/Authenticate/User/Id', 'field': user}]
    return fields + ([{'path': 'EventDetail/Authenticate/Outcome/Success', **success}] if success else [])


def common(time_field: str, pattern: str, host: str, ip: str, user: str) -> list[dict]:
    return [{'path': 'EventTime/TimeCreated', 'field': time_field, 'time_format': pattern}, *SYSTEM,
            {'path': 'EventSource/Device/HostName', 'field': host},
            {'path': 'EventSource/Device/IPAddress', 'field': ip},
            {'path': 'EventSource/User/Id', 'field': user}]


XML_SAMPLE = e2e.CASES['xml']['sample'].replace(
    '</logons>', '<logon><when>2026-09-28 12:02:00</when><who>judy</who><host>ws10</host><addr>10.0.2.10</addr>'
                 '<result>LOCKED</result></logon></logons>')

FIREWALL = {
    'template': 'Event Data (Text)',
    'converter': ('DATA_SPLITTER', e2e.CSV_SPLITTER),
    # Blank fields, some just a space, as the device writes them.
    'sample': 'timestamp,device,event_type,severity,src_ip,src_port,dst_ip,dst_port,protocol,action,rule_id,username,message\n'
              '2026-10-01T09:00:12+10:00,FW-EDGE-01,TRAFFIC,INFO,192.0.2.10,54321,198.51.100.20,443,TCP,ALLOW,1001,,Outbound HTTPS allowed\n'
              '2026-10-01T09:01:05+10:00,FW-EDGE-01,TRAFFIC,WARNING,203.0.113.45,49822,192.0.2.25,22,TCP,DENY,2003,,Inbound SSH blocked\n'
              '2026-10-01T09:10:03+10:00,FW-EDGE-01,SYSTEM,INFO,,,,,N/A,START,, ,Firewall logging service started\n'
              '2026-10-01T09:11:15+10:00,FW-EDGE-01,ADMIN,INFO,192.0.2.100,,,,HTTPS,LOGIN_SUCCESS,,admin,Administrator logged in\n'
              '2026-10-01T09:12:02+10:00,FW-EDGE-01,ADMIN,WARNING,203.0.113.90,,,,HTTPS,LOGIN_FAILED,,admin,Failed administrator login\n'
              '2026-10-01T09:16:08+10:00,FW-EDGE-01,ADMIN,INFO,192.0.2.100,,,,HTTPS,LOGOUT,,admin,Administrator logged out\n'
              '2026-10-01T09:17:24+10:00,FW-EDGE-01,SYSTEM,ERROR,,,,,N/A,VPN_TUNNEL_DOWN,,,VPN tunnel branch-01 went down\n'
              '2026-10-01T09:18:50+10:00,FW-EDGE-01,SYSTEM,NOTICE,,,,,N/A,VPN_TUNNEL_UP,,,VPN tunnel branch-01 restored\n',
}
ALLOW_DENY = {'field': 'action', 'map': {'ALLOW': 'true', 'DENY': 'false'}}
LOGIN = {'field': 'action', 'map': {'LOGIN_SUCCESS': 'true', 'LOGIN_FAILED': 'false'}}
ADMIN = [{'path': 'EventSource/Client/IPAddress', 'field': 'src_ip'},
         {'path': 'EventDetail/Authenticate/User/Id', 'field': 'username'}]

MAPPINGS = {
    # Value maps as xsl:map: one shared by Success and Permitted, one with a default; repeated elements as
    # named templates.
    'firewall': {
        'input': 'data_splitter',
        'common': [{'path': 'EventTime/TimeCreated', 'field': 'timestamp', 'time_format': "yyyy-MM-dd'T'HH:mm:ssXXX"},
                   *SYSTEM, {'path': 'EventSource/Device/HostName', 'field': 'device'},
                   {'path': 'EventSource/User/Id', 'field': 'username'},
                   {'path': 'EventDetail/TypeId', 'xpath': "concat(data[@name='event_type']/@value, '-', data[@name='action']/@value)"},
                   {'path': 'EventDetail/Description', 'field': 'message'}],
        'events': [
            {'name': 'traffic', 'when': [{'field': 'event_type', 'equals': 'TRAFFIC'}], 'fields': [
                {'path': 'EventDetail/Network/Open/Source/Device/IPAddress', 'field': 'src_ip'},
                {'path': 'EventDetail/Network/Open/Source/Port', 'field': 'src_port'},
                {'path': 'EventDetail/Network/Open/Destination/Device/IPAddress', 'field': 'dst_ip'},
                {'path': 'EventDetail/Network/Open/Destination/Port', 'field': 'dst_port'},
                {'path': 'EventDetail/Network/Open/Outcome/Success', **ALLOW_DENY},
                {'path': 'EventDetail/Network/Open/Outcome/Permitted', **ALLOW_DENY},
                {'path': 'EventDetail/Network/Open/Data', 'data_name': 'RuleId', 'field': 'rule_id'}]},
            {'name': 'logon', 'when': [{'field': 'event_type', 'equals': 'ADMIN'},
                                       {'field': 'action', 'one_of': ['LOGIN_SUCCESS', 'LOGIN_FAILED']}],
             'fields': ADMIN + [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'},
                                {'path': 'EventDetail/Authenticate/Outcome/Success', **LOGIN}]},
            {'name': 'logoff', 'when': [{'field': 'event_type', 'equals': 'ADMIN'}, {'field': 'action', 'equals': 'LOGOUT'}],
             'fields': ADMIN + [{'path': 'EventDetail/Authenticate/Action', 'value': 'Logoff'}]},
            {'name': 'system', 'when': [{'field': 'event_type', 'equals': 'SYSTEM'}], 'fields': [
                {'path': 'EventDetail/Alert/Type', 'value': 'Other'},
                {'path': 'EventDetail/Alert/Severity', 'field': 'severity',
                 'map': {'WARNING': 'Minor', 'ERROR': 'Major', 'CRITICAL': 'Critical'}, 'default': 'Info'}]}]},

    'csv': {'input': 'data_splitter',
            'common': common('time', "yyyy-MM-dd'T'HH:mm:ss", 'host', 'ip', 'user'),
            'events': [{'name': 'failed logon', 'when': [{'field': 'result', 'equals': 'fail'}],
                        'fields': logon('user', {'value': 'false'})
                        + [{'path': 'EventDetail/Description', 'value': 'Failed logon'},
                           {'path': 'EventDetail/Authenticate/Data', 'data_name': 'result', 'field': 'result'}]},
                       {'name': 'logon', 'fields': logon('user', {'field': 'result', 'map': {'ok': 'true'}})}]},
    'json': {'input': 'json',
             'common': common('ts', "yyyy-MM-dd'T'HH:mm:ssX", 'host', 'src', 'user'),
             'events': [{'name': 'logon', 'fields': logon('user', {'field': 'ok', 'map': {'true': 'true', 'false': 'false'}})}]},
    'xml': {'input': 'xml', 'root': 'logons', 'record': 'logon',
            'common': common('when', 'yyyy-MM-dd HH:mm:ss', 'host', 'addr', 'who'),
            'events': [{'name': 'logon', 'when': [{'field': 'result', 'one_of': ['SUCCESS', 'FAILURE']}],
                        'fields': logon('who', {'field': 'result', 'map': {'SUCCESS': 'true', 'FAILURE': 'false'}})}]},
    'syslog': {'input': 'data_splitter',
               'common': common('time', "yyyy-MM-dd'T'HH:mm:ss.SSSX", 'host', 'ip', 'user'),
               'events': [{'name': 'ssh logon', 'when': [{'field': 'app', 'equals': 'sshd'}],
                           'fields': logon('user', {'value': 'true'})
                           + [{'path': 'EventDetail/Authenticate/Data', 'data_name': 'method', 'field': 'method'}]}]},
}


async def run(ctx, fmt: str, stamp: str) -> None:
    print(f'\n### {fmt}')
    case = FIREWALL if fmt == 'firewall' else {**e2e.CASES[fmt], **({'sample': XML_SAMPLE} if fmt == 'xml' else {})}
    build, feed_name = f'gen-{fmt}-{stamp}', f'GEN-{fmt.upper()}-{stamp}'
    generated = await generation.build_translation_xslt(ctx, TranslationMapping.model_validate(MAPPINGS[fmt]))
    e2e.check(generated['ok'], f"generated from the mapping: problems {generated['problems']}")
    for warning in generated['warnings']:
        print(f'    warning: {warning}')
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed_name)
    raw = (await feeds.upload_sample(ctx, feed_name, case['sample']))['stream_id']
    template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                    if c['name'] == case['template'])
    props = []
    if 'converter' in case:
        tc = await translation.create_text_converter(ctx, build, feed_name, *case['converter'])
        props.append(PropertyValue(element='dsParser', name='textConverter', doc_uuid=tc['uuid'], doc_type='TextConverter'))
    x = await translation.create_xslt(ctx, build, f'{feed_name}-Events', generated['xslt'])
    props.append(PropertyValue(element='translationFilter', name='xslt', doc_uuid=x['uuid'], doc_type='XSLT'))
    if fmt == 'json':
        props.append(PropertyValue(element='jsonParser', name='addRootObject', value=False))
    pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed_name}-Events',
                               template_uuid=template['uuid'], set_properties=props)
    sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    groups = [(g['class'], g['severity'], g.get('examples') or g.get('message')) for g in sample['groups']]
    if fmt == 'xml':
        unmatched = [g for g in sample['groups'] if 'No event mapping matched record' in str(g)]
        e2e.check(len(unmatched) == 1 and unmatched[0]['count'] == 1,
                 f"the LOCKED record is reported by the generator's warning, not dropped silently: {groups}")
        e2e.check(all(g['class'] != 'blocking' for g in sample['groups']), 'nothing blocking')
    else:
        e2e.check(sample['verdict'] == 'clean', f"stepped {sample['records_stepped']} records clean: {groups}")
    for record in range(sample['records_stepped']):
        step = await stepping.step_pipeline(ctx, pipeline['uuid'], raw, record)
        output = step['elements']['translationFilter']['output']
        if '<Event>' not in output:
            continue
        valid = await validation.validate_events(ctx, output)
        e2e.check(valid['valid'], f"record {record} valid against {valid['schema']}: {valid['errors'][:2]}")
        quality = await validation.check_event_quality(ctx, output)
        e2e.check(quality['ok'], f"record {record} passes the quality checks: {quality.get('findings')}")


async def main():
    settings = e2e.target_settings()
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = e2e.STAMP
    try:
        for fmt in sys.argv[1:] or list(MAPPINGS):
            await run(ctx, fmt, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
