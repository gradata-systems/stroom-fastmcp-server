"""Access token verification against a generic OpenID Connect provider."""
from unittest.mock import patch

import httpx2
import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from joserfc.jwk import RSAKey

import security.audit as audit_module
from config import Settings
from security.auth import oidc_auth

KEYS = RSAKeyPair.generate()
JWKS = {'keys': [{**RSAKey.import_key(KEYS.public_key).as_dict(), 'kid': 'k1', 'use': 'sig'}]}


def provider(issuer: str, requests: list[str]) -> httpx2.AsyncClient:
    """A provider at `issuer` that publishes its keys at a path Keycloak doesn't use."""
    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        if str(request.url) == f"{issuer.rstrip('/')}/.well-known/openid-configuration":
            return httpx2.Response(200, json={'issuer': issuer, 'jwks_uri': 'https://idp.example/keys'})
        if str(request.url) == 'https://idp.example/keys':
            return httpx2.Response(200, json=JWKS)
        return httpx2.Response(404)
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, stroom_url='https://stroom.example', oidc_audience='stroom-mcp',
                    public_base_url='https://mcp.example', **overrides)


def token(issuer: str, **kwargs) -> str:
    return KEYS.create_token(issuer=issuer, audience=['stroom-mcp', 'stroom'], kid='k1', **kwargs)


# Auth0's issuer ends in '/', which must be kept to match the iss claim.
@pytest.mark.parametrize('issuer', ['https://login.example/tenant/v2.0', 'https://tenant.auth0.example/'])
async def test_signing_keys_come_from_the_discovery_document(issuer):
    requests = []
    auth = oidc_auth(settings(oidc_issuer_url=issuer), provider(issuer, requests))
    access = await auth.verify_token(token(issuer, subject='u1', scopes=['openid']))
    assert access is not None and access.claims['sub'] == 'u1'
    assert requests == [f"{issuer.rstrip('/')}/.well-known/openid-configuration", 'https://idp.example/keys']
    # Discovered once, not per token.
    await auth.verify_token(token(issuer, subject='u2', scopes=['openid']))
    assert len(requests) == 2


async def test_configured_jwks_uri_skips_discovery():
    requests = []
    auth = oidc_auth(settings(oidc_issuer_url='https://idp.example', oidc_jwks_uri='https://idp.example/keys'),
                     provider('https://idp.example', requests))
    assert await auth.verify_token(token('https://idp.example', scopes=['openid']))
    assert requests == ['https://idp.example/keys']


async def test_required_scopes_are_configurable():
    issuer = 'https://idp.example'
    entra_like = token(issuer, additional_claims={'scp': 'access'})
    assert await oidc_auth(settings(oidc_issuer_url=issuer), provider(issuer, [])).verify_token(entra_like) is None
    auth = oidc_auth(settings(oidc_issuer_url=issuer, oidc_required_scopes=[]), provider(issuer, []))
    assert await auth.verify_token(entra_like)


async def test_token_without_sub_is_rejected():
    issuer = 'https://idp.example'
    auth = oidc_auth(settings(oidc_issuer_url=issuer), provider(issuer, []))
    assert await auth.verify_token(token(issuer, subject='', scopes=['openid'])) is None


async def test_token_from_another_issuer_is_rejected():
    issuer = 'https://idp.example'
    auth = oidc_auth(settings(oidc_issuer_url=issuer), provider(issuer, []))
    assert await auth.verify_token(token('https://other.example', scopes=['openid'])) is None


@pytest.mark.parametrize('value, expected', [('openid', ['openid']), ('openid, profile api://x/y', ['openid', 'profile', 'api://x/y']),
                                             ('', [])])
def test_required_scopes_parse_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv('STROOM_MCP_OIDC_REQUIRED_SCOPES', value)
    assert Settings(_env_file=None, stroom_url='https://s').oidc_required_scopes == expected


@pytest.mark.parametrize('kwargs, check', [
    ({'expires_in_seconds': -60}, 'expired'),
    ({'issuer': 'https://other.example'}, 'issuer'),
    ({'audience': 'someone-else'}, 'audience'),
    ({'scopes': ['profile']}, 'scopes'),
    ({'subject': ''}, 'sub'),
])
async def test_rejected_tokens_are_audited_with_the_check_they_failed(kwargs, check):
    issuer = 'https://idp.example'
    auth = oidc_auth(settings(oidc_issuer_url=issuer), provider(issuer, []))
    claims = {'issuer': issuer, 'audience': ['stroom-mcp', 'stroom'], 'kid': 'k1', 'scopes': ['openid'], **kwargs}
    with patch('security.auth.audit') as audit:
        assert await auth.verify_token(KEYS.create_token(**claims)) is None
    audit.assert_called_once_with('access_denied', reason='invalid_token', check=check)


async def test_forged_and_unfetchable_tokens_are_audited():
    issuer = 'https://idp.example'
    forged = RSAKeyPair.generate().create_token(issuer=issuer, audience='stroom-mcp', kid='k1', scopes=['openid'])
    with patch('security.auth.audit') as audit:
        assert await oidc_auth(settings(oidc_issuer_url=issuer), provider(issuer, [])).verify_token(forged) is None
        audit.assert_called_once_with('access_denied', reason='invalid_token', check='signature')
        audit.reset_mock()
        down = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(503)))
        assert await oidc_auth(settings(oidc_issuer_url=issuer), down).verify_token(token(issuer, scopes=['openid'])) is None
        audit.assert_called_once_with('access_denied', reason='invalid_token', check='signing_key')
        audit.reset_mock()
        assert await oidc_auth(settings(oidc_issuer_url=issuer), provider(issuer, [])).verify_token('not.a.jwt') is None
        audit.assert_called_once_with('access_denied', reason='invalid_token', check='malformed')


@pytest.mark.parametrize('claims, username', [
    ({'preferred_username': 'alice', 'upn': 'alice@corp', 'email': 'a@corp'}, 'alice'),
    ({'upn': 'alice@corp', 'email': 'a@corp'}, 'alice@corp'),
    ({'email': 'a@corp'}, 'a@corp'),
    ({}, None),
])
def test_audit_username_falls_back_to_upn_then_email(claims, username):
    access = AccessToken(token='t', client_id='vscode', scopes=[], subject='u1', claims={'sub': 'u1', **claims})
    with patch('security.audit.get_access_token', return_value=access):
        assert audit_module._identity()['username'] == username
