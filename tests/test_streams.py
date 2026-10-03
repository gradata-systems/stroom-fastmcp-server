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
