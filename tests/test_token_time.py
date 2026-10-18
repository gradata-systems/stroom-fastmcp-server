"""A call that waits or loops stops before the caller's access token runs out and says to call again (seen: with
Stroom's internal identity provider, a token for this server lives 5 minutes and comes with as little as 1 left, and a
call that outlasted it failed part way)."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import respx

from tests.test_gateway import API, SETTINGS, USER_SETTINGS
from utils.stroom import TOKEN_MARGIN, StroomGateway


def test_a_tokens_time_left_is_kept_with_a_margin_and_none_without_an_expiry():
    with patch('utils.stroom.get_access_token', return_value=SimpleNamespace(expires_at=time.time() + 100)):
        left = StroomGateway(USER_SETTINGS).token_seconds_left()
    assert 100 - TOKEN_MARGIN - 2 < left <= 100 - TOKEN_MARGIN
    with patch('utils.stroom.get_access_token', return_value=SimpleNamespace(expires_at=None)):
        assert StroomGateway(USER_SETTINGS).token_seconds_left() is None
    assert StroomGateway(SETTINGS).token_seconds_left() is None                     # an API key: no expiry
    assert StroomGateway(USER_SETTINGS, authorization={'Authorization': 'Bearer x'}).token_seconds_left() is None


async def test_a_search_waits_no_longer_than_the_token_has_left():
    from tools import indexing
    stroom = StroomGateway(SETTINGS)
    dashboard = {'uuid': 'd', 'name': 'probe', 'dashboardConfig': {'components': [
        {'type': 'query', 'id': 'q', 'settings': {'dataSource': {'type': 'ElasticIndex', 'uuid': 'i'}}},
        {'type': 'table', 'id': 't', 'name': 'Table', 'settings': {'fields': [{'name': 'x'}]}}]}}
    with respx.mock, patch.object(StroomGateway, 'token_seconds_left', return_value=1.2):
        route = respx.post(f'{API}/dashboard/v1/search').mock(
            return_value=httpx.Response(200, json={'complete': False, 'results': []}))
        found = await indexing._search(SimpleNamespace(lifespan_context={'stroom': stroom}), dashboard,
                                       {'type': 'operator', 'op': 'AND', 'children': []})
    await stroom.close()
    assert not found['complete'] and json.loads(route.calls[0].request.content)['timeout'] <= 1200


@respx.mock
async def test_step_sample_stops_before_the_token_runs_out_and_records_no_clean_step():
    from tests.test_stepping import mock_pipeline, record
    from tests.test_triage import RULES
    from tools import stepping
    mock_pipeline()
    respx.post(f'{API}/stepping/v1/step').mock(side_effect=lambda request: httpx.Response(200, json=record(
        0 if json.loads(request.content)['stepType'] == 'FIRST'
        else json.loads(request.content)['stepLocation']['recordIndex'] + 1)))
    stroom = StroomGateway(SETTINGS)
    lefts = iter([30, 20, 10, 0])           # seconds the token has left, at each record
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom, 'rules': RULES})
    with patch.object(StroomGateway, 'token_seconds_left', side_effect=lambda: next(lefts)), \
            patch.object(stepping, 'remember_clean', AsyncMock()) as remembered:
        result = await stepping.step_sample(ctx, 'p-1', [7])
    await stroom.close()
    assert result['records_stepped'] == 4 and result['stopped_early']
    assert result['hint'].startswith('Stopped after 4 records before your sign-in (access token) ran out. Call again')
    assert not remembered.called
