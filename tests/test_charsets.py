"""A raw stream with bytes its feed's character set can't decode: Stroom's viewer refuses its text ("Unable to decode a
UTF-8 character"), its pipelines read on replacing them; the server reads it through the bytes as a pipeline does, and
says what was replaced and what to do."""
from types import SimpleNamespace

from utils.charsets import decode, hex_bytes, report_for
from tools.sampling import read_head
from tools.streams import raw_text

MIXED = (b'<recs><rec>utf8 caf\xc3\xa9 \xf0\x9f\x91\x80</rec><rec>latin1 caf\xe9</rec>'
         b'<rec>cp1252 \x93q\x94</rec></recs>' + b'<rec>pad</rec>' * 400)


def dump(data: bytes) -> str:
    """Stroom's HEX view of data: 32 bytes a line, an address, the hex in groups of four, the bytes as characters."""
    lines = []
    for at in range(0, len(data), 32):
        row = data[at:at + 32]
        groups = '  '.join(' '.join(f'{b:02X}' for b in row[i:i + 4]) for i in range(0, len(row), 4))
        lines.append(f'{at:010X}  {groups:<102}  ' + ''.join(chr(b) if 32 <= b < 127 else '.' for b in row))
    return '\n'.join(lines)


class ByteStroom:
    """A stream Stroom refuses as text, shown by its HEX view in pages of 1,000 lines."""

    def __init__(self, data: bytes, encoding: str = 'UTF-8'):
        self.data, self.encoding = data, encoding
        self.settings = SimpleNamespace(stroom_url='http://test')

    async def post(self, path: str, body: dict) -> dict:
        if body['displayMode'] != 'HEX':
            return {'errors': ['Error fetching data: Unable to decode a UTF-8 character starting at byte offset 40.']}
        start = body['sourceLocation']['dataRange']['byteOffsetFrom'] // 32 * 32
        page = self.data[start:start + 32 * 1000]
        text = dump(page)
        # Addresses as in the stream, not the page.
        text = '\n'.join(f'{int(line[:10], 16) + start:010X}' + line[10:] for line in text.splitlines())
        return {'data': text, 'totalBytes': len(self.data), 'totalItemCount': {'count': 1}}

    async def fetch_data(self, *args, **kwargs) -> dict:
        return {'errors': ['Error fetching data: Unable to decode a UTF-8 character starting at byte offset 40.']}

    async def get(self, path: str) -> dict:
        return {'feedName': 'F'} if path.startswith('/meta') else {'uuid': 'f'}

    async def get_doc(self, kind: str, uuid: str) -> dict:
        return {'encoding': self.encoding}


def test_a_hex_dump_gives_back_its_bytes_by_address():
    data = bytes(range(256)) * 3 + b'tail'
    lines = hex_bytes(dump(data), len(data))
    assert b''.join(lines[k] for k in sorted(lines)) == data


def test_bytes_a_utf8_feed_cant_decode_are_replaced_and_said():
    text, report = decode(MIXED, 'UTF-8', final=True)
    assert 'café \U0001F440' in text and 'latin1 caf�' in text and 'cp1252 �q�' in text
    assert report['undecodable_bytes'] == 3 and report['as_windows_1252'] == {'93': '“', '94': '”', 'E9': 'é'}
    assert 'mixes character sets' in report['advice']
    _, whole = decode('café “q”'.encode('cp1252'), 'UTF-8', final=True)
    assert 'set the feed\'s encoding to windows-1252' in whole['advice']
    assert decode('café'.encode(), 'UTF-8', final=True) == ('café', None)
    # A sequence cut off by the end of what was read is left out, not counted.
    assert decode('café'.encode()[:-1], 'UTF-8', final=False) == ('caf', None)


async def test_a_stream_stroom_wont_show_as_text_is_read_through_its_bytes():
    stroom = ByteStroom(MIXED)
    text, cut, parts = await read_head(stroom, 7, 0, 1_000_000)
    assert text == MIXED.decode('utf-8', 'replace') and not cut and parts == 1
    assert report_for(stroom, 7)['undecodable_bytes'] == 3
    text, truncated = await raw_text(stroom, 8, 1_000_000)
    assert text == MIXED.decode('utf-8', 'replace') and not truncated
    head, cut, _ = await read_head(stroom, 9, 0, 2_000)
    assert cut and MIXED.decode('utf-8', 'replace').startswith(head) and len(head) <= 2_000


async def test_the_feeds_own_encoding_decodes_the_bytes():
    data = 'café “q”\n'.encode('cp1252') * 50
    text, _, _ = await read_head(ByteStroom(data, 'windows-1252'), 10, 0, 100_000)
    assert text == data.decode('cp1252')
