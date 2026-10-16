"""The event-logging version a translation writes: the user's choice from those Stroom holds, the newest recommended.
Seen: the server defaulted to 3.5.2 on a Stroom that didn't hold it."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tools import generation
from utils.eventschema import EventSchema
from utils.schemas import SchemaCache

INSTALLED = SchemaCache.event_versions      # the real one: conftest pins Stroom to one version for the other tests
SCHEMA = EventSchema.parse((Path(__file__).parent / 'fixtures' / 'event-logging-v4.1.0.xsd').read_bytes())
CSV = 'time,user,action\n2026-10-01T10:00:00Z,alice,logon\n2026-10-01T10:05:00Z,bob,logoff\n'


class Choosing:
    """A consent store whose form the user answers with the option at `pick`."""
    def __init__(self, pick: int):
        self.pick, self.asked = pick, []

    async def choose(self, ctx, action, question, options):
        self.asked.append(options)
        return options[self.pick]


def _ctx(consent=None):
    return SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace()),
                                             **({'consent': consent} if consent else {})})


def _holding(*versions):
    async def installed(self):
        return list(versions)
    return patch.object(SchemaCache, 'event_versions', installed)


async def test_the_versions_stroom_holds_come_newest_first():
    found = {'values': [{'docRef': {'type': 'XMLSchema', 'uuid': u}} for u in ('a', 'b', 'c', 'd')]}
    ids = {'a': 'file://event-logging-v3.5.2.xsd', 'b': 'file://event-logging-v4.0.2.xsd',
           'c': 'file://records-v2.0.xsd', 'd': 'file://event-logging-v3.10.0.xsd'}
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value=found),
                             get=AsyncMock(side_effect=lambda path: {'systemId': ids[path.rsplit('/', 1)[-1]]}))
    assert await INSTALLED(SchemaCache(stroom)) == ['4.0.2', '3.10.0', '3.5.2']    # by number, not as text


async def test_without_forms_the_draft_asks_first_offering_the_newest():
    read = AsyncMock()
    with _holding('4.1.0', '4.0.2', '3.5.2'), patch.object(generation, 'read_sample_streams', read):
        asked = await generation.draft_translation_mapping(_ctx(), stream_ids=[7], source_name='Acme')
    assert asked['status'] == 'needs_guidance'
    assert asked['options'] == ['4.1.0', '4.0.2', '3.5.2'] and asked['recommended'] == '4.1.0'
    assert 'schema_version=' in asked['hint']
    read.assert_not_awaited()           # nothing is read before the user answers


async def test_the_users_pick_in_the_form_is_drafted_for_and_not_asked_again():
    consent = Choosing(pick=0)
    ctx = _ctx(consent)
    with _holding('4.1.0', '3.5.2'), patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)):
        drafted = await generation.draft_translation_mapping(ctx, {'acme.csv': CSV}, 'Acme', build='b1')
        assert consent.asked == [['4.1.0 (newest installed, recommended)', '3.5.2']]
        assert drafted['schema_version'] == '4.1.0' and 'schema_version=this schema_version' in drafted['hint']
        # build_translation_xslt, given no schema_version, writes the version chosen for the build.
        assert await generation.target_version(ctx, None, 'b1') == '4.1.0'
        assert len(consent.asked) == 1
        # Another build is asked about afresh.
        consent.pick = 1
        assert await generation.target_version(ctx, None, 'b2') == '3.5.2' and len(consent.asked) == 2


async def test_a_version_stroom_doesnt_hold_is_refused_with_those_it_does():
    with _holding('4.1.0', '4.0.2'), pytest.raises(ToolError, match=r'no event-logging schema v3\.5\.2; it holds 4\.1\.0, 4\.0\.2'):
        await generation.target_version(_ctx(), '3.5.2', 'b1')


async def test_a_4x_version_is_offered_as_much_as_a_3x_one():
    # Seen: refused 3.5.2, an agent took 3.4.2, the nearest 3.x, where 4.1.0 was installed: namespace
    # event-logging:3 read as a 3.x schema.
    with _holding('4.1.0', '3.4.2'):
        with pytest.raises(ToolError) as refused:
            await generation.target_version(_ctx(), '3.5.2', 'b1')
        asked = await generation.target_version(_ctx(), None, 'b1')
    assert 'not the nearest to the one asked for' in str(refused.value)
    for text in (str(refused.value), asked['hint']):
        assert "4.x as much as 3.x, writes namespace event-logging:3" in text


async def test_a_schema_stroom_doesnt_hold_is_refused_with_its_versions_newest_first():
    found = {'values': [{'docRef': {'type': 'XMLSchema', 'uuid': u}} for u in ('a', 'b', 'c')]}
    ids = {'a': 'file://event-logging-v3.4.2.xsd', 'b': 'file://event-logging-v4.1.0.xsd', 'c': 'file://records-v2.0.xsd'}
    stroom = SimpleNamespace(find_documents=AsyncMock(return_value=found),
                             get=AsyncMock(side_effect=lambda path: {'systemId': ids[path.rsplit('/', 1)[-1]]}))
    with pytest.raises(ToolError, match=r"newest first: 4\.1\.0, 3\.4\.2 \(all in namespace event-logging:3"):
        await SchemaCache(stroom).source('file://event-logging-v3.5.2.xsd')


async def test_with_one_version_installed_nothing_is_asked():
    consent = Choosing(pick=0)
    with _holding('4.0.2'):
        assert await generation.target_version(_ctx(consent), None, 'b1') == '4.0.2'
    assert consent.asked == []


async def test_no_event_logging_schema_at_all_says_to_import_it():
    with _holding(), pytest.raises(ToolError, match='holds no event-logging XML schema'):
        await generation.target_version(_ctx(), None, 'b1')


async def test_an_existing_translation_keeps_its_own_version():
    code = '<Events xsi:schemaLocation="event-logging:3 file://event-logging-v3.5.2.xsd" Version="3.5.2">'
    with _holding('4.1.0', '3.5.2'):
        ctx = _ctx()
        assert await generation.translation_version(ctx, {'schema_version': '4.0.2'}, code) == '4.0.2'   # kept with it
        assert await generation.translation_version(ctx, {}, code) == '3.5.2'           # the one its code writes
        assert await generation.translation_version(ctx, None, '<xsl:stylesheet/>') == '4.1.0'   # else the newest


async def test_a_saved_translation_records_the_version_it_was_generated_for():
    # Seen before: the kept mapping said the configured 3.5.2 for an XSLT generated for 4.1.0, so the save warned of a
    # hand edit, and regenerating from it would have changed the version back.
    from tools import translation
    from utils.mappingstore import read_mapping
    from tests.test_xsltgen import mapping as acme
    from utils.xsltgen import generate
    mapping = acme()
    code = generate(mapping, SCHEMA, '4.0.2')['xslt']
    doc = {'description': 'Acme events'}
    with _holding('4.1.0', '4.0.2'), patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)):
        extra = await translation._described(_ctx(), doc, code, mapping, None)
    assert read_mapping(doc['description'])[1]['schema_version'] == '4.0.2'
    assert extra.get('matches_mapping') is True


async def test_changes_to_a_saved_xslt_keep_its_version_and_ask_nothing():
    from tests.test_xsltgen import mapping as acme
    from tools import translation
    from utils.mappingstore import with_mapping
    kept = with_mapping('Acme events', 'translation', {'schema_version': '3.5.2',
                                                       'mapping': acme().model_dump(exclude_none=True)})
    gateway = SimpleNamespace(settings=SimpleNamespace(), get_doc=AsyncMock(return_value={'description': kept}))
    saved = {'type': 'XSLT', 'uuid': 'x-1', 'name': 'ACME-Events', 'version': 'v2'}
    schema = AsyncMock(return_value=SCHEMA)
    consent = Choosing(pick=0)
    with _holding('4.1.0', '3.5.2'), patch.object(generation, 'event_schema', schema), \
            patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})), \
            patch.object(generation, 'gateway_from', lambda c: gateway), \
            patch.object(translation, 'update_xslt', AsyncMock(return_value=saved)):
        result = await generation.build_translation_xslt(_ctx(consent), changes={'unmatched': 'warn'}, uuid='x-1')
        assert result['saved']['version'] == 'v2' and result['schema_version'] == '3.5.2'
        assert consent.asked == []
        # An XSLT that names no version (made empty) is asked about like a new translation.
        gateway.get_doc = AsyncMock(return_value={'description': '', 'data': ''})
        asked = await generation.build_translation_xslt(_ctx(), acme(), uuid='x-2')
    assert asked['status'] == 'needs_guidance' and asked['recommended'] == '4.1.0'
