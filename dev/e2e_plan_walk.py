"""The onboarding plan, followed as an agent would, against the local Docker stack (see dev/stroom).

    uv run python dev/e2e_plan_walk.py [lucene] [elasticsearch]   # Elasticsearch needs the elastic profile

The other suites drive the tools in an order they already know, so they never test the guidance an agent is
given. This one starts with start_onboarding and from then on does what `next` says: at every step it takes
the call the server names, fills in only its <placeholders> (the decisions an agent makes with the user: names,
the mapping, the user's example index template, the dashboard's columns), and calls exactly that tool. It checks:

- the steps come in the expected order for the path (Lucene, or Elasticsearch with an index template agreed);
- following `next` never leads into a refusal;
- each write's own `next` agrees with build_status, and the work is not `done` before promotion;
- wrong moves along the way (seen from real clients) are refused with the right call named, and leave `next`
  where it was: processing before a clean step, indexing before the template is agreed, a template without the
  user's example, following another index unasked, waiting with no processor filter, promoting early.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
import main_tools  # noqa: E402
from config import Settings  # noqa: E402
from e2e_elastic_handover import ES, LIVE_COMPONENT, LIVE_EXAMPLE, _request, fixtures, live_cluster  # noqa: E402
from e2e_generator import MAPPINGS  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from security.guard import guard_from  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools.plan import build_status  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import FieldPlan  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

check = e2e.check
TOOLS = {t.__name__: t for m in main_tools.TOOL_MODULES for t in m.ALL_TOOLS}
SAMPLE = e2e.CASES['csv']['sample']
STAGE_1 = ['feed', 'samples', 'converter', 'translation', 'pipeline', 'stepped', 'processed', 'documented']
PATHS = {
    'lucene': STAGE_1 + ['index', 'indexing_pipeline', 'indexed', 'index_documented', 'promoted'],
    'elasticsearch': STAGE_1 + ['index', 'indexing_pipeline', 'index_template', 'indexed', 'index_documented', 'promoted'],
}
# The first call `next` names for each step (indexed names the next one as it goes: the filter, the wait, verify_index).
FIRST_CALL = {'feed': 'create_feed', 'samples': 'upload_sample', 'converter': 'build_data_splitter',
              'translation': 'draft_translation_mapping', 'pipeline': 'find_pipeline_templates', 'stepped': 'step_sample',
              'processed': 'create_processor_filter', 'documented': 'write_documentation', 'index': 'draft_index_mapping',
              'indexing_pipeline': 'save_xslt', 'index_template': 'step_sample', 'indexed': 'create_processor_filter',
              'index_documented': 'write_documentation', 'promoted': 'promote_build'}
# Lucene has no template step: the new indexing pipeline is stepped as the first call of indexed.
FIRST_CALL_ON = {'lucene': {'indexed': 'step_sample'}}


def placeholders(arguments: dict) -> list[str]:
    return [k for k, v in arguments.items() if isinstance(v, str) and v.startswith('<')]


def fill(call: dict, **decisions) -> dict:
    """The call's arguments as `next` gave them, with the agent's decisions in place of every <placeholder>."""
    arguments = {**call['arguments'], **decisions}
    left = placeholders(arguments)
    check(not left, f"{call['tool']}: every placeholder decided ({left or 'none left'})")
    return arguments


async def run(ctx, tool: str, **arguments):
    """A call the plan led to: it must not be refused. Confirmations and approvals are given, as the user would."""
    try:
        return await e2e.agreed(TOOLS[tool], ctx=ctx, **arguments)
    except ToolError as e:
        check(False, f'{tool}, as the plan led to it, is not refused: {e}')


async def refused(ctx, tool: str, **arguments) -> str:
    try:
        result = await TOOLS[tool](ctx, **arguments)
    except ToolError as e:
        return str(e)
    return json.dumps(result)


async def at(ctx, build: str, step: str, tool: str | None = None) -> dict:
    """build_status says the next step is this one (and its call this tool)."""
    status = await build_status(ctx, build)
    nxt = status['next']
    check(nxt['step'] == step and (tool is None or nxt['call']['tool'] == tool),
          f"next: {nxt['step']} -> {nxt['call']['tool']} (expected {step} -> {tool or 'any'})")
    return nxt


class Walk:
    """The agent's decisions, and what it has made so far."""

    def __init__(self, ctx, path: str, stamp: str, es: httpx.AsyncClient | None):
        self.ctx, self.path, self.stamp, self.es = ctx, path, stamp, es
        self.backend = path
        self.feed = f'WALK-{path[:3].upper()}-{stamp}'
        self.index_name = f'walk-door-{stamp}-v1' if path == 'elasticsearch' else f'WALK-{stamp}-INDEX-V1'
        self.build = self.events = self.plan = self.index = self.cluster = None
        self.template_applied = False
        self.old_stream = None      # a stream of an earlier, deleted feed with the walk's feed name

    # Stage 1 ----------------------------------------------------------------------------------------------

    async def feed_step(self, call):
        made = await run(self.ctx, 'create_feed', **fill(call, name=self.feed))
        if self.old_stream:
            check(str(self.old_stream) in made.get('note', '') and 'earlier feed' in made['note'],
                  "the earlier feed's streams, still under the name, are pointed out and not taken as samples")
        return made

    async def samples_step(self, call):
        return await run(self.ctx, 'upload_sample', **fill(call, sample=SAMPLE))

    async def converter_step(self, call):
        return await run(self.ctx, 'build_data_splitter', **fill(call, save_as=f'{self.feed}-CSV'))

    async def translation_step(self, call):
        draft = await run(self.ctx, 'draft_translation_mapping', **fill(call))
        check('mapping' in draft, 'drafted a mapping from the sample')
        # The agent decides the actions with the user; here, the generator suite's logon mapping.
        return await run(self.ctx, 'build_translation_xslt', mapping=MAPPINGS['csv'], stream_ids=call['arguments']['stream_ids'],
                         build=self.build, name=f'{self.feed}-Translation')

    async def pipeline_step(self, call):
        found = await run(self.ctx, 'find_pipeline_templates', **fill(call))
        template = next(c for c in found['candidates'] if c['name'] == 'Event Data (Text)')
        return await run(self.ctx, 'create_pipeline', build=self.build, name=f'{self.feed}-Events',
                         template_uuid=template['uuid'])

    async def stepped_step(self, call):
        result = await run(self.ctx, 'step_sample', **fill(call))
        check(result['verdict'] == 'clean', f"stepped clean: {result['verdict']}")
        return result

    async def processed_step(self, call):
        arguments = fill(call)
        await run(self.ctx, 'create_processor_filter', **arguments)
        done = await run(self.ctx, 'wait_for_processing', pipeline_uuid=arguments['pipeline_uuid'],
                         stream_ids=arguments['stream_ids'])
        check(done['gate'] == 'pass', f"processed into Events: {done['streams']}")
        self.events = [e for s in done['streams'] for e in s['events']]
        record = (await run(self.ctx, 'read_stream', stream_id=self.events[0], record_count=1))['records'][0]
        valid = await run(self.ctx, 'check_events', events_xml=record)
        check(valid['ok'], 'the Events validate')
        return done

    async def documented_step(self, call):
        # As an agent got it wrong: the Events stream instead of the raw sample, and no change line. The server
        # documents from the raw stream it was made from, and a new doc's change line is 'Created'.
        written = await run(self.ctx, 'write_documentation', **fill(
            call, markdown='## Purpose and data\n\nLogons from the walk.\n', stream_ids=self.events))
        check('which it was made from' in (written.get('note') or '') and written.get('field_mapping'),
              "documented from the raw sample, though given the Events stream, with no change line")
        return written

    # Stage 2 ----------------------------------------------------------------------------------------------

    async def index_step(self, call):
        if self.backend == 'lucene':
            await TOOLS['get_field_conventions'](self.ctx)   # guidance: the user picks the convention
            draft = await run(self.ctx, 'draft_index_mapping', **fill(
                call, backend='lucene', index_name=self.index_name, convention='stroom-flat'))
            self.plan = FieldPlan.model_validate(draft['plan'])
            self.index = await run(self.ctx, 'create_index_doc', build=self.build, backend='lucene', name=self.index_name,
                                   time_field=self.plan.time_field, plan=self.plan)
            return self.index
        self.cluster = await live_cluster(self.ctx.lifespan_context['stroom'])
        options = await TOOLS['get_field_conventions'](self.ctx, backend='elasticsearch')
        check(options['options'][0]['option'].startswith('example index template'), "the user's example is offered first")
        # Wrong moves: a convention alone drafts nothing; another index is the user's choice, in a form.
        offered = await TOOLS['draft_index_mapping'](self.ctx, **fill(
            call, backend='elasticsearch', index_name=self.index_name, convention='ecs'))
        check(offered.get('drafted') is False, 'a convention alone is not drafted: the user is asked for their example')
        existing = options['options'][1]['existing_indexes']
        check(bool(existing), f'existing indexes listed to follow: {len(existing)}')
        try:
            asked = await TOOLS['draft_index_mapping'](self.ctx, backend='elasticsearch', index_name=self.index_name,
                                                       events_stream_ids=call['arguments']['events_stream_ids'],
                                                       like_index=existing[0]['uuid'])
            # Its fields are read through Stroom first, so the user sees what they'd get before agreeing.
            check(asked.get('status') == 'needs_confirmation' and existing[0]['name'] in json.dumps(asked['details'])
                  and 'fields from' in asked['details']['read through Stroom'],
                  f"following another index ({existing[0]['name']}) is the user's to confirm, its fields read first")
        except ToolError as e:
            # A cluster Stroom can't reach lists no fields: no form, the user is asked for the template instead.
            check('paste its index template' in str(e), f"an index whose fields can't be read: {str(e)[:100]}")
        # The user pastes their example: the plan follows it.
        draft = await run(self.ctx, 'draft_index_mapping', **fill(
            call, backend='elasticsearch', index_name=self.index_name, convention='ecs'),
            example_template=LIVE_EXAMPLE, component_templates=[LIVE_COMPONENT])
        self.plan = FieldPlan.model_validate(draft['plan'])
        # The plan names the index: no index_name (an agent was refused for want of it).
        self.index = await run(self.ctx, 'create_index_doc', build=self.build, backend='elasticsearch',
                               name=self.index_name, time_field=self.plan.time_field, plan=self.plan,
                               cluster_uuid=self.cluster['uuid'])
        return self.index

    async def indexing_pipeline_step(self, call):
        xslt = await run(self.ctx, 'save_xslt', **fill(call, name=f'{self.index_name}-XSLT', index_plan=self.plan))
        candidates = (await run(self.ctx, 'find_pipeline_templates', stage='indexing'))['candidates']
        if self.backend == 'lucene':
            template = next(c for c in candidates if c['backend'] == 'lucene' and c['name'] == 'Indexing')
            target = {'index_uuid': self.index['uuid']}
        else:
            template = await fixtures(self.ctx.lifespan_context['stroom'])
            template = template[0]
            target = {'index_uuid': self.index['uuid']}     # the Elastic Index doc names the index and cluster
        return await run(self.ctx, 'create_indexing_pipeline', build=self.build, name=f'{self.index_name} - Indexing',
                         template_uuid=template['uuid'], xslt_uuid=xslt['uuid'], events_stream_ids=self.events, **target)

    async def index_template_step(self, call):
        # The plan steps the new pipeline first (the template is checked against the documents it writes).
        if call['tool'] == 'step_sample':
            stepped = await run(self.ctx, 'step_sample', **fill(call))
            check(stepped['verdict'] == 'clean', f"indexing pipeline stepped clean: {stepped['verdict']}")
            call = (await at(self.ctx, self.build, 'index_template', 'propose_index_template'))['call']
        pipeline_uuid = call['arguments']['pipeline_uuid']
        # Wrong moves, seen in VS Code: indexing before the template is agreed; a template without the user's
        # example; waiting on a pipeline with nothing set to process.
        message = await refused(self.ctx, 'create_processor_filter', pipeline_uuid=pipeline_uuid, stream_ids=self.events,
                                source_pipeline_uuid=(await self._events_pipeline()))
        check(message.startswith('No Elasticsearch index template has been agreed')
              and f'propose_index_template pipeline_uuid={pipeline_uuid}' in message,
              'indexing before the template is agreed is refused, naming the call that agrees it')
        unasked = await run(self.ctx, 'propose_index_template', pipeline_uuid=pipeline_uuid, plan=self.plan,
                            events_stream_ids=self.events)
        check(unasked.get('needs') == 'example_template' and 'dev_tools' not in unasked,
              "without the user's example nothing is built for the cluster")
        started = time.monotonic()
        waited = await run(self.ctx, 'wait_for_processing', pipeline_uuid=pipeline_uuid, stream_ids=self.events,
                           expect_events=False, timeout_seconds=60)
        check(time.monotonic() - started < 30 and 'no processor filter' in waited['problems'][0],
              'waiting with no processor filter returns at once')
        await at(self.ctx, self.build, 'index_template', 'propose_index_template')
        # As the plan says: with the user's example, exactly as they pasted it.
        agreed = await run(self.ctx, 'propose_index_template', **fill(
            call, plan=self.plan, example_template=LIVE_EXAMPLE), component_templates=[LIVE_COMPONENT])
        check(agreed.get('agreed') is True, 'the user agreed the index template')
        # The cluster admin commits it (component template first), as the user then says.
        for text in (LIVE_COMPONENT, agreed['dev_tools']):
            path, body = _request(text)
            response = await self.es.put(f'/{path}', json=body)
            check(response.status_code == 200, f'the admin applied {path}: {response.status_code}')
        return agreed

    async def indexed_step(self, call):
        # The plan names each call in turn: (a step,) the filter, the wait while it runs, then verify_index.
        tools, result = [], None
        for _ in range(8):
            tools.append(call['tool'])
            if call['tool'] == 'step_sample':
                result = await run(self.ctx, 'step_sample', **fill(call))
            elif call['tool'] == 'create_processor_filter':
                result = await run(self.ctx, 'create_processor_filter', **fill(call))
                # Not verified yet: promotion's approval would say so.
                gate = await TOOLS['promote_build'](self.ctx, build=self.build, destinations=self._destinations())
                check(any('has not been indexed and verified' in w for w in gate['details'].get('warnings') or []),
                      "promoting now: the approval warns the sample isn't indexed and verified")
            elif call['tool'] == 'wait_for_processing':
                result = await run(self.ctx, 'wait_for_processing', **fill(call))
            elif call['tool'] == 'verify_index':
                check(call['arguments']['fields'].startswith('<the columns the user chose; suggest '),
                      f"the plan suggests the dashboard's columns: {call['arguments']['fields']}")
                fields = (['EventTime', 'UserId', 'HostName'] if self.backend == 'lucene'
                          else ['@timestamp', 'User.Id', 'TypeId'])
                user = 'UserId' if self.backend == 'lucene' else 'User.Id'
                if self.es:
                    await self.es.post(f'/{self.index_name}/_refresh')
                result = await run(self.ctx, 'verify_index', **fill(
                    call, expected_documents=3, fields=fields), exact=[{'field': user, 'value': 'bob'}])
                check(result['passed'], 'verify_index passed')
            else:
                check(False, f"indexed: an unexpected call {call['tool']}")
            status = await build_status(self.ctx, self.build)
            if status['next']['step'] != 'indexed':
                break
            call = status['next']['call']
        print(f"    indexed by: {' -> '.join(tools)}")
        check(tools[-1] == 'verify_index' and 'create_processor_filter' in tools,
              'indexed only once verify_index passed, after the filter')
        return result

    async def index_documented_step(self, call):
        return await run(self.ctx, 'write_documentation', **fill(
            call, markdown=f'## Purpose and data\n\nLogons indexed into {self.index_name}.\n'), change='Created')

    async def promoted_step(self, call):
        return await run(self.ctx, 'promote_build', **fill(call, destinations=self._destinations()))

    # -------------------------------------------------------------------------------------------------------

    def _destinations(self) -> dict:
        folder = f'System/Walk Promoted {self.stamp}/{self.path}'
        # No destination for the verification dashboard: it goes where the index it searches goes.
        return {t: folder for t in ('Feed', 'Pipeline', 'XSLT', 'TextConverter', 'Documentation', 'Index',
                                    'ElasticIndex')}

    async def _events_pipeline(self) -> str:
        status = await build_status(self.ctx, self.build)
        return next(p['uuid'] for p in status['pipelines'] if p['stage'] == 'translation')


