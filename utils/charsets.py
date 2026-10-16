"""A raw stream Stroom can't show as text, because some of its bytes aren't in its character set.

Stroom's data viewer refuses the whole stream ("Unable to decode a UTF-8 character starting at byte offset 66"), while
its pipelines read on, replacing each such byte with U+FFFD and logging an error ("Found un-decodable byte [93]"). So
the server reads such a stream's bytes through the viewer's HEX mode and decodes them as a pipeline would: the agent
then sees the stream as processing will. What was replaced is kept with the stream (its bytes never change), for the
tools that show stream text to say: which bytes, how they would read in Windows-1252, and what to do about it.
"""
import codecs
import re
from typing import Any

UNDECODABLE = 'Unable to decode'        # in the data viewer's error for such a stream
HEX_LINE = 32                           # bytes a HEX dump line holds (1,000 lines a fetch)
_LINE = re.compile(r'^([0-9A-Fa-f]{10})  ')
_REPORTS: dict[tuple[str, int], dict[str, Any]] = {}


def undecodable(errors: list[str] | None) -> bool:
    return any(UNDECODABLE in (e or '') for e in errors or [])


def hex_bytes(dump: str, total: int | None) -> dict[int, bytes]:
    """{byte offset: bytes} from one page of Stroom's HEX view: lines of an address, 32 bytes as hex, and the bytes
    as characters. The last line's count comes from the stream's size, as its character column follows the hex."""
    out: dict[int, bytes] = {}
    for line in dump.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        at = int(m.group(1), 16)
        want = HEX_LINE if total is None else max(0, min(HEX_LINE, total - at))
        tokens = line[12:].split()[:want]
        if len(tokens) == want and all(len(t) == 2 for t in tokens):
            out[at] = bytes.fromhex(''.join(tokens))
    return out


def python_codec(encoding: str | None) -> str:
    """The Python codec for a Java charset name (a feed's encoding); UTF-8 when there is none or it is unknown."""
    try:
        return codecs.lookup(encoding or 'UTF-8').name
    except LookupError:
        return 'utf-8'


def decode(data: bytes, encoding: str | None, final: bool) -> tuple[str, dict[str, Any] | None]:
    """(text, report): data decoded as a pipeline would, each byte the character set can't decode replaced by
    U+FFFD; the report says how many, where, and how they would read, or is None when every byte decoded. A
    sequence cut off at the end (final=False: only the head was read) is left out, not replaced."""
    codec = python_codec(encoding)
    text = codecs.getincrementaldecoder(codec)(errors='surrogateescape').decode(data, final=final)
    bad = [(i, ord(c) - 0xDC00) for i, c in enumerate(text) if 0xDC80 <= ord(c) <= 0xDCFF]
    if not bad:
        return text, None
    clean = ''.join('�' if 0xDC80 <= ord(c) <= 0xDCFF else c for c in text)
    return clean, _report(clean, bad, encoding or 'UTF-8', codec)


def _cp1252(byte: int) -> str | None:
    try:
        return bytes([byte]).decode('cp1252')
    except UnicodeDecodeError:
        return None


def _report(text: str, bad: list[tuple[int, int]], encoding: str, codec: str) -> dict[str, Any]:
    values = sorted({b for _, b in bad})
    as_1252 = {f'{b:02X}': _cp1252(b) for b in values[:40]}
    # Characters the stream does decode beyond ASCII: real UTF-8 in it, so it mixes character sets.
    decoded_beyond_ascii = any(ord(c) > 0x7F and c != '�' for c in text)
    examples = [{'at_character': i, 'byte': f'{b:02X}', 'as_windows_1252': _cp1252(b),
                 'context': text[max(0, i - 30):i + 30]} for i, b in bad[:5]]
    if codec != 'utf-8':
        advice = (f"These bytes aren't characters in the feed's encoding ({encoding}). Ask the user which character "
                  f"set the source writes, and set it as the feed's encoding, or have the source fixed.")
    elif decoded_beyond_ascii:
        advice = ("The stream mixes character sets: UTF-8 (which decodes) and single bytes from another one, most likely "
                  "Windows-1252 or Latin-1 (how they would read is given). No one encoding reads it all: Stroom replaces "
                  "these bytes with the replacement character and logs an error for each, and the characters are lost. "
                  "Tell the user; the fix is at the source (to send UTF-8 throughout).")
    elif all(as_1252.values()):
        advice = ("Nothing in the stream is UTF-8 beyond ASCII: it reads whole as Windows-1252 (how these bytes read is "
                  "given). Ask the user to confirm, then set the feed's encoding to windows-1252, so Stroom reads these "
                  "characters instead of replacing them (or have the source send UTF-8).")
    else:
        advice = ("Nothing in the stream is UTF-8 beyond ASCII, and some bytes aren't Windows-1252 either: it may be "
                  "ISO-8859-1 or another single-byte character set. Ask the user which, and set it as the feed's encoding.")
    return {'encoding': encoding, 'undecodable_bytes': len(bad), 'byte_values': [f'{b:02X}' for b in values[:40]],
            'as_windows_1252': as_1252, 'examples': examples, 'advice': advice}


