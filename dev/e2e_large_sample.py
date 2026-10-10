"""Onboarding a sample far larger than any model's context, against the local Stroom stack (see dev/stroom).

    cd dev/stroom && docker compose up -d
    uv run python dev/e2e_large_sample.py [--records 200000]

A model cannot hold a 15 MB file, so it never sees it: the file reaches Stroom as the source would send it (here
posted to the datafeed receiver, as curl or the source system would), and the agent works from its stream id. Every
step an agent takes is a tool call here, and each reply is measured: none may exceed REPLY_BUDGET, a small part of
a model's context, whatever the file's size. The tools read a bounded part of the stream (max_sample_chars) to
profile, draft and check; stepping takes a bounded number of records; Stroom processes the whole stream itself.
The full record count then has to come out as events.
"""
import argparse
import asyncio
import json
import random
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
from tools import feeds, generation, pipeline_writes, processing_writes, stepping, streams, translation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

REPLY_BUDGET = 64_000          # characters of JSON a tool reply may take, whatever the file's size
USERS = ['alice', 'bob', 'carol', 'dave', 'erin', 'frank', 'grace', 'heidi']


def big_csv(records: int) -> str:
    """A VPN gateway's log: one logon or logoff per line, success or failure."""
    rng = random.Random(7)
    start = 1790000000
    lines = ['time,user,src_ip,gateway,action,result']
    for n in range(records):
        t = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(start + n * 3))
        lines.append(f"{t},{rng.choice(USERS)},10.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1, 255)},"
                     f"vpn-gw{rng.randrange(1, 4)},{rng.choice(['login', 'login', 'logout'])},"
                     f"{rng.choice(['success', 'success', 'success', 'failure'])}")
    return '\n'.join(lines) + '\n'


class Meter:
    """Each tool reply's size, as the model would receive it."""

    def __init__(self):
        self.sizes: dict[str, int] = {}

    async def __call__(self, label: str, call, /, **kwargs):
        result = await (e2e.agreed(call, **kwargs) if kwargs.pop('agreed', False) else call(**kwargs))
        size = len(json.dumps(result, default=str))
        self.sizes[label] = max(size, self.sizes.get(label, 0))
        print(f"    {label}: {size:,} characters")
        return result


async def run(ctx, stroom: StroomGateway, stamp: str, records: int) -> None:
    build, feed = f'e2e-large-{stamp}', f'E2E-LARGE-{stamp}'
    meter = Meter()
    print(f'\n### the file: {records:,} records, sent to Stroom as the source would, never to the model')
    data = big_csv(records).encode('utf-8')
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    sent = await stroom.datafeed(feed, data, {'Type': 'Raw Events'})
    e2e.check(sent.status_code == 200, f"{len(data) / 1e6:.1f} MB posted to the datafeed receiver")
    raw = None
    for _ in range(30):
        found = await streams.find_streams(ctx, feed=feed, stream_type='Raw Events', limit=5)
        if found['streams']:
            raw = found['streams'][0]['id']
            break
        await asyncio.sleep(2)
    e2e.check(raw is not None, f'stored as raw stream {raw}')

    print('\n### the agent works from the stream id; every reply measured')
    profile = await meter('profile_sample', feeds.profile_sample, ctx=ctx, stream_ids=[raw])
    e2e.check(profile.get('format') in ('delimited', 'csv'), f"profiled from a bounded read: {profile.get('format')}")
    draft = await meter('draft_translation_mapping', generation.draft_translation_mapping, ctx=ctx, stream_ids=[raw],
                        source_name='Acme VPN gateway', system_name='Acme VPN', environment='Eval')
    # As the agent does with the draft's notes: the gateway column is the device (the draft could not tell).
    e2e.check(any('No host field was recognised' in n for n in draft['notes']), 'the draft says the host is left to decide')
    draft['mapping']['common'].append({'path': 'EventSource/Device/HostName', 'field': 'gateway'})
    await translation.create_text_converter(ctx, build, feed, *e2e.CASES['csv']['converter'])
    saved = await meter('build_translation_xslt', generation.build_translation_xslt, ctx=ctx, agreed=True,
                        mapping=draft['mapping'], stream_ids=[raw], build=build, name=f'{feed}-Events')
    e2e.check(saved.get('ok', True) and saved.get('saved'), f"translation saved: {saved.get('problems')}")
    template = next(v['docRef'] for v in (await stroom.find_documents('Event Data (Text)', ['Pipeline'], 20))['values']
                    if v['docRef']['name'] == 'Event Data (Text)')
    pipeline = await meter('create_pipeline', pipeline_writes.create_pipeline, ctx=ctx, agreed=True, build=build,
                           name=f'{feed}-Events', template_uuid=template['uuid'])
    stepped = await meter('step_sample', stepping.step_sample, ctx=ctx, pipeline_uuid=pipeline['uuid'], stream_ids=[raw])
    e2e.check(stepped['verdict'] == 'clean', f"stepped {stepped.get('records_stepped')} records (a bounded number): "
                                             f"{stepped['verdict']}")
    started = await meter('create_processor_filter', processing_writes.create_processor_filter, ctx=ctx, agreed=True,
                          pipeline_uuid=pipeline['uuid'], stream_ids=[raw])
    done = await meter('wait_for_processing', processing_writes.wait_for_processing, ctx=ctx,
                       pipeline_uuid=pipeline['uuid'], stream_ids=[raw], timeout_seconds=900,
                       filter_id=started.get('filter_id'))
    events = (done.get('streams') or [{}])[0].get('events') or []
    e2e.check(done.get('gate') == 'pass' and events, f"the whole stream processed by Stroom: {done.get('streams')}")
    described = await meter('describe_stream', streams.describe_stream, ctx=ctx, stream_id=events[0])
    for _ in range(20):
        # Stroom records a stream's counts a moment after the stream completes.
        if described.get('processing'):
            break
        await asyncio.sleep(3)
        described = await meter('describe_stream', streams.describe_stream, ctx=ctx, stream_id=events[0])
    counts = described.get('processing') or {}
    e2e.check(counts.get('records_read') == records and counts.get('records_written') == records
              and not counts.get('errors') and not counts.get('fatal_errors'),
              f"every record became an event, read from the stream's counts, not its text: {counts}")

    print('\n### no reply grew with the file')
    worst = max(meter.sizes.items(), key=lambda kv: kv[1])
    e2e.check(worst[1] <= REPLY_BUDGET, f"the largest reply is {worst[0]}'s, {worst[1]:,} characters, within "
                                        f"{REPLY_BUDGET:,}; the file is {len(data):,} bytes")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--records', type=int, default=200_000)
    args = parser.parse_args()
    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=e2e.VERSION)
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    try:
        await run(ctx, stroom, e2e.run_stamp(), args.records)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
