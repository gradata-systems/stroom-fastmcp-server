"""What a long tool call tells the client as it goes (asked for by the user: describe_document on an index ran for
minutes with nothing to show what it was doing)."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import respx

from tests.test_gateway import API, SETTINGS
from tools import indexing
from utils.progress import Progress, progress_from
from utils.stroom import StroomGateway


class Ctx:
    """A tool call's context as fastmcp gives it: request state, and progress for a client that asked for it."""

    def __init__(self, stroom=None):
        self.lifespan_context = {'stroom': stroom}
        self.state: dict = {}
        self.sent: list[tuple[float, str]] = []

    async def get_state(self, key):
        return self.state.get(key)

    async def set_state(self, key, value, serializable=True):
        self.state[key] = value

    async def report_progress(self, progress, total=None, message=None):
        self.sent.append((progress, message))


async def test_one_call_counts_its_progress_up_wherever_it_is_told():
    ctx = Ctx()
    await (await progress_from(ctx))('one')
    await (await progress_from(ctx))('two')        # e.g. write_documentation surveying the index a second time
    assert ctx.sent == [(1, 'one'), (2, 'two')]


async def test_progress_never_fails_the_call():
    async def broken(*args, **kwargs):
        raise RuntimeError('the client went away')
    await Progress(SimpleNamespace(report_progress=broken))('still here')
    await Progress(None)('no context at all')
    assert isinstance(await progress_from(SimpleNamespace()), Progress)      # no request state: one of its own


async def test_the_survey_says_what_it_is_doing_and_a_search_says_it_is_still_going():
    stroom = StroomGateway(SETTINGS)
    ctx = Ctx(stroom)
    with respx.mock:
        respx.get(f'{API}/elasticIndex/v1/i').mock(return_value=httpx.Response(200, json={
            'name': 'TWITTER', 'indexName': 'stroom-twitter-debug*', 'timeField': '@timestamp'}))
        respx.post(f'{API}/dataSource/v1/findFields').mock(return_value=httpx.Response(200, json={'values': [
            {'fldName': n, 'fldType': 'KEYWORD'} for n in ('StreamId', 'EventId', '@timestamp')]}))
        # The last 30 days: none. Any time: Stroom still looking, at every poll.
        searches = respx.post(f'{API}/dashboard/v1/search').mock(side_effect=lambda request: httpx.Response(
            200, json={'complete': 'day()-30d' in request.content.decode(), 'results': []}))
        respx.post(f'{API}/explorer/v2/find').mock(return_value=httpx.Response(200, json={'values': []}))
        respx.post(f'{API}/explorer/v2/findInContent').mock(return_value=httpx.Response(200, json={'values': []}))
        with patch.object(indexing, 'SAMPLE_WAIT', 0.6):
            await indexing.survey_index(ctx, 'ElasticIndex', 'i', profile=False)
    await stroom.close()
    assert [p for p, _ in ctx.sent] == list(range(1, len(ctx.sent) + 1))
    said = [m for _, m in ctx.sent]
    assert said[:3] == ['Reading the fields of TWITTER (stroom-twitter-debug*)',
                        'Searching stroom-twitter-debug* for up to 100 documents from the last 30 days',
                        'Searching stroom-twitter-debug* for up to 100 documents of any time']
    assert said[3].startswith('Searching stroom-twitter-debug* for up to 100 documents of any time: still searching, ')
    assert said[-1] == 'Looking for the pipelines that write to stroom-twitter-debug*'
    # Each poll held only as long as the wait has left.
    assert all(json.loads(c.request.content)['timeout'] <= 2000 for c in searches.calls)


