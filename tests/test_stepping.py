import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from tests.test_gateway import API, SETTINGS
from tests.test_triage import RULES
from tools import stepping
from utils.stroom import StroomGateway

LAYERS = [
    {'sourcePipeline': {'name': 'Event Data (Text)'}, 'pipelineData': {
        'elements': {'add': [{'id': 'dsParser', 'type': 'DSParser'}, {'id': 'translationFilter', 'type': 'XSLTFilter'},
                             {'id': 'schemaFilter', 'type': 'SchemaFilter'}]},
        'links': {'add': [{'from': 'dsParser', 'to': 'translationFilter'},
                          {'from': 'translationFilter', 'to': 'schemaFilter'}]}}},
    {'sourcePipeline': {'name': 'ACME'}, 'pipelineData': {
        'properties': {'add': [{'element': 'translationFilter', 'name': 'xslt', 'value': {'entity': {'uuid': 'x'}}}]}}},
]


def record(index, errors=None):
    return {'complete': True, 'foundRecord': True, 'sessionUuid': f's{index}',
            'foundLocation': {'metaId': 7, 'partIndex': 0, 'recordIndex': index},
            'stepData': {'elementMap': {
                'translationFilter': {'input': '<records/>', 'output': f'<Events>{index}</Events>'},
                'schemaFilter': {'indicators': {'errorCount': {'ERROR': 1}, 'uniqueErrorSet': errors}} if errors else {},
            }}}


@pytest.fixture
async def ctx():
    gw = StroomGateway(SETTINGS)
    yield SimpleNamespace(lifespan_context={'stroom': gw, 'rules': RULES})
    await gw.close()


def mock_pipeline():
    respx.get(f'{API}/pipeline/v1/p-1').mock(return_value=httpx.Response(200, json={'name': 'ACME', 'uuid': 'p-1'}))
    respx.post(f'{API}/pipeline/v1/fetchPipelineLayers').mock(return_value=httpx.Response(200, json=LAYERS))
    # The sample stream's feed: none here, so no stream is older than its feed.
    respx.post(f'{API}/meta/v1/find').mock(return_value=httpx.Response(200, json={'values': []}))


@respx.mock
async def test_step_sample_steps_every_record_with_fresh_requests(ctx):
    mock_pipeline()
    bad = [{'severity': 'ERROR', 'elementId': {'id': 'schemaFilter'}, 'message': 'bad TimeCreated',
            'location': {'lineNo': 5, 'colNo': 6}}]
    responses = [httpx.Response(200, json=record(0)), httpx.Response(200, json=record(1, bad)),
                 httpx.Response(200, json={'complete': True, 'foundRecord': False})]
    route = respx.post(f'{API}/stepping/v1/step').mock(side_effect=responses)

    result = await stepping.step_sample(ctx, 'p-1', [7])

    sent = [json.loads(c.request.content) for c in route.calls]
    assert [r['stepType'] for r in sent] == ['FIRST', 'FORWARD', 'FORWARD']
    assert all('sessionUuid' not in r for r in sent)
    assert sent[2]['stepLocation']['recordIndex'] == 1
    assert (result['records_stepped'], result['records_with_errors'], result['verdict']) == (2, 1, 'blocking')
    # These outputs have no Event: that is blocking too (the record templates select nothing), besides the error.
    assert [g['reason'] for g in result['groups']][0] == 'No record produced an Event'
    assert result['groups'][1]['records'] == ['7:1']
    assert result['first_record_output'] == {'translationFilter': '<Events>0</Events>'}