def _key(stroom, stream_id: int) -> tuple[str, int]:
    return getattr(getattr(stroom, 'settings', None), 'stroom_url', '') or '', stream_id


def remember(stroom, stream_id: int, report: dict[str, Any] | None) -> None:
    if report:
        if len(_REPORTS) > 500:
            _REPORTS.pop(next(iter(_REPORTS)))
        _REPORTS[_key(stroom, stream_id)] = report


def report_for(stroom, stream_id: int) -> dict[str, Any] | None:
    """What reading the stream replaced, when it was read through its bytes."""
    return _REPORTS.get(_key(stroom, stream_id))


def note(stream_id: int, report: dict[str, Any]) -> str:
    """One line for a tool's notes."""
    return (f"stream {stream_id}: Stroom can't show it as text, because {report['undecodable_bytes']} byte(s) aren't "
            f"{report['encoding']} (bytes {', '.join(report['byte_values'][:8])}); read through its bytes instead, each "
            f"replaced with the replacement character, as a pipeline reads it. {report['advice']}")


async def feed_encoding(stroom, stream_id: int) -> str | None:
    """The character set Stroom reads a stream with: its feed's encoding (UTF-8 unless set)."""
    from urllib.parse import quote
    try:
        meta = await stroom.get(f'/meta/v1/{stream_id}')
        ref = await stroom.get(f"/feed/v1/getDocRefForName/{quote(meta['feedName'], safe='')}")
        return (await stroom.get_doc('Feed', ref['uuid'])).get('encoding') or None
    except Exception:       # the feed unreadable: Stroom's default
        return None


async def read_as_bytes(stroom, stream_id: int, part: int, max_bytes: int) -> tuple[str, bool, int]:
    """(text, cut, parts): the start of one part read through Stroom's HEX view, up to max_bytes, decoded as a
    pipeline would; what was replaced is remembered for the stream."""
    from fastmcp.exceptions import ToolError

    async def fetch(offset: int) -> dict[str, Any]:
        body = await stroom.post('/data/v1/fetch', {
            'sourceLocation': {'metaId': stream_id, 'partIndex': part, 'recordIndex': 0,
                               'dataRange': {'byteOffsetFrom': offset, 'length': HEX_LINE * 1000}},
            'displayMode': 'HEX', 'showAsHtml': False, 'recordCount': 1, 'expandedSeverities': []})
        if body.get('errors'):
            raise ToolError(f"Stroom could not read stream {stream_id}: {'; '.join(body['errors'])}")
        return body
    first = await fetch(0)
    total = first.get('totalBytes')
    parts = (first.get('totalItemCount') or {}).get('count') or 1
    lines = hex_bytes(first.get('data') or '', total)
    pos = max(lines) + HEX_LINE if lines else 0
    while lines and pos < min(total or 0, max_bytes):
        page = hex_bytes((await fetch(pos)).get('data') or '', total)
        if not page or max(page) < pos:
            break
        lines.update(page)
        pos = max(page) + HEX_LINE
    data = b''.join(lines[k] for k in sorted(lines))
    cut = len(data) > max_bytes or bool(total and len(data) < total)
    data = data[:max_bytes]
    text, report = decode(data, await feed_encoding(stroom, stream_id), final=not cut)
    remember(stroom, stream_id, report)
    return text, cut, parts
