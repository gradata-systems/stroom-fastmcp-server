"""Documenting an existing index, against the local Stroom stack and Elasticsearch 9 (see dev/stroom).

    cd dev/stroom && docker compose --profile elastic up -d
    uv run python dev/e2e_document_index.py

The user asks for an index to be documented. As the document_index prompt asks, the agent locates the index doc
(an Elastic Index or a Lucene Index doc) and confirms it with the user; surveys the data through Stroom
(describe_document: the fields Stroom has for it, a sample of its documents read through a dashboard that is never
saved, and the pipelines that feed it); drafts the documentation in a build (write_documentation index_uuid=...),
the field table generated like the indexing pipeline's, and gives the user its link; and, once the user consents,
promotes it beside the index doc, or where the user chooses.

1. an index nothing in Stroom feeds: loaded straight into Elasticsearch by another process, its documents
   pointing at a stream in this Stroom (StreamId, EventId), its Elastic Index doc in production. The field table comes from the
   survey alone: each field's type, how often the sample held it, its values, and a description from what the
   sample shows. Promoted beside the index doc, by default.
   Documented again later: the doc beside the index doc is changed through a working copy and written back on
   promotion (after a backup), one doc with both change-log lines, not a second doc of the same name.
   Beside it, an index whose documents have no StreamId: Stroom returns a hit only when its StreamId is a stream
   the user may read, so it returns none of them (no error); the survey and the doc say so, and the fields come
   from the mapping only. And a wide index (more fields than one dashboard search reads): surveyed in groups of
   columns joined on StreamId and EventId, every field with its values.
2. an index a production pipeline feeds, from an index plan kept with its XSLT: the table also says where each
   field comes from in the events, described from the event-logging schema. Promoted where the user chooses.
   Then the pipeline writes a new field the index doesn't have: documenting is refused, naming it, for the user to
   reindex or check again; documented anyway only as the user chooses, the field said to be not in the index. Once
   Elasticsearch maps it and a document carries it, how long Stroom takes to list it is checked, and the doc is
   written again with its values.
3. a Lucene index, fed by a pipeline whose XSLT was written by hand (no plan): surveyed and documented the same
   way, where the user chooses; a field that is not stored is shown as such, not as missing from the sample.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from e2e_elastic_handover import ES, live_cluster  # noqa: E402
from config import Settings  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import builds, explorer, feeds, indexing  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

LEGACY = [
    {'@timestamp': '2026-09-20T08:00:00Z', 'user': {'name': 'alice'}, 'source': {'ip': '10.3.0.1'},
     'http': {'status': 200}, 'message': 'GET /index.html'},
    {'@timestamp': '2026-09-21T09:30:00Z', 'user': {'name': 'bob'}, 'source': {'ip': '10.3.0.2'},
     'http': {'status': 404}, 'message': 'GET /missing'},
    {'@timestamp': '2026-09-22T10:15:00Z', 'user': {'name': 'carol'}, 'source': {'ip': '10.3.0.3'},
     'http': {'status': 500}, 'message': 'POST /upload'},
    {'@timestamp': '2026-09-23T11:45:00Z', 'user': {'name': 'alice'}, 'source': {'ip': '10.3.0.1'},
     'message': 'health check'},                                   # no status: 3 of the 4 documents have one
]
# Each document points at the stream it came from, as Stroom's indexing filters write them; Stroom only returns
# hits whose stream the user may read.
def with_ids(stream_id: int) -> list[dict]:
    return [{'StreamId': stream_id, 'EventId': n + 1, **d} for n, d in enumerate(LEGACY)]
LEGACY_MAPPING = {'mappings': {'properties': {
    'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}, '@timestamp': {'type': 'date'}, 'user': {'properties': {'name': {'type': 'keyword'}}},
    'source': {'properties': {'ip': {'type': 'ip'}}}, 'http': {'properties': {'status': {'type': 'long'}}},
    'message': {'type': 'text'}}}}


async def locate(ctx, name: str, doc_type: str) -> dict:
    """As the agent does: find the index doc by name and show the user where it is, to confirm."""
    found = await explorer.find_documents(ctx, name, [doc_type])
    matches = [d for d in found['documents'] if d['name'] == name]
    e2e.check(len(matches) == 1, f"located the {doc_type} doc '{name}' at {matches[0]['path'] if matches else '?'}")
    return matches[0]


async def survey(ctx, doc: dict, fields: list[str], documents: int) -> dict:
    described = await explorer.describe_document(ctx, doc['type'], doc['uuid'])
    s = described.get('survey') or {}
    names = [f['name'] for f in s.get('fields') or []]
    e2e.check(all(f in names for f in fields) and s.get('documents_sampled') == documents,
              f"surveyed through Stroom: {len(names)} fields, {s.get('documents_sampled')} documents sampled, "
              f"newest first, through a dashboard that is not saved")
    return s


async def draft(ctx, build: str, doc: dict, purpose: str) -> dict:
    """The user confirms the index doc before anything is written; the draft comes back with its link."""
    asked = await builds.write_documentation(ctx, build, index_uuid=doc['uuid'],
                                             markdown=f'## Purpose and data\n\n{purpose}\n', change='Created')
    folder = (doc.get('path') or '').replace(' / ', '/')
    e2e.check(asked.get('status') == 'needs_confirmation' and doc['name'] in asked['summary']
              and f"{folder}/{doc['name']}" in json.dumps(asked['details']),
              f"the user confirms the index doc first, named with its folder: {asked['details'].get('index doc')}")
    written = await e2e.agreed(builds.write_documentation, ctx=ctx, build=build, index_uuid=doc['uuid'],
                               markdown=f'## Purpose and data\n\n{purpose}\n', change='Created')
    link = written.get('link') or ''
    e2e.check(f"docUuid={written['uuid']}" in link and 'docType=Documentation' in link,
              f"drafted in the build, with its link for the user: {link}")
    return written


def summarised(written: dict, documents: int, feed: str, stream_type: str, produced_by: str | None = None) -> None:
    """The Data surveyed summary under the agent's Purpose and data: what the whole index holds and where from."""
    summary = written.get('data_surveyed') or ''
    made = f", produced by pipeline `{produced_by}`" if produced_by else ''
    e2e.check(f"holds {documents} documents" in summary and f"From feed `{feed}` ({stream_type} streams{made})" in summary,
              f"Purpose and data says what the index holds and where from: "
              f"{[l for l in summary.splitlines() if l.startswith('- ')][:2]}")


