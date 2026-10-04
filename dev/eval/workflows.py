"""Agent-level cases for the workflows beyond onboarding, run by run_agent.py --workflow.

Each workflow sets up the starting state on the local stack (through the server's own tools, as the e2e suites do),
names the prompt the agent starts from and what the user asks, gives the scripted user the facts it may answer
from, and scores the outcome from Stroom, whatever the agent says about it. A reference run (--reference) does
the workflow through the tools directly, without a model: it checks the setup and the scorer cost nothing.

    uv run python dev/eval/run_agent.py --workflow document_index              # the agent, on Haiku
    uv run python dev/eval/run_agent.py --workflow document_index --reference  # no model: setup and scorer
"""
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

ES = 'http://127.0.0.1:19200'


@dataclass
class Workflow:
    id: str
    prompt: str
    setup: Callable[[Any, str], Awaitable[dict[str, Any]]]
    reference: Callable[[Any, dict[str, Any]], Awaitable[None]]
    score: Callable[[Any, dict[str, Any]], Awaitable[tuple[list[str], list[str]]]]
    done: str           # when the work is finished, for the scripted user
    promote: str        # the user's answer when asked whether or where to promote


# --- document_index: an index another system loads, documented and promoted beside its index doc ---
LEGACY = [
    {'@timestamp': '2026-09-20T08:00:00Z', 'user': {'name': 'alice'}, 'source': {'ip': '10.3.0.1'},
     'http': {'status': 200}, 'message': 'GET /index.html'},
    {'@timestamp': '2026-09-21T09:30:00Z', 'user': {'name': 'bob'}, 'source': {'ip': '10.3.0.2'},
     'http': {'status': 404}, 'message': 'GET /missing'},
    {'@timestamp': '2026-09-22T10:15:00Z', 'user': {'name': 'carol'}, 'source': {'ip': '10.3.0.3'},
     'http': {'status': 500}, 'message': 'POST /upload'},
    {'@timestamp': '2026-09-23T11:45:00Z', 'user': {'name': 'alice'}, 'source': {'ip': '10.3.0.1'},
     'message': 'health check'},
]
LEGACY_FIELDS = ['StreamId', 'EventId', '@timestamp', 'user.name', 'source.ip', 'http.status', 'message']
MAPPING = {'mappings': {'properties': {
    'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}, '@timestamp': {'type': 'date'},
    'user': {'properties': {'name': {'type': 'keyword'}}}, 'source': {'properties': {'ip': {'type': 'ip'}}},
    'http': {'properties': {'status': {'type': 'long'}}}, 'message': {'type': 'text'}}}}
PURPOSE = ("It holds the web access logs of the edge proxy in front of the intranet portal, loaded by the proxy "
           "team's own shipper. The SOC searches it when investigating suspicious access to the portal.")


async def document_index_setup(ctx, stamp: str) -> dict[str, Any]:
    import e2e_translation as e2e
    from e2e_elastic_handover import live_cluster
    from tools import builds, feeds, indexing
    stroom = ctx.lifespan_context['stroom']
    index, feed = f'eval-legacy-web-{stamp}', f'EVAL-LEGACY-WEB-{stamp}'
    setup, folder = f'eval-docindex-setup-{stamp}', f'System/E2E Production/eval-legacy-{stamp}'
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=setup, name=feed)
    stream = (await feeds.upload_sample(ctx, feed, '\n'.join(json.dumps(d) for d in LEGACY)))['stream_id']
    async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
        if (await es.put(f'/{index}', json=MAPPING)).status_code != 200:
            raise RuntimeError(f'could not create Elasticsearch index {index}')
        bulk = ''.join(json.dumps({'index': {}}) + '\n' + json.dumps({'StreamId': stream, 'EventId': n + 1, **d}) + '\n'
                       for n, d in enumerate(LEGACY))
        loaded = (await es.post(f'/{index}/_bulk?refresh=true', content=bulk,
                                headers={'Content-Type': 'application/x-ndjson'})).json()
        if loaded.get('errors'):
            raise RuntimeError(f'bulk load failed: {str(loaded)[:300]}')
    cluster = await live_cluster(stroom)
    await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=setup, backend='elasticsearch', name=index,
                     time_field='@timestamp', index_name=index, cluster_uuid=cluster['uuid'])
    await e2e.agreed(builds.promote_build, ctx=ctx, build=setup, destinations={'ElasticIndex': folder, 'Feed': folder})
    return {'index': index, 'folder': folder, 'build': f'eval-docidx-{stamp}',
            'prompt_args': {'index': index},
            'request': (f"Please document our existing Elasticsearch index `{index}` in Stroom. Use the build "
                        f"`eval-docidx-{stamp}`, give me the link to the draft, and put it beside the index doc once "
                        f"I've agreed."),
            'facts': f"The index is `{index}`, its Elastic Index doc is in {folder}. {PURPOSE} Put the documentation "
                     f"beside the index doc."}


