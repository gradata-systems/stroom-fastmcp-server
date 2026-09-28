import json
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from config import Settings
from tools import explorer
from utils.stroom import StroomGateway

SETTINGS = Settings(
    _env_file=None,
    stroom_url='https://stroom.example/',
    dev_no_auth=True, stroom_api_key='sak_test',
    keycloak_realm_url='https://kc.example/realms/r',
    keycloak_audience='stroom-mcp',
    public_base_url='https://mcp.example',
)
API = 'https://stroom.example/api'


@pytest.fixture
async def gateway():
    gw = StroomGateway(SETTINGS)
    yield gw
    await gw.close()


@respx.mock
async def test_requests_carry_the_api_key_and_trailing_slash_is_ignored(gateway):
    route = respx.get(f'{API}/meta/v1/getTypes').mock(return_value=httpx.Response(200, json=['Events']))
    assert await gateway.get('/meta/v1/getTypes') == ['Events']
    assert route.calls.last.request.headers['Authorization'] == 'Bearer sak_test'


@respx.mock
async def test_find_sends_view_permission_and_a_name_filter(gateway):
    route = respx.post(f'{API}/explorer/v2/find').mock(return_value=httpx.Response(200, json={'values': []}))
    await gateway.find_documents('', ['Pipeline'], 10)
    sent = json.loads(route.calls.last.request.content)
    assert sent['filter']['requiredPermissions'] == ['VIEW']
    assert sent['filter']['nameFilter'] == '*'
    assert sent['filter']['includedTypes'] == ['Pipeline']


@pytest.mark.parametrize('status, message', [
    (403, 'Stroom denied access'),
    (404, 'Not found in Stroom'),
    (500, 'Stroom rejected the request (500): boom'),
])
@respx.mock
async def test_errors_become_tool_errors_with_stroom_reason(gateway, status, message):
    respx.get(f'{API}/xslt/v1/x').mock(return_value=httpx.Response(status, json={'message': 'boom'}))
    with pytest.raises(ToolError, match=message.replace('(', r'\(').replace(')', r'\)')):
        await gateway.get('/xslt/v1/x')


@respx.mock
async def test_connection_failure_is_reported_as_unavailable(gateway):
    respx.get(f'{API}/xslt/v1/x').mock(side_effect=httpx.ConnectError('refused'))
    with pytest.raises(ToolError, match='Stroom is unavailable'):
        await gateway.get('/xslt/v1/x')


@respx.mock
async def test_get_document_redacts_cluster_credentials(gateway):
    respx.get(f'{API}/elasticCluster/v1/c-1').mock(return_value=httpx.Response(200, json={
        'name': 'ES_PROD', 'connection': {'connectionUrls': ['https://es:9200'], 'apiKeySecret': 's3cret',
                                          'caCertificate': '-----BEGIN'}}))
    ctx = SimpleNamespace(lifespan_context={'stroom': gateway})
    doc = await explorer.get_document(ctx, 'ElasticCluster', 'c-1')
    assert doc['connection'] == {'connectionUrls': ['https://es:9200'], 'apiKeySecret': '<redacted>',
                                 'caCertificate': '<redacted>'}


@respx.mock
async def test_find_documents_flattens_results_and_hints_when_truncated(gateway):
    respx.post(f'{API}/explorer/v2/find').mock(return_value=httpx.Response(200, json={
        'values': [{'docRef': {'type': 'Folder', 'uuid': 'f-1', 'name': 'Keycloak'},
                    'path': 'System / Elastic Indices'},
                   {'docRef': {'type': 'Pipeline', 'uuid': 'p-1', 'name': 'Keycloak - Indexing'},
                    'path': 'System / Elastic Indices / Keycloak'}],
        'pageResponse': {'total': 5}}))
    ctx = SimpleNamespace(lifespan_context={'stroom': gateway})
    result = await explorer.find_documents(ctx, 'Keycloak', ['Pipeline'], 1)
    assert result['documents'] == [{'type': 'Pipeline', 'uuid': 'p-1', 'name': 'Keycloak - Indexing',
                                    'path': 'System / Elastic Indices / Keycloak'}]
    assert 'hint' in result


USER_SETTINGS = SETTINGS.model_copy(update={'dev_no_auth': False, 'stroom_api_key': None})


def _token(aud, expires_at=None):
    from fastmcp.server.auth import AccessToken
    return AccessToken(token='user-jwt', client_id='openwebui', scopes=[], expires_at=expires_at,
                       claims={'aud': aud, 'preferred_username': 'alice'})


@respx.mock
async def test_calls_act_as_the_caller_with_their_own_token(monkeypatch):
    monkeypatch.setattr('utils.stroom.get_access_token', lambda: _token(['stroom-mcp', 'stroom']))
    route = respx.get(f'{API}/meta/v1/getTypes').mock(return_value=httpx.Response(200, json=[]))
    gw = StroomGateway(USER_SETTINGS)
    await gw.get('/meta/v1/getTypes')
    await gw.close()
    assert route.calls.last.request.headers['Authorization'] == 'Bearer user-jwt'


@pytest.mark.parametrize('token, message', [
    (None, 'No caller identity'),
    (_token('stroom-mcp'), "lacks 'stroom'"),
    (_token(['stroom', 'stroom-mcp'], expires_at=1), 'expired'),
])
async def test_calls_refuse_without_a_token_stroom_accepts(monkeypatch, token, message):
    monkeypatch.setattr('utils.stroom.get_access_token', lambda: token)
    gw = StroomGateway(USER_SETTINGS)
    with pytest.raises(ToolError, match=message):
        await gw.get('/meta/v1/getTypes')
    await gw.close()
