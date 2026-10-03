"""The evaluation set run by an agent: headless Claude Code, connected to this checkout's server, with a scripted user.

    uv run python dev/eval/run_agent.py                          # every case, Claude Code's default model
    uv run python dev/eval/run_agent.py --model haiku 06 json    # some cases, a lighter model
    uv run python dev/eval/run_agent.py --model default --model haiku 01   # one case per model, side by side
    uv run python dev/eval/run_agent.py --repeat 3                # each case three times: a pass rate per case

Needs the local Stroom stack (dev/stroom) and the `claude` CLI signed in: it runs on that sign-in, no API key.

Per case: the server's onboard_data_source prompt, rendered with the case's sample, is the first message, with
the case's request and the names to use. The agent (claude -p) has only this server's tools and its resources;
no file, shell or web tools, and none of your settings, CLAUDE.md or other MCP servers. Confirmations come back
as pending ids (no elicitation), so the agent has to ask the user. When it stops, a second model plays the user:
it reads the agent's last message and agrees, answers from the request, gives the case's next hint (counted)
when the agent asks for help, or tells it to carry on, until the agent says it has finished.

The output is then scored by the same checks as --reference, read from Stroom rather than from what the agent
says: the build's Events (record count, event types, paths, validity) and a verification search on its index.

A model's run of a case is one sample: with --repeat, a case passes when most of its runs pass, so a flaky case
shows as such. The exit criterion (run_eval.criterion) is every case passing with no hints, for the default model;
lighter models are measured against it, not held to it.

Results go to dev/eval/results/<time>-agent-<model>.json, with each case's transcript (stream-json) and the
server's log in dev/eval/results/<time>-agent/.
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_eval import (MAX_HINTS, RESULTS, ROOT, Score, check_output, criterion, load_cases, local_ctx,  # noqa: E402
                      print_score, sample_text, summary)

STROOM_URL = 'http://127.0.0.1:18080'
SERVER = 'stroom'
PROMPT = 'onboard_data_source'
# Besides the server's own tools: reading its resources (the guides and conventions the prompt points to).
RESOURCE_TOOLS = ['ListMcpResourcesTool', 'ReadMcpResourceTool']
PENDING_ID = re.compile(r'(?:conf|appr)-[a-z2-7]+\.[a-z2-7]{16}')   # utils/consent.py's pending ids

USER_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['action', 'reply'],
    'properties': {'action': {'type': 'string', 'enum': ['agree', 'answer', 'help', 'continue', 'promote', 'done']},
                   'reply': {'type': 'string'}}}
USER_ROLE = """You play the user in an evaluation of an AI agent that onboards a log source into Stroom through an
MCP server. You know only the request below. Read the agent's latest message and choose the action:

- agree: it asks you to confirm or approve something within the request: a feed or index name, a pending
  confirmation or approval, a proposed template, backend, volume group, convention, mapping or plan. Reply with a
  short agreement ("Yes, go ahead.").
- answer: it asks a question the request or sample answers (there are no more sample files than those given; the
  index is Lucene, with the stroom-flat convention). Reply briefly, using only the request and sample.
- help: it is stuck, reports a problem it cannot solve, or asks how to do something technical the request does not
  say (which element, regex, XSLT or tool to use, how to fix an error). Leave reply empty.
- continue: it stopped without asking anything before the work is finished (events processed and validated, then
  indexed, the index verified and both pipelines documented), or it asks whether to carry on. Leave reply empty.
- promote: it asks whether or where to promote the build. Leave reply empty.
- done: it says the work is finished, or that it gives up. Leave reply empty.

