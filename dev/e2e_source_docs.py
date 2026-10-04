"""The user's vendor and event reference documentation, kept in Stroom and used, against the local stack (dev/stroom).

    cd dev/stroom && docker compose up -d
    uv run python dev/e2e_source_docs.py

The sample is a directory's logon log whose codes mean nothing on their own (evt=4625, res=0xC000006A).

1. Documentation small enough to pass along: record_source_notes keeps it verbatim in Stroom, with the notes
   condensed from it. draft_translation_mapping (build=) drafts from the notes: the user and address where the
   dictionary puts them, a rule per catalogued event with its action, TypeId, description and outcome. The events
   stepped from the draft carry what the documentation says (a failed logon, its TypeId). A mapping that
   contradicts the catalogue is reported by build_translation_xslt. The pipeline's documentation lists the source
   fields with the documentation's meanings; promotion puts the notes and the reference document beside the feed.
2. A long vendor manual, as Markdown the user attached in their client: the agent keeps it in parts (one
   record_source_notes call each), then reads only the passage about an event (describe_document find=), every
   reply within a budget; and a status-code table the user had already made in Stroom, registered by uuid.
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
from tools import builds, explorer, feeds, generation, pipeline_writes, stepping, translation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402
from utils.xsltgen import TranslationMapping  # noqa: E402

SAMPLE = ("ts,host,evt,usr,src,res\n"
          "2026-10-01T08:00:00Z,dc01,4624,alice,10.0.0.1,0x0\n"
          "2026-10-01T08:05:00Z,dc01,4625,bob,10.0.0.2,0xC000006A\n"
          "2026-10-01T08:10:00Z,dc01,4634,alice,10.0.0.1,0x0\n")
REFERENCE = """# Acme directory audit log reference

## Fields

| Field | Meaning |
| --- | --- |
| evt | Event id: what happened (see Events) |
| usr | The account the event is about |
| src | The workstation's IP address |
| res | Status: 0x0 success, 0xC000006A failed (bad password) |

## Events

| evt | Meaning |
| --- | --- |
| 4624 | An account logged on |
| 4625 | An account failed to log on |
| 4634 | An account logged off |
"""
FIELDS = [
    {'field': 'usr', 'meaning': 'The account the event is about', 'event_logging_path': 'EventSource/User/Id'},
    {'field': 'src', 'meaning': "The workstation's IP address", 'event_logging_path': 'EventSource/Client/IPAddress'},
    {'field': 'res', 'meaning': 'Status', 'values': {'0x0': 'success', '0xC000006A': 'failed: bad password'}},
    {'field': 'evt', 'meaning': 'Event id: what happened'},
]
EVENTS = [
    {'event': '4624', 'description': 'An account logged on', 'event_detail': 'Authenticate', 'type_id': 'Logon',
     'field': 'evt', 'value': '4624', 'action': 'Logon', 'success': True},
    {'event': '4625', 'description': 'An account failed to log on', 'event_detail': 'Authenticate',
     'type_id': 'Logon failed', 'field': 'evt', 'value': '4625', 'action': 'Logon', 'success': False},
    {'event': '4634', 'description': 'An account logged off', 'event_detail': 'Authenticate', 'type_id': 'Logoff',
     'field': 'evt', 'value': '4634', 'action': 'Logoff'},
]
REPLY_BUDGET = 64_000


def manual(events: int) -> str:
    """A vendor manual far larger than a model's context: a section per event id."""
    parts = ['# Acme directory event reference\n\nEvery event the directory writes, by id.\n']
    for n in range(1000, 1000 + events):
        parts.append(f"## Event {n}\n\nWritten when operation {n} completes. " + 'Background detail. ' * 30 + '\n')
    parts.insert(len(parts) // 2, "## Event 4625\n\nAn account failed to log on. Status 0xC000006A means the password "
                                  "was wrong; 0xC0000234 means the account is locked out.\n")
    return '\n'.join(parts)


