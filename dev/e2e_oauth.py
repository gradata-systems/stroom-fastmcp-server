"""Sign-in the way VS Code does it, against the MCP server running with Keycloak (dev/keycloak).

Start Keycloak (dev/keycloak) and the server with Keycloak auth, e.g.
    STROOM_MCP_STROOM_URL=http://127.0.0.1:18080 STROOM_MCP_OIDC_ISSUER_URL=http://127.0.0.1:18180/realms/stroom \\
    STROOM_MCP_OIDC_AUDIENCE=stroom-mcp STROOM_MCP_PUBLIC_BASE_URL=http://127.0.0.1:8765 \\
    STROOM_MCP_HOST=127.0.0.1 STROOM_MCP_PORT=8765 uv run python main.py
then
    uv run python dev/e2e_oauth.py [--stroom]

1. Discovery: an unauthenticated request gets 401 with the protected resource metadata URL; that names the
   Keycloak realm, whose metadata offers the authorization code flow with PKCE (S256), as VS Code needs.
2. A token for the test user through the VS Code client (a password grant stands in for the browser step)
   carries both audiences.
3. Over MCP with that token: tools, prompts and resources list, and a local tool runs.
4. --stroom: a Stroom tool runs as the user (needs Stroom to trust the same Keycloak: dev/stroom oidc override).
"""
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

SERVER = 'http://127.0.0.1:8765'
REALM = 'http://127.0.0.1:18180/realms/stroom'


def check(ok: bool, message: str) -> None:
    print(('  PASS ' if ok else '  FAIL ') + message)
    if not ok:
        raise SystemExit(1)


def admin_key() -> str:
    env = dict(line.split('=', 1) for line in (Path(__file__).parent / 'stroom' / '.env').read_text().splitlines() if '=' in line)
    return env['STROOM_ADMIN_API_KEY'].strip()


def claims(token: str) -> dict:
    part = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))


async def main(with_stroom: bool) -> None:
    async with httpx.AsyncClient() as http:
        print('### 1. discovery')
        bare = await http.post(f'{SERVER}/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                               headers={'Accept': 'application/json, text/event-stream'})
        challenge = bare.headers.get('www-authenticate', '')
        check(bare.status_code == 401 and 'resource_metadata=' in challenge, f"401 without a token: {challenge[:120]}")
        metadata_url = challenge.split('resource_metadata="')[1].split('"')[0]
        resource = (await http.get(metadata_url)).json()
        check(REALM in [s.rstrip('/') for s in resource.get('authorization_servers', [])],
              f"protected resource metadata names the realm: {resource.get('authorization_servers')}")
        realm = (await http.get(f'{REALM}/.well-known/openid-configuration')).json()
        check('S256' in realm.get('code_challenge_methods_supported', []) and 'authorization_code' in realm.get('grant_types_supported', []),
              'Keycloak offers the authorization code flow with PKCE (S256)')

        print('\n### 2. a token through the VS Code client')
        token = (await http.post(realm['token_endpoint'], data={
            'grant_type': 'password', 'client_id': 'stroom-mcp-vscode', 'username': 'analyst',
            'password': 'analyst-dev', 'scope': 'openid'})).json()['access_token']
        c = claims(token)
        check({'stroom-mcp', 'stroom'} <= set(c['aud'] if isinstance(c['aud'], list) else [c['aud']]),
              f"aud {c['aud']}, user {c.get('preferred_username')}")

    print('\n### 3. MCP with the token')
    async with Client(f'{SERVER}/mcp', auth=BearerAuth(token)) as client:
        tools = await client.list_tools()
        prompts = await client.list_prompts()
        resources = await client.list_resource_templates()
        # The surface was cut to the cap tests/test_surface.py holds it to (54 with CEF and coverage).
        check(len(tools) >= 50 and len(prompts) >= 7 and resources,
              f"{len(tools)} tools, {len(prompts)} prompts, {len(resources)} resource templates")
        profile = await client.call_tool('profile_sample', {'sample': 'a,b\n1,2\n3,4\n'})
        check(profile.structured_content.get('format') == 'delimited', 'a local tool runs as the signed-in user')
        stroom_call = await client.call_tool('get_instructions', {}, raise_on_error=False)
        text = ' '.join(getattr(b, 'text', '') for b in stroom_call.content or [])
        if with_stroom:
            check(not stroom_call.is_error, f"a Stroom tool runs as the user: {text[:160]}")
            stamp = f"{time.strftime('%H%M%S')}{os.getpid() % 1000:03d}"
            args = {'build': f'oauth-{stamp}', 'name': f'OAUTH-{stamp}'}
            first = (await client.call_tool('create_feed', args)).structured_content
            feed = (await client.call_tool('create_feed', {**args, 'confirmation_id': first['confirmation_id']})).structured_content
            async with httpx.AsyncClient() as http:
                doc = (await http.get(f"http://127.0.0.1:18080/api/feed/v1/{feed['uuid']}",
                                      headers={'Authorization': f"Bearer {admin_key()}"})).json()
            check(doc.get('createUser') == 'analyst',
                  f"Stroom records the feed as created by the signed-in user: createUser={doc.get('createUser')}")
            upload = await client.call_tool('upload_sample', {'feed': args['name'], 'sample': 'a,b\n1,2\n'},
                                            raise_on_error=False)
            detail = upload.structured_content or ' '.join(getattr(b, 'text', '') for b in upload.content or [])
            check(not upload.is_error and (upload.structured_content or {}).get('stream_id'),
                  f"the sample is uploaded to /stroom/datafeed with the user's token: {str(detail)[:160]}")
        else:
            print(f"    Stroom call without Stroom trusting Keycloak: error={stroom_call.is_error} {text[:160]}")
    if with_stroom:
        print('\n### 4. approvals as forms (elicitation), as VS Code shows them')
        asked = []

        async def agree(message, response_type, params, context):
            asked.append(message)
            return {'value': True}

        async def refuse(message, response_type, params, context):
            # Declined, as VS Code's Decline button does. Not accepted with value false: a form with a value to edit
            # (the feed name) is confirmed by accepting it, whatever else comes back.
            from mcp.types import ElicitResult
            asked.append(message)
            return ElicitResult(action='decline')
        stamp = f"{time.strftime('%H%M%S')}{os.getpid() % 1000:03d}"
        async with Client(f'{SERVER}/mcp', auth=BearerAuth(token), elicitation_handler=agree) as client:
            made = await client.call_tool('create_feed', {'build': f'oauth-{stamp}', 'name': f'OAUTH-E-{stamp}'})
            check(made.structured_content.get('uuid') and 'OAUTH-E-' in asked[-1],
                  f"the confirmation is asked as a form and the feed is created in one call: {asked[-1][:80]!r}")
        async with Client(f'{SERVER}/mcp', auth=BearerAuth(token), elicitation_handler=refuse) as client:
            declined = await client.call_tool('create_feed', {'build': f'oauth-{stamp}', 'name': f'OAUTH-N-{stamp}'},
                                              raise_on_error=False)
            text = ' '.join(getattr(b, 'text', '') for b in declined.content or [])
            check(declined.is_error and 'did not agree' in text, f"declining the form stops the call: {text[:100]}")
    async with httpx.AsyncClient() as http:
        tampered = await http.post(f'{SERVER}/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                                   headers={'Accept': 'application/json, text/event-stream',
                                            'Authorization': f'Bearer {token[:-6]}AAAAAA'})
        check(tampered.status_code == 401, 'a tampered token is refused')
    print(chr(10) + 'ALL PASSED')


if __name__ == '__main__':
    asyncio.run(main('--stroom' in sys.argv))