async def detours_before(walk: Walk, step: str, call: dict) -> None:
    """Wrong moves at this point, refused with the right call named; `next` stays where it was."""
    ctx, build = walk.ctx, walk.build
    if step == 'stepped':
        message = await refused(ctx, 'create_processor_filter', pipeline_uuid=call['arguments']['pipeline_uuid'],
                                stream_ids=call['arguments']['stream_ids'])
        check('no clean step' in message and 'step_sample' in message,
              'processing before a clean step is refused, naming step_sample')
        check(walk.old_stream not in call['arguments']['stream_ids'], "the plan names only the feed's own streams")
        if walk.old_stream:
            message = await refused(ctx, 'step_sample', pipeline_uuid=call['arguments']['pipeline_uuid'],
                                    stream_ids=[walk.old_stream])
            check('older than feed' in message and str(call['arguments']['stream_ids'][0]) in message,
                  "stepping the deleted feed's stream is refused, naming the feed's own")
        await at(ctx, build, 'stepped', 'step_sample')
    if step in ('index_template', 'indexed'):
        status = await build_status(ctx, build)
        check(status['next']['step'] not in ('index_documented', 'promoted'),
              f'not indexed yet: neither documentation nor promotion is next ({status["next"]["step"]})')


async def deleted_feed_before(ctx, walk: Walk) -> None:
    """Seen in VS Code: a feed of the same name made and deleted before, its streams still in Stroom under the name."""
    stroom = ctx.lifespan_context['stroom']
    folder = await guard_from(ctx).build_folder(f'walk-old-{walk.stamp}')
    node = await stroom.post('/explorer/v2/create', {
        'docType': 'Feed', 'docName': walk.feed, 'permissionInheritance': 'DESTINATION',
        'destinationFolder': {k: v for k, v in folder.items() if not k.startswith('_')}})
    ref = node.get('docRef', node)
    response = await stroom.datafeed(walk.feed, json.dumps([{'old': True}]).encode(), {'Type': 'Raw Events'})
    check(response.is_success, 'data sent to the earlier feed')
    rows = []
    for _ in range(20):
        rows = (await stroom.find_meta([{'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': walk.feed}],
                                       5)).get('values') or []
        if rows:
            break
        await asyncio.sleep(1)
    check(bool(rows), 'the earlier feed has a stream')
    walk.old_stream = rows[0]['meta']['id']
    await stroom.request('DELETE', '/explorer/v2/delete', {'docRefs': [{k: ref[k] for k in ('type', 'uuid', 'name')}]})
    await stroom.request('DELETE', '/explorer/v2/delete', {'docRefs': [
        {'type': 'Folder', 'uuid': folder['uuid'], 'name': folder['name']}]})
    await asyncio.sleep(2)       # the new feed doc is made after the stream
    print(f'    an earlier feed {walk.feed} made, sent stream {walk.old_stream}, and deleted')