async def promoted_to(ctx, stroom, build: str, written: dict, folder: str, destinations: dict | None = None) -> None:
    result = await e2e.agreed(builds.promote_build, ctx=ctx, build=build, destinations=destinations or {})
    print(f"    {result.get('promoted')}")
    moved = await stroom.find_documents(written['name'], ['Documentation'], 20)
    paths = [v.get('path') or '' for v in moved.get('values') or [] if v['docRef']['uuid'] == written['uuid']]
    e2e.check(paths and paths[0].replace(' / ', '/').endswith(folder.split('/', 1)[-1]),
              f"promoted, once the user consented, to {paths[0] if paths else '?'}")


async def nothing_feeds(ctx, stroom, es: httpx.AsyncClient, stamp: str) -> None:
    print('\n### 1. an index nothing in Stroom feeds, its Elastic Index doc in production')
    index, bare = f'legacy-web-{stamp}', f'legacy-bare-{stamp}'
    cluster = await live_cluster(stroom)
    setup = f'e2e-docindex-setup-{stamp}'
    folder = f'System/E2E Production/legacy-{stamp}'
    feed = f'E2E-LEGACY-WEB-{stamp}'
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=setup, name=feed)
    stream = (await feeds.upload_sample(ctx, feed, '\n'.join(json.dumps(d) for d in LEGACY)))['stream_id']
    for name, documents in ((index, with_ids(stream)), (bare, LEGACY)):
        e2e.check((await es.put(f'/{name}', json=LEGACY_MAPPING)).status_code == 200, f'index {name} created')
        bulk = ''.join(json.dumps({'index': {}}) + '\n' + json.dumps(d) + '\n' for d in documents)
        loaded = (await es.post(f'/{name}/_bulk?refresh=true', content=bulk,
                                headers={'Content-Type': 'application/x-ndjson'})).json()
        e2e.check(not loaded.get('errors'), f"{len(documents)} documents loaded straight into Elasticsearch"
                                            + (f', pointing at stream {stream}' if name == index else ', with no StreamId'))
        await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=setup, backend='elasticsearch', name=name,
                         time_field='@timestamp', index_name=name, cluster_uuid=cluster['uuid'])
    await e2e.agreed(builds.promote_build, ctx=ctx, build=setup, destinations={'ElasticIndex': folder, 'Feed': folder})
    e2e.check(True, f"their Elastic Index docs are in production, at {folder}")

    doc = await locate(ctx, index, 'ElasticIndex')
    found = await survey(ctx, doc, ['StreamId', 'EventId', '@timestamp', 'user.name', 'source.ip', 'http.status',
                                    'message'], 4)
    e2e.check(not found.get('fed_by'), 'no pipeline in Stroom feeds it')
    build = f'e2e-docindex-{stamp}'
    written = await draft(ctx, build, doc, 'Web access logs, loaded into Elasticsearch by another system.')
    summarised(written, 4, f'E2E-LEGACY-WEB-{stamp}', 'Raw Events')
    text = (await stroom.get_doc('Documentation', written['uuid'])).get('data') or ''
    e2e.check(text.index('loaded into Elasticsearch by another system') < text.index('### Data surveyed')
              < text.index('## Field mapping'), "the summary sits under the agent's own words, before the field table")
    await e2e.documented_to_the_field(stroom, written, ['@timestamp', 'user.name', 'source.ip', 'http.status', 'message'],
                                      {'user.name': 'alice', 'source.ip': '10.3.0.1', 'http.status': '200'},
                                      'the existing index')
    rows = e2e.field_rows(written.get('field_mapping') or '')
    e2e.check('75% of documents' in ' '.join(rows['http.status']) and 'IP addresses' in rows['source.ip'][0],
              f"http.status in 3 of the 4 documents; source.ip described from its values: {rows['source.ip'][0]!r}")
    await promoted_to(ctx, stroom, build, written, folder)

    print('\n### 1, again: the doc beside the index doc changed through a working copy, not a second doc')
    again = f'e2e-docindex-again-{stamp}'
    asked = await builds.write_documentation(ctx, again, index_uuid=doc['uuid'], change='Surveyed again',
                                             markdown='## Purpose and data\n\nWeb access logs, surveyed again.\n')
    e2e.check('written back into the existing Documentation' in json.dumps(asked.get('details')),
              f"the user is told it changes the doc already beside it: {asked['details'].get('promoted (once you agree)')}")
    rewritten = await e2e.agreed(builds.write_documentation, ctx=ctx, build=again, index_uuid=doc['uuid'],
                                 change='Surveyed again',
                                 markdown='## Purpose and data\n\nWeb access logs, surveyed again.\n')
    e2e.check(rewritten['updated'] and rewritten['uuid'] != written['uuid'], 'drafted as a working copy in the new build')
    result = await e2e.agreed(builds.promote_build, ctx=ctx, build=again, destinations={})
    print(f"    {result.get('promoted')}")
    found = (await stroom.find_documents(index, ['Documentation'], 20)).get('values') or []
    beside = [v for v in found if v['docRef']['name'] == index
              and (v.get('path') or '').replace(' / ', '/') == folder]
    text = (await stroom.get_doc('Documentation', beside[0]['docRef']['uuid'])).get('data') or '' if beside else ''
    from utils.versionlog import rows_of
    rows = [(r['version'], r['change']) for r in rows_of(text)]
    e2e.check(len(beside) == 1 and beside[0]['docRef']['uuid'] == written['uuid'] and 'surveyed again' in text
              and rows == [('1', 'Created'), ('2', 'Surveyed again')],
              f"written back into the one doc beside the index doc, a version row for each build ({len(beside)} doc, {rows})")

    print('\n### 1b. its documents have no StreamId: Stroom returns none of them, and the doc says so')
    doc = await locate(ctx, bare, 'ElasticIndex')
    described = (await explorer.describe_document(ctx, 'ElasticIndex', doc['uuid']))['survey']
    e2e.check(described['documents_sampled'] == 0 and 'a stream in this Stroom' in (described.get('note') or '')
              and len(described['fields']) >= 5,
              f"the survey has the mapping's {len(described['fields'])} fields, no documents, and says why: "
              f"{(described.get('note') or '')[:90]}")
    build = f'e2e-docindex-bare-{stamp}'
    written = await draft(ctx, build, doc, 'Web access logs, loaded by another system without StreamId.')
    section = written.get('field_mapping') or ''
    rows = e2e.field_rows(section)
    e2e.check('a stream in this Stroom' in (written.get('data_surveyed') or ''), 'the summary says why nothing was read')
    e2e.check('no field below was read' in section and all(r[-2:] == ['not read', '-'] for r in rows.values())
              and {'user.name', 'source.ip', 'http.status', 'message'} <= set(rows),
              'documented from the mapping: every field listed, marked not read, and why')
    await promoted_to(ctx, stroom, build, written, folder)


