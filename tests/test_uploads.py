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
    # The ticket in the URL: one quoted argument besides it, nothing to escape. An agent retyping a header form
    # dropped a quote, and PowerShell waited on the command for minutes.
    [one] = commands('https://mcp.example/upload', 'Xk3f9QpL2mWa', ['sample-data/fortios/001_1.json'])
    assert one['bash'] == ('curl -sS --fail-with-body --data-binary "@sample-data/fortios/001_1.json" '
                           '"https://mcp.example/upload/Xk3f9QpL2mWa"')
    assert one['powershell'].startswith('curl.exe -sS')       # curl alone is Invoke-WebRequest in Windows PowerShell


def test_a_short_code_is_kept_until_it_runs_out_and_a_sealed_ticket_still_opens():
    tickets = UploadTickets(KEYS)
    code = tickets.issue(payload())
    assert len(code) == 12 and tickets.open(code)['feed'] == 'FORTIOS-EVENTS-V1.0'
    assert UploadTickets(KEYS).open(code) is None                      # another replica never heard of it
    stale = tickets.issue(payload(exp=int(time.time()) - 1))
    assert tickets.open(stale) is None
    tickets.issue(payload())                                           # issuing clears what has run out
    assert stale not in tickets._codes
    sealed = tickets.issue(payload(), 'sealed')
    assert sealed.startswith('upl-') and UploadTickets(KEYS).open(sealed)['feed'] == 'FORTIOS-EVENTS-V1.0'


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
    app = Starlette(routes=[Route('/upload/{ticket}', endpoint, methods=['POST']),
                            Route('/upload', endpoint, methods=['POST'])])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as client:
        if 'code' in headers:
            return await client.post(f"/upload/{headers.pop('code')}", headers=headers, content=body)
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


async def test_the_code_in_the_url_is_the_credential():
    tickets = UploadTickets(KEYS)
    Gateway.sent = []
    response = await post(tickets, {'code': tickets.issue(payload())}, b'[{"a": 1}]')
    assert response.status_code == 200 and Gateway.sent[0][0] == 'FORTIOS-EVENTS-V1.0'
    refused = await post(tickets, {'code': 'not-a-code12'}, b'x')
    assert refused.status_code == 401 and 'not copied exactly' in refused.json()['error']


async def test_once_a_feed_has_had_commands_text_samples_are_refused():
    # Seen: the terminal command failed, and the agent uploaded two records of each file as text instead.
    from unittest.mock import AsyncMock, patch
    import pytest
    from fastmcp.exceptions import ToolError
    from tools import feeds
    feed = {'type': 'Feed', 'uuid': 'f1', 'name': 'FORTIOS-FIREWALL-V1.0'}
    guard = SimpleNamespace(tags=AsyncMock(return_value=['mcp-managed']))
    with patch.object(feeds, '_build_feed', AsyncMock(return_value=feed)),             patch.object(feeds, '_sample_files', AsyncMock(return_value=True)), \
            patch.object(feeds, 'guard_from', lambda ctx: guard), patch.object(feeds, 'gateway_from', lambda ctx: None):
        with pytest.raises(ToolError, match="samples are files on the user's disk .* exactly as given"):
            await feeds.upload_sample(SimpleNamespace(lifespan_context={}), 'FORTIOS-FIREWALL-V1.0',
                                      sample='[{"a": 1}]')


async def test_a_reference_sample_applies_from_long_ago_unless_told_otherwise():
    # Eval case 15 on Haiku: a directory uploaded without an effective time took effect when it arrived, after the
    # event samples, whose lookups then found nothing.
    from unittest.mock import AsyncMock, patch
    from tools import feeds
    feed = {'type': 'Feed', 'uuid': 'f1', 'name': 'ACME-USERS'}
    guard = SimpleNamespace(tags=AsyncMock(return_value=['mcp-managed']))
    send = AsyncMock(return_value={'receipt_id': 'r', 'stream_id': None})
    ticket = AsyncMock(return_value={})
    with patch.object(feeds, '_build_feed', AsyncMock(return_value=feed)), patch.object(feeds, 'send_to_feed', send),             patch.object(feeds, '_sample_files', AsyncMock(return_value=False)), \
            patch.object(feeds, 'guard_from', lambda ctx: guard), patch.object(feeds, 'gateway_from', lambda ctx: None), \
            patch.object(feeds, 'upload_ticket', ticket):
        ctx = SimpleNamespace(lifespan_context={})
        await feeds.upload_sample(ctx, 'ACME-USERS', sample='user,name\nalice,Alice\n', stream_type='Raw Reference')
        assert send.await_args.args[3]['EffectiveTime'] == '2000-01-01T00:00:00.000Z'
        await feeds.upload_sample(ctx, 'ACME-USERS', sample='user,name\nalice,Alice\n', stream_type='Raw Reference',
                                  effective_time='2026-01-01T00:00:00.000Z')
        assert send.await_args.args[3]['EffectiveTime'] == '2026-01-01T00:00:00.000Z'
        await feeds.upload_sample(ctx, 'ACME-USERS', files=['users.csv'], stream_type='Raw Reference')
        assert ticket.await_args.args[4] == {'EffectiveTime': '2000-01-01T00:00:00.000Z'}
        await feeds.upload_sample(ctx, 'ACME', sample='a,b\n1,2\n')
        assert 'EffectiveTime' not in send.await_args.args[3]


