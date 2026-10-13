"""The plan for a build on data already in Stroom: an existing feed's records, a production pipeline's new version or
working copy. Seen in VS Code: `next` said create_feed and upload_sample whatever the build was, so the agent uploaded
a workspace file though the user chose production streams, and made a new feed to build an indexing pipeline's next
version."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tools import plan


def _doc(kind, uuid, name, **extra):
    return {'type': kind, 'uuid': uuid, 'name': name, 'path': 'b', 'working_copy_of': None, 'version_of': None, **extra}


async def status(docs, sources=(), stages=None, mappings=None, stepped=False, verified=False, own_raw=False):
    stroom = SimpleNamespace(
        get_doc=AsyncMock(side_effect=lambda t, u: {'uuid': u, 'name': {'prod-ev': 'ACME-V1.0-Events'}.get(u, u),
                                                    'description': u}),
        pipeline_layers=AsyncMock(return_value=[]),
        find_meta=AsyncMock(return_value={'values': []}))
    with patch('tools.builds._build_docs', AsyncMock(return_value=docs)), \
            patch('tools.builds.build_checks', AsyncMock(return_value=[])), \
            patch.object(plan, 'gateway_from', lambda c: stroom), \
            patch.object(plan, 'guard_from', lambda c: None), \
            patch.object(plan, 'source_feeds', AsyncMock(return_value=list(sources))), \
            patch.object(plan, 'sample_streams', AsyncMock(return_value={'Raw Events': [{'id': 7}]} if own_raw else {})), \
            patch.object(plan, 'sample_format', AsyncMock(return_value=None)), \
            patch('utils.mappingstore.read_mapping', lambda d: (mappings or {}).get(d)), \
            patch('tools.templates._shape', AsyncMock(side_effect=lambda s, u: {'stage': (stages or {})[u], 'parser': None})), \
            patch('tools.pipeline_writes.open_slots', AsyncMock(return_value=[])), \
            patch('tools.pipelines.merge_layers', lambda layers: {}), \
            patch('tools.stepping.stepped_clean', AsyncMock(return_value=stepped)), \
            patch('tools.stepping.verified', AsyncMock(return_value=verified)), \
            patch('tools.stepping.validated', AsyncMock(return_value=False)), \
            patch('tools.processing_writes.elastic_destination', AsyncMock(return_value=None)), \
            patch('tools.processing_writes.agreement_problem', AsyncMock(return_value=None)), \
            patch('tools.processing.processing_status', AsyncMock(return_value={'filters': []})):
        return await plan.status(None, 'b')


def states(result):
    return {s['step']: s['state'] for s in result['steps']}


async def test_an_existing_feed_needs_no_feed_or_upload_before_any_pipeline():
    result = await status([], sources=['ACME'])
    assert result['next']['step'] == 'existing data' and result['works_from'] == ['ACME']
    assert result['next']['call'] == {'tool': 'find_streams', 'arguments': {'feed': 'ACME'}}
    assert 'create no feed and upload nothing' in result['next']['then']
    assert states(result)['feed'] == 'not needed' and states(result)['samples'] == 'not needed'


async def test_a_surveyed_feed_goes_on_to_the_translation_not_a_feed():
    result = await status([_doc('Documentation', 's', 'ACME - Survey')], sources=['ACME'])
    assert result['next']['step'] == 'translation'
    assert states(result)['processed'] == 'not needed' and states(result)['index'] == 'not needed'


async def test_a_new_version_of_an_events_pipeline_steps_production_records():
    docs = [_doc('Pipeline', 'ev2', 'ACME-V1.1-Events', version_of='prod-ev'), _doc('XSLT', 'x2', 'ACME-V1.1-Events')]
    result = await status(docs, sources=['ACME'], stages={'ev2': 'translation'}, mappings={'x2': ('translation', {})})
    assert result['next']['step'] == 'stepped' and result['next']['call']['tool'] == 'step_sample'
    assert result['next']['call']['arguments']['stream_ids'] == '<recent raw streams of ACME (find_streams)>'
    # Stepped clean: documented next, then promotion; nothing about processing a feed of its own or indexing.
    done = await status(docs, sources=['ACME'], stages={'ev2': 'translation'}, mappings={'x2': ('translation', {})},
                        stepped=True)
    assert done['next']['step'] == 'documented'
    assert states(done)['index_documented'] == 'not needed'


async def test_a_working_copy_is_documented_under_the_production_pipelines_name():
    docs = [_doc('Pipeline', 'wc', 'ACME-V1.0-Events copy', working_copy_of='prod-ev'), _doc('XSLT', 'x', 'X'),
            _doc('Documentation', 'd', 'ACME-V1.0-Events')]
    result = await status(docs, stages={'wc': 'translation'}, stepped=True)
    assert result['next']['step'] == 'promoted'


async def test_a_new_indexing_version_needs_no_feed_and_no_events_pipeline():
    docs = [_doc('Pipeline', 'ix2', 'ACME-Indexing-v2', version_of='ix1'), _doc('XSLT', 'ix2-x', 'ACME-v2-XSLT')]
    result = await status(docs, stages={'ix2': 'indexing'}, mappings={'ix2-x': ('index', {})}, stepped=True)
    step = states(result)
    assert step['feed'] == step['translation'] == step['documented'] == 'not needed'
    assert step['index'] == 'done' and result['next']['step'] == 'indexed'
    assert result['next']['call']['tool'] == 'create_processor_filter'
    assert result['next']['call']['arguments']['stream_ids'].startswith('<recent Events streams from')


async def test_a_test_feed_in_such_a_build_brings_processing_back():
    docs = [_doc('Feed', 'f', 'ACME-COPY'), _doc('Pipeline', 'ev2', 'ACME-V1.1-Events', version_of='prod-ev'),
            _doc('XSLT', 'x2', 'X')]
    result = await status(docs, sources=['ACME'], stages={'ev2': 'translation'}, mappings={'x2': ('translation', {})},
                          stepped=True, own_raw=True)
    assert result['next']['step'] == 'processed'


async def test_a_new_source_still_starts_with_its_feed():
    assert (await status([]))['next']['step'] == 'feed'


async def test_start_build_records_the_existing_feeds_it_works_from():
    from tools import builds
    guard = SimpleNamespace(build_folder=AsyncMock(return_value={'_path': 'System/MCP Workspace/b', 'uuid': 'bf'}),
                            build_of=AsyncMock(side_effect=lambda ref: 'b' if ref['uuid'] == 'test' else None))
    named = {'ACME': [{'type': 'Feed', 'uuid': 'prod', 'name': 'ACME'}],
             'ACME-COPY': [{'type': 'Feed', 'uuid': 'test', 'name': 'ACME-COPY'}], 'NEW': []}
    recorded = AsyncMock()
    from config import Settings
    stroom = SimpleNamespace(settings=Settings(stroom_url='http://s', cluster_name='PROD'))
    with patch.object(builds, 'guard_from', lambda c: guard), patch.object(builds, 'gateway_from', lambda c: stroom), \
            patch('tools.feeds.feeds_named', AsyncMock(side_effect=lambda s, n: named[n])), \
            patch.object(builds.build_record, 'add_source_feeds', recorded), \
            patch.object(builds, 'applicable_instructions', AsyncMock(return_value={})), \
            patch('tools.plan.next_step', AsyncMock(return_value=None)):
        result = await builds.start_build(None, 'b', feeds=['ACME', 'ACME-COPY', 'NEW'])
    # Only a feed outside the build counts: the build's own (a test feed) and one not made yet don't.
    assert result['works_from'] == ['ACME'] and recorded.await_args.args[2] == ['ACME']
    # One not here is said, naming the cluster: the user may mean another.
    assert result['not_found'].startswith("No feed named 'NEW' in Stroom 'PROD' (http://s).")
