"""changes= on a first save: applied to the session's draft, so a large drafted mapping needn't be sent whole.

Seen in testing: a Fortigate draft of 50 KB (60 extract entries, 10 rules with their data lists) had to be sent whole
in the first build_translation_xslt call; the agent couldn't write it out in one call, and stopped twice to ask."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tests.test_sample_check_speed import SAMPLE
from tests.test_xsltgen import SCHEMA
from tools import generation
from utils.mappingstore import with_mapping

SYSTEM = {'common': [{'path': 'EventSource/System/Name', 'value': 'FortiOS'}]}


def world(xslts=()):
    stroom = SimpleNamespace(settings=SimpleNamespace(sample_check_seconds=60),
                             get_doc=AsyncMock(side_effect=lambda kind, uuid: dict(next(x for x in xslts if x['uuid'] == uuid))))
    guard = SimpleNamespace(folder_contents=AsyncMock(return_value=[
        {'type': 'XSLT', 'uuid': x['uuid'], 'name': x['name']} for x in xslts]))
    ctx = SimpleNamespace(lifespan_context={'stroom': stroom})
    return ctx, guard


async def created(ctx, build, name, code, **kw):
    return {'type': 'XSLT', 'uuid': f'new-{name}', 'name': name, 'version': '1'}


def patches(guard):
    import security.guard as guard_module
    from tools import translation
    from contextlib import ExitStack

    class All(ExitStack):
        def __enter__(self):
            super().__enter__()
            for p in (patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)),
                      patch.object(generation, 'target_version', AsyncMock(return_value='4.1.0')),
                      patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})),
                      patch.object(guard_module, 'guard_from', lambda ctx: guard),
                      patch.object(translation, 'create_xslt', created)):
                self.enter_context(p)
            return self
    return All()


def system_name(result) -> str | None:
    return next((e.get('value') for e in result['mapping_sent']['common'] if e['path'] == 'EventSource/System/Name'), None)


async def save(ctx, **kw):
    """build_translation_xslt's merged mapping, read where apply_changes hands it on."""
    seen = {}
    real = generation.apply_changes

    def spy(base, changes):
        merged, applied = real(base, changes)
        seen['mapping'] = merged
        return merged, applied
    with patch.object(generation, 'apply_changes', spy):
        result = await generation.build_translation_xslt(ctx, **kw)
    return {**result, 'mapping_sent': seen.get('mapping')} if isinstance(result, dict) else result


async def test_a_first_save_applies_changes_to_the_draft():
    ctx, guard = world()
    with patches(guard):
        draft = await generation.draft_translation_mapping(ctx, samples={'fw.jsonl': SAMPLE}, source_name='Fortigate',
                                                           build='fg-v1')
        assert 'changes= only the rules and entries you change' in draft['hint']
        result = await save(ctx, changes=SYSTEM, build='fg-v1', name='FG-Events', sample=SAMPLE)
    assert result['changes_applied_to'] == 'the draft draft_translation_mapping made'
    assert system_name(result) == 'FortiOS'
    # The rest of the draft came with it: its key/value pairs and rules, not sent again.
    assert draft['mapping']['pairs'] and 'extract' not in draft['mapping']
    assert result['mapping_sent']['pairs'] == draft['mapping']['pairs']
    assert [r['name'] for r in result['mapping_sent']['events']] == [r['name'] for r in draft['mapping']['events']]


async def test_what_was_sent_or_saved_comes_before_the_draft():
    saved = {'common': [{'path': 'EventSource/System/Name', 'value': 'Saved'}], 'input': 'json', 'json_layout': 'lines',
             'events': [{'name': 'all', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'x'}]}]}
    xslt = {'type': 'XSLT', 'uuid': 'x-1', 'name': 'FG-Events',
            'description': with_mapping('', 'translation', {'mapping': saved, 'schema_version': '4.1.0'})}
    ctx, guard = world([xslt])
    with patches(guard):
        await generation.draft_translation_mapping(ctx, samples={'fw.jsonl': SAMPLE}, build='fg-v1')
        # The build's XSLT of that name, saved (from another session, say): its mapping, not the draft.
        result = await save(ctx, changes={'description': 'kept'}, build='fg-v1', name='FG-Events', sample=SAMPLE)
        assert 'changes_applied_to' not in result and system_name(result) == 'Saved'
        # A new name: the draft.
        result = await save(ctx, changes=SYSTEM, build='fg-v1', name='FG-Events-V2', sample=SAMPLE)
        assert result['changes_applied_to'] and system_name(result) == 'FortiOS'
        # Sent since (the draft changed once): changes go to that, not the draft again.
        result = await save(ctx, changes={'description': 'again'}, build='fg-v1', name='FG-Events-V2', sample=SAMPLE)
        assert 'changes_applied_to' not in result and system_name(result) == 'FortiOS'


async def test_with_no_draft_or_mapping_the_refusal_says_what_to_do():
    ctx, guard = world()
    with patches(guard), pytest.raises(ToolError, match='draft one, or give the whole mapping'):
        await generation.build_translation_xslt(ctx, changes=SYSTEM, build='fg-v1', name='FG-Events', sample=SAMPLE)