CODES = """# Status codes

| res | Meaning |
| --- | --- |
| 0xC000006A | Bad password |
| 0xC0000234 | Account locked out |
"""


async def small(ctx, stroom: StroomGateway, stamp: str) -> None:
    build, feed = f'e2e-srcdocs-{stamp}', f'E2E-SRCDOCS-{stamp}'
    print('\n### 1. documentation kept in Stroom, and the notes from it used')
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, SAMPLE))['stream_id']
    noted = await feeds.record_source_notes(ctx, build, 'Acme directory', 'The directory audit log.', FIELDS, EVENTS,
                                            documents=[{'title': 'audit log reference', 'text': REFERENCE}])
    kept = noted['documents_kept']
    e2e.check(len(kept) == 1 and kept[0]['name'] == 'Acme directory reference - audit log reference',
              f"the reference kept verbatim in Stroom: {kept[0]['name']}")
    text = (await stroom.get_doc('Documentation', kept[0]['uuid'])).get('data') or ''
    e2e.check('| 4625 | An account failed to log on |' in text, 'its tables as the vendor wrote them')

    draft = await generation.draft_translation_mapping(ctx, stream_ids=[raw], source_name='Acme directory',
                                                       system_name='Acme directory', environment='Eval', build=build)
    e2e.check(draft['notes'][0].startswith('Drafted from the source documentation'), f"drafted from the notes: {draft['notes'][0][:160]}")
    await translation.create_text_converter(ctx, build, feed, *e2e.CASES['csv']['converter'])
    saved = await e2e.agreed(generation.build_translation_xslt, ctx=ctx, mapping=draft['mapping'], stream_ids=[raw],
                             build=build, name=f'{feed}-Events')
    e2e.check(saved.get('source_notes_check') == 'the mapping agrees with the event catalogue',
              f"checked against the catalogue: {saved.get('source_notes_check')}")
    template = next(v['docRef'] for v in (await stroom.find_documents('Event Data (Text)', ['Pipeline'], 20))['values']
                    if v['docRef']['name'] == 'Event Data (Text)')
    pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                template_uuid=template['uuid'])
    stepped = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    e2e.check(stepped['verdict'] == 'clean', f"stepped clean: {[(g['class'], g['element']) for g in stepped['groups']]}")
    failed = (await stepping.step_pipeline(ctx, pipeline['uuid'], raw, 1))['elements']['translationFilter']['output']
    e2e.check('<TypeId>Logon failed</TypeId>' in failed and '<Success>false</Success>' in failed
              and '<Action>Logon</Action>' in failed,
              "the failed logon (evt=4625, record 2) comes out as the documentation says: TypeId 'Logon failed', "
              "a Logon that did not succeed")

    print('\n### a mapping that contradicts the documentation is reported')
    wrong = json.loads(json.dumps(draft['mapping']))
    for rule in wrong['events']:
        if rule['name'] == '4625':
            rule['fields'] = [f for f in rule['fields'] if f['path'] != 'EventDetail/TypeId'] + [
                {'path': 'EventDetail/TypeId', 'value': 'Logon'}]
    # The check runs where the agent saves (build=): this mapping is saved apart, linked to nothing.
    checked = await e2e.agreed(generation.build_translation_xslt, ctx=ctx, mapping=TranslationMapping.model_validate(wrong),
                               stream_ids=[raw], build=build, name=f'{feed}-Events-contradicting')
    e2e.check(any("TypeId is 'Logon failed'" in w for w in checked.get('warnings') or []),
              f"reported: {[w for w in checked.get('warnings') or [] if 'source documentation' in w]}")

    print('\n### documented with the source fields, promoted beside the feed')
    written = await builds.write_documentation(ctx, build, pipeline['uuid'], '## Purpose and data\n\nDirectory logons.\n',
                                               'Created', stream_ids=[raw])
    section = written.get('field_mapping') or ''
    e2e.check('### Source fields' in section and '`0xC000006A`: failed: bad password' in section,
              'the field mapping lists each source field with what the documentation says')
    folder = f'System/E2E Feeds/srcdocs-{stamp}'
    result = await e2e.agreed(builds.promote_build, ctx=ctx, build=build, destinations={
        'Feed': folder, 'Pipeline': folder, 'XSLT': folder, 'TextConverter': folder})
    print(f"    {result.get('promoted')}")
    for name in ('Acme directory source notes', 'Acme directory reference - audit log reference'):
        found = [v for v in (await stroom.find_documents(name, ['Documentation'], 20)).get('values') or []
                 if v['docRef']['name'] == name and (v.get('path') or '').replace(' / ', '/') == folder]
        e2e.check(len(found) >= 1, f"'{name}' beside the feed, in {folder}")


