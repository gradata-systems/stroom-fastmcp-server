"""The mapping kept with an XSLT, and the documentation generated from it."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tests.test_xsltgen import SCHEMA, mapping
from tools import builds
from utils.fielddoc import index_field_mapping_markdown
from utils.fieldplan import FieldPlan, PlannedField
from utils.mappingstore import (DOC_MARK, digest, doc_digest, normalise_xslt, read_mapping, replace_section,
                                with_mapping)
from utils.xsltgen import generate


def test_the_mapping_travels_in_the_xslt_description_and_is_read_back():
    payload = {'schema_version': '4.1.0', 'mapping': mapping().model_dump(exclude_none=True, exclude_defaults=True)}
    description = with_mapping('Written by the onboarding run.', 'translation', payload)
    assert description.startswith('Written by the onboarding run.')
    assert read_mapping(description) == ('translation', payload)
    # Replaced, not appended, when saved again; other text kept.
    again = with_mapping(description, 'translation', {**payload, 'schema_version': '3.5.2'})
    assert again.count('stroom-mcp translation mapping') == 1 and read_mapping(again)[1]['schema_version'] == '3.5.2'
    assert read_mapping('plain text') is None and read_mapping(None) is None


def test_normalised_xslt_ignores_layout_and_the_digest_marks_the_documentation():
    xslt = generate(mapping(), SCHEMA, '4.1.0')['xslt']
    assert normalise_xslt(xslt) == normalise_xslt(xslt.replace('\n', '\n  '))
    assert normalise_xslt(xslt) != normalise_xslt(xslt.replace('Acme VPN', 'Other'))
    mark = DOC_MARK.format(digest=digest('a', 'b'))
    assert doc_digest(f'## Field mapping\n\ntable\n\n{mark}\n') == digest('a', 'b') and doc_digest('none') is None


def test_the_field_mapping_section_is_replaced_in_place_or_added_before_output():
    doc = '## Purpose and data\n\nx\n\n## Field mapping\n\nold table\n\n## Output\n\ny\n'
    new = replace_section(doc, 'Field mapping', '### EventSource\n\nnew table')
    assert 'old table' not in new and new.index('new table') < new.index('## Output')
    assert new.count('## Field mapping') == 1
    without = replace_section('## Purpose and data\n\nx\n\n## Output\n\ny\n', 'Field mapping', 'table')
    assert without.index('## Field mapping') < without.index('## Output')
    assert replace_section('## Purpose and data\n\nx\n', 'Field mapping', 'table').rstrip().endswith('table')


def test_index_field_mapping_table():
    plan = FieldPlan(backend='lucene', index_name='ACME-INDEX', time_field='EventTime', drop_when=["EventDetail/TypeId = 'hb'"],
                     fields=[PlannedField(name='StreamId', type='id', source='@StreamId'),
                             PlannedField(name='EventTime', type='date', source='EventTime/TimeCreated'),
                             PlannedField(name='UserId', type='keyword', source='EventSource/User/Id')])
    text = index_field_mapping_markdown(plan, {'EventTime/TimeCreated': 100.0, 'EventSource/User/Id': 66.7})
    assert "- `EventDetail/TypeId = 'hb'`" in text
    assert '| `StreamId` | The id of the Events stream the event came from. | id | `@StreamId` | always |' in text
    assert '| `UserId` |  | keyword | `EventSource/User/Id` | 66.7% of events |' in text   # no schema, no sample
    assert '| In sample |' not in index_field_mapping_markdown(plan)


def test_elasticsearch_rows_show_long_ids_and_group_fields_by_object():
    fields = [('StreamId', 'id', '@StreamId'), ('@timestamp', 'date', 'EventTime/TimeCreated'),
              ('host.name', 'keyword', 'EventSource/Device/HostName'), ('user.id', 'keyword', 'EventSource/User/Id'),
              ('message', 'text', 'EventDetail/Description'), ('host.ip', 'ip', 'EventSource/Device/IPAddress'),
              ('user.name', 'keyword', 'EventSource/User/Name')]
    plan = FieldPlan(backend='elasticsearch', index_name='people-v1', time_field='@timestamp',
                     fields=[PlannedField(name=n, type=t, source=s) for n, t, s in fields])
    text = index_field_mapping_markdown(plan)
    rows = [line.split(' | ')[0].strip('| `') for line in text.splitlines() if line.startswith('| `')]
    assert rows == ['StreamId', '@timestamp', 'host.name', 'host.ip', 'user.id', 'user.name', 'message']
    assert '| `StreamId` | The id of the Events stream the event came from. | long | `@StreamId` |' in text and '| `host.ip` |  | ip |' in text


def test_each_index_field_is_described_from_the_schema_and_the_sample():
    from utils.eventschema import EventSchema
    schema = EventSchema.parse((Path(__file__).parent / 'fixtures' / 'event-logging-v4.1.0.xsd').read_bytes())
    fields = [('StreamId', 'id', '@StreamId'), ('host.ip', 'ip', 'EventSource/Device/IPAddress'),
              ('user.id', 'keyword', 'EventSource/User/Id'), ('user.email', 'keyword', 'EventSource/User/EmailAddress'),
              ('product', 'keyword', 'EventSource/System/Name'),
              ('agent', 'keyword', "EventDetail/Authenticate/Data[@Name='agent']/@Value"),
              ('outcome', 'keyword', 'EventDetail/Authenticate/Outcome/Success')]
    plan = FieldPlan(backend='elasticsearch', index_name='people-v1', time_field='@timestamp',
                     fields=[PlannedField(name=n, type=t, source=s, description='Whether the logon succeeded.'
                                          if n == 'outcome' else '') for n, t, s in fields])
    documents = [{'StreamId': ['7'], 'host.ip': ['10.0.0.1'], 'user.id': ['alice'], 'user.email': ['a@x.org'],
                  'product': ['E2E'], 'agent': ['curl'], 'outcome': ['true']},
                 {'StreamId': ['7'], 'host.ip': ['10.0.0.2'], 'user.id': ['bob'], 'user.email': ['b@x.org'],
                  'product': ['E2E'], 'outcome': ['false']}]
    text = index_field_mapping_markdown(plan, None, documents, schema)
    described = {line.split(' | ')[0].strip('| `'): line.split(' | ')[1] for line in text.splitlines()
                 if line.startswith('| `')}
    # What each is; how often and which values are the In sample and Sample values columns' to say.
    assert described['StreamId'] == 'The id of the Events stream the event came from.'
    assert described['host.ip'] == 'The network IP address of the device, e.g. 192.168.0.3.'
    assert described['user.email'] == 'The email address of the user.'
    assert described['product'] == 'The name of the system.'
    assert described['agent'].startswith('The `agent` value recorded in a Data element') and 'sample' not in described['agent']
    assert described['outcome'] == 'Whether the logon succeeded.'   # the plan's own
    # The schema's words for the user's id (its base type's 'the object' named for the user), then the sample.
    assert described['user.id'] == 'An identifier for the user.'
    # The schema's opening ('This element contains information about the ...') goes.
    from utils.fielddoc import schema_description
    assert schema_description(schema, 'EventTime/TimeCreated') == 'Time the event was created.'
    assert schema_description(schema, 'EventSource').startswith('Where the event came from')


async def test_write_documentation_needs_streams_for_a_kept_mapping_and_a_section_otherwise():
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(get_doc=AsyncMock(return_value={'name': 'Acme', 'uuid': 'p'}),
                                                                       settings=SimpleNamespace(event_logging_version='4.1.0'))})
    kept = {'kind': 'translation', 'payload': {'mapping': {}}, 'element': 'translationFilter', 'xslt': {'data': ''}}
    with patch.object(builds, 'kept_mapping', AsyncMock(return_value=kept)):
        with pytest.raises(ToolError, match='Give stream_ids'):
            await builds.write_documentation(ctx, 'b', 'p', '## Purpose and data\n\nx\n', 'Created')
    with patch.object(builds, 'kept_mapping', AsyncMock(return_value=None)), \
            patch('tools.templates._shape', AsyncMock(return_value={'stage': 'translation'})):
        with pytest.raises(ToolError, match="needs a '## Field mapping' section"):
            await builds.write_documentation(ctx, 'b', 'p', '## Purpose and data\n\nx\n', 'Created')


async def test_a_field_mapping_with_no_sampled_events_is_not_written_and_says_why():
    # A section of "(not in the sample)" was written, given an Events stream where the raw input belongs.
    stroom = SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'),
                             find_meta=AsyncMock(return_value={'values': [{'meta': {'id': 873, 'feedName': 'ACME', 'typeName': 'Events'}}]}))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    kept = {'kind': 'translation', 'payload': {'mapping': mapping().model_dump(exclude_none=True)},
            'element': 'translationFilter', 'xslt': {'data': ''}}
    pipeline = SimpleNamespace()
    with patch.object(builds, 'gateway_from', lambda c: stroom), \
            patch('tools.generation.event_schema', AsyncMock(return_value=SCHEMA)), \
            patch.object(builds._Pipeline, 'load', AsyncMock(return_value=pipeline)), \
            patch.object(builds, '_outputs', AsyncMock(return_value={'873:0': '<Events xmlns="event-logging:3"/>'})), \
            patch('tools.stepping._step', AsyncMock(return_value={'foundRecord': True})):
        with pytest.raises(ToolError, match=r"no events \(1 records stepped\)\. stream 873 \(ACME, Events\): its records "
                                            r"produced no Event\. An events pipeline's stream_ids are its raw sample streams"):
            await builds.field_mapping_section(ctx, {'uuid': 'p'}, kept, [873])
        # An index plan's Events streams with nothing in them are refused the same way.
        plan = FieldPlan(backend='lucene', index_name='acme', time_field='EventTime',
                         fields=[PlannedField(name='StreamId', type='id', source='@StreamId')])
        with patch.object(builds, 'summarise_events', AsyncMock(return_value={'path_population': {}})):
            with pytest.raises(ToolError, match=r"streams \['873 \(ACME, Events\)'\] hold no events"):
                await builds.field_mapping_section(ctx, {'uuid': 'p'}, {'kind': 'index', 'payload': plan.model_dump(),
                                                                        'element': 'xsltFilter'}, [873])


def test_the_index_section_shows_what_each_index_field_got_from_the_sample():
    from utils.fielddoc import index_documents
    plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v2', time_field='@timestamp',
                     fields=[PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
                             PlannedField(name='user.name', type='keyword', source='EventSource/User/Id'),
                             PlannedField(name='source.ip', type='ip', source='EventSource/Client/IPAddress')])
    elastic = ('<array xmlns="http://www.w3.org/2005/xpath-functions"><map><string key="@timestamp">2026-10-01T10:00:00.000Z'
               '</string><map key="user"><string key="name">alice</string></map></map></array>')
    lucene = ('<records xmlns="records:2"><record><data name="UserId" value="bob"/><data name="UserId" value="carol"/>'
              '</record></records>')
    documents = index_documents([elastic])
    assert documents == [{'@timestamp': ['2026-10-01T10:00:00.000Z'], 'user': [], 'user.name': ['alice']}]
    assert index_documents([lucene]) == [{'UserId': ['bob', 'carol']}]
    section = index_field_mapping_markdown(plan, {'EventSource/User/Id': 100.0}, documents)
    assert 'Sample values are what the 1 documents written from the sample got.' in section
    assert ('| `user.name` | Not described: no plan or schema covers it. | keyword | `EventSource/User/Id` | 100% of events | '
            '`alice` |') in section
    assert ('| `source.ip` | Not described: no plan or schema covers it. | ip | `EventSource/Client/IPAddress` | not in the sample | '
            '(none in the sample) |') in section


async def test_the_index_section_names_the_agreed_elasticsearch_template():
    from types import SimpleNamespace
    from unittest.mock import patch
    from tools import builds
    from utils.mappingstore import with_agreed_template
    plan = FieldPlan(backend='elasticsearch', index_name='stroom-door-v1', time_field='@timestamp',
                     fields=[PlannedField(name='User.Id', type='keyword', source='EventSource/User/Id')])
    pipeline = {'uuid': 'p', 'description': with_agreed_template('Doors.', {
        'name': 'stroom-door-v1', 'index': 'stroom-door-v1', 'cluster': 'ES', 'component_templates': ['stroom-base'],
        'xslt': 'x', 'agreed': '2026-10-04T01:00:00Z', 'dev_tools': 'PUT _index_template/stroom-door-v1\n{}'})}
    kept = {'kind': 'index', 'payload': plan.model_dump(), 'element': 'xsltFilter', 'xslt': {'data': ''}}
    with patch('tools.builds.gateway_from', return_value=SimpleNamespace()):
        section = await builds.field_mapping_section(None, pipeline, kept, [])
    assert "Elasticsearch index template `stroom-door-v1`, agreed with the user 2026-10-04, composed of `stroom-base`." in section


def _survey(fed_by=None, stored=True):
    return {'index': {'type': 'ElasticIndex', 'uuid': 'i', 'name': 'legacy-web', 'path': 'System/Prod/legacy-web'},
            'backend': 'elasticsearch', 'index_name': 'legacy-web', 'time_field': '@timestamp',
            'fields': [{'name': 'message', 'type': 'text'}, {'name': '@timestamp', 'type': 'date'},
                       {'name': 'user.name', 'type': 'keyword'}, {'name': 'http.status', 'type': 'long',
                                                                  **({} if stored else {'stored': False})},
                       {'name': 'source.ip', 'type': 'ip'}],
            'documents': [{'@timestamp': ['2026-09-23T11:45:00.000Z'], 'user.name': ['alice'], 'source.ip': ['10.3.0.1'],
                           'http.status': [], 'message': ['health check']},
                          {'@timestamp': ['2026-09-20T08:00:00.000Z'], 'user.name': ['bob'], 'source.ip': ['10.3.0.2'],
                           'http.status': ['200'], 'message': ['GET /']}],
            'earliest': '2026-09-20T08:00:00.000Z', 'latest': '2026-09-23T11:45:00.000Z', 'fed_by': fed_by or []}


def test_an_existing_index_is_documented_from_its_survey():
    from utils.fielddoc import existing_index_markdown
    text = existing_index_markdown(_survey(), {}, None)
    rows = {line.split(' | ')[0].strip('| `'): line.split(' | ')[1:] for line in text.splitlines() if line.startswith('| `')}
    assert list(rows) == ['@timestamp', 'message', 'user.name', 'http.status', 'source.ip']    # time first, then as Stroom lists
    # The overall facts are the summary's; the field table says only what explains its columns.
    assert 'No pipeline was found' not in text and 'newest documents' not in text and 'come from elsewhere' not in text
    assert 'Where each field comes from is not recorded: no pipeline that writes to it keeps an index plan.' in text
    assert rows['http.status'] == ['Numbers.', 'long', '(not recorded)',
                                   '50% of documents', '`200` |']
    assert rows['source.ip'][0] == 'IP addresses.'
    unstored = existing_index_markdown(_survey(stored=False), {}, None)
    assert '| `http.status` | Indexed but not stored: searchable, its values cannot be shown. | long | (not recorded) | not stored | - |' in unstored


def test_an_existing_index_fed_by_a_plan_says_where_each_field_comes_from():
    from utils.eventschema import EventSchema
    from utils.fielddoc import existing_index_markdown
    schema = EventSchema.parse((Path(__file__).parent / 'fixtures' / 'event-logging-v4.1.0.xsd').read_bytes())
    planned = {'user.name': PlannedField(name='user.name', type='keyword', source='EventSource/User/Id')}
    text = existing_index_markdown(_survey([{'name': 'Web - Indexing', 'uuid': 'p', 'plan': {'fields': []}}]), planned, schema)
    row = next(line for line in text.splitlines() if line.startswith('| `user.name`'))
    assert '| `user.name` | An identifier for the user. | keyword | `EventSource/User/Id` |' in row
    assert 'Fed by' not in text and 'not recorded' not in text.split('|')[0]


def test_an_index_stroom_reads_no_documents_from_is_documented_from_its_mapping_saying_so():
    from utils.fielddoc import existing_index_markdown
    survey = {**_survey(), 'documents': [], 'earliest': None, 'latest': None,
              'note': "No documents came back through Stroom. Stroom returns a hit only when its StreamId is a stream in this Stroom"}
    text = existing_index_markdown(survey, {}, None)
    assert 'No documents came back through Stroom, so no field below was read (see Data surveyed).' in text
    assert '| `user.name` | Not read: no documents came back through Stroom. | keyword | (not recorded) | not read | - |' in text


def test_fields_past_what_the_survey_reads_are_listed_as_not_surveyed():
    from utils.fielddoc import existing_index_markdown
    survey = {**_survey(), 'surveyed_fields': ['@timestamp', 'user.name', 'message']}
    text = existing_index_markdown(survey, {}, None)
    assert 'The survey read 3 of the 5 fields; the other 2 are listed as not surveyed.' in text
    assert '| `source.ip` | Not surveyed: past the fields the survey reads. | ip | (not recorded) | not surveyed | - |' in text
    assert '| `user.name` | Not described: no plan or schema covers it. |' in text


async def test_a_wide_index_is_surveyed_in_groups_of_columns_joined_on_the_ids():
    from tools import indexing
    names = ['StreamId', 'EventId', '@timestamp'] + [f'f{n:03}' for n in range(250)]
    docs = [{'StreamId': '7', 'EventId': str(e), '@timestamp': f'2026-10-0{e}T00:00:00.000Z',
             **{f'f{n:03}': f'v{n}-{e}' for n in range(250)}} for e in (1, 2)]
    searched = []

    async def search(ctx, dashboard, expression, length=100):
        columns = [c['name'] for c in dashboard['dashboardConfig']['components'][1]['settings']['fields']]
        searched.append(columns)
        return {'rows': [{c: d.get(c) for c in columns} for d in reversed(docs)], 'errors': []}

    async def post(path, body):
        if path == '/dataSource/v1/findFields':
            return {'values': [{'fldName': n, 'fldType': 'KEYWORD'} for n in names]}
        return {'values': []}       # findInContent: nothing feeds it
    stroom = SimpleNamespace(get_doc=AsyncMock(return_value={'name': 'wide', 'indexName': 'wide', 'timeField': '@timestamp'}),
                             post=post, find_documents=AsyncMock(return_value={'values': []}),
                             settings=SimpleNamespace(stroom_ui_url=None, stroom_url='http://s'))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    with patch.object(indexing, '_search', search), \
            patch.object(indexing, 'index_profile', AsyncMock(return_value={'documents': 2})):
        survey = await indexing.survey_index(ctx, 'ElasticIndex', 'i')
    assert len(searched) == 3 and all(len(c) <= 100 and c[:3] == ['StreamId', 'EventId', '@timestamp'] for c in searched)
    assert len(survey['surveyed_fields']) == 253 and survey['populated']['f249'] == 100.0
    assert survey['values']['f249'] == ['v249-2', 'v249-1'] and survey['documents_sampled'] == 2


def test_a_pipeline_writes_to_an_index_by_its_effective_properties_not_a_mention():
    from tools.indexing import writes_to
    lucene = {('indexingFilter', 'index'): {'value': {'type': 'Index', 'uuid': 'u1'}}}
    assert writes_to(lucene, 'Index', 'u1', None) and not writes_to(lucene, 'Index', 'u2', None)
    named = lambda value: {('elasticIndexingFilter', 'indexName'): {'value': value}}
    assert writes_to(named('foo-v1'), 'ElasticIndex', 'x', 'foo-v1')
    assert not writes_to(named('foo-v10'), 'ElasticIndex', 'x', 'foo-v1')
    assert writes_to(named('ecs-windows{_suffix}v1'), 'ElasticIndex', 'x', 'ecs-windows-dc-v1')     # built from values
    assert writes_to(named('ecs-windows-v1'), 'ElasticIndex', 'x', 'ecs-windows*')                  # a pattern or alias
    assert not writes_to({('xsltFilter', 'xslt'): {'value': 'foo-v1'}}, 'ElasticIndex', 'x', 'foo-v1')


async def test_describe_document_still_returns_an_index_doc_when_its_survey_fails():
    from tools import explorer, indexing
    with patch.object(explorer, 'get_document', AsyncMock(return_value={'name': 'IDX', 'indexName': 'idx'})), \
            patch.object(indexing, 'survey_index', AsyncMock(side_effect=ToolError('cluster unreachable'))):
        doc = await explorer.describe_document(None, 'ElasticIndex', 'i')
    assert doc['name'] == 'IDX' and 'cluster unreachable' in doc['survey_error'] and 'survey' not in doc


async def test_documenting_an_index_needs_a_change_line_and_takes_no_pipeline_arguments():
    from tools import builds
    with pytest.raises(ToolError, match='Give change'):
        await builds.write_documentation(None, 'b', index_uuid='i', markdown='## Purpose and data\n\nx\n')
    with pytest.raises(ToolError, match='Leave them out with index_uuid'):
        await builds.write_documentation(None, 'b', index_uuid='i', markdown='## Purpose and data\n\nx\n',
                                         change='Created', stream_ids=[1])


def test_an_existing_index_describes_its_ids_and_time_field_without_repeating_the_sample():
    from utils.fielddoc import existing_index_markdown, field_description
    survey = _survey()
    survey['fields'] = [{'name': 'StreamId', 'type': 'long'}, {'name': 'EventId', 'type': 'long'}] + survey['fields']
    for n, d in enumerate(survey['documents']):
        d.update({'StreamId': ['7'], 'EventId': [str(n + 1)]})
    text = existing_index_markdown(survey, {}, None)
    described = {line.split(' | ')[0].strip('| `'): line.split(' | ')[1] for line in text.splitlines() if line.startswith('| `')}
    assert described['StreamId'].startswith('The id of the stream the document came from.') and 'Events' not in described['StreamId']
    assert described['EventId'].startswith("The record's number in that stream")
    assert described['@timestamp'].startswith("The index's time field: searches and dashboards filter on it.")
    # Only what the other columns do not show: several values in one document; the kind, when nothing else says.
    assert field_description('The user.', [['a'], ['b', 'c']]) == 'The user. Several values per document.'
    assert field_description('', [['200'], [], ['404']]) == 'Numbers.'
    assert field_description('', [['x'], ['y']]) == 'Not described: no plan or schema covers it.'
    assert field_description('', None) == ''


def test_the_data_surveyed_summary_says_what_the_index_holds_and_where_it_comes_from():
    from utils.fielddoc import existing_index_summary
    survey = {**_survey([{'name': 'Web - Indexing', 'uuid': 'p', 'plan': {'fields': []}}]), 'documents_sampled': 2,
              'profile': {'documents': 1200, 'earliest': '2026-01-01T00:00:00.000Z', 'latest': '2026-09-23T11:45:00.000Z',
                          'streams': 40, 'streams_examined': 20,
                          'sources': [{'feed': 'WEB-ACCESS', 'type': 'Events', 'produced_by': 'WEB-ACCESS-Events',
                                       'streams': 20, 'documents': 900, 'description': 'Proxy access logs.'}]}}
    text = existing_index_summary(survey)
    assert text.startswith('### Data surveyed')
    assert '`legacy-web` holds 1200 documents, from 2026-01-01T00:00:00.000Z to 2026-09-23T11:45:00.000Z, in 40 streams.' in text
    assert ("From feed `WEB-ACCESS` (Events streams, produced by pipeline `WEB-ACCESS-Events`): 20 streams, 900 documents. "
            "The feed's description: Proxy access logs.") in text
    assert '(Sources from the 20 streams with the most documents.)' in text
    assert 'Indexed by pipeline `Web - Indexing`, from the index plan kept with its XSLT.' in text
    assert 'The field table is drawn from the 2 newest documents' in text and 'In sample and Sample values' in text


def test_the_summary_goes_under_the_agents_own_purpose():
    from tools.builds import _below_section
    text = _below_section('## Purpose and data\n\nProxy logs.\n\n## Field mapping\n\nT\n', 'Purpose and data',
                          '### Data surveyed\n\n- x\n')
    assert text.index('Proxy logs.') < text.index('### Data surveyed') < text.index('## Field mapping')
    assert _below_section('## Field mapping\n\nT\n', 'Purpose and data', '### S\n').startswith('## Purpose and data\n\n### S')
