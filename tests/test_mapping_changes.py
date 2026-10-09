"""A fix to a translation mapping sends only what changes (changes=), merged into the mapping sent before."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tests.test_xsltgen import SCHEMA, mapping
from tools import generation
from utils.mappingchanges import apply_changes

BASE = {'input': 'json', 'common': [{'path': 'EventTime/TimeCreated', 'field': 'ts'},
                                    {'path': 'EventDetail/Unknown/Data', 'data_name': 'a', 'field': 'a'}],
        'events': [{'name': 'logon', 'fields': []}, {'name': 'other', 'fields': []}]}


def test_rules_by_name_common_entries_by_path_and_other_keys_whole():
    merged, done = apply_changes(BASE, {
        'events': [{'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'}]},
                   {'name': 'logoff', 'fields': []}, {'name': 'logon', 'remove': True}],
        'common': [{'path': 'EventDetail/Unknown/Data', 'data_name': 'a', 'field': 'b'},
                   {'path': 'EventTime/TimeCreated', 'remove': True}],
        'unmatched': 'skip'})
    assert [r['name'] for r in merged['events']] == ['other', 'logoff']
    assert merged['events'][0]['fields'] == [{'path': 'EventDetail/TypeId', 'value': 'x'}]
    assert merged['common'] == [{'path': 'EventDetail/Unknown/Data', 'data_name': 'a', 'field': 'b'}]
    assert merged['unmatched'] == 'skip' and BASE['events'][0]['name'] == 'logon'      # the base is left alone
    assert done == ["rule 'other' replaced", "rule 'logoff' added after the others", "rule 'logon' removed",
                    "common 'EventDetail/Unknown/Data' (Data 'a') replaced", "common 'EventTime/TimeCreated' removed",
                    "'unmatched' replaced"]


@pytest.mark.parametrize('changes, message', [
    ({}, 'only what changes'),
    ({'events': [{'fields': []}]}, 'needs its name'),
    ({'events': [{'name': 'nope', 'remove': True}]}, "removes rule 'nope', which the mapping does not have"),
    ({'common': [{'value': 'x'}]}, 'needs its path'),
])
def test_changes_that_cannot_apply_are_refused(changes, message):
    with pytest.raises(ToolError, match=message):
        apply_changes(BASE, changes)


def ctx():
    return SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))})


async def test_a_fix_sends_only_the_rule_it_changes():
    # Seen (Gemma 4 31B, VS Code): four failed builds, each resending the whole mapping at ~40 s of writing apiece.
    context = ctx()
    broken = mapping(events=[{'name': 'logon', 'fields': [{'path': 'EventDetail/Nope', 'value': 'x'}]},
                             mapping().model_dump(exclude_none=True)['events'][1]])
    from tools import translation
    saved = {'type': 'XSLT', 'uuid': 'x-1', 'name': 'ACME-Events', 'version': 'v1'}
    with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)), \
            patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})), \
            patch.object(translation, 'create_xslt', AsyncMock(return_value=saved)) as create:
        failed = await generation.build_translation_xslt(context, broken.model_dump(exclude_none=True),
                                                         build='acme-v1', name='ACME-Events')
        assert not failed['ok'] and 'changes=' in failed['hint'] and 'same build and name' in failed['hint']
        good = mapping().model_dump(exclude_none=True)['events'][0]
        fixed = await generation.build_translation_xslt(context, changes={'events': [good]}, build='acme-v1',
                                                        name='ACME-Events', include_xslt=False)
    assert fixed['changes_applied'] == ["rule 'logon' replaced"]
    assert fixed['problems'] == [] and fixed['ok'] and fixed['saved']['uuid'] == 'x-1'
    assert [r.name for r in create.await_args.kwargs['mapping'].events] == ['logon', 'other']
    # and the next fix starts from what was saved, under its uuid
    assert ('-', 'x-1') in context.lifespan_context['sent_mappings']


async def test_changes_to_a_saved_xslt_start_from_the_mapping_kept_with_it():
    # Another replica, or a new session: nothing remembered, so the mapping saved with the XSLT is changed.
    from utils.mappingstore import with_mapping
    kept = with_mapping('Acme events', 'translation', {'mapping': mapping().model_dump(exclude_none=True)})
    gateway = SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'),
                              get_doc=AsyncMock(return_value={'description': kept}))
    from tools import translation
    saved = {'type': 'XSLT', 'uuid': 'x-1', 'name': 'ACME-Events', 'version': 'v2'}
    with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)), \
            patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})), \
            patch.object(generation, 'gateway_from', lambda c: gateway), \
            patch.object(translation, 'update_xslt', AsyncMock(return_value=saved)) as update:
        result = await generation.build_translation_xslt(ctx(), changes={'unmatched': 'warn'}, uuid='x-1')
    assert result['changes_applied'] == ["'unmatched' replaced"] and result['saved']['version'] == 'v2'
    assert update.await_args.kwargs['mapping'].unmatched == 'warn'
    assert [r.name for r in update.await_args.kwargs['mapping'].events] == ['logon', 'other']


async def test_changes_need_a_mapping_to_change():
    with pytest.raises(ToolError, match='Nothing was sent for those yet'):
        await generation.build_translation_xslt(ctx(), changes={'unmatched': 'warn'}, build='b', name='new')
    with pytest.raises(ToolError, match='not both'):
        await generation.build_translation_xslt(ctx(), mapping(), changes={'unmatched': 'warn'})
    with pytest.raises(ToolError, match='Give mapping'):
        await generation.build_translation_xslt(ctx())