PART = 400_000      # characters the agent sends per record_source_notes call for a long document


async def large(ctx, stroom: StroomGateway, stamp: str) -> None:
    print('\n### 2. a long manual kept in parts, read a passage at a time; a table already in Stroom registered')
    build = f'e2e-srcdocs-large-{stamp}'
    text = manual(4000)
    parts = [text[i:i + PART] for i in range(0, len(text), PART)]
    replies, kept = [], []
    for n, part in enumerate(parts, start=1):
        noted = await feeds.record_source_notes(ctx, build, 'Acme directory', 'The directory event reference.', [], [],
                                                documents=[{'title': f'event reference part {n}', 'text': part,
                                                            'source': 'event-reference.md'}])
        replies.append((f'record_source_notes part {n}', noted))
        kept += noted['documents_kept']
    e2e.check(len(text) > 1_000_000 and len(kept) == len(parts),
              f"a {len(text):,}-character manual kept in {len(parts)} parts, one call each")
    # A table the user had already made in Stroom (in the UI, say): registered by uuid, left where it is.
    async def fill(ref):
        doc = await stroom.get_doc('Documentation', ref['uuid'])
        doc['data'] = CODES
        return await stroom.put_doc(doc)
    from security.guard import guard_from
    table = await guard_from(ctx).create_filled('Documentation', f'status codes {stamp}', build, fill)
    noted = await feeds.record_source_notes(ctx, build, 'Acme directory', 'The directory event reference.', [], [],
                                            documents=[{'title': 'status codes', 'uuid': table['uuid']}])
    replies.append(('record_source_notes by uuid', noted))
    e2e.check(noted['documents_kept'] and noted['documents_kept'][0].get('already_in_stroom'),
              'a document already in Stroom registered by uuid, named in the notes')
    holder = None
    for k in kept:      # the part the event's section landed in
        if '4625' in ((await stroom.get_doc('Documentation', k['uuid'])).get('data') or ''):
            holder = k
            break
    whole = await explorer.describe_document(ctx, 'Documentation', holder['uuid'])
    replies.append(('describe_document', whole))
    e2e.check('Cut short' in (whole.get('note') or '') and whole.get('outline'),
              f"read whole, a {whole['characters']:,}-character part comes back cut short, with its outline")
    passage = await explorer.describe_document(ctx, 'Documentation', holder['uuid'], find='Event 4625')
    replies.append(('describe_document find', passage))
    e2e.check(passage['matches'] >= 1 and any('0xC000006A' in p['text'] for p in passage['passages']),
              f"find='Event 4625' returns the passage about it: {passage['passages'][0]['text'][:90]!r}")
    worst = max((len(json.dumps(r, default=str)), n) for n, r in replies)
    e2e.check(worst[0] <= REPLY_BUDGET, f"the largest reply is {worst[1]}'s, {worst[0]:,} characters, within {REPLY_BUDGET:,}")


async def main():
    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=e2e.VERSION)
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = time.strftime('%H%M%S')
    try:
        await small(ctx, stroom, stamp)
        await large(ctx, stroom, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
