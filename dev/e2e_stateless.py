"""Stateless HTTP (FASTMCP_STATELESS_HTTP=true) against the local Stroom, spoken to as Ask Stroom AI's Java client
speaks (asked for by the user: forms were expected to break, stateless, with MCP's Java SDK).

    cd dev/stroom && docker compose up -d        # the local stack runs Stroom's internal provider
    uv run python dev/e2e_stateless.py

1. Signs in to the local Stroom as admin through its internal provider (as e2e_internal_idp), and starts the server
   stateless with that provider.
2. A client speaking protocol 2025-11-25, the newest MCP's Java SDK speaks as a client (2.0.1 to 2.2.0-M1; 2.2.0-M1's
   2026-07-28 support is for servers only), declaring form elicitation, as Stroom AI does: initialize gets no session
   id; tools list; a Stroom tool runs.
3. Forms: create_feed in a build asks its confirmation. Stateless, each POST has its own transport, so the server can
   write a form to the call's stream but the client's answer has nowhere to land (the MCP SDK refuses to send it: no
   back channel). The call answers at once with a confirmation id instead, and never sends an elicitation request;
   the id passed back in a new request creates the feed. That is the fallback seen in VS Code on prod: forms became
   ids, which a model can pass back without asking the user.
4. Progress: describe_document on an index doc with a progressToken gets progress notifications on the call's stream
   (a notification needs no back channel). It needs an index doc on the stack (e2e_lucene_indexing makes one).

Creates a build and a feed named with the run stamp: dev/e2e_cleanup.py STAMP --apply removes them.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dev'))

from e2e_internal_idp import STROOM, check, free_port, internal_client, sign_in  # noqa: E402

PROTOCOL = '2025-11-25'     # what MCP's Java SDK speaks as a client
STAMP = f"{time.strftime('%H%M%S')}{os.getpid() % 1000:03d}"


class JavaLikeClient:
    """JSON-RPC over streamable HTTP as MCP's Java SDK client sends it: protocol 2025-11-25, form elicitation
    declared, every message a POST, SSE or JSON answers read whole."""

    def __init__(self, http: httpx.AsyncClient, url: str, token: str):
        self.http, self.url, self.token, self.next_id = http, url, token, 0
        self.session: str | None = None

    async def send(self, method: str, params: dict | None = None,
                   notification: bool = False) -> tuple[list[dict], httpx.Response]:
        body: dict = {'jsonrpc': '2.0', 'method': method, **({'params': params} if params is not None else {})}
        if not notification:
            self.next_id += 1
            body['id'] = self.next_id
        headers = {'Accept': 'application/json, text/event-stream', 'Authorization': f'Bearer {self.token}',
                   'MCP-Protocol-Version': PROTOCOL, **({'Mcp-Session-Id': self.session} if self.session else {})}
        response = await self.http.post(self.url, json=body, headers=headers)
        return self.messages(response), response

    @staticmethod
    def messages(response: httpx.Response) -> list[dict]:
        kind = response.headers.get('content-type', '')
        if not response.content:        # a notification's 202
            return []
        if 'application/json' in kind:
            return [response.json()]
        out, data = [], []
        for line in response.text.splitlines() + ['']:
            if line.startswith('data:'):
                data.append(line[5:].strip())
            elif not line and data:
                out.append(json.loads('\n'.join(data)))
                data = []
        return out

    async def call(self, tool: str, arguments: dict, progress: str | None = None) -> tuple[dict, list[dict]]:
        """The tool's result, and every message the call's stream carried before it."""
        params = {'name': tool, 'arguments': arguments, **({'_meta': {'progressToken': progress}} if progress else {})}
        messages, response = await self.send('tools/call', params)
        check(response.status_code == 200, f'{tool}: HTTP {response.status_code}')
        final = next((m for m in messages if m.get('id') == self.next_id and ('result' in m or 'error' in m)), None)
        check(final is not None, f'{tool}: a result on the call\'s stream ({len(messages)} messages)')
        return final.get('result') or {'error': final.get('error')}, messages


def structured(result: dict) -> dict:
    data = result.get('structuredContent')
    if data is None:
        text = next((c.get('text') for c in result.get('content') or [] if c.get('type') == 'text'), '') or ''
        try:
            data = json.loads(text)
        except ValueError:
            data = {'text': text}
    return data.get('result', data) if isinstance(data, dict) and set(data) == {'result'} else data


async def main() -> None:
    print(f'RUN STAMP {STAMP} against the local stack')
    print('\n### a token from Stroom\'s internal provider, as Stroom AI is given one')
    client_id, secret = internal_client()
    tokens = sign_in(client_id, secret)
    check(bool(tokens.get('access_token')), 'signed in as admin')

    print('\n### the server, stateless (FASTMCP_STATELESS_HTTP=true), with STROOM_MCP_IDENTITY_PROVIDER=stroom_internal')
    port = free_port()
    env = {**os.environ, 'FASTMCP_STATELESS_HTTP': 'true', 'STROOM_MCP_IDENTITY_PROVIDER': 'stroom_internal',
           'STROOM_MCP_OIDC_AUDIENCE': client_id, 'STROOM_MCP_STROOM_URL': STROOM, 'STROOM_MCP_HOST': '127.0.0.1',
           'STROOM_MCP_PORT': str(port)}
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

        async with httpx.AsyncClient(timeout=180) as http:
            client = JavaLikeClient(http, f'{url}/mcp', tokens['access_token'])

            print(f'\n### a client speaking {PROTOCOL}, with form elicitation, as MCP\'s Java SDK')
            messages, response = await client.send('initialize', {
                'protocolVersion': PROTOCOL, 'capabilities': {'elicitation': {'form': {}}},
                'clientInfo': {'name': 'stroom-ai-e2e', 'version': STAMP}})
            init = next((m['result'] for m in messages if 'result' in m), {})
            check(init.get('protocolVersion') == PROTOCOL, f"initialized at {init.get('protocolVersion')}")
            check('mcp-session-id' not in response.headers, 'no session id given: stateless')
            _, done = await client.send('notifications/initialized', notification=True)
            check(done.status_code in (200, 202), f'initialized notification taken: {done.status_code}')
            listed = next((m['result'] for m in (await client.send('tools/list'))[0] if 'result' in m), {})
            check(len(listed.get('tools') or []) > 50, f"{len(listed.get('tools') or [])} tools listed")
            found, _ = await client.call('find_documents', {'name': 'Event Data*', 'types': ['Pipeline']})
            check(not found.get('isError') and 'error' not in found, 'find_documents ran against Stroom')

            print('\n### a form, stateless: a confirmation id at once, no elicitation request sent')
            build = f'stateless-{STAMP}'
            started, _ = await client.call('start_build', {'build': build})
            check(not started.get('isError'), f'build {build} started')
            feed = f'STATELESS-{STAMP}'      # this Stroom's feed names: ^[A-Z0-9_-]{3,}$
            asked_at = time.monotonic()
            asked, stream = await client.call('create_feed', {'build': build, 'name': feed,
                                                              'description': 'stateless e2e'})
            took = time.monotonic() - asked_at
            check(not any(m.get('method') == 'elicitation/create' for m in stream),
                  'no elicitation request on the call\'s stream (no back channel for its answer)')
            gate = structured(asked)
            check(gate.get('status') == 'needs_confirmation' and bool(gate.get('confirmation_id')),
                  f"needs_confirmation with an id instead of a form: {str(gate)[:200]}")
            check(took < 30, f'answered at once ({took:.1f} s), not left waiting on a form')
            made, _ = await client.call('create_feed', {'build': build, 'name': feed, 'description': 'stateless e2e',
                                                        'confirmation_id': gate['confirmation_id']})
            check(not made.get('isError') and structured(made).get('status') != 'needs_confirmation',
                  f'the id passed back in a new request (no session) created the feed: {str(structured(made))[:160]}')

            print('\n### progress, stateless: notifications on the call\'s stream')
            indexes, _ = await client.call('find_documents', {'name': '*', 'types': ['Index', 'ElasticIndex'],
                                                              'limit': 5})
            docs = structured(indexes).get('documents') or structured(indexes).get('values') or []
            ref = next(((d.get('docRef') or d) for d in docs if (d.get('docRef') or d).get('uuid')), None)
            check(ref is not None, 'an index doc on the stack to describe (e2e_lucene_indexing makes one)')
            _, stream = await client.call('describe_document', {'type': ref['type'], 'uuid': ref['uuid']},
                                          progress='stateless-progress')
            told = [m['params'] for m in stream if m.get('method') == 'notifications/progress']
            check(bool(told) and all(p.get('progressToken') == 'stateless-progress' for p in told),
                  f'{len(told)} progress notifications, for this call')
            check(any((p.get('message') or '').startswith('Reading the fields of') for p in told),
                  f"what the survey was doing: {[p.get('message') for p in told][:3]}")
        print(f'\nALL PASSED (remove what it made: uv run python dev/e2e_cleanup.py {STAMP} --apply)')
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == '__main__':
    asyncio.run(main())
