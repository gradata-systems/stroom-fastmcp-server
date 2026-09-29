"""Mapping-to-XSLT check against the local Stroom stack (see dev/stroom).

    uv run python dev/e2e_generator.py [csv json xml syslog]

For each Phase 2 sample, builds the translation the way a model would with build_translation_xslt: a
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

import e2e_phase2 as p2  # noqa: E402
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


XML_SAMPLE = p2.CASES['xml']['sample'].replace(
    '</logons>', '<logon><when>2026-09-28 12:02:00</when><who>judy</who><host>ws10</host><addr>10.0.2.10</addr>'
                 '<result>LOCKED</result></logon></logons>')

MAPPINGS = {
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
    case = {**p2.CASES[fmt], **({'sample': XML_SAMPLE} if fmt == 'xml' else {})}
    build, feed_name = f'gen-{fmt}-{stamp}', f'GEN-{fmt.upper()}-{stamp}'
    generated = await generation.build_translation_xslt(ctx, TranslationMapping.model_validate(MAPPINGS[fmt]))
    p2.check(generated['ok'], f"generated from the mapping: problems {generated['problems']}")
    for warning in generated['warnings']:
        print(f'    warning: {warning}')
    await p2.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed_name)
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
    pipeline = await p2.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed_name}-Events',
                               template_uuid=template['uuid'], properties=props)
    sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    groups = [(g['class'], g['severity'], g.get('examples') or g.get('message')) for g in sample['groups']]
    if fmt == 'xml':
        unmatched = [g for g in sample['groups'] if 'No event mapping matched record' in str(g)]
        p2.check(len(unmatched) == 1 and unmatched[0]['count'] == 1,
                 f"the LOCKED record is reported by the generator's warning, not dropped silently: {groups}")
        p2.check(all(g['class'] != 'blocking' for g in sample['groups']), 'nothing blocking')
    else:
        p2.check(sample['verdict'] == 'clean', f"stepped {sample['records_stepped']} records clean: {groups}")
    for record in range(sample['records_stepped']):
        step = await stepping.step_pipeline(ctx, pipeline['uuid'], raw, record)
        output = step['elements']['translationFilter']['output']
        if '<Event>' not in output:
            continue
        valid = await validation.validate_events(ctx, output)
        p2.check(valid['valid'], f"record {record} valid against {valid['schema']}: {valid['errors'][:2]}")
        quality = await validation.check_event_quality(ctx, output)
        p2.check(quality['ok'], f"record {record} passes the quality checks: {quality.get('findings')}")


async def main():
    settings = p2.target_settings()
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = p2.STAMP
    try:
        for fmt in sys.argv[1:] or list(MAPPINGS):
            await run(ctx, fmt, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
