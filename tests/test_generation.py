"""build_translation_xslt: a documentation table only from a sampled run, and saving the XSLT by reference."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tests.test_xsltgen import SCHEMA, mapping
from tools import generation
from utils.xsltgen import TranslationMapping, generate


async def test_no_field_mapping_without_a_sample():
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))})
    with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)), \
            patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})):
        result = await generation.build_translation_xslt(ctx, mapping())
    assert result['ok'] and result['xslt']
    # A table from the mapping alone shows how values are computed; it used to be copied into docs as it was.
    assert result['field_mapping'] is None
    assert 'pipeline_uuid and stream_ids' in result['field_mapping_needs']


async def test_a_single_stream_id_is_taken_as_a_list_of_one():
    # Clients send the model's arguments as they are; one sample stream often arrives as a bare number, which
    # failed validation ("invalid input") and left the documentation to be written by hand.
    from fastmcp import Client, FastMCP

    server = FastMCP('test', lifespan=None)
    server.tool(generation.build_translation_xslt)
    stepped = AsyncMock(return_value={})
    pipeline = SimpleNamespace(default_outputs=lambda: ['translationFilter'])
    with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)), \
            patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})), \
            patch.object(generation, 'gateway_from', lambda ctx: SimpleNamespace(
                settings=SimpleNamespace(event_logging_version='4.1.0'))), \
            patch.object(generation._Pipeline, 'load', AsyncMock(return_value=pipeline)), \
            patch.object(generation, 'read_sample_streams', AsyncMock(return_value=({}, []))), \
            patch.object(generation, '_outputs', stepped):
        async with Client(server) as client:
            result = await client.call_tool('build_translation_xslt', {
                'mapping': mapping().model_dump(exclude_none=True), 'pipeline_uuid': 'p-1', 'stream_ids': 15783601,
                'field_mapping': True})
    assert not result.is_error
    assert stepped.await_args.args[2] == [15783601]


def ctx_and_patches():
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))})
    return ctx, (patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)),
                 patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})))


async def test_saving_returns_the_document_not_the_code():
    # The XSLT need not pass through the model: generated, saved with its mapping, referred to by uuid.
    from tools import translation
    ctx, (schema, instructions) = ctx_and_patches()
    saved = {'type': 'XSLT', 'uuid': 'x-1', 'name': 'ACME-Events', 'version': 'v1', 'next': {'step': 'pipeline'}, 'done': False}
    with schema, instructions, patch.object(translation, 'create_xslt', AsyncMock(return_value=saved)) as create, \
            patch.object(translation, 'update_xslt', AsyncMock(return_value={**saved, 'version': 'v2'})) as update:
        result = await generation.build_translation_xslt(ctx, mapping(), build='acme-v1', name='ACME-Events')
        assert 'xslt' not in result and result['saved'] == {'type': 'XSLT', 'uuid': 'x-1', 'name': 'ACME-Events', 'version': 'v1'}
        assert result['next'] == {'step': 'pipeline'} and "uuid='x-1'" in result['hint']
        (_, build, name, code), kwargs = create.await_args
        assert (build, name) == ('acme-v1', 'ACME-Events') and code.startswith('<?xml') and kwargs['mapping'] == mapping()
        again = await generation.build_translation_xslt(ctx, mapping(), uuid='x-1', include_xslt=True)
        assert update.await_args.args[1] == 'x-1' and again['saved']['version'] == 'v2' and again['xslt']
        broken = mapping(events=[{'name': 'bare', 'fields': [{'path': 'EventDetail/Nope', 'value': 'x'}]}])
        failed = await generation.build_translation_xslt(ctx, broken, build='acme-v1', name='ACME-Events')
        assert not failed['ok'] and 'saved' not in failed and create.await_count == 1   # nothing saved
        with pytest.raises(ToolError, match='give name'):
            await generation.build_translation_xslt(ctx, mapping(), build='acme-v1')


async def test_save_xslt_generates_an_indexing_xslt_from_its_plan_and_takes_no_mapping():
    # A translation from a mapping is saved by build_translation_xslt; save_xslt's copy of the mapping schema was
    # ~4,500 tokens of tool definition in every model's context.
    import inspect
    from tools import translation
    from utils.fieldplan import FieldPlan, PlannedField
    assert 'mapping' not in inspect.signature(translation.save_xslt).parameters
    plan = FieldPlan(backend='lucene', index_name='acme', time_field='EventTime',
                     fields=[PlannedField(name='StreamId', type='id', source='@StreamId'),
                             PlannedField(name='EventTime', type='date', source='EventTime/TimeCreated')])
    with patch.object(translation, 'create_xslt', AsyncMock(return_value={'uuid': 'i-1'})) as create:
        await translation.save_xslt(SimpleNamespace(), 'acme-v1', 'ACME-INDEX-XSLT', index_plan=plan)
        assert create.await_args.args[3] == plan.xslt() and create.await_args.args[5] == plan
        with pytest.raises(ToolError, match='build_translation_xslt'):
            await translation.save_xslt(SimpleNamespace(), 'acme-v1', 'ACME-Events')


async def test_tools_read_the_sample_from_its_streams_instead_of_its_text():
    # The text goes through the model once, to upload_sample; the tools after it take stream_ids.
    from tools import feeds
    csv = 'time,user,action\n2026-10-01T10:00:00Z,alice,login\n2026-10-01T10:05:00Z,bob,logout\n'
    read = AsyncMock(return_value=({'stream 7': csv}, []))
    ctx, (schema, instructions) = ctx_and_patches()
    with schema, instructions, patch.object(generation, 'read_sample_streams', read), \
            patch.object(feeds, 'read_sample_streams', read), \
            patch.object(generation, 'gateway_from', lambda ctx: SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))):
        splitter = await generation.build_data_splitter(ctx, stream_ids=[7])
        assert splitter['records'] == 2 and splitter['spec']['kind'] == 'delimited'
        draft = await generation.draft_translation_mapping(ctx, stream_ids=[7])
        assert draft['mapping']['input'] == 'data_splitter'
        assert (await feeds.profile_sample(ctx, stream_ids=[7]))['format'].startswith('delimited')
        typo = mapping(common=[{'path': 'EventTime/TimeCreated', 'field': 'tiem'}] + mapping().common[1:])
        from utils.dsgen import SplitterSpec
        checked = await generation.build_translation_xslt(ctx, typo, stream_ids=[7],
                                                          splitter=SplitterSpec.model_validate(splitter['spec']))
        # Checked against the stream's records: a near miss of a sample field is a problem, so nothing is generated.
        assert any("'tiem'" in p and "did you mean ['time']" in p for p in checked['problems']) and not checked['ok']
    assert [c.args[1] for c in read.await_args_list] == [[7]] * 4


async def test_raw_streams_are_read_in_pages_up_to_the_limit():
    from tools import streams
    text = ''.join(f'line {n}\n' for n in range(100))
    pages = []

    async def post(path, body):
        start = body['sourceLocation']['dataRange']['charOffsetFrom']
        pages.append(start)
        return {'data': text[start:start + body['sourceLocation']['dataRange']['length'] + 1]}   # Stroom returns one more
    stroom = SimpleNamespace(fetch_data=AsyncMock(return_value={'data': text[:50], 'totalCharacterCount': {'count': len(text)}}),
                             post=post)
    with patch.object(streams, 'RAW_PAGE_CHARS', 200):
        whole, truncated = await streams.raw_text(stroom, 7, 10_000)
        assert whole == text and not truncated and len(pages) > 1
        head, truncated = await streams.raw_text(stroom, 7, 300)
        assert truncated and text.startswith(head) and head.endswith('\n') and len(head) <= 300


async def test_stroom_s_placeholder_past_the_end_is_not_read_as_data():
    from tools import streams
    csv = 'ts,evt\n2026-10-01T08:00:00Z,4624\n'
    # The count says a character more than the first page holds; the range past the end comes back as Stroom's
    # placeholder, which would end a CSV sample with a line of one column.
    stroom = SimpleNamespace(fetch_data=AsyncMock(return_value={'data': csv, 'totalCharacterCount': {'count': len(csv) + 1}}),
                             post=AsyncMock(return_value={'data': '## No Data ##'}))
    text, truncated = await streams.raw_text(stroom, 7, 10_000)
    assert '## No Data ##' not in text and text.startswith(csv.rstrip())
    # Nor is the count's extra character a sign the text was cut: a sample with no final newline keeps its last line
    # (seen: eval case 16's JSON array, cut back to its last line end, failed to parse).
    array = '[{"a": 1},\n {"a": 2}]'
    stroom.fetch_data = AsyncMock(return_value={'data': array, 'totalCharacterCount': {'count': len(array) + 1}})
    assert await streams.raw_text(stroom, 7, 10_000) == (array, False)


async def test_keeping_unknown_needs_the_users_confirmation_before_saving():
    from tools import translation
    from utils.consent import ConsentStore
    kept = mapping(events=[mapping().events[0].model_dump(exclude_none=True),
                           {'name': 'status', 'when': [{'field': 'action', 'equals': 'status'}],
                            'allow_unknown': 'status lines carry no activity',
                            'fields': [{'path': 'EventDetail/TypeId', 'value': 'status'},
                                       {'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]},
                           mapping().events[-1].model_dump(exclude_none=True)])
    ctx, (schema, instructions) = ctx_and_patches()
    ctx.lifespan_context['consent'] = ConsentStore(use_elicitation=False)
    # A CSV sample with no splitter: the spec is inferred, so the check sees the records.
    sample = ('time,action,user,result,sid,host,port\n2026-09-28T10:00:00.000Z,login,alice,ok,s1,ws01,22\n'
              '2026-09-28T10:01:00.000Z,status,,,,ws01,0\n2026-09-28T10:02:00.000Z,status,,,,ws02,0\n'
              '2026-09-28T10:03:00.000Z,login,bob,fail,s2,ws03,22\n')
    with schema, instructions, patch.object(translation, 'create_xslt', AsyncMock(return_value={
            'type': 'XSLT', 'uuid': 'x-1', 'name': 'N', 'version': 'v'})) as create:
        # Seen in VS Code: asked with "no sample records checked", the user couldn't see what went to Unknown.
        with pytest.raises(ToolError, match='call again with stream_ids'):
            await generation.build_translation_xslt(ctx, kept, build='b', name='N')
        asked = await generation.build_translation_xslt(ctx, kept, build='b', name='N', sample=sample)
        assert asked['status'] == 'needs_confirmation' and not create.await_count
        assert asked['summary'].endswith('in the saved XSLT: status: 2 records (action: status)'), asked['summary']
        assert asked['details']["rule 'status'"] == {
            'reason given': 'status lines carry no activity', 'sample records it keeps Unknown': '2 of 4',
            'what they hold': asked['details']["rule 'status'"]['what they hold']}
        assert 'action: status' in asked['details']["rule 'status'"]['what they hold']
        done = await generation.build_translation_xslt(ctx, kept, build='b', name='N', sample=sample,
                                                      confirmation_id=asked['confirmation_id'])
        assert done['saved']['uuid'] == 'x-1'
        # Checking without saving asks nothing.
        assert 'status' not in await generation.build_translation_xslt(ctx, kept)
        # A rule keeping none of the sample Unknown holds nothing to agree to: saved without asking (seen: the
        # user asked to keep 'other' as Unknown for "none of the 1000" records).
        logins = sample.replace(',status,,,,', ',login,carol,ok,s3,')
        create.reset_mock()
        saved = await generation.build_translation_xslt(ctx, kept, build='b', name='N', sample=logins)
        assert saved['saved']['uuid'] == 'x-1' and create.await_count == 1


async def test_a_field_an_inferred_splitter_lacks_is_a_warning_not_a_refusal():
    # Seen: a hand-written syslog splitter names a field 'pwd'; the sample, read with the spec inferred from it,
    # has 'pid' instead, and the mapping was refused though the build's converter does write 'pwd'.
    m = mapping()
    m.common.append(m.common[0].model_copy(update={'path': 'EventSource/Device/Name', 'field': 'hosts',
                                                   'value': None, 'time_format': None}))
    sample = ('time,action,user,result,sid,host,port\n2026-09-28T10:00:00.000Z,login,alice,ok,s1,ws01,22\n'
              '2026-09-28T10:03:00.000Z,login,bob,fail,s2,ws03,22\n')
    ctx, (schema, instructions) = ctx_and_patches()
    with schema, instructions:
        guessed = await generation.build_translation_xslt(ctx, m, sample=sample)
        assert guessed['ok'], guessed['problems']
        assert any("field 'hosts'" in w and 'inferred Data Splitter' in w for w in guessed['warnings'])
        given = await generation.build_translation_xslt(ctx, m, sample=sample, splitter={'kind': 'delimited', 'header': True})
        assert not given['ok'] and any("field 'hosts'" in p for p in given['problems'])


async def test_a_mapping_refused_for_its_shape_says_what_is_missing_and_shows_a_whole_one():
    # Seen (a VS Code session): input left out, a rule without name, and an xpath run into its path's string
    # ('EventDetail/View/User/Id**,xpath:'), answered with "Field required" and "give exactly one of".
    from tools.generation import MAPPING_EXAMPLE
    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))})
    bad = {'events': [{'name': 'a'}, {'fields': [{'path': 'EventDetail/View/User/Id**,xpath: "User"'}]}]}
    with pytest.raises(ToolError) as raised:
        await generation.build_translation_xslt(ctx, bad)
    text = str(raised.value)
    assert "input: Field required (What the XSLT reads: data_splitter" in text
    assert "events[1].name: Field required (Short name for this kind of event, e.g. 'logon'.)" in text
    assert "holds more than a path" in text and '{"path": "EventDetail/View/User/Id", "xpath": "<the xpath>"}' in text
    assert json.dumps(MAPPING_EXAMPLE) in text
    # A value problem alone gets no example: the shape is right
    with pytest.raises(ToolError) as raised:
        await generation.build_translation_xslt(ctx, {**MAPPING_EXAMPLE, 'common': [{'path': 'EventSource/System/Name', 'field': 'a', 'any_of': ['b']}]})
    assert 'given field and any_of' in str(raised.value) and 'any_of alone' in str(raised.value)
    assert 'A whole mapping looks like' not in str(raised.value)


def test_the_example_mapping_generates():
    from tools.generation import MAPPING_EXAMPLE
    result = generate(TranslationMapping.model_validate(MAPPING_EXAMPLE), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
