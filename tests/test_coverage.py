"""Whole-feed coverage of an events pipeline, and the pipelines that follow it."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tools import coverage
from utils import cef
from utils.fieldplan import FieldPlan, PlannedField

EVENTS = '''<Events xmlns="event-logging:3">
<Event><EventSource><User><Id>carol</Id></User></EventSource><EventDetail><TypeId>View</TypeId>
 <View><Resource><Type>Secret</Type><Name>Payroll</Name></Resource></View></EventDetail></Event>
</Events>'''


def term(field, value, condition='EQUALS'):
    return {'type': 'term', 'field': field, 'condition': condition, 'value': value}


async def test_follow_on_pipelines_are_those_whose_filters_read_its_events():
    pipeline = {'uuid': 'ev', 'name': 'Acme-Events'}
    filters = [
        {'id': 1, 'pipelineUuid': 'idx', 'pipelineName': 'Acme-Index', 'queryData': {'expression': {'children': [
            {'type': 'operator', 'children': [term('Feed', 'ACME'), term('Type', 'Events')]},
            {'type': 'term', 'field': 'Pipeline', 'condition': 'IS_DOC_REF', 'docRef': {'uuid': 'ev'}}]}}},
        {'id': 2, 'pipelineUuid': 'cef', 'pipelineName': 'Acme-CEF', 'queryData': {'expression': {'children': [
            term('Feed', 'acme'), term('Type', 'Events')]}}},                  # its Events feed, whatever its case
        {'id': 3, 'pipelineUuid': 'raw', 'queryData': {'expression': {'children': [
            term('Feed', 'ACME'), term('Type', 'Raw Events')]}}},              # reads the raw data, not the Events
        {'id': 4, 'pipelineUuid': 'gone', 'deleted': True, 'queryData': {'expression': {'children': [
            term('Feed', 'ACME'), term('Type', 'Events')]}}},
        {'id': 5, 'pipelineUuid': 'ev', 'queryData': {'expression': {'children': [term('Feed', 'ACME')]}}},
    ]
    stroom = SimpleNamespace(post=AsyncMock(return_value={'values': [{'processorFilter': f} for f in filters]}))
    with patch.object(coverage, 'gateway_from', return_value=stroom):
        found = await coverage.follow_on_pipelines(SimpleNamespace(), pipeline, {'ACME'})
    assert [(f['uuid'], f['filters']) for f in found] == [('idx', [1]), ('cef', [2])]


async def test_missed_records_come_from_the_error_streams_of_the_raw_streams_they_were_in():
    errors = [{'id': 10, 'parentMetaId': 3, 'createMs': 1, 'attributes': {'Warning Count': '2'}},
              {'id': 11, 'parentMetaId': 4, 'attributes': {'Warning Count': '0', 'Error Count': '0', 'Info Count': '0'}},
              {'id': 12, 'parentMetaId': 5, 'attributes': {'Rule': 'None'}}]                    # counts not given: read
    markers = {10: ['Log - No event mapping matched record 1 (action=view | type=)',
                    'Log - No event mapping matched record 2'],                                  # an older XSLT's line
               12: ["Log - Kept as Unknown by rule 'other': record 0 (action=export)", 'some other warning']}
    stroom = SimpleNamespace(fetch_data=AsyncMock(side_effect=lambda sid, *a: {'markers': [
        {'type': 'storedError', 'message': m} for m in markers.get(sid, [])]}))
    found = await coverage._logged(stroom, errors)
    assert [(f['raw'], f['record'], f['rule'], f['values']) for f in found] == [
        (3, 1, None, {'action': 'view', 'type': ''}), (3, 2, None, {}), (5, 0, 'other', {'action': 'export'})]
    assert [c.args[0] for c in stroom.fetch_data.await_args_list] == [10, 12]      # the counted-clean one isn't read


async def test_a_large_feed_is_read_in_bounded_batches_that_carry_on():
    # Seen as a concern: a feed of hundreds of thousands of streams. Pages by Id, at most the budget a call.
    ids = list(range(1, 1001))

    async def find_meta(terms, limit, newest_first=True):
        after = int(next(t['value'] for t in terms if t.get('field') == 'Id'))
        return {'values': [{'meta': {'id': i, 'status': 'UNLOCKED'}} for i in ids if i > after][:limit]}
    stroom = SimpleNamespace(find_meta=AsyncMock(side_effect=find_meta))
    first, more = await coverage._error_batch(stroom, {'uuid': 'p'}, 0, 450)
    assert len(first) == 450 and more == 450 and stroom.find_meta.await_count == 3          # pages of 200 at most
    rest, more = await coverage._error_batch(stroom, {'uuid': 'p'}, 450, 1000)
    assert len(rest) == 550 and more is None and rest[0]['id'] == 451


def test_an_index_plan_lists_the_event_paths_no_field_takes():
    plan = FieldPlan(backend='elasticsearch', index_name='x', time_field='@timestamp', fields=[
        PlannedField(name='UserId', type='keyword', source='EventSource/User/Id'),
        PlannedField(name='Resource', type='keyword', source='EventDetail/*/Resource/Name')])
    gaps = coverage._index_gaps(plan, cef.events_of(EVENTS))
    assert gaps == ['EventDetail/TypeId (1 of 1 events)', 'EventDetail/View/Resource/Type (1 of 1 events)']


def test_a_cef_plan_is_extended_without_moving_what_it_has():
    plan = cef.CefPlan(vendor=cef.CefValue(value='Acme'), product=cef.CefValue(value='P'), version=cef.CefValue(value='1'),
                       signature=cef.CefValue(source='EventDetail/TypeId'), name=cef.CefValue(value='n'), topic='t',
                       common=[cef.CefField(path='EventSource/User/Id', key='suser')],
                       events={'View': [cef.CefField(path='EventDetail/View/Document/Title', key='cs1', label='Title')]})
    added, notes = cef.extend(plan, cef.events_of(EVENTS))
    by_path = {a['path']: a for a in added}
    # cs1 is the plan's (for a value these Events don't hold): the new ones take other slots.
    assert by_path['EventDetail/View/Resource/Name']['key'] not in ('cs1', 'suser')
    assert {a['key'] for a in added if a['path'].startswith('EventDetail/View/Resource')} <= {'cs2', 'cs3', 'cs4', 'cs5', 'cs6'}
    assert all(a['event_type'] == 'View' for a in added) and 'EventSource/User/Id' not in by_path