async def document_index_reference(ctx, prepared: dict[str, Any]) -> None:
    """As an agent following the prompt would: locate, survey, draft (confirmed), promote beside the index doc."""
    import e2e_translation as e2e
    from tools import builds, explorer
    found = await explorer.find_documents(ctx, prepared['index'], ['ElasticIndex'])
    doc = next(d for d in found['documents'] if d['name'] == prepared['index'])
    await explorer.describe_document(ctx, 'ElasticIndex', doc['uuid'])
    await e2e.agreed(builds.write_documentation, ctx=ctx, build=prepared['build'], index_uuid=doc['uuid'],
                     change='Created', markdown=f"## Purpose and data\n\n{PURPOSE} Each document is one request to the "
                                                f"portal: who made it, from which address, what was asked for and the "
                                                f"HTTP status it got.\n")
    await e2e.agreed(builds.promote_build, ctx=ctx, build=prepared['build'], destinations={})


async def document_index_score(ctx, prepared: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The documentation beside the index doc: every field in the table, the Data surveyed summary, and the agent's
    own Purpose and data prose, built on what the user said of the index's purpose (the agent has to ask)."""
    import e2e_translation as e2e
    stroom = ctx.lifespan_context['stroom']
    problems, notes = [], []
    found = (await stroom.find_documents(prepared['index'], ['Documentation'], 20)).get('values') or []
    docs = [v for v in found if v['docRef']['name'] == prepared['index']]
    beside = [v for v in docs if (v.get('path') or '').replace(' / ', '/') == prepared['folder']]
    if not docs:
        return [f"no Documentation named '{prepared['index']}'"], notes
    if not beside:
        problems.append(f"not promoted beside the index doc ({prepared['folder']}): at "
                        f"{[(v.get('path') or '') for v in docs]}")
    text = (await stroom.get_doc('Documentation', (beside or docs)[0]['docRef']['uuid'])).get('data') or ''
    section = text.split('## Field mapping')[1].split('\n## ')[0] if '## Field mapping' in text else ''
    rows = e2e.field_rows(section)
    missing = [f for f in LEGACY_FIELDS if f not in rows]
    if missing:
        problems.append(f"field table lacks {missing}")
    if '### Data surveyed' not in text:
        problems.append('no Data surveyed summary')
    purpose = text.split('## Purpose and data')[1].split('### Data surveyed')[0] if '## Purpose and data' in text else ''
    prose = ' '.join(purpose.split())
    if len(prose) < 150:
        problems.append(f"Purpose and data is thin ({len(prose)} characters): {prose[:120]!r}")
    # The user knows why the index exists (the facts): the agent must ask, not infer a purpose from the data.
    if not re.search(r'proxy|portal|\bSOC\b', prose, re.I):
        problems.append("Purpose and data does not use what the user said of the index's purpose (the edge proxy in "
                        "front of the intranet portal, searched by the SOC): the agent did not ask, or did not listen")
    notes.append(f"purpose: {prose[:200]!r}")
    return problems, notes


WORKFLOWS = {
    'document_index': Workflow(
        id='document_index', prompt='document_index', setup=document_index_setup,
        reference=document_index_reference, score=document_index_score,
        done="the documentation is drafted, the user has its link, and it is promoted beside the index doc",
        promote="Yes, put it beside the index doc."),
}
