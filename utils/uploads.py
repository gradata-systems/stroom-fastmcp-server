"""Sample files sent to Stroom without passing through the model.

A file reaches the server's tools only as text in a tool call, through the model: VS Code's read_file cuts a line at
2,000 characters, and a 755 KB one-line JSON file arrived as three records, the last completed with values the agent
made up. Instead, upload_sample with files= gives the agent a short-lived ticket for one feed, and a curl command
it runs in the user's terminal (the user approves it): the file goes to this server's /upload, which sends it to
Stroom's datafeed as the user, exactly as read from disk.

The ticket is sealed (AES-GCM) with a key derived from the request-state keys every replica shares, so the upload can
land on any replica. It carries the feed, the user and their access token (Stroom receives data as them), and
expires within minutes, never after the token does: it is a bearer credential for uploads to that one feed until
then.
"""
import asyncio
import base64
import hashlib
import json
import os
import time
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = 'upl-'
HEADER = 'X-Stroom-MCP-Ticket'


def _key(secret: str) -> bytes:
    return hashlib.sha256(b'stroom-mcp upload ticket\0' + secret.encode()).digest()


class UploadTickets:
    def __init__(self, keys: list[str]):
        # Without shared keys (one replica), a per-process key: tickets don't outlive a restart.
        self._keys = [_key(k) for k in keys if k] or [AESGCM.generate_key(bit_length=256)]

    def seal(self, payload: dict[str, Any]) -> str:
        nonce = os.urandom(12)
        sealed = AESGCM(self._keys[0]).encrypt(nonce, json.dumps(payload).encode(), PREFIX.encode())
        return PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode().rstrip('=')

    def open(self, ticket: str) -> dict[str, Any] | None:
        """The ticket's payload, or None when it isn't one of ours (any replica's) or has expired."""
        if not ticket or not ticket.startswith(PREFIX):
            return None
        try:
            raw = base64.urlsafe_b64decode(ticket[len(PREFIX):] + '=' * (-len(ticket[len(PREFIX):]) % 4))
        except (ValueError, TypeError):
            return None
        for key in self._keys:
            try:
                payload = json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], PREFIX.encode()))
            except (InvalidTag, ValueError):
                continue
            return payload if payload.get('exp', 0) > time.time() else None
        return None


async def send_to_feed(stroom, feed: str, data: bytes, receipt: dict[str, str], stream_type: str) -> dict[str, Any]:
    """Send data to the feed's datafeed receiver and find the stream it made: {'receipt_id', 'stream_id', 'bytes'}."""
    started = int(time.time() * 1000) - 1000
    response = await stroom.datafeed(feed, data, receipt)
    terms = [{'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': feed},
             {'type': 'term', 'field': 'Type', 'condition': 'EQUALS', 'value': stream_type}]
    for _ in range(20):
        rows = (await stroom.find_meta(terms, 5)).get('values') or []
        fresh = [r['meta'] for r in rows if (r['meta'].get('createMs') or 0) >= started]
        if fresh:
            return {'receipt_id': response.text.strip(), 'stream_id': fresh[0]['id'], 'bytes': len(data)}
        await asyncio.sleep(0.5)
    return {'receipt_id': response.text.strip(), 'stream_id': None, 'bytes': len(data)}


def commands(url: str, ticket: str, files: list[str]) -> list[dict[str, str]]:
    """The curl command for each file, for a POSIX shell and for PowerShell (where curl alone is Invoke-WebRequest)."""
    out = []
    for name in files:
        quoted = name.replace('"', '\\"')
        args = (f'-sS --fail-with-body -X POST "{url}" -H "{HEADER}: {ticket}" -H "X-File-Name: {quoted}" '
                f'--data-binary "@{quoted}"')
        out.append({'file': name, 'bash': f'curl {args}', 'powershell': f'curl.exe {args}'})
    return out


async def handle_upload(request, tickets: UploadTickets, settings, gateway: Any = None):
    """POST /upload: the file in the body, sent to the ticket's feed as the ticket's user. The ticket is the
    credential, so the route takes no other; it opens on any replica sharing the request-state keys."""
    from fastmcp.exceptions import ToolError
    from starlette.responses import JSONResponse
    from security.audit import audit
    from utils.stroom import StroomGateway
    name = request.headers.get('X-File-Name', '')[:300]
    payload = tickets.open(request.headers.get(HEADER, ''))
    if payload is None:
        audit('upload', outcome='refused', reason='no valid ticket', file=name)
        return JSONResponse({'error': "No valid upload ticket: it has run out (a ticket lasts minutes, never past the "
                                      "sign-in) or isn't this server's. Ask the agent for a new one (upload_sample "
                                      "with files=)."},
                            status_code=401)
    limit = settings.max_upload_mb * 1024 * 1024
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            audit('upload', outcome='refused', reason='too large', sub=payload.get('sub'), feed=payload['feed'],
                  file=name)
            return JSONResponse({'error': f"Larger than {settings.max_upload_mb} MB: send a part of the file, or "
                                          f"send it to Stroom directly"}, status_code=413)
    if not data:
        return JSONResponse({'error': "No data: give the file with --data-binary @<file>"}, status_code=400)
    stroom = (gateway or StroomGateway)(settings, authorization={'Authorization': payload['auth']}
                                        if payload.get('auth') else None)
    try:
        sent = await send_to_feed(stroom, payload['feed'], bytes(data),
                                  {'Type': payload['type'], **(payload.get('headers') or {})}, payload['type'])
    except ToolError as e:
        audit('upload', outcome='error', error=str(e), sub=payload.get('sub'), feed=payload['feed'], file=name,
              bytes=len(data))
        return JSONResponse({'error': str(e)}, status_code=502)
    finally:
        await stroom.close()
    audit('upload', outcome='success', sub=payload.get('sub'), feed=payload['feed'], file=name, **sent)
    return JSONResponse({'feed': payload['feed'], 'file': name, **sent})