async def wide(ctx, stroom, es: httpx.AsyncClient, stamp: str) -> None:
    print('\n### 1c. a wide index: more fields than one dashboard search reads')
    index = f'legacy-wide-{stamp}'
    setup, folder = f'e2e-docindex-wide-setup-{stamp}', f'System/E2E Production/legacy-{stamp}'
    feed = f'E2E-LEGACY-WIDE-{stamp}'
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=setup, name=feed)
    stream = (await feeds.upload_sample(ctx, feed, 'wide'))['stream_id']
    names = [f'attr{n:03}' for n in range(180)]
    mapping = {'mappings': {'properties': {'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'},
                                           '@timestamp': {'type': 'date'}, **{n: {'type': 'keyword'} for n in names}}}}
    e2e.check((await es.put(f'/{index}', json=mapping)).status_code == 200, f'index {index} created, {len(names) + 3} fields')
    documents = [{'StreamId': stream, 'EventId': e, '@timestamp': f'2026-09-2{e}T10:00:00Z',
                  **{n: f'{n}-{e}' for n in names}} for e in (1, 2, 3)]
    bulk = ''.join(json.dumps({'index': {}}) + '\n' + json.dumps(d) + '\n' for d in documents)
    loaded = (await es.post(f'/{index}/_bulk?refresh=true', content=bulk, headers={'Content-Type': 'application/x-ndjson'})).json()
    e2e.check(not loaded.get('errors'), f'3 documents loaded, pointing at stream {stream}')
    cluster = await live_cluster(stroom)
    await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=setup, backend='elasticsearch', name=index,
                     time_field='@timestamp', index_name=index, cluster_uuid=cluster['uuid'])
    await e2e.agreed(builds.promote_build, ctx=ctx, build=setup, destinations={'ElasticIndex': folder, 'Feed': folder})
    doc = await locate(ctx, index, 'ElasticIndex')
    found = await survey(ctx, doc, ['StreamId', 'EventId', '@timestamp', 'attr000', 'attr179'], 3)
    e2e.check(len(found['surveyed_fields']) == 183 and found['populated'].get('attr179') == 100.0,
              f"all {len(found['surveyed_fields'])} fields surveyed, the last as fully as the first")
    build = f'e2e-docindex-wide-{stamp}'
    written = await draft(ctx, build, doc, 'Wide records from another system.')
    summarised(written, 3, feed, 'Raw Events')
    rows = e2e.field_rows(written.get('field_mapping') or '')
    thin = [n for n in names if (rows.get(n) or ['', '', '', '?'])[3] != '100% of documents']
    e2e.check(len(rows) == 183 and not thin and f'`attr179-1`' in rows['attr179'][-1],
              f"every one of the {len(rows)} fields documented with its values, none shown as missing: {thin[:3]}")


