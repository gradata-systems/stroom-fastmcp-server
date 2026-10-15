"""Stroom's own identity provider (INTERNAL_IDP): the server takes the access token Stroom's Ask Stroom AI forwards for
its signed-in user, and calls Stroom with it (asked for by the user).

    cd dev/stroom && docker compose up -d        # the local stack runs Stroom's internal provider
    uv run python dev/e2e_internal_idp.py

1. Signs in to the local Stroom as admin and gets an access token from its internal provider, through Stroom's own
   OAuth client, as Stroom's UI does (the client's id and secret read from the local database).
2. Starts the server with STROOM_MCP_IDENTITY_PROVIDER=stroom_internal and that client's id as the audience.
3. With the access token: tools list, and a Stroom tool runs as admin.
4. Refused: no token, and the ID token; no OAuth sign-in is offered to clients. (Another audience, issuer or key:
   tests/test_auth.py.)
"""
import asyncio
import base64
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

ROOT = Path(__file__).resolve().parents[1]
STROOM = 'http://127.0.0.1:18080'
REDIRECT = 'https://localhost:18080/api/auth/flow/v1/signin-oidc'    # the only redirect Stroom's provider accepts


def check(condition: bool, message: str) -> None:
    print(('  PASS ' if condition else '  FAIL ') + message)
    if not condition:
        raise SystemExit(1)


def internal_client() -> tuple[str, str]:
    """Stroom's own OAuth client (local stack only): its id is what an operator configures as the audience."""
    out = subprocess.run(['docker', 'exec', 'stroom-mcp-dev-mysql-1', 'mysql', '-uroot', '-pdev-root-password', '-N',
                          '-e', 'select client_id, client_secret from oauth_client limit 1', 'stroom'],
                         capture_output=True, text=True, check=True).stdout.split()
    return out[0], out[1]


def sign_in(client_id: str, secret: str) -> dict:
    """admin's tokens from Stroom's internal provider, by the code flow Stroom's UI uses."""
    with httpx.Client(base_url=STROOM, follow_redirects=False, timeout=30) as http:
        login = http.post('/api/authentication/v1/login', json={'userId': 'admin', 'password': 'admin'})
        login.raise_for_status()
        # The session cookie is Secure; the local stack is plain HTTP, so it is sent back by hand.
        cookie = '; '.join(h.split(';')[0] for h in login.headers.get_list('set-cookie'))
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        auth = http.get('/api/oauth2/v1/auth', headers={'Cookie': cookie}, params={
            'response_type': 'code', 'client_id': client_id, 'scope': 'openid email', 'nonce': secrets.token_hex(8),
            'state': secrets.token_hex(8), 'code_challenge': challenge, 'code_challenge_method': 'S256',
            'redirect_uri': REDIRECT})
        code = parse_qs(urlparse(auth.headers.get('location', '')).query)['code'][0]
        tokens = http.post('/api/oauth2/v1/token', data={
            'grant_type': 'authorization_code', 'code': code, 'client_id': client_id, 'client_secret': secret,
            'redirect_uri': REDIRECT, 'code_verifier': verifier})
        tokens.raise_for_status()
        return tokens.json()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def main() -> None:
    print('\n### a token from Stroom\'s internal provider, as Stroom\'s UI gets one')
    client_id, secret = internal_client()
    tokens = sign_in(client_id, secret)
    check(bool(tokens.get('access_token')) and bool(tokens.get('id_token')), 'signed in as admin: access and ID tokens')

    print('\n### the server, with STROOM_MCP_IDENTITY_PROVIDER=stroom_internal')
    port = free_port()
    env = {**os.environ, 'STROOM_MCP_IDENTITY_PROVIDER': 'stroom_internal', 'STROOM_MCP_OIDC_AUDIENCE': client_id,
           'STROOM_MCP_STROOM_URL': STROOM, 'STROOM_MCP_HOST': '127.0.0.1', 'STROOM_MCP_PORT': str(port)}
    for key in ('STROOM_MCP_STROOM_API_KEY', 'STROOM_MCP_DEV_NO_AUTH', 'STROOM_MCP_OIDC_ISSUER_URL'):
        env.pop(key, None)
    # Its output to a file: a pipe nobody reads fills up, and the server then blocks writing its audit lines.
    log = tempfile.NamedTemporaryFile('w+b', suffix='.log', delete=False)
    server = subprocess.Popen([sys.executable, str(ROOT / 'main.py')], cwd=ROOT, env=env, stdout=log,
                              stderr=subprocess.STDOUT)
    url = f'http://127.0.0.1:{port}'
    try:
        for _ in range(60):
            try:
                if httpx.get(f'{url}/healthz', timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if server.poll() is not None:
                raise SystemExit(f'the server stopped: {Path(log.name).read_text(errors="replace")[-2000:]}')
            time.sleep(1)
        print(f'    up on {url}')

        print('\n### with the access token: tools list, and a Stroom tool runs as admin')
        async with Client(f'{url}/mcp', auth=BearerAuth(tokens['access_token'])) as client:
            tools = await client.list_tools()
            check(len(tools) > 50, f'{len(tools)} tools listed')
            found = await client.call_tool('find_documents', {'name': 'Event Data*', 'types': ['Pipeline']})
            data = found.structured_content or {}
            check(not found.is_error and bool(data.get('documents') or data.get('values')),
                  'find_documents ran, calling Stroom with the forwarded token')

        print('\n### refused')
        async with httpx.AsyncClient(timeout=10) as http:
            body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
            headers = {'Accept': 'application/json, text/event-stream'}
            none = await http.post(f'{url}/mcp', json=body, headers=headers)
            check(none.status_code == 401, f'no token: {none.status_code}')
            id_token = await http.post(f'{url}/mcp', json=body, headers={**headers,
                                       'Authorization': f"Bearer {tokens['id_token']}"})
            # Stroom refuses its ID token as a bearer; the server takes only what Stroom would (scopes: openid).
            check(id_token.status_code == 401, f'the ID token: {id_token.status_code}')
            metadata = await http.get(f'{url}/.well-known/oauth-protected-resource/mcp')
            check(metadata.status_code == 404, 'no OAuth sign-in offered to other clients (Stroom\'s provider signs in '
                                               'only Stroom)')
        print('\nALL PASSED')
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == '__main__':
    asyncio.run(main())