@respx.mock
async def test_events_with_no_event_in_any_record_are_blocking_and_one_event_is_enough(ctx):
    # Seen: XML fragments read with no namespace where the wrapper gives them records:2: every record came out as an
    # empty Events, and stepping said clean.
    mock_pipeline()

    def steps(*outputs):
        responses = []
        for n, output in enumerate(outputs):
            step = record(n)
            step['stepData']['elementMap']['translationFilter']['output'] = output
            responses.append(httpx.Response(200, json=step))
        return responses + [httpx.Response(200, json={'complete': True, 'foundRecord': False})]
    empty = '<Events xmlns="event-logging:3" Version="4.1.0"/>'
    respx.post(f'{API}/stepping/v1/step').mock(side_effect=steps(empty, empty))
    result = await stepping.step_sample(ctx, 'p-1', [7])
    assert result['verdict'] == 'blocking' and result['groups'][0]['reason'] == 'No record produced an Event'
    assert 'xml_namespace' in result['groups'][0]['examples'][0]['message']
    respx.post(f'{API}/stepping/v1/step').mock(side_effect=steps(empty, '<Events xmlns="event-logging:3"><Event/></Events>'))
    result = await stepping.step_sample(ctx, 'p-1', [7])
    assert all(g['reason'] != 'No record produced an Event' for g in result['groups'])   # a dropped record is fine


@respx.mock
async def test_reference_data_with_no_reference_in_any_record_is_blocking(ctx):
    # Seen: a directory's header read as a record (columns col1 ...), the key found nowhere: an empty referenceData,
    # no Reference stream, and stepping said clean.
    mock_pipeline()
    empty = '<referenceData xmlns="reference-data:2" version="2.0.1"/>'
    steps = []
    for n in range(2):
        step = record(n)
        step['stepData']['elementMap']['translationFilter']['output'] = empty
        steps.append(httpx.Response(200, json=step))
    respx.post(f'{API}/stepping/v1/step').mock(side_effect=steps + [httpx.Response(200, json={'complete': True, 'foundRecord': False})])
    result = await stepping.step_sample(ctx, 'p-1', [7])
    assert result['verdict'] == 'blocking' and result['groups'][0]['reason'] == 'No record produced reference data'
    assert 'col1, col2' in result['groups'][0]['examples'][0]['message']

@respx.mock
async def test_nothing_stepped_is_never_clean(ctx):
    mock_pipeline()
    # An XSLT that doesn't compile: Stroom finds no record and reports the error on the element.
    fatal = {'complete': True, 'foundRecord': False, 'generalErrors': [], 'stepData': {'elementMap': {'translationFilter': {
        'indicators': {'uniqueErrorSet': [{'severity': 'FATAL_ERROR', 'elementId': {'id': 'translationFilter'},
                                           'message': 'XsltPool - Variable user has not been declared',
                                           'location': {'lineNo': 61, 'colNo': 5}}]}}}}}
    respx.post(f'{API}/stepping/v1/step').mock(return_value=httpx.Response(200, json=fatal))
    result = await stepping.step_sample(ctx, 'p-1', [7])
    assert (result['records_stepped'], result['verdict']) == (0, 'blocking')
    assert 'Variable user has not been declared' in result['groups'][0]['examples'][0]['message']
    assert 'before its first record' in result['hint']
    located = await stepping.step_records(ctx, 'p-1', [{'stream': 7, 'record': 0}])
    assert (located['records_stepped'], located['verdict']) == (0, 'blocking') and located['groups']

    # No record and no error (an empty stream, a parser that reads nothing): still not clean.
    respx.post(f'{API}/stepping/v1/step').mock(return_value=httpx.Response(200, json={'complete': True, 'foundRecord': False}))
    empty = await stepping.step_sample(ctx, 'p-1', [7])
    assert empty['verdict'] == 'blocking' and 'found none in streams [7]' in empty['hint']


@respx.mock
async def test_incomplete_step_is_polled_with_its_session(ctx):
    mock_pipeline()
    route = respx.post(f'{API}/stepping/v1/step').mock(side_effect=[
        httpx.Response(200, json={'complete': False, 'sessionUuid': 'abc'}),
        httpx.Response(200, json=record(4)),
    ])
    ctx.lifespan_context['stroom'].settings = SETTINGS
    result = await stepping.step_pipeline(ctx, 'p-1', 7, 4, draft_code={'translationFilter': '<xsl/>'})
    first, poll = [json.loads(c.request.content) for c in route.calls]
    assert first['stepType'] == 'REFRESH' and first['stepLocation']['recordIndex'] == 4
    assert first['code'] == {'translationFilter': '<xsl/>'}
    assert poll['sessionUuid'] == 'abc'
    assert result['record'] == 4 and result['verdict'] == 'clean'
    assert result['elements']['translationFilter']['output'] == '<Events>4</Events>'


