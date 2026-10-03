"""Searching an indexed Elasticsearch index both ways, for the e2e suites.

Each search runs through Stroom, as people will search (a verification dashboard, via verify_index, with each hit
traced back to its record by stepping the indexing pipeline), and directly in Elasticsearch, as an independent
check of what was indexed. Both must return the expected count: Elasticsearch finding a document Stroom does not
points at Stroom's field list or query translation; neither finding it, at the mapping or the indexing XSLT.
"""
from typing import Any

import httpx

import e2e_phase2 as p2
from tools import indexing


async def paired(ctx, es: httpx.AsyncClient, build: str, index: str, index_uuid: str, stream_ids: list[int],
                 total: int, fields: list[str], checks: list[tuple[str, str, str, int, dict[str, Any]]],
                 pipeline_uuid: str | None = None) -> None:
    """checks: (field, Stroom condition, value, expected rows, the same search as an Elasticsearch query)."""
    await es.post(f'/{index}/_refresh')
    searches = [indexing.SearchCheck(field=f, condition=c, value=v, expected=n) for f, c, v, n, _ in checks]
    verified = await indexing.verify_index(ctx, build, index_uuid, 'elasticsearch', stream_ids, total, fields,
                                           searches=searches, pipeline_uuid=pipeline_uuid)
    every, by_search = verified['checks'][0], verified['checks'][1:]
    trace = every.get('trace')
    p2.check(every['pass'], f"Stroom finds all {total} documents of streams {stream_ids}: {every['returned']}"
                            + (f"; the first traced to record {trace['event']} of stream {trace['stream']}" if trace else ''))
    for (field, condition, value, expected, query), check in zip(checks, by_search):
        direct = (await es.post(f'/{index}/_count', json={'query': query})).json().get('count')
        traced = check.get('trace')
        p2.check(check['returned'] == expected and direct == expected and check['pass'],
                 f"{field} {condition} {value!r}: Stroom {check['returned']}, Elasticsearch {direct}, expected "
                 f"{expected}" + (f", hit traced to record {traced['event']}" if traced and traced['traced'] else
                                  f", trace: {traced}" if traced else '')
                 + (f"; errors {check['errors']}" if check['errors'] else ''))
