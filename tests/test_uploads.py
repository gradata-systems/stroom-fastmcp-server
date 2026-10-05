"""Sample files sent from the user's machine with a ticket, not through the model: VS Code's read_file cut 755 KB
one-line JSON files at 2,000 characters, and the agent uploaded three records, the last completed with made-up values."""
import time
from types import SimpleNamespace

import httpx
from starlette.applications import Starlette
from starlette.routing import Route

from utils.uploads import HEADER, UploadTickets, commands, handle_upload

KEYS = ['k' * 40]


def payload(**over):
    return {'feed': 'FORTIOS-EVENTS-V1.0', 'type': 'Raw Events', 'headers': {}, 'auth': 'Bearer user-token',
            'exp': int(time.time()) + 600, 'sub': 'u1', **over}


def test_a_ticket_opens_on_any_replica_sharing_the_keys_and_not_after_it_runs_out():
    ticket = UploadTickets(KEYS).seal(payload())
    assert ticket.startswith('upl-') and 'user-token' not in ticket          # sealed, not readable
    assert UploadTickets(['x' * 40, *KEYS]).open(ticket)['feed'] == 'FORTIOS-EVENTS-V1.0'   # rotation: any key
    assert UploadTickets(['x' * 40]).open(ticket) is None                    # another server's keys
    assert UploadTickets(KEYS).open(UploadTickets(KEYS).seal(payload(exp=int(time.time()) - 1))) is None
    assert UploadTickets(KEYS).open(ticket[:-4] + 'AAAA') is None             # tampered
    assert UploadTickets(KEYS).open('not a ticket') is None


def test_each_file_gets_a_command_for_bash_and_powershell():
    [one] = commands('https://mcp.example/upload', 'upl-T', ['sample-data/fortios/001_1.json'])
    assert one['bash'] == ('curl -sS --fail-with-body -X POST "https://mcp.example/upload" -H "X-Stroom-MCP-Ticket: '
                           'upl-T" -H "X-File-Name: sample-data/fortios/001_1.json" --data-binary '
                           '"@sample-data/fortios/001_1.json"')
    assert one['powershell'].startswith('curl.exe -sS')       # curl alone is Invoke-WebRequest in Windows PowerShell


class Gateway:
    """Stroom as the upload route sees it: what it was sent, and as whom."""
    sent: list = []

    def __init__(self, settings, authorization=None):
        self.authorization = authorization

    async def datafeed(self, feed, data, headers):
        Gateway.sent.append((feed, data, headers, self.authorization))
        return SimpleNamespace(text='receipt-1\n')

    async def find_meta(self, terms, limit):
        return {'values': [{'meta': {'id': 77, 'createMs': int(time.time() * 1000) + 5000}}]}

    async def close(self):
        pass


async def post(tickets, headers, body, max_mb=1):
    settings = SimpleNamespace(max_upload_mb=max_mb)

    async def endpoint(request):
        return await handle_upload(request, tickets, settings, Gateway)
    app = Starlette(routes=[Route('/upload', endpoint, methods=['POST'])])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as client:
        return await client.post('/upload', headers=headers, content=body)


async def test_the_whole_file_goes_to_the_tickets_feed_as_the_user():
    tickets = UploadTickets(KEYS)
    body = b'[' + b',' * 800_000 + b']'                  # far past the 2,000 characters a reader gave the agent
    Gateway.sent = []
    response = await post(tickets, {HEADER: tickets.seal(payload()), 'X-File-Name': '001_1.json'}, body)
    assert response.status_code == 200 and response.json()['stream_id'] == 77 and response.json()['bytes'] == len(body)
    feed, data, headers, authorization = Gateway.sent[0]
    assert (feed, data, headers['Type']) == ('FORTIOS-EVENTS-V1.0', body, 'Raw Events')
    assert authorization == {'Authorization': 'Bearer user-token'}       # Stroom receives it as the user


async def test_no_ticket_too_large_or_empty_is_refused():
    tickets = UploadTickets(KEYS)
    Gateway.sent = []
    assert (await post(tickets, {}, b'x')).status_code == 401
    assert (await post(tickets, {HEADER: tickets.seal(payload())}, b'x' * (1024 * 1024 + 1))).status_code == 413
    assert (await post(tickets, {HEADER: tickets.seal(payload())}, b'')).status_code == 400
    assert Gateway.sent == []
