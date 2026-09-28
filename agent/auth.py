"""Sign the agent in as the person using it, so every Stroom call is made as them.

The Stroom MCP server forwards the caller's token to Stroom, so the agent must not use a service account.
A terminal agent signs in with Keycloak's device authorization grant (the person opens a link and logs in),
then refreshes the token for as long as the run lasts. The Keycloak client must be public, allow the device
grant, and map both the MCP server's audience and `stroom` into the access token's aud claim.
"""
import asyncio
import time
from typing import Any, Callable

import httpx
import httpx2


class UserSession(httpx2.Auth):
    """Bearer auth for the MCP client that refreshes the person's token before it expires."""

    def __init__(self, realm_url: str, client_id: str, tokens: dict[str, Any]):
        self.token_url = realm_url.rstrip('/') + '/protocol/openid-connect/token'
        self.client_id = client_id
        self._set(tokens)
        self._lock = asyncio.Lock()

    def _set(self, tokens: dict[str, Any]) -> None:
        self.access_token = tokens['access_token']
        self.refresh_token = tokens.get('refresh_token')
        self.expires = time.time() + tokens.get('expires_in', 300)

    async def _fresh(self) -> str:
        async with self._lock:
            if self.refresh_token and time.time() > self.expires - 30:
                async with httpx.AsyncClient() as client:
                    response = await client.post(self.token_url, data={
                        'grant_type': 'refresh_token', 'refresh_token': self.refresh_token, 'client_id': self.client_id})
                    response.raise_for_status()
                    self._set(response.json())
            return self.access_token

    async def async_auth_flow(self, request):
        request.headers['Authorization'] = f'Bearer {await self._fresh()}'
        yield request

    def sync_auth_flow(self, request):
        raise RuntimeError("UserSession is for async clients")


async def device_login(realm_url: str, client_id: str, show: Callable[[str], None] = print) -> UserSession:
    """Keycloak device authorization grant: show a link, wait for the person to sign in."""
    base = realm_url.rstrip('/') + '/protocol/openid-connect'
    async with httpx.AsyncClient() as client:
        response = await client.post(f'{base}/auth/device', data={'client_id': client_id, 'scope': 'openid'})
        response.raise_for_status()
        device = response.json()
        show(f"Sign in to continue: {device.get('verification_uri_complete') or device['verification_uri']}"
             f" (code {device['user_code']})")
        interval = device.get('interval', 5)
        deadline = time.time() + device.get('expires_in', 600)
        while time.time() < deadline:
            await asyncio.sleep(interval)
            response = await client.post(f'{base}/token', data={
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                'device_code': device['device_code'], 'client_id': client_id})
            body = response.json()
            if response.is_success:
                return UserSession(realm_url, client_id, body)
            if body.get('error') == 'slow_down':
                interval += 5
            elif body.get('error') != 'authorization_pending':
                raise RuntimeError(f"Sign-in failed: {body.get('error_description') or body.get('error')}")
    raise RuntimeError("Sign-in timed out")
