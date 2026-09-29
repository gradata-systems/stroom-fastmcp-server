"""Read-only checks of the tools against the live instance in .ai/secrets (a production node).

    uv run python dev/live_readonly.py [FEED]

Only read-only tools are called, and the gateway itself refuses anything that could change Stroom: PUT, DELETE,
uploads, and POSTs other than known read endpoints (searches, fetches, stepping). Stepping creates a temporary
session in Stroom and nothing else. Output is summaries only: counts, names and verdicts, not event content.
"""
import asyncio
import json
import re
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import (diagnosis, explorer, generation, instructions, pipelines, processing, sampling,  # noqa: E402
                   stepping, streams, templates, translation, validation, indexing, feeds)
from utils.consent import ConsentStore  # noqa: E402
from utils.elastic import ElasticTemplates  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402
from utils.xsltgen import TranslationMapping  # noqa: E402

# POST endpoints that only read. Everything else that isn't a GET is refused.
READ_POSTS = re.compile(r'^/(explorer/v2/(find|fetchExplorerNodes|getFromDocRef|info)|meta/v1/find\w*|data/v1/fetch'
                        r'|stepping/v1/(step|terminateStepping)|processorFilter/v1/find|processorTask/v1/find\w*'
                        r'|elasticIndex/v1/testIndex|elasticCluster/v1/testCluster|pipeline/v1/fetchPipelineLayers'
                        r'|[a-zA-Z]+/v1/find\w*)$')


class ReadOnlyGateway(StroomGateway):
    refused: list[str] = []

    async def request(self, method, path, body=None):
        if method != 'GET' and not (method == 'POST' and READ_POSTS.match(path.split('?')[0])):
            self.refused.append(f'{method} {path}')
            raise ToolError(f'read-only run: refused {method} {path}')
        return await super().request(method, path, body)

    async def datafeed(self, *args, **kwargs):
        self.refused.append('datafeed upload')
        raise ToolError('read-only run: refused an upload')


results: list[tuple[str, bool, str]] = []


async def check(name: str, call, judge=lambda r: (True, '')):
    """Run one tool call; record whether it worked and a one-line summary. Never stops the run."""
    started = time.monotonic()
    try:
        result = await call
        ok, summary = judge(result)
    except Exception as e:  # recorded, not raised: the run carries on
        result, ok, summary = None, False, f'{type(e).__name__}: {str(e)[:200]}'
        if not isinstance(e, ToolError):
            summary += ' | ' + traceback.format_exc().strip().splitlines()[-2].strip()[:150]
    results.append((name, ok, summary))
    print(f"  {'PASS' if ok else 'FAIL'} {name} ({time.monotonic() - started:.1f}s): {summary}")
    return result


def env(path: Path) -> dict[str, str]:
    return {k.strip(): v.strip() for k, v in (l.split('=', 1) for l in path.read_text().splitlines() if '=' in l)}


