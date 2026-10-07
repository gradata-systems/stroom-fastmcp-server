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
    stroom = SimpleNamespace(fetch_data=AsyncMock(return_value=body), settings=SimpleNamespace(max_stream_chars=len(event) * 3 - 5))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    read = await streams.read_stream(ctx, stream_id=7, first_record=0, record_count=5)
    assert len(read['records']) == 2 and all(etree.fromstring(r.encode()) is not None for r in read['records'])
    assert read['next_first_record'] == 2 and 'truncated' not in read
    # A single record larger than the limit is still returned (cut), flagged.
    stroom.settings.max_stream_chars = 40
    one = await streams.read_stream(ctx, stream_id=7, first_record=0, record_count=5)
    assert len(one['records']) == 1 and one['truncated']