Never give technical guidance of your own."""
CONTINUE = ("Please carry on. The work is done when the events are processed and valid, indexed, the index is verified "
            "and both pipelines are documented.")
PROMOTE = ("Don't promote the build: this is a scratch environment. Stop once the index is verified and both pipelines "
           "are documented.")
NO_HINT = "I can't help with that; do what you think is right."
# Claude Code's own refusal to run (the plan's session or usage limit): not the agent's failure, and every run after
# it would fail the same way in seconds.
LIMIT = re.compile(r'(session|usage|weekly|rate) limit', re.I)


class UsageLimit(Exception):
    pass


@dataclass
class AgentScore(Score):
    model: str = ''
    run: int = 1
    build: str = ''
    user_turns: int = 0
    help_requests: int = 0
    tool_calls: int = 0
    self_confirmed: int = 0
    cost_usd: float = 0.0
    ended: str = ''
    transcript: str = ''


def first_message(prompt: str, case: dict[str, Any], build: str, feed: str) -> str:
    # Lucene: the local stack has an Elasticsearch cluster doc (dev/e2e_elastic_handover.py) but no Elasticsearch.
    return (f"{prompt}\n\n{case['request'].strip()}\n\nThose are all the sample files there are. Index into Lucene "
            f"(there is no Elasticsearch here) with the stroom-flat field convention. Name the build `{build}` and the "
            f"feed `{feed}`. Stop once the index is verified and both pipelines are documented; don't promote the build.")


async def render_prompt(url: str, case: dict[str, Any]) -> str:
    """The server's onboarding prompt for the case, as a client gets it."""
    from fastmcp import Client
    async with Client(url) as client:
        result = await client.get_prompt(PROMPT, {'sample': sample_text(case), 'source_name': case['name']})
    return '\n\n'.join(m.content.text for m in result.messages if getattr(m.content, 'text', None))


# --- headless Claude Code ---
def claude_exe() -> str:
    exe = shutil.which('claude')
    if not exe:
        raise SystemExit("The claude CLI is not on PATH")
    return exe