async def test_a_feed_differing_from_another_only_in_case_is_refused():
    # Qwen in VS Code made DELINEA-SECRETSERVER-V1.0 beside an earlier Delinea-SecretServer-V1.0: Stroom filed the new
    # feed's streams under the earlier spelling, and processing refused them as another build's feed.
    from unittest.mock import AsyncMock, patch
    import pytest
    from fastmcp.exceptions import ToolError
    from tools import feeds
    found = {'values': [{'docRef': {'type': 'Feed', 'uuid': 'old', 'name': 'Delinea-SecretServer-V1.0'},
                         'path': 'System / MCP Workspace / onboard-delinea'}]}
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value=found), get=AsyncMock(return_value=None))
    asked = AsyncMock(return_value={'status': 'needs_confirmation'})
    with patch.object(feeds, 'gateway_from', lambda ctx: stroom), \
            patch.object(feeds, 'consent_from', lambda ctx: SimpleNamespace(require=asked)), \
            patch('tools.plan.works_from', AsyncMock(return_value=[])):
        with pytest.raises(ToolError, match=r"'Delinea-SecretServer-V1.0' \(System / MCP Workspace / onboard-delinea\) "
                                            r"already exists, differing from 'DELINEA-SECRETSERVER-V1.0' only in case"):
            await feeds.create_feed(None, 'b', 'DELINEA-SECRETSERVER-V1.0')
        assert not asked.await_count                     # refused before the user is asked
        assert (await feeds.create_feed(None, 'b', 'DELINEA-SECRETSERVER-V2.0'))['status'] == 'needs_confirmation'



async def test_a_feed_of_the_same_name_is_refused_before_anything_or_carried_on_with_in_its_own_build():
    # Seen on the local stack: Stroom refused the duplicate only after the user confirmed it, as a bare 500, and the
    # new build's folder was left empty. Now refused first, saying where the feed is; in its own build, carried on.
    from unittest.mock import AsyncMock, patch
    import pytest
    from fastmcp.exceptions import ToolError
    from tools import feeds
    ref = {'type': 'Feed', 'uuid': 'f1', 'name': 'ACME-VPN-V1.0'}
    found = {'values': [{'docRef': ref, 'path': 'System / Feeds / ACME'}]}
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value=found), get=AsyncMock(return_value=ref),
                             get_doc=AsyncMock(return_value={**ref, 'streamType': 'Raw Events', 'encoding': 'UTF-8'}))
    asked = AsyncMock(return_value={'status': 'needs_confirmation'})
    guard = SimpleNamespace(build_of=AsyncMock(return_value=None), create=AsyncMock())
    with patch.object(feeds, 'gateway_from', lambda ctx: stroom),             patch.object(feeds, 'guard_from', lambda ctx: guard),             patch.object(feeds, 'consent_from', lambda ctx: SimpleNamespace(require=asked)),             patch('tools.plan.with_next', AsyncMock(side_effect=lambda ctx, build, result: result)):
        with pytest.raises(ToolError, match=r"A feed named 'ACME-VPN-V1.0' already exists \(System / Feeds / ACME\)"):
            await feeds.create_feed(None, 'onboard-acme', 'ACME-VPN-V1.0')
        assert not asked.await_count and not guard.create.await_count     # nothing asked, nothing made
        guard.build_of.return_value = 'onboard-acme'                     # an agent's retry in the same build
        again = await feeds.create_feed(None, 'onboard-acme', 'ACME-VPN-V1.0')
        assert again['existing'] and again['uuid'] == 'f1' and 'nothing new was made' in again['note']
        assert not asked.await_count and not guard.create.await_count