async def test_counting_a_whole_index_says_it_reads_all_of_it():
    ctx = Ctx(SimpleNamespace())
    doing = []

    async def search(ctx, dashboard, expression, length=100, wait=None, **kwargs):
        doing.append(kwargs.get('doing'))
        return {'rows': [], 'errors': [], 'complete': True}
    with patch.object(indexing, '_search', search):
        await indexing.index_profile(ctx, {'type': 'ElasticIndex', 'uuid': 'i', 'name': 'TWITTER'}, None, ['f'],
                                     label='stroom-twitter-debug*')
    assert doing == ['Counting every document in stroom-twitter-debug* (this reads the whole index)']


@respx.mock
async def test_step_sample_says_when_each_stream_starts_and_only_now_and_then_while_stepping_it():
    # Asked for by the user: not an update for each and every record stepped.
    from tests.test_stepping import mock_pipeline, record
    from tests.test_triage import RULES
    from tools import stepping
    from utils import progress
    mock_pipeline()
    clock = [0.0]

    def step(request):
        clock[0] += 2       # each step takes 2 s
        sent = json.loads(request.content)
        at = 0 if sent['stepType'] == 'FIRST' else sent['stepLocation']['recordIndex'] + 1
        return httpx.Response(200, json=record(at) if at < 7 else {'complete': True, 'foundRecord': False})
    respx.post(f'{API}/stepping/v1/step').mock(side_effect=step)
    stroom = StroomGateway(SETTINGS)
    ctx = Ctx(stroom)
    ctx.lifespan_context['rules'] = RULES
    with patch.object(progress, 'time', SimpleNamespace(monotonic=lambda: clock[0])):
        result = await stepping.step_sample(ctx, 'p-1', [7])
    await stroom.close()
    assert result['records_stepped'] == 7
    assert [m for _, m in ctx.sent] == ['Stepping stream 7 (1 of 1) through ACME: up to 50 records',
                                        'Stepping stream 7 (1 of 1): 3 of up to 50 records stepped',
                                        'Stepping stream 7 (1 of 1): 6 of up to 50 records stepped']


async def test_stepping_a_spread_of_records_says_so_once_and_then_now_and_then():
    # Seen: 200 records stepped for write_documentation in 132 s, with nothing shown. Found through the call's own
    # context (current_progress): step_spread is handed the gateway, not the context.
    from contextlib import nullcontext
    from tools import stepping
    from utils import progress
    ctx, clock = Ctx(), [0.0]

    async def step(stroom, pipeline, stream_id, step_type, location, code):
        clock[0] += 1
        at = 0 if step_type == 'FIRST' else location['recordIndex'] + 1
        return {'foundRecord': at < 30, 'foundLocation': {'metaId': stream_id, 'partIndex': 0, 'recordIndex': at}}
    with patch.object(stepping, '_step', step), patch.object(progress, 'get_context', lambda: ctx), \
            patch.object(progress, 'time', SimpleNamespace(monotonic=lambda: clock[0])):
        steps = await stepping.step_spread(SimpleNamespace(stepping=nullcontext, token_seconds_left=lambda: None), SimpleNamespace(doc={'name': 'ACME'}),
                                           [1, 2], None, 40)
    said = [m for _, m in ctx.sent]
    assert len(steps) == 40 and said[0] == 'Stepping up to 40 records of 2 streams through ACME'
    assert 2 <= len(said) <= 10 and all(m.endswith('of up to 40 records stepped') for m in said[1:])


async def test_finding_a_feeds_streams_says_so():
    # Seen: 28 stream searches, 2.4 s each, most of a 35 s survey_feed call.
    from tools import sampling
    from utils import progress
    ctx = Ctx()
    stream = {'id': 6, 'createMs': 1_700_000_000_000, 'status': 'UNLOCKED'}
    stroom = SimpleNamespace(find_meta=AsyncMock(return_value={'values': [{'meta': stream}]}))
    with patch.object(progress, 'get_context', lambda: ctx):
        picked, _ = await sampling.pick_spread(stroom, [], 3, set())
    assert picked == [stream] and ctx.sent[0][1] == "Finding up to 3 streams spread over the feed's time range"
