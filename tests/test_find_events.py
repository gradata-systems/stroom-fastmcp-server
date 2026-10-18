"""find_events: an event the user describes, found in an index through Stroom (asked for by the user: one rare event
among many indexed streams, which sampling would miss)."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tools import indexing


def _ctx():
    stroom = SimpleNamespace(get_doc=AsyncMock(return_value={'name': 'web', 'timeField': '@timestamp'}), settings=None)
    return SimpleNamespace(lifespan_context={'stroom': stroom})


async def test_only_fields_the_index_has_with_the_one_meant_named():
    fields = [{'name': n, 'type': t} for n, t in (('StreamId', 'long'), ('EventId', 'long'), ('@timestamp', 'date'),
                                                  ('user.name', 'keyword'), ('source.ip', 'ip'))]
    with patch.object(indexing, '_index_fields', AsyncMock(return_value=fields)):
        with pytest.raises(ToolError, match=r"Not fields of index 'web': user\.nmae \(like user\.name\)"):
            await indexing.find_events(_ctx(), 'i', where=[{'field': 'user.nmae', 'value': 'alice'}])
        # A search Stroom answers wrongly is refused, as verify_index refuses it.
        with pytest.raises(ToolError, match='CIDR'):
            await indexing.find_events(_ctx(), 'i', where=[{'field': 'source.ip', 'value': '10.0.*'}])
        with pytest.raises(ToolError, match="doesn't list the index"):
            await indexing.find_events(_ctx(), 'i')


async def test_hits_come_with_their_stream_and_event_and_where_the_rest_are():
    fields = [{'name': n, 'type': 'keyword'} for n in ('StreamId', 'EventId', '@timestamp', 'user.name')]
    rows = [{'StreamId': '7', 'EventId': '3', '@timestamp': '2026-10-13T00:00:00.000Z', 'user.name': 'alice'}]
    searches = []

    async def search(ctx, dashboard, expression, length=100, doing=None):
        searches.append((dashboard, expression, doing))
        table = next(c for c in dashboard['dashboardConfig']['components'] if c['type'] == 'table')
        if any(f.get('group') == 0 for f in table['settings']['fields']):
            return {'rows': [{'StreamId': '7', 'events': '2'}], 'errors': []}
        return {'rows': rows, 'errors': []}
    with patch.object(indexing, '_index_fields', AsyncMock(return_value=fields)), \
            patch.object(indexing, '_search', search), patch.object(indexing, '_count', AsyncMock(return_value=(2, []))):
        found = await indexing.find_events(_ctx(), 'i', where=[{'field': 'user.name', 'value': 'alice'}],
                                           time_from='day()-7d')
    assert found['total'] == 2 and found['hits'] == rows and found['by_stream'] == [{'stream_id': '7', 'events': '2'}]
    assert found['searched'] == ['user.name EQUALS alice', '@timestamp BETWEEN day()-7d,day()+1d']
    assert 'locate_event stream_id=<its StreamId> event_id=<its EventId>' in found['next']
    # Each search told to the client as it runs (asked for by the user).
    assert [d for _, _, d in searches] == [
        'Searching web for user.name EQUALS alice AND @timestamp BETWEEN day()-7d,day()+1d',
        'Grouping the matching events in web by stream']


async def test_events_are_located_one_pass_per_raw_stream():
    # Proving a fix on exactly what a search found: each hit to its raw record, however many events a record gives.
    from tools import diagnosis
    outputs = ['<Events><Event/></Events>', '<Events/>', '<Events><Event/><Event/></Events>', '<Events><Event/></Events>']
    steps = iter(range(len(outputs)))

    async def step(stroom, pipeline, raw, kind, location, code):
        n = next(steps)
        return {'foundRecord': True, 'foundLocation': {'partIndex': 0, 'recordIndex': n + 1},
                'stepData': {'elementMap': {'out': {'output': outputs[n]}}}}
    pipeline = SimpleNamespace(default_outputs=lambda: ['out'])
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(max_sample_records=500),
                                                                   stepping=nullcontext)})
    with patch.object(diagnosis, '_resolve', AsyncMock(return_value={'raw': 6, 'pipeline': 'p', 'events': 8})), \
            patch.object(diagnosis._Pipeline, 'load', AsyncMock(return_value=pipeline)), \
            patch.object(diagnosis, '_step', step):
        found = await diagnosis.event_locations(ctx, [diagnosis.EventRef(stream_id=8, event_id=3),
                                                      diagnosis.EventRef(stream_id=8, event_id=1)])
    # Record 1 gives event 1, record 2 none, record 3 events 2 and 3.
    assert [(f['event_id'], f['record']) for f in found] == [(1, 1), (3, 3)] and all(f['raw'] == 6 for f in found)


async def test_all_streams_lists_what_a_change_reaches():
    stroom = SimpleNamespace(find_meta=AsyncMock(return_value={'values': [
        {'meta': {'id': 8, 'parentMetaId': 6}}, {'meta': {'id': 9, 'parentMetaId': 7}}]}))
    affected = await indexing._affected(stroom, {'indexName': 'ecs-web-v1'},
                                        [{'stream_id': '8', 'events': '5'}, {'stream_id': '9.0', 'events': '1'}], 6)
    assert affected['events_streams'] == [8, 9] and affected['raw_streams'] == [6, 7] and affected['events'] == 6
    assert affected['elasticsearch_delete'] == 'POST ecs-web-v1/_delete_by_query {"query": {"terms": {"StreamId": [8, 9]}}}'
    assert 'reprocessed' in affected['delete_when'] and "user's to reprocess" in affected['reprocess']