async def test_a_build_on_an_existing_feed_makes_a_feed_only_for_samples_the_user_chose():
    # Asked for by the user: asked to onboard an existing feed, the agent used the sample files in the VS Code
    # workspace. The build works from the feed (its Events go to a test feed the server makes): no feed is made for raw
    # samples, unless the user names files of their own.
    from unittest.mock import AsyncMock, patch
    import pytest
    from fastmcp.exceptions import ToolError
    from tools import feeds
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value={'values': []}), get=AsyncMock(return_value=None))
    asked = AsyncMock(return_value={'status': 'needs_confirmation'})
    with patch.object(feeds, 'gateway_from', lambda ctx: stroom), \
            patch.object(feeds, 'consent_from', lambda ctx: SimpleNamespace(require=asked)), \
            patch('tools.plan.works_from', AsyncMock(return_value=['VPN-GATEWAY'])):
        with pytest.raises(ToolError, match=r"works from the existing feed\(s\) VPN-GATEWAY.*user_samples="):
            await feeds.create_feed(None, 'b', 'VPN-GATEWAY-SAMPLES')
        assert not asked.await_count
        await feeds.create_feed(None, 'b', 'VPN-GATEWAY-SAMPLES', user_samples=['samples/vpn_1.csv'])
    summary, details = asked.await_args.args[3], asked.await_args.args[4]
    # The confirmation shows the user the choice: their files instead of the feed's data.
    assert summary == ("Create test feed 'VPN-GATEWAY-SAMPLES' for your sample files (samples/vpn_1.csv) instead of "
                       "the data in VPN-GATEWAY")
    assert details['works from'] == ['VPN-GATEWAY']


async def test_a_build_on_an_existing_feed_makes_its_test_feed_under_the_users_name():
    # Seen in Ask Stroom AI: the server's VPN-GATEWAY-MCP-TEST name was an earlier build's, the user chose another, and
    # the agent's create_feed (stream_type Events) was refused as a feed the build doesn't make.
    from unittest.mock import AsyncMock, patch
    import pytest
    from fastmcp.exceptions import ToolError
    from tools import feeds, processing_writes
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value={'values': []}), get=AsyncMock(return_value=None),
                             settings=SimpleNamespace(stroom_url='http://stroom'))
    asked = AsyncMock(return_value=None)
    made = AsyncMock(return_value={'uuid': 'u', 'name': 'VPN-GATEWAY-TEST-V2'})
    with patch.object(feeds, 'gateway_from', lambda ctx: stroom), \
            patch.object(feeds, 'consent_from', lambda ctx: SimpleNamespace(require=asked)), \
            patch.object(feeds, 'edited', lambda ctx, key, value: value), \
            patch.object(feeds, 'doc_link', lambda *a: 'link'), \
            patch.object(processing_writes, 'make_test_feed', made), \
            patch('tools.plan.with_next', AsyncMock(side_effect=lambda ctx, build, result: result)), \
            patch('tools.plan.works_from', AsyncMock(return_value=['VPN-GATEWAY'])):
        for asked_as in ({'test_feed': True}, {'stream_type': 'Events'}):
            result = await feeds.create_feed(None, 'b', 'VPN-GATEWAY-TEST-V2', **asked_as)
            assert result['test_feed'] and result['name'] == 'VPN-GATEWAY-TEST-V2'
            assert made.await_args.args[1:] == ('b', 'VPN-GATEWAY-TEST-V2', ['VPN-GATEWAY'])
        assert asked.await_args.args[3] == "Create test feed 'VPN-GATEWAY-TEST-V2' for the Events made from VPN-GATEWAY"
    with patch.object(feeds, 'gateway_from', lambda ctx: stroom), \
            patch('tools.plan.works_from', AsyncMock(return_value=[])):
        with pytest.raises(ToolError, match="no test feed to name"):
            await feeds.create_feed(None, 'b', 'ACME-TEST', test_feed=True)


async def test_samples_go_to_a_build_on_an_existing_feed_only_through_a_feed_made_for_the_users_files():
    # The test feed the server makes for the Events (output_for) takes no samples either.
    from unittest.mock import AsyncMock, patch
    import pytest
    from fastmcp.exceptions import ToolError
    from tools import feeds
    ref = {'type': 'Feed', 'uuid': 'f9', 'name': 'VPN-GATEWAY-MCP-TEST'}
    stroom = SimpleNamespace(get=AsyncMock(return_value=ref))
    guard = SimpleNamespace(check_managed=AsyncMock(), check_in_build=AsyncMock(return_value='b'), build_of=AsyncMock(return_value='b'))
    entries = {'f9': {'output_for': ['VPN-GATEWAY']}}
    with patch.object(feeds, 'gateway_from', lambda ctx: stroom), patch.object(feeds, 'guard_from', lambda ctx: guard), \
            patch('tools.plan.works_from', AsyncMock(return_value=['VPN-GATEWAY'])), \
            patch('security.record.entry', AsyncMock(side_effect=lambda g, b, u: entries.get(u, {}))):
        with pytest.raises(ToolError, match="wasn't made for sample files the user chose instead: upload nothing"):
            await feeds._build_feed(None, 'VPN-GATEWAY-MCP-TEST')
        entries['f9'] = {'user_samples': ['samples/vpn_1.csv']}
        assert await feeds._build_feed(None, 'VPN-GATEWAY-MCP-TEST') == ref