async def fed_by_a_plan(ctx, stroom, es: httpx.AsyncClient, stamp: str) -> None:
    print('\n### 2. an index a production pipeline feeds, from the plan kept with its XSLT')
    from e2e_index_versions import production
    prod = await production(ctx, stroom, es, stamp)
    doc = await locate(ctx, prod['v1_doc']['name'], 'ElasticIndex')
    found = await survey(ctx, doc, ['@timestamp', 'user.name'], 3)
    fed = found.get('fed_by') or []
    e2e.check(any(p['uuid'] == prod['v1']['uuid'] and p.get('plan') for p in fed),
              f"fed by {[p['name'] for p in fed]}, whose XSLT keeps its index plan")
    build = f'e2e-docindex-v1-{stamp}'
    written = await draft(ctx, build, doc, 'CSV logons, indexed.')
    summarised(written, 3, prod['csv']['feed'], 'Events', prod['csv']['pipeline']['name'])
    e2e.check(f"Indexed by pipeline `{prod['v1']['name']}`, from the index plan" in written['data_surveyed'],
              'and the pipeline that indexes it')
    rows = e2e.field_rows(written.get('field_mapping') or '')
    await e2e.documented_to_the_field(stroom, written, ['@timestamp', 'user.name'], {'user.name': 'alice'},
                                      'the index a plan feeds')
    e2e.check(rows['user.name'][2] == '`EventSource/User/Id`' and rows['user.name'][0] == 'An identifier for the user.',
              f"where each field comes from in the events, described from the schema: {rows['user.name'][0]!r}")
    chosen = f'System/E2E Docs/indexes-{stamp}'
    await promoted_to(ctx, stroom, build, written, chosen, {'Documentation': chosen})
    return prod, doc


