"""Searching an indexed Elasticsearch index both ways, for the e2e suites.

Each search runs through Stroom, as people will search (a verification dashboard, via verify_index, with each hit
traced back to its record by stepping the indexing pipeline), and directly in Elasticsearch, as an independent
check of what was indexed. Both must return the expected count: Elasticsearch finding a document Stroom does not
points at Stroom's field list or query translation; neither finding it, at the mapping or the indexing XSLT.
"""
from typing import Any

import httpx

import e2e_translation as e2e
from tools import indexing


async def paired(ctx, es: httpx.AsyncClient, build: str, index: str, index_uuid: str, stream_ids: list[int],
                 total: int, fields: list[str], checks: list[tuple[str, str, str, int, dict[str, Any]]],
                 pipeline_uuid: str | None = None) -> None:
    """checks: (field, Stroom condition, value, expected rows, the same search as an Elasticsearch query)."""
    await es.post(f'/{index}/_refresh')
    searches = [indexing.SearchCheck(field=f, condition=c, value=v, expected=n) for f, c, v, n, _ in checks]
    verified = await e2e.agreed(indexing.verify_index, ctx=ctx, build=build, index_uuid=index_uuid,
                                backend='elasticsearch', stream_ids=stream_ids, expected_documents=total, fields=fields,
                                searches=searches, pipeline_uuid=pipeline_uuid)
    await dashboard_designed(ctx, verified['dashboard'], fields, total)
    every, by_search = verified['checks'][0], verified['checks'][1:]
    trace = every.get('trace')
    e2e.check(every['pass'], f"Stroom finds all {total} documents of streams {stream_ids}: {every['returned']}"
                            + (f"; the first traced to record {trace['event']} of stream {trace['stream']}" if trace else ''))
    for (field, condition, value, expected, query), check in zip(checks, by_search):
        direct = (await es.post(f'/{index}/_count', json={'query': query})).json().get('count')
        traced = check.get('trace')
        e2e.check(check['returned'] == expected and direct == expected and check['pass'],
                 f"{field} {condition} {value!r}: Stroom {check['returned']}, Elasticsearch {direct}, expected "
                 f"{expected}" + (f", hit traced to record {traced['event']}" if traced and traced['traced'] else
                                  f", trace: {traced}" if traced else '')
                 + (f"; errors {check['errors']}" if check['errors'] else ''))


async def dashboard_designed(ctx, dashboard: dict[str, Any], fields: list[str], total: int) -> None:
    """The verification dashboard as Stroom kept it: the user's columns (no StreamId or EventId shown), newest first;
    a query on the time field from a 30-day boundary through today, which finds the sample by itself; and a text pane
    on the selected row's record, with stepping and no extraction pipeline."""
    if not dashboard.get('saved'):
        e2e.check(True, f"another build's index: searched through an unsaved dashboard, nothing added to this build")
        return
    doc = await ctx.lifespan_context['stroom'].get_doc('Dashboard', dashboard['uuid'])
    components = {c['type']: c for c in doc['dashboardConfig']['components']}
    query, table, text = (components[t]['settings'] for t in ('query', 'table', 'text'))
    shown = [c['name'] for c in table['fields'] if c.get('visible', True)]
    hidden = {c['name']: c for c in table['fields'] if not c.get('visible', True)}
    sorted_on = [c['name'] for c in table['fields'] if c.get('sort')]
    e2e.check(shown == [f for f in fields if f not in ('StreamId', 'EventId')] and {'StreamId', 'EventId'} <= set(hidden),
              f"the table shows the user's columns {shown}; StreamId and EventId hidden")
    window = (query['expression']['children'] or [{}])[0].get('value', '')
    e2e.check(len(sorted_on) == 1 and query['automate']['open'] and window.endswith(',day()+1d')
              and window[:10] in dashboard['initial query'],
              f"newest first on {sorted_on}; opens with {sorted_on[0] if sorted_on else '?'} from {window}")
    e2e.check(text.get('showStepping') is True and not text.get('pipeline') and table.get('extractValues') is False
              and text['streamIdField']['id'] == hidden['StreamId']['id']
              and text['recordNoField']['id'] == hidden['EventId']['id'],
              "the text pane shows the selected row's record by its StreamId and EventId, with stepping, no "
              "extraction pipeline")
    opened = await indexing._search(ctx, doc, query['expression'])
    e2e.check(len(opened['rows']) >= total and not opened['errors'],
              f"the initial query finds the sample by itself: {len(opened['rows'])} of at least {total}")