async def claude(args: list[str], message: str, cwd: Path, transcript: Path | None, timeout: float) -> list[dict[str, Any]]:
    """Run `claude -p` with the message on stdin; the stream-json events it printed (appended to the transcript)."""
    proc = await asyncio.create_subprocess_exec(
        claude_exe(), '-p', *args, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024)
    proc.stdin.write(message.encode('utf-8'))
    proc.stdin.close()
    events: list[dict[str, Any]] = []

    async def read():
        async for line in proc.stdout:
            text = line.decode('utf-8', errors='replace').strip()
            if not text:
                continue
            if transcript:
                with transcript.open('a', encoding='utf-8') as f:
                    f.write(text + '\n')
            try:
                events.append(json.loads(text))
            except ValueError:
                pass
        await proc.wait()
    try:
        await asyncio.wait_for(read(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        events.append({'type': 'result', 'is_error': True, 'subtype': 'timeout', 'result': f'timed out after {timeout:.0f}s'})
    if proc.returncode and not any(e.get('type') == 'result' for e in events):
        err = (await proc.stderr.read()).decode('utf-8', errors='replace').strip()
        events.append({'type': 'result', 'is_error': True, 'subtype': 'exit', 'result': f'exit {proc.returncode}: {err[-2000:]}'})
    return events


def isolation_args(model: str | None) -> list[str]:
    """No settings, hooks or plugins of the user's, and no MCP servers but those given."""
    return ['--setting-sources', 'project', '--strict-mcp-config', *(['--model', model] if model else [])]


class Agent:
    """One case's agent session: the first message starts it, each later one resumes it."""

    def __init__(self, model: str | None, effort: str | None, server_url: str, workdir: Path, transcript: Path,
                 timeout: float):
        self.session = str(uuid.uuid4())
        self.started = False
        self.workdir, self.transcript, self.timeout = workdir, transcript, timeout
        config = workdir / 'mcp.json'
        config.write_text(json.dumps({'mcpServers': {SERVER: {'type': 'http', 'url': server_url}}}), encoding='utf-8')
        self.args = [*isolation_args(model), '--mcp-config', str(config), '--tools', ','.join(RESOURCE_TOOLS),
                     '--allowedTools', ','.join([f'mcp__{SERVER}', *RESOURCE_TOOLS]), '--permission-mode', 'dontAsk',
                     '--output-format', 'stream-json', '--verbose', *(['--effort', effort] if effort else [])]
        self.cost, self.tool_calls, self.self_confirmed = 0.0, 0, 0

    async def send(self, message: str) -> dict[str, Any]:
        session = ['--resume', self.session] if self.started else ['--session-id', self.session]
        self.started = True
        events = await claude([*self.args, *session], message, self.workdir, self.transcript, self.timeout)
        init = next((e for e in events if e.get('type') == 'system' and e.get('subtype') == 'init'), None)
        if init:
            status = {s.get('name'): s.get('status') for s in init.get('mcp_servers') or []}
            if status.get(SERVER) != 'connected':
                raise RuntimeError(f"Claude Code could not connect to the server: {status}")
        calls = [b for e in events if e.get('type') == 'assistant'
                 for b in (e.get('message') or {}).get('content') or [] if b.get('type') == 'tool_use']
        self.tool_calls += len(calls)
        # A pending id passed back in the same turn it was issued: agreed to without asking the user.
        issued = set(PENDING_ID.findall(json.dumps([e for e in events if e.get('type') == 'user'])))
        self.self_confirmed += sum(1 for c in calls for key in ('confirmation_id', 'approval_id')
                                   if (c.get('input') or {}).get(key) in issued)
        result = next((e for e in reversed(events) if e.get('type') == 'result'), None) or {
            'is_error': True, 'result': 'no result from claude'}
        self.cost += result.get('total_cost_usd') or 0.0
        return result


async def play_user(case: dict[str, Any], agent_said: str, model: str, workdir: Path) -> dict[str, str]:
    """The scripted user's decision on the agent's last message: {'action', 'reply'}."""
    said = agent_said if len(agent_said) <= 8000 else agent_said[:2000] + '\n[...]\n' + agent_said[-6000:]
    message = (f"{USER_ROLE}\n\n## The request\n\n{case['request'].strip()}\n\n{sample_text(case)}\n\n"
               f"## The agent's latest message\n\n{said}")
    events = await claude([*isolation_args(model), '--tools', '', '--output-format', 'json', '--json-schema',
                           json.dumps(USER_SCHEMA), '--no-session-persistence'], message, workdir, None, 300)
    result = next((e for e in events if e.get('type') == 'result'), {})
    decision = result.get('structured_output')
    if not isinstance(decision, dict) or decision.get('action') not in USER_SCHEMA['properties']['action']['enum']:
        return {'action': 'continue', 'reply': ''}
    return decision


# --- one case ---
async def run_case(case: dict[str, Any], args: argparse.Namespace, model: str | None, server_url: str, out: Path,
                   run: int = 1) -> AgentScore:
    tag, stamp = case['id'].split('_', 1)[0], time.strftime('%H%M%S')   # a case takes minutes: one stamp per run
    build, feed = f'eval-{tag}-{stamp}', f'EVAL-{tag}-{stamp}'
    name = f"{case['id']}-run{run}.jsonl" if args.repeat > 1 else f"{case['id']}.jsonl"
    transcript = out / (model or 'default') / name
    transcript.parent.mkdir(parents=True, exist_ok=True)
    score = AgentScore(case['id'], 'agent', model=model or 'default', run=run, build=build,
                       transcript=str(transcript.relative_to(ROOT)))
    started = time.monotonic()
    hints = list(case.get('hints') or [])
    with tempfile.TemporaryDirectory(prefix='stroom-eval-') as tmp:
        workdir = Path(tmp)
        agent = Agent(model, args.effort, server_url, workdir, transcript, args.turn_timeout)
        try:
            message = first_message(await render_prompt(server_url, case), case, build, feed)
            while True:
                result = await agent.send(message)
                if result.get('is_error'):
                    if LIMIT.search(str(result.get('result'))):
                        raise UsageLimit(str(result.get('result'))[:200])
                    score.ended = f"agent error: {str(result.get('result'))[:300]}"
                if score.user_turns >= args.max_user_turns:
                    score.ended = f"stopped after {score.user_turns} user turns"
                    break
                decision = await play_user(case, result.get('result') or '', args.user_model, workdir)
                action = decision['action']
                print(f"    user: {action}{(' - ' + decision['reply'][:100]) if decision['reply'] else ''}")
                if action == 'done':
                    score.ended = 'agent finished'
                    break
                if action == 'help':
                    score.help_requests += 1
                    if hints:
                        score.hints += 1
                        message = hints.pop(0)
                    else:
                        message = NO_HINT
                else:
                    message = {'continue': CONTINUE, 'promote': PROMOTE}.get(action) or decision['reply'] or CONTINUE
                score.user_turns += 1
        except UsageLimit:
            raise
        except Exception as e:  # a case failing must not stop the evaluation
            score.ended = f"{type(e).__name__}: {e}"
        score.cost_usd, score.tool_calls, score.self_confirmed = round(agent.cost, 4), agent.tool_calls, agent.self_confirmed
    await score_build(score, case, build, feed)
    score.seconds = round(time.monotonic() - started, 1)
    score.passed = score.stage1 and score.indexed and score.hints <= MAX_HINTS
    return score


async def score_build(score: AgentScore, case: dict[str, Any], build: str, feed: str) -> None:
    """Score what the build holds in Stroom, whatever the agent said about it."""
    from tools import builds, indexing, streams, validation
    ctx = local_ctx()
    tools = {'read_stream': streams.read_stream, 'validate_events': validation.validate_events}

    async def call(name: str, **kwargs):
        return await tools[name](ctx, **kwargs)
    try:
        try:
            docs = await builds._build_docs(ctx, build)
        except Exception:
            docs = []
        if not docs:
            score.problems.append(f"no build '{build}' (the agent was asked to use that name)")
        feeds = list(dict.fromkeys([d['name'] for d in docs if d['type'] == 'Feed'] + [feed]))
        raws = []
        for name in feeds:
            found = await streams.find_streams(ctx, feed=name, stream_type='Raw Events', limit=50)
            raws += [s['id'] for s in found['streams']]
        if not raws:
            score.problems.append(f"no Raw Events streams in {feeds}")
            return
        events = []
        for raw in sorted(raws):
            children = (await streams.get_stream_children(ctx, raw))['children']
            produced = [c['id'] for c in children if c['type'] == 'Events']
            if produced:
                events.append(max(produced))   # the latest, should the agent have reprocessed
            else:
                score.problems.append(f"raw stream {raw} has no Events stream")
        await check_output(call, score, case, events)
        indexes = [d for d in docs if d['type'] == 'Index']
        if not indexes:
            score.problems.append('no Lucene index in the build')
            return
        if not events:
            return
        # An agent may abandon an index (one made without fields, say) and index into another: any index of the build
        # that finds the events counts, newest first.
        failed = []
        for index in reversed(indexes):
            verified = await indexing.verify_index(ctx, build, index['uuid'], 'lucene', events, score.events,
                                                   ['StreamId', 'EventId'], retries=4 if not failed else 1)
            if verified.get('passed'):
                score.indexed = True
                break
            failed.append(f"{index['name']}: {[c for c in verified.get('checks', []) if not c.get('pass')]}")
        if not score.indexed:
            score.problems.append(f"index searches: {failed}")
        warnings = await builds.build_checks(ctx, docs)
        if warnings:
            score.notes.append(f"before promotion: {warnings}")
    except Exception as e:
        score.problems.append(f"scoring: {type(e).__name__}: {e}")
    finally:
        await ctx.lifespan_context['stroom'].close()


def repeated_summary(scores: list[AgentScore], repeat: int) -> str:
    """Per case over its runs: a case passes when most of its runs pass."""
    rows = ['| Case | Passed runs | Stage 1 | Indexed | Help requests | Confirmed without asking | Minutes (mean) |',
            '| --- | --- | --- | --- | --- | --- | --- |']
    passing = 0
    for case in dict.fromkeys(s.case for s in scores):
        runs = [s for s in scores if s.case == case]
        passed = sum(s.passed for s in runs)
        passing += passed * 2 > len(runs)
        rows.append(f"| {case} | {passed}/{len(runs)} | {sum(s.stage1 for s in runs)}/{len(runs)} | "
                    f"{sum(s.indexed for s in runs)}/{len(runs)} | {sum(s.help_requests for s in runs)} | "
                    f"{sum(s.self_confirmed for s in runs)} | {sum(s.seconds for s in runs) / len(runs) / 60:.1f} |")
    cases = len(rows) - 2
    rows.append(f"\n{passing} of {cases} cases passed in most of their {repeat} runs; {criterion(passing, cases)}.")
    return '\n'.join(rows)


async def rescore(path: Path) -> None:
    """Score an earlier run's builds again from Stroom, with the current checks; the agent's own figures are kept."""
    import re
    cases = {c['id']: c for c in load_cases()}
    rescored = []
    for old in json.loads(path.read_text(encoding='utf-8')):
        if LIMIT.search(old.get('ended') or ''):
            print(f"  {old['case']} run {old.get('run', 1)}: never ran (Claude Code's limit); left out")
            continue
        build = old.get('build')
        if not build:   # results from before builds were recorded: the agent's tool calls name it
            tag = old['case'].split('_', 1)[0]
            found = re.search(rf'"build":\s*"(eval-{tag}-[0-9]+)"', (ROOT / old['transcript']).read_text(encoding='utf-8'))
            if not found:
                print(f"  {old['case']} run {old.get('run', 1)}: no build in its transcript; kept as it was")
                rescored.append(AgentScore(**{k: v for k, v in old.items() if k in AgentScore.__dataclass_fields__}))
                continue
            build = found.group(1)
        feed = re.sub(r'^eval-', 'EVAL-', build)
        keep = {k: old[k] for k in ('model', 'run', 'user_turns', 'help_requests', 'tool_calls', 'self_confirmed',
                                    'cost_usd', 'ended', 'transcript', 'seconds', 'hints') if k in old}
        score = AgentScore(old['case'], 'agent', build=build, **keep)
        await score_build(score, cases[old['case']], build, feed)
        score.passed = score.stage1 and score.indexed and score.hints <= MAX_HINTS
        if score.passed != old.get('passed'):
            print(f"  {old['case']} run {score.run}: {'pass' if old.get('passed') else 'fail'} -> "
                  f"{'pass' if score.passed else 'fail'}")
        rescored.append(score)
    out = path.with_name(path.stem + '-rescored.json')
    out.write_text(json.dumps([asdict(s) for s in rescored], indent=1), encoding='utf-8')
    runs = max((s.run for s in rescored), default=1)
    print('\n' + (repeated_summary(rescored, runs) if runs > 1 else summary(rescored)) + f"\nResults: {out}")


# --- the server ---
async def wait_healthy(url: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if (await client.get(url, timeout=2)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise SystemExit(f"Nothing healthy at {url}")
            await asyncio.sleep(1)


async def start_server(port: int, log: Path) -> asyncio.subprocess.Process:
    """This checkout's server on localhost, without sign-in, against the local stack, as the admin key."""
    import e2e_phase2 as p2
    key = p2.env(ROOT / 'dev' / 'stroom' / '.env')['STROOM_ADMIN_API_KEY']
    env = {**os.environ, 'STROOM_MCP_STROOM_URL': STROOM_URL, 'STROOM_MCP_STROOM_API_KEY': key,
           'STROOM_MCP_DEV_NO_AUTH': 'true', 'STROOM_MCP_HOST': '127.0.0.1', 'STROOM_MCP_PORT': str(port),
           'STROOM_MCP_EVENT_LOGGING_VERSION': p2.VERSION, 'STROOM_MCP_DEFAULT_CONVENTION': 'stroom-flat',
           'STROOM_MCP_USE_ELICITATION': 'false'}
    handle = log.open('wb')
    proc = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / 'main.py'), cwd=ROOT, env=env,
                                                stdout=handle, stderr=asyncio.subprocess.STDOUT)
    await wait_healthy(f'http://127.0.0.1:{port}/healthz', 60)
    return proc


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('cases', nargs='*', help="Case ids or parts of them, e.g. 06 json")
    parser.add_argument('--model', action='append', dest='models', metavar='MODEL',
                        help="Claude Code model for the agent (alias or full name), or 'default'; repeat to compare. "
                             "Default: Claude Code's default model.")
    parser.add_argument('--repeat', type=int, default=1, help="Runs per case and model; a case passes when most pass.")
    parser.add_argument('--user-model', default='haiku', help="Model playing the user (default: haiku).")
    parser.add_argument('--effort', choices=['low', 'medium', 'high', 'xhigh', 'max'], help="The agent's effort level.")
    parser.add_argument('--max-user-turns', type=int, default=30, help="Replies the user gives before the case stops.")
    parser.add_argument('--turn-timeout', type=float, default=2700, help="Seconds one agent turn may take.")
    parser.add_argument('--port', type=int, default=8767, help="Port for this checkout's server.")
    parser.add_argument('--server-url', help="Use a server already running (dev_no_auth, local stack) instead.")
    parser.add_argument('--rescore', type=Path, metavar='RESULTS',
                        help="Score an earlier results file's builds again from Stroom, without running the agent.")
    args = parser.parse_args()
    if args.rescore:
        await rescore(args.rescore)
        return
    cases = load_cases(args.cases)
    if not cases:
        raise SystemExit(f"No cases match {args.cases}")
    try:
        async with httpx.AsyncClient() as client:
            await client.get(STROOM_URL, timeout=5)
    except httpx.HTTPError:
        raise SystemExit(f"Stroom is not reachable at {STROOM_URL}: cd dev/stroom && docker compose up -d")

    when = time.strftime('%Y%m%d-%H%M%S')
    out = RESULTS / f'{when}-agent'
    out.mkdir(parents=True, exist_ok=True)
    server = None
    if args.server_url:
        url = args.server_url
    else:
        server = await start_server(args.port, out / 'server.log')
        url = f'http://127.0.0.1:{args.port}/mcp'
    models = [None if m == 'default' else m for m in (args.models or ['default'])]
    try:
        by_model = {}
        stopped = None
        for model in models:
            scores = []
            for case in cases:
                for run in range(1, args.repeat + 1):
                    label = f" run {run}/{args.repeat}" if args.repeat > 1 else ''
                    print(f"### {case['id']} ({model or 'default'}){label}")
                    try:
                        scores.append(await run_case(case, args, model, url, out, run))
                    except UsageLimit as e:
                        stopped = (model or 'default', case['id'], str(e))
                        break
                    s = scores[-1]
                    print_score(s)
                    print(f"    {s.ended}; {s.user_turns} user turns, {s.help_requests} help requests, {s.tool_calls} "
                          f"tool calls, {s.self_confirmed} confirmed without asking, ${s.cost_usd}")
                if stopped:
                    break
            name = model or 'default'
            path = RESULTS / f'{when}-agent-{name}.json'
            path.write_text(json.dumps([asdict(s) for s in scores], indent=1), encoding='utf-8')
            by_model[name] = (scores, path)
            if stopped:
                break
        if stopped:
            left = [c['id'] for c in cases[[c['id'] for c in cases].index(stopped[1]):]]
            print()
            print(f"Stopped: Claude Code would not run ({stopped[2]}). The runs before it are kept; to finish, once "
                  f"the limit resets, run {stopped[0]} on: {' '.join(c.split('_', 1)[0] for c in left)}")
        for name, (scores, path) in by_model.items():
            cost = sum(s.cost_usd for s in scores)
            table = repeated_summary(scores, args.repeat) if args.repeat > 1 else summary(scores)
            print(f"\n## {name}\n\n{table}\nAPI-equivalent cost ${cost:.2f}, "
                  f"{sum(s.tool_calls for s in scores)} tool calls, "
                  f"{sum(s.self_confirmed for s in scores)} confirmations or approvals given without asking the user."
                  f"\nResults: {path.relative_to(ROOT)}")
        print(f"Transcripts and server log: {out.relative_to(ROOT)}")
    finally:
        if server:
            server.terminate()
            await server.wait()


if __name__ == '__main__':
    asyncio.run(main())