async def find_one(ctx, doc: dict, prod: dict) -> None:
    """Asked for by the user: one rare event among many indexed streams, which sampling would miss, found as the user
    would on a dashboard and traced to its raw record."""
    print('\n### 2a. an event the user describes, found in the index and traced to its raw record')
    from tools.diagnosis import locate_event
    found = await indexing.find_events(ctx, doc['uuid'], where=[{'field': 'user.name', 'value': 'alice'}])
    hits = found['hits']
    e2e.check(found['total'] >= 1 and hits and all(h.get('user.name') == 'alice' and h.get('StreamId') and h.get('EventId')
                                                   for h in hits) and found['by_stream'],
              f"found {found['total']} event(s) for user.name = alice, each with its StreamId and EventId, and the "
              f"streams holding them: {found['by_stream']}")
    hit = hits[0]
    located = await locate_event(ctx, int(float(hit['StreamId'])), int(float(hit['EventId'])))
    e2e.check('alice' in located.get('record_input', '') and 'alice' in located.get('stored_event', ''),
              f"traced to its raw record: stream {located['raw_stream']}, record {located['location']}")
    # A fix proven on exactly the events found, wherever they are: their records stepped through the events pipeline
    # and a candidate (here the same pipeline, so nothing may differ).
    from tools import stepping
    events = [{'stream_id': int(float(h['StreamId'])), 'event_id': int(float(h['EventId']))} for h in hits]
    pipeline = prod['csv']['pipeline']['uuid']
    compared = await stepping.compare_outputs(ctx, pipeline, events=events, other_pipeline_uuid=pipeline)
    e2e.check(compared['records_compared'] == len({(e['stream_id'], e['event_id']) for e in events})
              and compared['records_changed'] == 0,
              f"compared at the {compared['records_compared']} found event(s)' records, nothing else stepped")
    every = await indexing.find_events(ctx, doc['uuid'], where=[{'field': 'user.name', 'value': 'alice'}],
                                       all_streams=True)
    affected = every.get('affected') or {}
    e2e.check(located['raw_stream'] in affected.get('raw_streams', [])
              and 'StreamId' in (affected.get('elasticsearch_delete') or ''),
              f"what a change to them reaches: raw streams {affected.get('raw_streams')} to reprocess, and the "
              f"delete for their old documents")
    try:
        await indexing.find_events(ctx, doc['uuid'], where=[{'field': 'user.nmae', 'value': 'alice'}])
        refused = ''
    except Exception as e:
        refused = str(e)
    e2e.check('Not fields of index' in refused and 'like user.name' in refused,
              f"a field the index doesn't have refused, with the one meant: {refused[:120]}")


