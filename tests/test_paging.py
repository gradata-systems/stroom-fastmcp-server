"""A stream read in pages comes back exactly as stored, emoji and all (seen in production: a survey of tweets read
a clean stream as records with <data ne="..."> and valvalue= in them, because each page after an emoji started
early by one character per emoji: Stroom counts characters in UTF-16 units, an emoji as two)."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastmcp import Client, FastMCP

from security.lenient import LenientArguments
from tools.sampling import read_head
from tools.streams import raw_text
from utils.stroom import stroom_chars

TWEETS = '<tweets>' + ''.join(
    f'<tweet><data name="id" value="{n}" /><data name="text" value="look {"👀" * (n % 4)} ❤️ şike {n}" /></tweet>'
    for n in range(400)) + '</tweets>'


class PagingStroom:
    """Stroom's data fetch for one single-line stream: ranges in UTF-16 units, at most `page` a fetch, widened to
    whole characters."""

    def __init__(self, text: str, page: int = 997):
        self.units = text.encode('utf-16-le')
        self.page = page

    def _range(self, offset: int, length: int) -> dict:
        end = min(offset + min(length, self.page), len(self.units) // 2)
        if offset >= len(self.units) // 2:
            return {'data': '## No Data ##'}
        if 0xD800 <= int.from_bytes(self.units[2 * end - 2:2 * end], 'little') <= 0xDBFF:
            end += 1        # a range ending inside a surrogate pair takes the whole character
        data = self.units[2 * offset:2 * end].decode('utf-16-le')
        return {'data': data, 'totalCharacterCount': {'count': len(self.units) // 2}, 'totalItemCount': {'count': 1}}

    async def post(self, path: str, body: dict) -> dict:
        r = body['sourceLocation'].get('dataRange') or {'charOffsetFrom': 0, 'length': self.page}
        return self._range(r['charOffsetFrom'], r['length'])

    async def fetch_data(self, *args, **kwargs) -> dict:
        return self._range(0, self.page)


def test_stroom_counts_an_emoji_as_two():
    assert stroom_chars('a👀b') == 4 and stroom_chars('şike ❤️') == 7


async def test_a_head_read_in_pages_is_the_stream_as_stored():
    text, cut, parts = await read_head(PagingStroom(TWEETS), 1, 0, 30_000)
    assert text == TWEETS[:len(text)] and len(text) >= 29_000 and cut and parts == 1
    whole, cut, _ = await read_head(PagingStroom(TWEETS), 1, 0, 10 * len(TWEETS))
    assert whole == TWEETS and not cut      # read to the end: not said to be cut, though an emoji counts twice


async def test_a_raw_stream_read_in_pages_is_the_stream_as_stored():
    text, truncated = await raw_text(PagingStroom(TWEETS + '\n'), 1, 10 * len(TWEETS))
    assert text == TWEETS + '\n' and not truncated


async def test_a_survey_doc_made_by_the_call_just_before_is_merged_into_not_made_again():
    # Seen: two survey calls back to back; the explorer had not yet listed the first call's doc when the second began,
    # and the second's creating it again was refused as a twin.
    from tools import sampling
    earlier = {'uuid': 'doc', 'name': 'F - Survey', 'data': 'the earlier survey'}
    written = []

    async def put_doc(doc):
        written.append(doc)
        return doc
    stroom = SimpleNamespace(put_doc=put_doc)
    guard = SimpleNamespace(create_filled=AsyncMock())
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    with patch.object(sampling, '_load_record', AsyncMock(return_value=earlier)), \
            patch.object(sampling, 'gateway_from', return_value=stroom), \
            patch.object(sampling, 'guard_from', return_value=guard), \
            patch.object(sampling, 'read_state', return_value={'streams_read': [1], 'shapes': {'a': {}}}) as read, \
            patch.object(sampling, 'merge', side_effect=lambda state, *a: {'shapes': state['shapes'], 'streams_read': state['streams_read']}), \
            patch.object(sampling, 'render', return_value='merged'):
        out = await sampling._keep(ctx, 'b', None, None, {'feed': 'F'}, {}, 3)
    assert guard.create_filled.await_count == 0 and read.called
    assert written and out['survey_doc']['uuid'] == 'doc'


async def test_a_note_on_ignored_arguments_keeps_the_reply_json():
    server = FastMCP('t', middleware=[LenientArguments()])

    @server.tool
    def survey(feed: str) -> dict:
        return {'feed': feed, 'records_read': 3}
    async with Client(server) as client:
        result = await client.call_tool('survey', {'feed': 'TWITTER', 'out': '/outputs/x.json'})
    assert len(result.content) == 1
    body = json.loads(result.content[0].text)
    assert body['records_read'] == 3 and "Ignored unknown argument(s) ['out']" in body['argument_notes'][0]
    assert result.structured_content == {'feed': 'TWITTER', 'records_read': 3}
