"""find_events: an event the user describes, found in an index through Stroom (asked for by the user: one rare event
among many indexed streams, which sampling would miss)."""
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

    async def search(ctx, dashboard, expression, length=100):
        searches.append((dashboard, expression))
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