async def new_field(ctx, stroom, es: httpx.AsyncClient, stamp: str, prod: dict, doc: dict) -> None:
    """Seen in production: documenting an Elastic Index doc after a field was added to the pipeline that feeds it, and
    once its data was indexed, Stroom didn't list the field; the doc came out without it, and the agent said the field
    didn't exist. A minute later Stroom listed it."""
    print('\n### 2b. a field the feeding pipeline now writes: before the index has it, and once it does')
    from tools.builds import kept_mapping
    from utils.mappingstore import with_mapping
    index = prod['v1_index']
    kept = await kept_mapping(ctx, prod['v1']['uuid'])
    xslt = kept['xslt']
    payload = {**kept['payload'], 'fields': [*kept['payload']['fields'], {'name': 'event.outcome', 'type': 'boolean',
                                                                        'source': 'EventDetail/Authenticate/Outcome/Success'}]}
    xslt['description'] = with_mapping(xslt.get('description'), 'index', payload)
    await stroom.put_doc(xslt)
    e2e.check(True, "v1's index plan now writes event.outcome (the pipeline updated), none of it indexed yet")
    build = f'e2e-docindex-outcome-{stamp}'
    markdown = '## Purpose and data\n\nCSV logons, indexed, now with whether each succeeded.\n'
    # Not documented without it, unsaid: refused, naming the field, for the user to reindex or check again.
    try:
        await e2e.agreed(builds.write_documentation, ctx=ctx, build=build, index_uuid=doc['uuid'], markdown=markdown,
                         change='event.outcome added')
        refused = ''
    except Exception as e:
        refused = str(e)
    e2e.check('has no field event.outcome' in refused and 'reindex' in refused and 'check again' in refused,
              f"refused, naming the field, for the user to reindex or check again: {refused[:160]}")
    # The user chooses to document it now: the field listed as not in the index.
    before = await e2e.agreed(builds.write_documentation, ctx=ctx, build=build, index_uuid=doc['uuid'],
                              markdown=markdown, change='event.outcome added', document_missing_fields=True)
    rows = e2e.field_rows(before.get('field_mapping') or '')
    e2e.check(before.get('not_in_index') == ['event.outcome'] and rows.get('event.outcome', [''])[-2:]
              == ['not in the index', '-'], f"documented as the user chose: not in the index: {rows.get('event.outcome')}")
    # Elasticsearch maps it (the template is strict: the mapping first, as an updated template would at rollover), a
    # document carrying it is indexed, and the index is refreshed: searchable now.
    mapped = await es.put(f'/{index}/_mapping', json={'properties': {'event': {'properties': {'outcome': {'type': 'boolean'}}}}})
    e2e.check(mapped.status_code == 200, f'Elasticsearch maps event.outcome: {mapped.text[:120]}')
    newest = (await es.post(f'/{index}/_search', json={'size': 1, 'sort': [{'@timestamp': 'desc'}]})).json()['hits']['hits'][0]
    source = {**newest['_source'], 'EventId': 1000, 'event': {'outcome': True}}
    added = await es.post(f'/{index}/_doc?refresh=true', json=source)
    e2e.check(added.status_code in (200, 201), f'a document carrying it indexed and refreshed: {added.status_code}')
    # How long Stroom takes to list it: the production case was a minute. Here it is checked against a bound, so a
    # Stroom that starts to cache its field list shows up.
    ref = {'type': 'ElasticIndex', 'uuid': doc['uuid'], 'name': doc['name']}
    started, listed = time.time(), False
    while time.time() - started < 120 and not listed:
        listed = 'event.outcome' in {f['name'] for f in await indexing._index_fields(stroom, 'ElasticIndex', ref)}
        if not listed:
            await asyncio.sleep(2)
    took = time.time() - started
    e2e.check(listed and took < 10, f"Stroom lists the field {took:.0f} s after Elasticsearch mapped it "
                                    f"(production: about a minute)")
    after = await e2e.agreed(builds.write_documentation, ctx=ctx, build=build, index_uuid=doc['uuid'],
                             markdown=markdown, change='event.outcome documented with its values')
    rows = e2e.field_rows(after.get('field_mapping') or '')
    e2e.check('not_in_index' not in after and 'not in the index' not in ' '.join(rows.get('event.outcome', []))
              and '`true`' in ' '.join(rows.get('event.outcome', [])),
              f"documented again: event.outcome with its values from the index: {rows.get('event.outcome')}")


