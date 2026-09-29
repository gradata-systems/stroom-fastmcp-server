"""Verifying Keycloak access tokens.

FastMCP's Keycloak provider fetches the realm's signing keys with the system CAs only, and logs why a
token was rejected at debug level when the keys can't be fetched or the signing algorithm is wrong, so
those failures show up only as "invalid token". This verifier trusts an extra CA for Keycloak and logs
those failures as warnings.
"""
from pathlib import Path
from typing import Any

import httpx2
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider
from fastmcp.utilities.auth import decode_jwt_header

from config import Settings
from utils.tls import trust


class KeycloakTokenVerifier(JWTVerifier):
    """JWTVerifier that reports signing key and algorithm failures, not just claim mismatches."""

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
                                "(STROOM_MCP_KEYCLOAK_TOKEN_ALGORITHM)", algorithm, self.algorithm)
        return await super().load_access_token(token)


def keycloak_http_client(ca_file: Path | None) -> httpx2.AsyncClient:
    """HTTPS client for Keycloak that trusts the system CAs plus `ca_file`, if given."""
    return httpx2.AsyncClient(verify=trust(ca_file), timeout=httpx2.Timeout(10.0))


def keycloak_auth(settings: Settings, http_client: httpx2.AsyncClient) -> KeycloakAuthProvider:
    realm_url = settings.keycloak_realm_url.rstrip('/')
    verifier: Any = KeycloakTokenVerifier(
        jwks_uri=f'{realm_url}/protocol/openid-connect/certs',
        issuer=realm_url,
        audience=settings.keycloak_audience,
        algorithm=settings.keycloak_token_algorithm,
        # 'openid' guarantees the sub claim Stroom matches users on.
        required_scopes=['openid'],
        http_client=http_client,
    )
    return KeycloakAuthProvider(realm_url=realm_url, base_url=settings.public_base_url, token_verifier=verifier)
