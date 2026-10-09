"""Off the path: what the server says at each point an agent can reach, including the wrong moves real clients made.

Part A walks the plan's states, in order and out of it: for each state of a build, the step `next` names and
its call. Part B is the wrong moves: each is refused (or held at a gate), and the message names the call that
puts it right, so an agent that reads it gets back on the path.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tools import builds, indexing, pipeline_writes, plan, processing_writes
from utils.consent import ConsentStore
from utils.fieldplan import FieldPlan, PlannedField

# Part A: the plan's states ---------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _events_streams():
    """The streams these tests index are Events: the stream-type check has its own test (tests/test_formats.py)."""
    from unittest.mock import AsyncMock, patch as _patch
    with _patch('tools.indexing.require_events', AsyncMock()):
        yield


DONE = {
    'feed': True, 'samples': True, 'converter': True, 'translation': True, 'pipeline': True, 'ev_stepped': True,
    'events': True, 'ev_validated': True, 'ev_documented': True, 'index_doc': True, 'ix_pipeline': True, 'ix_stepped': True,
    'agreed': True, 'filters': [{'finished': True}], 'verified': True, 'ix_documented': True,
}


def facts(backend: str, upto: str | None = None, **changes) -> dict:
    """A build of this backend with everything done up to (not including) a fact, then any changes."""
    order = list(DONE)
    done = {k: (DONE[k] if upto is None or order.index(k) < order.index(upto) else
                [] if k == 'filters' else False) for k in order}
    return {'backend': backend, **done, **changes}


async def status_of(f: dict) -> dict:
    """plan.status over a build described by facts: an events pipeline 'ev' (none for discovery), an index doc,
    an indexing (or discovery) pipeline 'ix'."""
    discovery = f['backend'] == 'discovery'
    elastic = f['backend'] in ('elasticsearch', 'discovery')
    doc = lambda t, u, n: {'type': t, 'uuid': u, 'name': n, 'path': 'b', 'working_copy_of': None}  # noqa: E731
    docs = ([doc('Feed', 'f', 'ACME')] if f['feed'] else []) \
        + ([doc('TextConverter', 'tc', 'ACME-CSV')] if f['converter'] else []) \
        + ([doc('XSLT', 'x', 'ACME-Translation')] if f['translation'] and not discovery else []) \
        + ([doc('Pipeline', 'ev', 'ACME-Events')] if f['pipeline'] and not discovery else []) \
        + ([doc('Documentation', 'd1', 'ACME-Events')] if f['ev_documented'] and not discovery else []) \
        + ([doc('ElasticIndex' if elastic else 'Index', 'ix-doc', 'ACME-V1')] if f['index_doc'] else []) \
        + ([doc('XSLT', 'ix-x', 'ACME-V1-XSLT'), doc('Pipeline', 'ix', 'ACME-Indexing')] if f['ix_pipeline'] else []) \
        + ([doc('Documentation', 'd2', 'ACME-Indexing')] if f['ix_documented'] else [])
    kinds = {'x': ('translation', {}), 'ix-x': ('index', {})}
    stroom = SimpleNamespace(
        get_doc=AsyncMock(side_effect=lambda t, u: {'uuid': u, 'name': u, 'description': u}),
        pipeline_layers=AsyncMock(return_value=[]),
        find_meta=AsyncMock(return_value={'values': [{'meta': {'id': 9, 'status': 'UNLOCKED'}}] if f['events'] else []}))
    stages = {'ev': 'translation', 'ix': 'discovery' if discovery else 'indexing'}
    stepped = {'ev': f['ev_stepped'], 'ix': f['ix_stepped']}
    with patch('tools.builds._build_docs', AsyncMock(return_value=docs)), \
            patch('tools.builds.build_checks', AsyncMock(return_value=[])), \
            patch.object(plan, 'gateway_from', lambda c: stroom), \
            patch.object(plan, 'guard_from', lambda c: None), \
            patch.object(plan, 'sample_streams', AsyncMock(return_value={'Raw Events': [{'id': 7}]} if f['samples'] else {})), \
            patch.object(plan, 'sample_format', AsyncMock(return_value={'needs_text_converter': True})), \
            patch('utils.mappingstore.read_mapping', lambda d: kinds.get(d)), \
            patch('tools.templates._shape', AsyncMock(side_effect=lambda s, u: {'stage': stages[u], 'parser': None})), \
            patch('tools.pipeline_writes.open_slots', AsyncMock(return_value=[])), \
            patch('tools.pipelines.merge_layers', lambda layers: {}), \
            patch('tools.stepping.stepped_clean', AsyncMock(side_effect=lambda c, p: stepped[p['uuid']])), \
            patch('tools.stepping.verified', AsyncMock(return_value=f['verified'])),             patch('tools.stepping.validated', AsyncMock(return_value=f['ev_validated'])), \
            patch('tools.processing_writes.elastic_destination', AsyncMock(
                side_effect=lambda s, u: {'index name': 'acme-v1', 'cluster': 'ES'} if elastic and u == 'ix' else None)), \
            patch('tools.processing_writes.agreement_problem', AsyncMock(return_value=None if f['agreed'] else 'no')), \
            patch('tools.processing.processing_status', AsyncMock(return_value={'filters': f['filters']})):
        return await plan.status(None, 'b')


RUNNING, FINISHED = [{'finished': False}], [{'finished': True}]
STATES = [
    # The path, in order: each state's next step and the call it names.
    ('nothing yet', facts('lucene', 'feed'), 'feed', 'create_feed'),
    ('a feed', facts('lucene', 'samples'), 'samples', 'upload_sample'),
    ('a sample', facts('lucene', 'converter'), 'converter', 'build_data_splitter'),
    ('a converter', facts('lucene', 'translation'), 'translation', 'draft_translation_mapping'),
    ('a translation', facts('lucene', 'pipeline'), 'pipeline', 'find_pipeline_templates'),
    ('an events pipeline', facts('lucene', 'ev_stepped'), 'stepped', 'step_sample'),
    ('stepped clean', facts('lucene', 'events'), 'processed', 'create_processor_filter'),
    ('Events', facts('lucene', 'ev_validated'), 'validated', 'check_events'),
    ('validated', facts('lucene', 'ev_documented'), 'documented', 'write_documentation'),
    ('documented', facts('lucene', 'index_doc'), 'index', 'get_field_conventions'),
    ('a Lucene index doc', facts('lucene', 'ix_pipeline'), 'indexing_pipeline', 'save_xslt'),
    ('a Lucene indexing pipeline', facts('lucene', 'ix_stepped'), 'indexed', 'step_sample'),
    ('stepped: no template step for Lucene', facts('lucene', 'agreed'), 'indexed', 'create_processor_filter'),
    ('Lucene indexing', facts('lucene', 'verified', filters=RUNNING), 'indexed', 'wait_for_processing'),
    ('Lucene indexed', facts('lucene', 'verified'), 'indexed', 'verify_index'),
    ('Lucene verified', facts('lucene', 'ix_documented'), 'index_documented', 'write_documentation'),
    ('all done', facts('lucene'), 'promoted', 'promote_build'),
    # Elasticsearch: the template step, between the pipeline and indexing.
    ('an Elasticsearch indexing pipeline', facts('elasticsearch', 'ix_stepped'), 'index_template', 'step_sample'),
    ('stepped, not agreed', facts('elasticsearch', 'agreed'), 'index_template', 'propose_index_template'),
    ('agreed', facts('elasticsearch', 'verified', filters=[]), 'indexed', 'create_processor_filter'),
    ('indexing', facts('elasticsearch', 'verified', filters=RUNNING), 'indexed', 'wait_for_processing'),
    ('indexed', facts('elasticsearch', 'verified', filters=FINISHED), 'indexed', 'verify_index'),
    ('verified', facts('elasticsearch', 'ix_documented'), 'index_documented', 'write_documentation'),
    # Out of order, as real clients went.
    ('VS Code: indexing documented before anything was agreed or indexed',
     facts('elasticsearch', 'agreed', ix_documented=True), 'index_template', 'propose_index_template'),
    ('VS Code: the user committed a template that was never agreed; documented',
     facts('elasticsearch', 'agreed', ix_documented=True, filters=[]), 'index_template', 'propose_index_template'),
    ('the indexing XSLT changed after the template was agreed',
     facts('elasticsearch', agreed=False, verified=False), 'index_template', 'propose_index_template'),
    ('changed after indexing was verified: stepped again first',
     facts('elasticsearch', ix_stepped=False, agreed=False, verified=False), 'index_template', 'step_sample'),
    ('the events pipeline changed after indexing: its step comes first',
     facts('elasticsearch', ev_stepped=False), 'stepped', 'step_sample'),
    ('an index doc and pipeline before the events pipeline was documented',
     facts('elasticsearch', ev_documented=False), 'documented', 'write_documentation'),
    # Discovery: no events pipeline; its index doc is made at verification.
    ('discovery: a pipeline, no index doc yet, not agreed',
     facts('discovery', 'ix_stepped', index_doc=False, translation=False, pipeline=False, ev_stepped=False,
           events=False, ev_documented=False), 'index_template', 'step_sample'),
    ('discovery: indexed, no index doc yet',
     facts('discovery', 'verified', index_doc=False, translation=False, pipeline=False, ev_stepped=False, events=False,
           ev_documented=False), 'indexed', 'create_index_doc'),
    ('discovery: with its index doc',
     facts('discovery', 'verified', translation=False, pipeline=False, ev_stepped=False, events=False,
           ev_documented=False), 'indexed', 'verify_index'),
]


@pytest.mark.parametrize('name,state,step,tool', STATES, ids=[s[0] for s in STATES])
async def test_each_state_names_the_right_step_and_call(name, state, step, tool):
    status = await status_of(state)
    assert (status['next']['step'], status['next']['call']['tool']) == (step, tool)


@pytest.mark.parametrize('name,state,step,tool', STATES, ids=[s[0] for s in STATES])
async def test_never_promotion_or_documentation_before_indexing(name, state, step, tool):
    status = await status_of(state)
    states = {s['step']: s['state'] for s in status['steps']}
    if states['indexed'] == 'to do':
        assert status['next']['step'] not in ('index_documented', 'promoted')
    if any(s == 'to do' for k, s in states.items() if k != 'promoted'):
        assert status['next']['step'] != 'promoted'
    # The template step is only for Elasticsearch.
    if state['backend'] == 'lucene':
        assert states['index_template'] == 'not needed'


@pytest.mark.parametrize('name,state,step,tool', STATES, ids=[s[0] for s in STATES])
async def test_every_named_call_takes_the_arguments_it_is_given(name, state, step, tool):
    import inspect
    import main_tools
    tools = {t.__name__: t for m in main_tools.TOOL_MODULES for t in m.ALL_TOOLS}
    call = (await status_of(state))['next']['call']
    assert set(call['arguments']) <= set(inspect.signature(tools[call['tool']]).parameters)


# Part B: wrong moves -----------------------------------------------------------------------------------------

def context():
    from config import Settings
    settings = Settings(_env_file=None, stroom_url='https://stroom.example', dev_no_auth=True, stroom_api_key='k')
    stroom = SimpleNamespace(settings=settings, get_doc=AsyncMock(return_value={'uuid': 'p1', 'name': 'Acme'}))
    return SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': ConsentStore(use_elicitation=False)})


PLAN = FieldPlan(backend='elasticsearch', index_name='acme-v1', time_field='@timestamp', fields=[
    PlannedField(name='StreamId', type='id', source='@StreamId'), PlannedField(name='EventId', type='id', source='@EventId'),
    PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated')])
PIPELINE = {'uuid': 'p1', 'name': 'Acme-Indexing', 'description': 'Indexes Acme.'}
ES = {'index name': 'acme-v1', 'cluster': 'ES'}


async def processing_filter(**kwargs):
    defaults = {'stepped': True, 'destination': None, 'processed': []}
    o = {**defaults, **kwargs.pop('given', {})}
    with patch.object(processing_writes, '_managed_pipeline', AsyncMock(return_value=PIPELINE)), \
            patch.object(processing_writes, 'stepped_clean', AsyncMock(return_value=o['stepped'])), \
            patch.object(processing_writes, '_build_feeds_only', AsyncMock()), \
            patch.object(processing_writes, 'refuse_older_than_feed', AsyncMock()), \
            patch.object(processing_writes, '_events_source', AsyncMock(return_value=None)), \
            patch.object(processing_writes, '_already_processed', AsyncMock(return_value=o['processed'])), \
            patch.object(processing_writes, 'elastic_destination', AsyncMock(return_value=o['destination'])), \
            patch.object(processing_writes, 'indexing_xslt_digest', AsyncMock(return_value='d')):
        return await processing_writes.create_processor_filter(context(), 'p1', **kwargs)


async def reprocess_unprocessed():
    with patch.object(processing_writes, '_managed_pipeline', AsyncMock(return_value=PIPELINE)), \
            patch.object(processing_writes, '_already_processed', AsyncMock(return_value=[])):
        return await processing_writes.reprocess_streams(context(), 'p1', [5])


async def wait_with_no_filter():
    with patch.object(processing_writes, 'processing_status', AsyncMock(return_value={'filters': []})), \
            patch('tools.plan.build_of', AsyncMock(return_value=None)):
        return await processing_writes.wait_for_processing(context(), 'p1', [5], timeout_seconds=60, expect_events=False)


async def propose(**kwargs):
    destination = kwargs.pop('destination', ES)
    with patch.object(indexing, 'elastic_destination', AsyncMock(return_value=destination)):
        return await indexing.propose_index_template(context(), 'p1', PLAN, [5], **kwargs)


async def check_template_on_events_pipeline():
    with patch.object(indexing, 'elastic_destination', AsyncMock(return_value=None)):
        return await indexing.check_index_template(context(), 'p1', '{"index_patterns": ["x*"]}', [5])


async def draft(**kwargs):
    ctx = context()
    ctx.lifespan_context['stroom'].find_documents = AsyncMock(return_value={'values': []})
    ctx.lifespan_context['stroom'].get_doc = AsyncMock(return_value={'name': 'FortiOS-V1'})
    example = ('PUT _index_template/fortios-v1\n{"index_patterns": ["fortios-v1*"]}',
               "followed the fields of Elastic Index doc 'FortiOS-V1' (2 fields, ...)",
               {'doc': 'FortiOS-V1', 'index': 'ecs-fortios-v1', 'fields': 2,
                'source': "the index doc's field list, with Elasticsearch's own types", 'examples': ['source.ip (ip)']})
    with patch.object(indexing, '_conventions', lambda c: {'ecs': {}}), \
            patch.object(indexing, '_example_from_index', AsyncMock(return_value=example)):
        return await indexing.draft_index_mapping(ctx, 'elasticsearch', 'acme-v1', events_stream_ids=[5], **kwargs)


async def documentation(**kwargs):
    return await builds.write_documentation(context(), 'b', 'p1', '## Purpose and data\n\nAcme.\n', **kwargs)


async def weaken_validation():
    return await pipeline_writes._keep_validation({'schemaFilter': 'SchemaFilter'}, [
        pipeline_writes.PropertyValue(element='schemaFilter', name='schemaValidation', value='false')])


async def misleading_search():
    from tools.indexing import SearchCheck
    return indexing._searchable('elasticsearch', [SearchCheck(field='User.Id', condition='CONTAINS', value='ali')])


async def wildcard_on_an_address():
    # Seen in VS Code: IpAddress EQUALS 192.0.2.* found nothing on Elasticsearch, and verify_index failed.
    from tools.indexing import SearchCheck
    return indexing._searchable('elasticsearch', [SearchCheck(field='IpAddress', value='192.0.2.*')], {'IpAddress'},
                                [{'field': 'IpAddress', 'value': '10.0.*'}])


async def a_deleted_feeds_stream():
    """Seen in VS Code: streams 15793567 and 15793566 belonged to a deleted feed of the same name."""
    from tools import streams
    metas = {7: {'id': 7, 'feedName': 'FW-V1', 'createMs': 1000, 'status': 'UNLOCKED'},
             9: {'id': 9, 'feedName': 'FW-V1', 'createMs': 5000, 'status': 'UNLOCKED'}}

    async def find_meta(terms, limit):
        if terms[0]['field'] == 'Id':
            return {'values': [{'meta': metas[int(terms[0]['value'])]}]}
        return {'values': [{'meta': m} for m in metas.values()]}
    ctx = context()
    stroom = ctx.lifespan_context['stroom']
    stroom.find_meta = AsyncMock(side_effect=find_meta)
    stroom.get = AsyncMock(return_value={'uuid': 'feed-1'})
    stroom.get_doc = AsyncMock(return_value={'uuid': 'feed-1', 'createTimeMs': 4000})
    await streams.refuse_older_than_feed(ctx, [9])          # the feed's own: fine
    return await streams.refuse_older_than_feed(ctx, [7, 9])


# (what the agent did wrong, the call, what the reply must say to put it right)
WRONG_MOVES = [
    ('process before a clean step', lambda: processing_filter(stream_ids=[5], given={'stepped': False}),
     ['no clean step', 'step_sample']),
    ('index before the template is agreed (VS Code)',
     lambda: processing_filter(stream_ids=[5], given={'destination': ES}),
     ['No Elasticsearch index template has been agreed', 'propose_index_template pipeline_uuid=p1', 'example_template=',
      'does not count']),
    ('both a sample and a feed scope', lambda: processing_filter(stream_ids=[5], feed='ACME'), ['Give either stream_ids']),
    ('a whole feed with no start time', lambda: processing_filter(feed='ACME'), ['needs created_after']),
    ('process a stream again with a new filter', lambda: processing_filter(stream_ids=[5], given={'processed': [5]}),
     ['already processed', 'reprocess_streams']),
    ('reprocess what was never processed', reprocess_unprocessed, ['have not been processed', 'create_processor_filter']),
    ('wait with nothing processing (VS Code)', wait_with_no_filter, ['no processor filter', 'create_processor_filter first']),
    ('a template without the user\'s example (VS Code)', lambda: propose(),
     ['"needs": "example_template"', 'exactly as the user pasted it', 'without_example=true']),
    ('a template for the events pipeline (VS Code)', lambda: propose(destination=None, example_template='{}'),
     ['not an Elasticsearch indexing pipeline', 'draft_index_mapping example_template=']),
    ('check the user\'s template on the events pipeline (VS Code)', check_template_on_events_pipeline,
     ['not an Elasticsearch indexing pipeline', 'draft_index_mapping example_template=']),
    ('draft an Elasticsearch index from a convention alone', lambda: draft(convention='ecs'),
     ['"drafted": false', 'example index template', 'like_index', 'without_example']),
    ('follow another index the user didn\'t choose (VS Code)', lambda: draft(like_index='FortiOS-V1'),
     ['"status": "needs_confirmation"', "Elastic Index doc 'FortiOS-V1'", '2 fields from', 'paste its index template']),
    ('go without an example unasked', lambda: draft(convention='ecs', without_example=True),
     ['"status": "needs_confirmation"', 'without an example index template']),
    ('weaken validation to get past an error', weaken_validation, ['Fix the output instead']),
    ('a search Elasticsearch answers wrongly through Stroom', misleading_search, ['use EQUALS']),
    ('a wildcard on an ip field (VS Code)', wildcard_on_an_address, ["use EQUALS '192.0.2.0/24'", "use the CIDR range '10.0.0.0/16'"]),
    ('build from a deleted feed\'s stream of the same name (VS Code)', a_deleted_feeds_stream,
                    ['stream(s) [7] are older than feed FW-V1', 'earlier feed of that name, since deleted',
                     "The feed's own raw streams: [9]"]),
]


@pytest.mark.parametrize('name,call,says', WRONG_MOVES, ids=[m[0] for m in WRONG_MOVES])
async def test_a_wrong_move_is_stopped_and_says_what_to_do_instead(name, call, says):
    try:
        reply = json.dumps(await call())
    except ToolError as e:
        reply = str(e)
    missing = [s for s in says if s not in reply]
    assert not missing, f'{name}: the reply lacks {missing}: {reply[:400]}'
    # Never a template the cluster could be given unagreed.
    assert '"dev_tools"' not in reply