async def lucene(ctx, stroom, stamp: str) -> None:
    print('\n### 3. a Lucene index, fed by a pipeline whose XSLT was written by hand')
    from e2e_lucene_indexing import index_stage
    csv = await e2e.onboard(ctx, 'csv', e2e.CASES['csv'], f'L{stamp}')
    v1 = await index_stage(ctx, csv, f'L{stamp}')
    doc = await locate(ctx, v1['index']['name'], 'Index')
    fields = (await stroom.post('/index/v2/findFields', {'dataSourceRef': {'type': 'Index', 'uuid': doc['uuid'], 'name': doc['name']},
                                                         'pageRequest': {'offset': 0, 'length': 100}}))['values']
    host = next(f for f in fields if f['fldName'] == 'HostName')
    await stroom.request('PUT', '/index/v2/updateField', {
        'indexDocRef': {'type': 'Index', 'uuid': doc['uuid'], 'name': doc['name']}, 'fieldName': 'HostName',
        'indexField': {**host, 'stored': False}})
    e2e.check(True, 'HostName set not stored, as a Lucene index can have it')
    found = await survey(ctx, doc, ['UserId'], 3)
    e2e.check(any(f['name'] == 'HostName' and f.get('stored') is False for f in found['fields']),
              'the survey knows HostName is not stored (from the index fields, not the index doc)')
    e2e.check(any(p['uuid'] == v1['pipeline']['uuid'] and not p.get('plan') for p in found.get('fed_by') or []),
              'fed by the Lucene indexing pipeline, whose XSLT keeps no plan')
    build = f'e2e-docindex-lucene-{stamp}'
    written = await draft(ctx, build, doc, 'CSV logons, indexed in Lucene.')
    summarised(written, 3, csv['feed'], 'Events', csv['pipeline']['name'])
    await e2e.documented_to_the_field(stroom, written, ['UserId'], {'UserId': 'alice'}, 'the Lucene index')
    rows = e2e.field_rows(written.get('field_mapping') or '')
    e2e.check(rows['HostName'][-2:] == ['not stored', '-'] and 'not stored' in rows['HostName'][0],
              f"HostName shown as not stored, not as missing from the sample: {rows['HostName'][0]!r}")
    chosen = f'System/E2E Docs/indexes-{stamp}'
    await promoted_to(ctx, stroom, build, written, chosen, {'Documentation': chosen})


async def main():
    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=e2e.VERSION,
                        conventions_dir=ROOT / 'conventions')
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = e2e.run_stamp()
    only = sys.argv[1:] or ['--nothing-feeds', '--plan', '--lucene']
    try:
        async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
            try:
                version = (await es.get('/')).json()['version']['number']
            except httpx.HTTPError:
                raise SystemExit(f"No Elasticsearch at {ES}: cd dev/stroom && docker compose --profile elastic up -d")
            e2e.check(version.startswith('9.'), f'Elasticsearch {version}')
            if '--nothing-feeds' in only:
                await nothing_feeds(ctx, stroom, es, stamp)
                await wide(ctx, stroom, es, stamp)
            if '--plan' in only:
                prod, doc = await fed_by_a_plan(ctx, stroom, es, stamp)
                await find_one(ctx, doc, prod)
                await new_field(ctx, stroom, es, stamp, prod, doc)
            if '--lucene' in only:
                await lucene(ctx, stroom, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
