"""The plan for a build on data already in Stroom: an existing feed's records, a production pipeline's new version or
working copy. Seen in VS Code: `next` said create_feed and upload_sample whatever the build was, so the agent uploaded
a workspace file though the user chose production streams, and made a new feed to build an indexing pipeline's next
version."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tools import plan


def _doc(kind, uuid, name, **extra):
    return {'type': kind, 'uuid': uuid, 'name': name, 'path': 'b', 'working_copy_of': None, 'version_of': None, **extra}


async def status(docs, sources=(), stages=None, mappings=None, stepped=False, verified=False, own_raw=False,
                 survey=None, checks=(), kept=None):
    stroom = SimpleNamespace(
        get_doc=AsyncMock(side_effect=lambda t, u: {'uuid': u, 'name': {'prod-ev': 'ACME-V1.0-Events'}.get(u, u),
                                                    'description': u}),
        pipeline_layers=AsyncMock(return_value=[]),
        find_meta=AsyncMock(return_value={'values': []}))
    with patch('tools.builds._build_docs', AsyncMock(return_value=docs)), \
            patch('tools.builds.build_checks', AsyncMock(return_value=list(checks))), \
            patch('tools.builds.kept_mapping', AsyncMock(return_value=kept)), \
            patch.object(plan, '_survey_state', AsyncMock(return_value=survey)), \
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
    # Processed where it is, into a test feed the server makes (asked for by the user); then indexed, as a new source is.
    assert states(result)['processed'] == 'to do' and states(result)['index'] == 'to do'


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


async def test_an_onboarded_existing_feed_goes_on_to_indexing_after_its_documentation():
    # Seen in Ask Stroom AI: the user asked for an index, and after the events pipeline's documentation, and again
    # after save_xslt index_plan=, next said "step 9 of 9, promoted": a build on an existing feed counted indexing only
    # once it held an index doc or an indexing pipeline.
    docs = [_doc('Documentation', 's', 'ACME - Survey'), _doc('Pipeline', 'ev', 'ACME-Events'),
            _doc('XSLT', 'x', 'ACME-Events'), _doc('Documentation', 'd', 'ACME-Events')]
    args = dict(sources=['ACME'], stages={'ev': 'translation'}, stepped=True,
                survey={'saturated': True, 'locations': None, 'streams': [5]})
    result = await status(docs, mappings={'x': ('translation', {})}, **args)
    step = states(result)
    assert step['index'] == step['indexing_pipeline'] == step['indexed'] == step['index_documented'] == 'to do'
    assert step['index_template'] == 'not needed'     # until the backend is known to be Elasticsearch
    assert result['next']['progress'].startswith('step 5 of 12, processed')
    with_plan = await status(docs + [_doc('XSLT', 'ixx', 'ACME - Indexing')],
                             mappings={'x': ('translation', {}), 'ixx': ('index', {})}, **args)
    assert states(with_plan)['index'] == 'to do'


async def test_indexing_an_existing_feeds_events_counts_from_its_indexing_xslt():
    # save_xslt index_plan= before the index doc (as the agent did): the indexing steps are counted from then.
    result = await status([_doc('XSLT', 'ixx', 'ACME - Indexing')], sources=['ACME-EVENTS'],
                          mappings={'ixx': ('index', {})})
    assert result['next']['step'] == 'index' and states(result)['indexed'] == 'to do'


async def test_a_surveyed_feed_with_more_locations_than_next_can_give_is_stepped_over_its_streams():
    # Seen in Ask Stroom AI: 66 locations, and create_pipeline's next said step_records with a placeholder for them,
    # while the plan's step says step_sample.
    docs = [_doc('Documentation', 's', 'ACME - Survey'), _doc('Pipeline', 'ev', 'ACME-Events'), _doc('XSLT', 'x', 'X')]
    result = await status(docs, sources=['ACME'], stages={'ev': 'translation'}, mappings={'x': ('translation', {})},
                          survey={'saturated': True, 'locations': None, 'streams': [5]})
    assert result['next']['call'] == {'tool': 'step_sample', 'arguments': {'pipeline_uuid': 'ev', 'stream_ids': [5]}}


async def test_a_new_sources_survey_of_its_own_feed_keeps_indexing_to_do():
    # Seen: onboarding surveyed its sample with the build, and the plan took it for an existing feed and skipped
    # indexing. source_feeds leaves the build's own feed out, so nothing makes this build one on existing data.
    docs = [_doc('Feed', 'f', 'ACME'), _doc('Documentation', 's', 'ACME - Survey'),
            _doc('Pipeline', 'ev', 'ACME-Events'), _doc('XSLT', 'x', 'ACME-Events'), _doc('Documentation', 'd', 'ACME-Events')]
    result = await status(docs, stages={'ev': 'translation'}, mappings={'x': ('translation', {})}, stepped=True,
                          own_raw=True)
    step = states(result)
    assert step['index'] == step['indexing_pipeline'] == step['indexed'] == 'to do'
    assert step['covered'] == 'not needed' and step['cef_pipeline'] == 'not needed'


@pytest.mark.parametrize('sources', [['ACME-EVENTS'], []])
async def test_a_cef_build_goes_from_its_mapping_to_its_pipeline_and_promotion(sources):
    # CEF output had no steps: next never reached promotion, and without start_build feeds= it said create_feed.
    docs = [_doc('XSLT', 'c', 'ACME-CEF')]
    result = await status(docs, sources=sources, mappings={'c': ('cef', {})})
    step = states(result)
    assert step['cef_mapping'] == 'done' and result['next']['step'] == 'cef_pipeline'
    assert step['feed'] == step['translation'] == step['index'] == 'not needed'
    docs += [_doc('Pipeline', 'cp', 'ACME-CEF'), _doc('Documentation', 'd', 'ACME-CEF')]
    done = await status(docs, sources=sources, stages={'cp': 'forwarding'}, mappings={'c': ('cef', {})}, stepped=True)
    assert done['next']['step'] == 'promoted'


async def test_a_discovery_build_is_known_by_its_plan_whatever_its_parser():
    # An XML discovery pipeline from an XMLParser indexing template reads as indexing: its plan says discovery.
    docs = [_doc('Feed', 'f', 'RAW'), _doc('Pipeline', 'dp', 'RAW-Discovery'), _doc('XSLT', 'dx', 'RAW-Discovery')]
    result = await status(docs, stages={'dp': 'indexing'}, mappings={'dx': ('index', {'discovery': {'input': 'xml'}})},
                          stepped=True, own_raw=True)
    step = states(result)
    assert step['translation'] == step['processed'] == 'not needed' and result['next']['step'] == 'index_template'
    call, _ = plan.next_call('indexed', 'b', ['RAW'], [1], [1], None, 'dp', discovery=True)
    assert 'source_pipeline_uuid' not in call['arguments']   # raw streams: refused with one


async def test_an_existing_feed_is_stepped_where_it_is_then_surveyed_until_covered():
    locations = [{'stream': 5, 'part': 0, 'record': 3}]
    docs = [_doc('Documentation', 's', 'ACME - Survey'), _doc('Pipeline', 'ev', 'ACME-Events'), _doc('XSLT', 'x', 'X')]
    args = dict(sources=['ACME'], stages={'ev': 'translation'}, mappings={'x': ('translation', {})})
    result = await status(docs, survey={'saturated': False, 'locations': locations}, **args)
    assert result['next']['call'] == {'tool': 'step_records', 'arguments': {'pipeline_uuid': 'ev', 'locations': locations}}
    stepped = await status(docs, survey={'saturated': False, 'locations': locations}, stepped=True, **args)
    assert stepped['next']['step'] == 'covered' and stepped['next']['call']['tool'] == 'survey_feed'
    assert stepped['next']['call']['arguments'] == {'feed': 'ACME', 'build': 'b'}
    covered = await status(docs, survey={'saturated': True, 'locations': locations, 'streams': [5]}, stepped=True,
                           **args)
    # Then the surveyed streams are processed where they are (their Events go to the build's test feed).
    assert covered['next']['step'] == 'processed'
    assert covered['next']['call'] == {'tool': 'create_processor_filter',
                                       'arguments': {'pipeline_uuid': 'ev', 'stream_ids': [5]}}


async def test_indexing_an_existing_feeds_events_names_them_and_their_pipeline():
    docs = [_doc('Index', 'i', 'acme-v1')]
    result = await status(docs, sources=['ACME-EVENTS'])
    assert result['next']['step'] == 'indexing_pipeline' and 'events_stream_ids=' in result['next']['then']
    docs += [_doc('Pipeline', 'ix', 'ACME-Indexing'), _doc('XSLT', 'ixx', 'ACME-Indexing')]
    indexed = await status(docs, sources=['ACME-EVENTS'], stages={'ix': 'indexing'}, mappings={'ixx': ('index', {})},
                           stepped=True)
    assert indexed['next']['step'] == 'indexed'
    assert indexed['next']['call']['arguments']['source_pipeline_uuid'].startswith('<the events pipeline that wrote')


async def test_a_working_copy_of_an_indexing_pipeline_is_not_indexed_in_the_build():
    # A change upstream reaches it: stepped clean in the build; indexing production Events is the user's. Its
    # documentation is the production pipeline's, under its name.
    docs = [_doc('Pipeline', 'wc', 'ACME-Indexing copy', working_copy_of='prod-ix'), _doc('XSLT', 'x', 'X'),
            _doc('Documentation', 'd', 'prod-ix')]
    result = await status(docs, sources=['ACME'], stages={'wc': 'indexing'}, mappings={'x': ('index', {})},
                          stepped=True)
    assert states(result)['indexed'] == states(result)['index_documented'] == 'done'


async def test_a_hand_edited_translation_is_read_back_before_it_is_documented():
    # Code a mapping can't express, saved with save_xslt: the docs stayed stale and next said write_documentation
    # again and again.
    docs = [_doc('Pipeline', 'ev', 'ACME-V1.1-Events', version_of='prod-ev'), _doc('XSLT', 'x', 'X')]
    result = await status(docs, sources=['ACME'], stages={'ev': 'translation'}, mappings={'x': ('translation', {})},
                          stepped=True, kept={'xslt': {'uuid': 'x'}},
                          checks=["Pipeline 'ACME-V1.1-Events': its XSLT differs from what its mapping generates: ..."])
    assert result['next']['step'] == 'documented'
    assert result['next']['call'] == {'tool': 'rebuild_mapping', 'arguments': {'uuid': 'x', 'pipeline_uuid': 'ev'}}


async def test_an_existing_feeds_format_is_read_from_its_survey():
    # Seen (e2e_existing_feed): with no raw stream of its own, the plan profiled the survey's examples and failed on a
    # key the profile doesn't have.
    docs = [_doc('Documentation', 's', 'ACME - Survey'), _doc('Pipeline', 'ev', 'ACME-Events'), _doc('XSLT', 'x', 'X')]
    survey = {'saturated': False, 'locations': None, 'streams': [5], 'sample': 'time,user,action\n10:00,alice,login\n'}
    result = await status(docs, sources=['ACME'], survey=survey, stages={'ev': 'translation'},
                          mappings={'x': ('translation', {})})
    assert states(result)['converter'] == 'to do'      # delimited text needs a Data Splitter
