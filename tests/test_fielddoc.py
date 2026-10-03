"""The mapping kept with an XSLT, and the documentation generated from it."""
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
    assert '| `StreamId` | id | `@StreamId` | always |' in text
    assert '| `UserId` | keyword | `EventSource/User/Id` | 66.7% of events |' in text
    assert '| In sample |' not in index_field_mapping_markdown(plan)


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
    assert '| `user.name` | keyword | `EventSource/User/Id` | 100% of events | `alice` |' in section
    assert '| `source.ip` | ip | `EventSource/Client/IPAddress` | not in the sample | (none in the sample) |' in section


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