def unknown_record(index):
    out = (f'<Events xmlns="event-logging:3"><Event><EventDetail><TypeId>TRAFFIC</TypeId><Unknown/></EventDetail>'
           f'</Event></Events>')
    return {'complete': True, 'foundRecord': True, 'sessionUuid': f's{index}',
            'foundLocation': {'metaId': 7, 'partIndex': 0, 'recordIndex': index},
            'stepData': {'elementMap': {'translationFilter': {'input': '<records/>', 'output': out}}}}


@respx.mock
async def test_unknown_from_an_xslt_saved_without_a_mapping_blocks(ctx, monkeypatch):
    # As an agent did: TRAFFIC records written as an empty EventDetail/Unknown by a hand-written XSLT, past every
    # check build_translation_xslt makes, then processed and documented.
    mock_pipeline()
    tags = ['mcp-managed']

    class Guard:
        async def tags(self, ref):
            return tags

        async def tag(self, refs, names):
            pass
    monkeypatch.setattr(stepping, 'guard_from', lambda ctx: Guard())
    kept = {'mapping': None}

    async def kept_mapping(ctx, uuid):
        return kept['mapping']
    import tools.builds
    monkeypatch.setattr(tools.builds, 'kept_mapping', kept_mapping)
    monkeypatch.setattr(stepping, 'code_fingerprint', lambda *a, **k: _async({'x': 'v1'}))

    def steps():
        return [httpx.Response(200, json=unknown_record(0)), httpx.Response(200, json=record(1)),
                httpx.Response(200, json={'complete': True, 'foundRecord': False})]
    respx.post(f'{API}/stepping/v1/step').mock(side_effect=steps())
    result = await stepping.step_sample(ctx, 'p-1', [7])
    group = result['groups'][0]
    assert result['verdict'] == 'blocking' and group['records'] == ['7:0']
    assert group['examples'][0]['message'].startswith('1 of 2 records come out as EventDetail/Unknown')
    assert 'build_translation_xslt' in group['examples'][0]['message']
    respx.post(f'{API}/stepping/v1/step').mock(side_effect=steps()[:1])
    located = await stepping.step_records(ctx, 'p-1', [{'stream': 7, 'record': 0}])
    assert located['verdict'] == 'blocking' and 'EventDetail/Unknown' in located['groups'][0]['reason']

    # Saved from a mapping, Unknown was checked and agreed there; a working copy of a production pipeline, or a
    # pipeline outside a build, is someone else's design.
    for mapping, tagged in (({'kind': 'mapping'}, ['mcp-managed']), (None, ['mcp-managed', 'mcp-copy-of-p-9']), (None, [])):
        kept['mapping'], tags[:] = mapping, tagged
        respx.post(f'{API}/stepping/v1/step').mock(side_effect=steps())
        assert (await stepping.step_sample(ctx, 'p-1', [7]))['verdict'] == 'clean', (mapping, tagged)


async def _async(value):
    return value


@respx.mock
async def test_a_slow_steps_follow_up_carries_its_own_cookies_and_none_are_shared(ctx):
    # Two Stroom nodes behind an ingress with cookie affinity: a step outlasting Stroom's wait comes back unfinished,
    # and the follow-up must reach the node holding the session. Seen: "No stepping session found" on 755 KB arrays.
    stroom = ctx.lifespan_context['stroom']
    route = respx.post(f'{API}/stepping/v1/step').mock(side_effect=[
        httpx.Response(200, json={'complete': False, 'sessionUuid': 's1'},
                       headers=[('set-cookie', 'INGRESSCOOKIE=node-b; Path=/; HttpOnly'),
                                ('set-cookie', 'JSESSIONID=abc; Path=/')]),
        httpx.Response(200, json={'complete': True, 'foundRecord': False}),
        httpx.Response(200, json={'complete': True, 'foundRecord': False})])
    await stroom.step({'stepType': 'FIRST'}, poll_seconds=0)
    assert 'cookie' not in route.calls[0].request.headers
    assert route.calls[1].request.headers['cookie'] == 'INGRESSCOOKIE=node-b; JSESSIONID=abc'
    # Nothing kept for the next request, whoever makes it.
    await stroom.step({'stepType': 'FIRST'}, poll_seconds=0)
    assert 'cookie' not in route.calls[2].request.headers and len(stroom._client.cookies) == 0


