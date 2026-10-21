"""Reading streams: records as well-formed documents."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lxml import etree

from tools import streams

PREFIXED = ('<?xml version="1.1" encoding="UTF-8"?><evt:Events xmlns:evt="event-logging:3" Version="4.1.0">'
            '<evt:Event><evt:EventDetail><evt:TypeId>x</evt:TypeId></evt:EventDetail></evt:Event></Events>')


def test_a_prefixed_root_is_closed_by_its_own_name():
    # Stroom closes a segmented record's root by its local name, so an XSLT that writes <evt:Events> reads back
    # as <evt:Events>...</Events>, which nothing could parse.
    fixed = streams._root_closed(PREFIXED)
    assert fixed.endswith('</evt:Events>') and etree.QName(etree.fromstring(fixed.encode())).localname == 'Events'
    plain = '<Events xmlns="event-logging:3"><Event/></Events>'
    assert streams._root_closed(plain) == plain
    assert streams._root_closed('raw text, not xml') == 'raw text, not xml'


async def test_segmented_records_are_read_well_formed():
    body = {'data': PREFIXED, 'dataType': 'SEGMENTED', 'totalItemCount': {'count': 1}}
    stroom = SimpleNamespace(fetch_data=AsyncMock(return_value=body))
    records, _ = await streams.read_records(stroom, 7, 0, 5, None, 100_000)
    assert records == [streams._root_closed(PREFIXED)]
    raw = {'data': 'a,b\n1,2\n</Events>', 'dataType': 'NON_SEGMENTED'}   # raw data is returned as it is
    stroom.fetch_data = AsyncMock(return_value=raw)
    assert (await streams.read_records(stroom, 7, 0, 1, None, 100_000))[0] == [raw['data']]


async def test_a_record_the_size_limit_would_cut_is_left_for_the_next_page():
    # Seen: 100 events of a large stream read at once, the last cut mid-attribute and validated as "attributes
    # construct error". Whole records only, and where to read on.
    event = ('<?xml version="1.1" encoding="UTF-8"?><Events xmlns="event-logging:3"><Event><EventDetail>'
             '<TypeId>x</TypeId></EventDetail></Event></Events>')
    body = {'data': event, 'dataType': 'SEGMENTED', 'totalItemCount': {'count': 10}}
    stroom = SimpleNamespace(fetch_data=AsyncMock(return_value=body), settings=SimpleNamespace(read_stream_chars=len(event) * 3 - 5))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    read = await streams.read_stream(ctx, stream_id=7, first_record=0, record_count=5)
    assert len(read['records']) == 2 and all(etree.fromstring(r.encode()) is not None for r in read['records'])
    assert read['next_first_record'] == 2 and 'truncated' not in read
    # A single record larger than the limit is still returned (cut), flagged.
    stroom.settings.read_stream_chars = 40
    one = await streams.read_stream(ctx, stream_id=7, first_record=0, record_count=5)
    assert len(one['records']) == 1 and one['truncated']
    # The limit is characters, which the user may raise: then all five fit.
    assert 'not of records' in read['hint'] and 'max_chars' in one['hint']
    more = await streams.read_stream(ctx, stream_id=7, first_record=0, record_count=5, max_chars=len(event) * 5 + 10)
    assert len(more['records']) == 5 and 'truncated' not in more


async def test_a_raw_stream_held_as_one_record_is_read_by_its_records():
    # Seen in production: reads of records 200, 450, 700 and 950 of a 1,000-fragment sample each returned its first
    # four, as Stroom holds a raw stream as one record; the agent went on to parse the file on disk in PowerShell.
    from unittest.mock import patch
    lines = [f'<Event xmlns="urn:win"><Id>{n}</Id></Event>' for n in range(1000)]
    text = '\n'.join(lines) + '\n'
    body = {'data': text[:500], 'dataType': 'NON_SEGMENTED', 'totalItemCount': {'count': 1}}
    stroom = SimpleNamespace(fetch_data=AsyncMock(return_value=body), settings=SimpleNamespace(read_stream_chars=60_000))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    with patch.object(streams, '_cached_raw_text', AsyncMock(return_value=(text, False))):
        read = await streams.read_stream(ctx, stream_id=7, first_record=450, record_count=2)
        assert read['records'] == lines[450:452] and read['total_records'] == 1000      # as in the stream
        assert 'xml fragments' in read['records_are']
        past = await streams.read_stream(ctx, stream_id=7, first_record=1000, record_count=2)
        assert past['records'] == [] and 'past the end' in past['hint']
    with patch.object(streams, '_cached_raw_text', AsyncMock(return_value=(text[:30_000], True))):
        partly = await streams.read_stream(ctx, stream_id=7, first_record=900, record_count=2)
        assert partly['records'] == [] and partly['total_records'] is None and 'max_sample_chars' in partly['hint']
    one = {'data': 'just one line of text', 'dataType': 'NON_SEGMENTED', 'totalItemCount': {'count': 1}}
    stroom.fetch_data = AsyncMock(return_value=one)
    with patch.object(streams, '_cached_raw_text', AsyncMock(return_value=(one['data'], False))):
        assert (await streams.read_stream(ctx, stream_id=7))['records'] == [one['data']]
