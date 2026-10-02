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