async def walk_path(ctx, path: str, stamp: str, es: httpx.AsyncClient | None) -> None:
    print(f'\n### {path}: start_onboarding, then what next says')
    walk = Walk(ctx, path, stamp, es)
    if path == 'lucene':
        await deleted_feed_before(ctx, walk)
    started = await run(ctx, 'start_onboarding', source_name=f'Walk {path} {stamp}', samples={'logons.csv': SAMPLE})
    walk.build = started['build']
    check(started['next']['step'] == 'feed' and started['next']['call']['tool'] == 'create_feed',
          'the walk starts at the feed')
    expected = PATHS[path]
    for i, step in enumerate(expected):
        nxt = await at(ctx, walk.build, step, FIRST_CALL_ON.get(path, {}).get(step, FIRST_CALL[step]))
        print(f'\n#### {i + 1}. {step}: {nxt["call"]["tool"]}')
        await detours_before(walk, step, nxt['call'])
        if step == 'promoted':
            status = await build_status(ctx, walk.build)
            check(all(s['state'] in ('done', 'not needed', 'not recorded') for s in status['steps'] if s['step'] != 'promoted'),
                  f"every step done before promotion: {[s['step'] for s in status['steps'] if s['state'] == 'to do']}")
        result = await getattr(walk, f'{step}_step')(nxt['call'])
        following = expected[i + 1] if i + 1 < len(expected) else None
        if following and isinstance(result, dict) and 'next' in result:
            check(result['next']['step'] == following and (result.get('done') is False) == (following != 'promoted'),
                  f"the result's own next is {following}" + ('' if following == 'promoted' else ', and not done')
                  + f" (got {result['next']['step']}, done {result.get('done')})")
    check(any(p.startswith('moved Pipeline') for p in result['promoted']), f"promoted: {result['promoted'][:3]}")


async def main():
    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=e2e.VERSION)
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = time.strftime('%H%M%S')
    paths = [p for p in sys.argv[1:] if p in PATHS] or list(PATHS)
    try:
        async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
            for path in paths:
                if path == 'elasticsearch':
                    try:
                        (await es.get('/')).raise_for_status()
                    except httpx.HTTPError:
                        raise SystemExit(f"No Elasticsearch at {ES}: cd dev/stroom && docker compose --profile elastic up -d")
                await walk_path(ctx, path, stamp, es if path == 'elasticsearch' else None)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
