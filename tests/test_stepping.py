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
    assert result['groups'][0]['records'] == ['7:1']
    assert result['first_record_output'] == {'translationFilter': '<Events>0</Events>'}


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
