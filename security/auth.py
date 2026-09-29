"""Verifying access tokens from an OpenID Connect provider (Keycloak, Entra ID, Okta, Auth0, ...).

The server is an OAuth2 protected resource: it points clients at the provider (the issuer) and verifies
the access tokens they bring. The provider's signing keys come from oidc_jwks_uri, or else from the
jwks_uri in the issuer's discovery document, fetched when the first token arrives so that startup and
/healthz don't depend on the provider being up.

FastMCP's JWTVerifier fetches keys with the system CAs only, and logs why a token was rejected at debug
level when the keys can't be fetched or the signing algorithm is wrong, so those failures show up only as
"invalid token". This verifier trusts an extra CA for the provider and logs those failures as warnings.
"""
from pathlib import Path
from typing import Any

import httpx2
from fastmcp.server.auth import AccessToken, RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.utilities.auth import decode_jwt_header
from pydantic import AnyHttpUrl

from config import Settings
from utils.tls import trust


def discovery_url(issuer: str) -> str:
    return f"{issuer.rstrip('/')}/.well-known/openid-configuration"


class OIDCTokenVerifier(JWTVerifier):
    """JWTVerifier that discovers the signing keys, requires a sub claim, and reports signing key and
    algorithm failures, not just claim mismatches."""

    def __init__(self, *, issuer: str, jwks_uri: str | None, **kwargs: Any):
        # Until discovery runs, jwks_uri holds the discovery URL (JWTVerifier insists on one) for messages.
        super().__init__(issuer=issuer, jwks_uri=jwks_uri or discovery_url(issuer), **kwargs)
        self._discover = not jwks_uri

    async def _fetch_jwks(self) -> dict[str, Any]:
        if self._discover:
            self.jwks_uri = await self._discover_jwks_uri()
            self._discover = False
        return await super()._fetch_jwks()

    async def _discover_jwks_uri(self) -> str:
        assert isinstance(self.issuer, str) and self._http_client is not None
        response = await self._http_client.get(discovery_url(self.issuer))
        response.raise_for_status()
        metadata = response.json()
        if metadata.get('issuer') != self.issuer:
            self.logger.warning("The provider's discovery document names issuer %r, but tokens must come from %r "
                                "(STROOM_MCP_OIDC_ISSUER_URL)", metadata.get('issuer'), self.issuer)
        if not metadata.get('jwks_uri'):
            raise ValueError(f"no jwks_uri in {discovery_url(self.issuer)}; set STROOM_MCP_OIDC_JWKS_URI")
        return metadata['jwks_uri']

    async def _get_verification_key(self, token: str) -> str | bytes:
        try:
            return await super()._get_verification_key(token)
        except Exception as e:
            self.logger.warning("Bearer token rejected: couldn't get its signing key from %s: %s", self.jwks_uri, e)
            raise

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            algorithm = decode_jwt_header(token).get('alg')
        except Exception:
            algorithm = None
        if algorithm and algorithm != self.algorithm:
            self.logger.warning("Bearer token rejected: signed with %s, but only %s is accepted "
                                "(STROOM_MCP_OIDC_TOKEN_ALGORITHM)", algorithm, self.algorithm)
        access_token = await super().load_access_token(token)
        # Stroom matches users on sub; without it, calls would fail later and less clearly.
        if access_token and not (access_token.claims or {}).get('sub'):
            self.logger.warning("Bearer token rejected for client %s: no sub claim", access_token.client_id)
            return None
        return access_token


def oidc_http_client(ca_file: Path | None) -> httpx2.AsyncClient:
    """HTTPS client for the provider that trusts the system CAs plus `ca_file`, if given."""
    return httpx2.AsyncClient(verify=trust(ca_file), timeout=httpx2.Timeout(10.0))


def oidc_auth(settings: Settings, http_client: httpx2.AsyncClient) -> RemoteAuthProvider:
    # The issuer is compared with the iss claim as is: some providers' end in '/' (Auth0), most don't.
    issuer = settings.oidc_issuer_url
    verifier = OIDCTokenVerifier(
        issuer=issuer,
        jwks_uri=settings.oidc_jwks_uri,
        audience=settings.oidc_audience,
        algorithm=settings.oidc_token_algorithm,
        required_scopes=settings.oidc_required_scopes,
        http_client=http_client,
    )
    return RemoteAuthProvider(token_verifier=verifier, authorization_servers=[AnyHttpUrl(issuer)],
                              base_url=AnyHttpUrl(settings.public_base_url.rstrip('/')))