async def main(feed: str):
    secrets = env(ROOT / '.ai' / 'secrets')
    settings = Settings(_env_file=None, stroom_url=secrets['STROOM_URL'], dev_no_auth=True,
                        stroom_api_key=secrets['STROOM_API_KEY'], event_logging_version='3.5.2')
    stroom = ReadOnlyGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False),
        'elastic': ElasticTemplates(settings)})
    try:
        print('\n### templates and documents')
        found = await check('find_pipeline_templates translation', templates.find_pipeline_templates(ctx, 'translation'),
                            lambda r: (bool(r.get('candidates')), f"{len(r.get('candidates') or [])} candidates, "
                                       f"first {((r.get('candidates') or [{}])[0]).get('name')}"))
        await check('find_pipeline_templates indexing', templates.find_pipeline_templates(ctx, 'indexing'),
                    lambda r: (bool(r.get('candidates')), f"{len(r.get('candidates') or [])} candidates, backends "
                               f"{sorted({c.get('backend') for c in r.get('candidates') or []} - {None})}"))
        if found and found.get('candidates'):
            first = found['candidates'][0]['uuid']
            await check('describe_template_contract', templates.describe_template_contract(ctx, first),
                        lambda r: (True, f"child supplies {[s['element'] for s in r.get('child_must_supply', [])]}, "
                                   f"{len(r.get('shared', []))} shared"))
            await check('list_template_children', templates.list_template_children(ctx, first),
                        lambda r: (True, f"{len(r.get('children') or [])} children"))
        docs = await check('find_documents pipeline', explorer.find_documents(ctx, f'{feed}*', ['Pipeline'], 20),
                           lambda r: (bool(r.get('documents') or r.get('values')), f"{len(r.get('documents') or r.get('values') or [])} found"))
        pipe = next((d for d in (docs or {}).get('documents') or [] if d.get('name') == f'{feed}-Events'), None)
        if not pipe:
            print(f"  (no pipeline named {feed}-Events; stopping the pipeline checks)")
            return
        described = await check('describe_pipeline', pipelines.describe_pipeline(pipe['uuid'], ctx),
                                lambda r: (bool(r.get('elements')), f"{len(r.get('elements') or [])} elements"))
        xslt = next((p['value'] for p in (described or {}).get('properties') or []
                     if p.get('name') == 'xslt' and isinstance(p.get('value'), dict)
                     and p['value'].get('name') == f'{feed}-Events'), None)
        if xslt:
            await check('describe_translation', validation.describe_translation(ctx, xslt_uuid=xslt['uuid']),
                        lambda r: (True, f"keys {sorted(r)[:8]}"))

        print('\n### streams and processing')
        raws = await check('find_streams raw', streams.find_streams(ctx, feed=feed, stream_type='Raw Events', limit=5),
                           lambda r: (bool(r.get('streams')), f"{len(r.get('streams') or [])} streams"))
        raw = (raws or {}).get('streams', [{}])[0].get('id')
        kids = await check('get_stream_children', streams.get_stream_children(ctx, raw),
                           lambda r: (True, f"{[(c.get('type'), c.get('id')) for c in r.get('children') or []][:4]}"))
        await check('get_stream_attributes', streams.get_stream_attributes(ctx, raw),
                    lambda r: (bool(r), f"{len(r.get('attributes') or r)} attributes"))
        events_id = next((c['id'] for c in (kids or {}).get('children') or [] if c.get('type') == 'Events'), None)
        await check('processing_status', processing.processing_status(ctx, pipe['uuid']),
                    lambda r: (True, f"{len(r.get('filters') or [])} filter(s)"))
        if events_id:
            await check('summarise_events', streams.summarise_events(ctx, [events_id]),
                        lambda r: (True, f"keys {sorted(r)[:6]}"))
            await check('summarise_errors', streams.summarise_errors(ctx, raw),
                        lambda r: (True, f"keys {sorted(r)[:6]}"))
            read = await check('read_stream events', streams.read_stream(ctx, events_id, record_count=3),
                               lambda r: (bool(r.get('records')), f"{len(r.get('records') or [])} of {r.get('total_records')} "
                                          f"records, {sum(map(len, r.get('records') or []))} chars (not shown)"))
            xml = ((read or {}).get('records') or [''])[0]  # each record is a whole Events document
            if xml:
                await check('validate_events (local XSD)', validation.validate_events(ctx, xml, '3.5.2'),
                            lambda r: (True, f"valid={r.get('valid')}, {len(r.get('errors') or [])} error(s)"))
                await check('check_event_quality', validation.check_event_quality(ctx, xml),
                            lambda r: (True, f"{len(r.get('findings') or r.get('issues') or [])} finding(s)"))

        print('\n### stepping, survey and diagnosis')
        await check('step_pipeline record 0', stepping.step_pipeline(ctx, pipe['uuid'], raw, record=0),
                    lambda r: (True, f"{len(r.get('errors') or [])} error(s), elements {sorted(r.get('elements') or {})[:4]}"))
        await check('step_sample 20 records', stepping.step_sample(ctx, pipe['uuid'], [raw], records_per_stream=20),
                    lambda r: (r.get('records_stepped', 0) > 0, f"{r.get('records_stepped')} stepped, verdict {r.get('verdict')}"))
        survey = await check('survey_feed (no build)', sampling.survey_feed(ctx, feed, max_streams=4),
                             lambda r: (bool(r.get('shapes')), f"{len(r['shapes'])} kinds in {len(r['streams_read'])} streams; {r['coverage'][:30]}"))
        if survey:
            await check('step_records survey locations', stepping.step_records(ctx, pipe['uuid'], survey['locations'][:30]),
                        lambda r: (r['records_stepped'] > 0, f"{r['records_stepped']} stepped, {r['records_with_errors']} flagged, "
                                   f"{len(r['shapes_not_clean'])} kinds not clean"))
        if events_id:
            await check('locate_event 1', diagnosis.locate_event(ctx, events_id, event_id=1),
                        lambda r: (r.get('same_as_stored') is not None, f"location {r.get('location')}, same_as_stored={r.get('same_as_stored')}"))

        print('\n### generation, instructions and elastic (reads only)')
        await check('get_instructions', instructions.get_instructions(ctx, feeds=[feed]),
                    lambda r: (True, f"{len(r['instructions'])} apply, {len(r['other_instruction_docs'])} elsewhere"))
        await check('get_field_conventions', indexing.get_field_conventions(ctx),
                    lambda r: (True, f"keys {sorted(r)[:5]}"))
        await check('find_elastic_clusters', indexing.find_elastic_clusters(ctx),
                    lambda r: (True, f"{len(r.get('clusters') or [])} cluster(s)"))
        mapping = TranslationMapping.model_validate({'input': 'json', 'common': [
            {'path': 'EventTime/TimeCreated', 'field': 'timestamp'}, {'path': 'EventSource/System/Name', 'value': 'Keycloak'},
            {'path': 'EventSource/System/Environment', 'value': 'Prod'}, {'path': 'EventSource/Generator', 'value': 'keycloak'},
            {'path': 'EventSource/Device/HostName', 'field': 'hostname'}],
            'events': [{'name': 'any', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'log'},
                                                  {'path': 'EventDetail/Unknown/Data', 'data_name': 'body', 'field': 'body'}]}]})
        await check('build_translation_xslt (v3.5.2 schema from Stroom)',
                    generation.build_translation_xslt(ctx, mapping, '3.5.2', [feed]),
                    lambda r: (r.get('ok'), f"ok={r.get('ok')}, problems {r.get('problems')[:2]}"))
    finally:
        await stroom.close()
        passed = sum(ok for _, ok, _ in results)
        print(f"\n{passed}/{len(results)} passed; refused write requests: {stroom.refused or 'none'}")


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else 'Keycloak-V1.2'))