@respx.mock
async def test_a_follow_up_on_another_node_says_why(ctx):
    stroom = ctx.lifespan_context['stroom']
    respx.post(f'{API}/stepping/v1/step').mock(side_effect=[
        httpx.Response(200, json={'complete': False, 'sessionUuid': 's1'}),
        httpx.Response(500, json={'message': 'No stepping session found for key: s1'})])
    with pytest.raises(Exception, match='reached a Stroom node other than the one stepping it') as e:
        await stroom.step({'stepType': 'FIRST'}, poll_seconds=0)
    assert 'addRootObject' in str(e.value) and 'sessionAffinity' in str(e.value)


@respx.mock
async def test_large_streams_are_stepped_from_their_head(ctx, monkeypatch):
    mock_pipeline()
    monkeypatch.setattr(stepping, 'PER_STREAM', 2)
    respx.post(f'{API}/stepping/v1/step').mock(side_effect=[httpx.Response(200, json=record(i)) for i in range(3)])
    result = await stepping.step_sample(ctx, 'p-1', [7])
    assert result['records_stepped'] == 2 and result['hint'].startswith('Stepped the first 2 records of each stream')


async def test_a_json_array_mapping_sets_the_parser_to_read_each_item_as_a_record():
    # Seen: four 755 KB arrays each stepped as one record of 985 events, every step outlasting Stroom's wait.
    from unittest.mock import AsyncMock
    from tools.pipeline_writes import PropertyValue, _json_array_parser
    from utils.mappingstore import with_mapping
    merged = {'elements': [{'id': 'jsonParser', 'type': 'JSONParser'}, {'id': 'translationFilter', 'type': 'XSLTFilter'}]}
    xslt = [PropertyValue(element='translationFilter', name='xslt', doc_uuid='x1', doc_type='XSLT')]

    def stroom_with(layout):
        described = with_mapping('', 'translation', {'mapping': {'input': 'json', 'json_layout': layout}})
        return SimpleNamespace(get_doc=AsyncMock(return_value={'description': described}))
    added = await _json_array_parser(stroom_with('array'), merged, xslt)
    assert (added.element, added.name, added.value) == ('jsonParser', 'addRootObject', False)
    assert await _json_array_parser(stroom_with('lines'), merged, xslt) is None     # JSON lines need the root map
    # With no mapping (an XSLT written by hand, or none yet), the sample decides: seen, never set otherwise.
    unmapped = SimpleNamespace(get_doc=AsyncMock(return_value={'description': ''}))
    assert await _json_array_parser(unmapped, merged, [], sample_is_array=False) is None
    assert (await _json_array_parser(unmapped, merged, [], sample_is_array=True)).value is False
    chosen = xslt + [PropertyValue(element='jsonParser', name='addRootObject', value=True)]
    assert await _json_array_parser(stroom_with('array'), merged, chosen) is None   # the agent's own choice stands
    # A pipeline stepping each array as one record says so.
    layers = [{'pipelineData': {'elements': {'add': merged['elements']}}}]
    assert stepping._Pipeline({'name': 'p'}, layers).json_root_map
    layers[0]['pipelineData']['properties'] = {'add': [{'element': 'jsonParser', 'name': 'addRootObject',
                                                        'value': {'boolean': False}}]}
    assert not stepping._Pipeline({'name': 'p'}, layers).json_root_map
