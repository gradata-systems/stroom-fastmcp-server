"""A source whose own XML is <records><record>...: never taken for an Events stream, nor for a Data Splitter's records.

    uv run python dev/e2e_records_source.py

Against the local Stroom stack (see dev/stroom), for two sources: <records> in no namespace, and <records> that
declares records:2 itself.

1. The template: profiled as XML for the XMLParser (Event Data (XML)), which create_pipeline takes; the Data
   Splitter template (Event Data (Text)) is refused for it.
2. The XSLT: generated in the source's own namespace (none, or records:2). A hand-written XSLT reading records
   with no xpath-default-namespace is saved for the source in no namespace, where those are its own elements, and
   refused for the records:2 one, where they would select nothing.
3. Validation: the translation's output is event-logging Events, valid against the event-logging schema in Stroom;
   a source record is not Events (no namespace: said so) or is validated against the records schema (records:2).
4. The index: the raw stream is refused as the Events to index; the Events are indexed into Lucene, the indexing
   XSLT's records:2 output valid against the records schema, every record indexed and found by its user.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from e2e_formats import decide  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from format_samples import SAMPLES  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import (feeds, generation, indexing, pipeline_writes, processing_writes, stepping, templates,  # noqa: E402
                   translation, validation)
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import FieldPlan  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

XSL = 'http://www.w3.org/1999/XSL/Transform'
SOURCES = {
    'no namespace': (SAMPLES['xml_records'], ''),
    'records:2': (SAMPLES['xml_records'].replace('<records>', '<records xmlns="records:2">'), 'records:2'),
}
# Written by hand, reading the source's elements by bare name with no xpath-default-namespace.
HAND = (f'<xsl:stylesheet xmlns:xsl="{XSL}" version="2.0"><xsl:template match="records">'
        '<Events xmlns="event-logging:3"><xsl:apply-templates select="record"/></Events></xsl:template>'
        '<xsl:template match="record"/></xsl:stylesheet>')


async def refusal(call, **kwargs) -> str:
    try:
        result = await call(**kwargs)
    except ToolError as e:
        return str(e)
    return '' if not (isinstance(result, dict) and str(result.get('status', '')).startswith('needs_')) else ''


async def run(ctx, stroom: StroomGateway, label: str, sample: str, namespace: str, stamp: str) -> None:
    tag = 'plain' if not namespace else 'rec2'
    print(f'\n### a <records> source, {label}')
    build, feed = f'records-{tag}-{stamp}', f'RECORDS-{tag.upper()}-{stamp}'

    print('  1. the template')
    profile = await feeds.profile_sample(ctx, sample)
    e2e.check(profile['format'] == 'xml' and (profile.get('namespace') or '') == namespace
              and profile['root'] == 'records' and 'XMLParser' in profile['suggested_parser'],
              f"profiled as XML in {namespace or 'no namespace'}, root records: {profile['suggested_parser']}")
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, sample))['stream_id']
    candidates = {c['name']: c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']}
    refused = await refusal(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-TEXT',
                            template_uuid=candidates['Event Data (Text)']['uuid'])
    e2e.check(bool(refused), f"the Data Splitter template is refused for it: {refused[:150]}")

    print('  2. the XSLT')
    draft = await generation.draft_translation_mapping(ctx, stream_ids=[raw], source_name='Acme', system_name='Acme',
                                                       environment='Test')
    mapping = decide(draft['mapping'], {'user': 'user'})
    e2e.check((mapping['input'], mapping.get('root'), mapping.get('record'), mapping.get('xml_namespace') or '')
              == ('xml', 'records', 'record', namespace), f"drafted as the source's own XML: {mapping['input']}, "
              f"{mapping.get('root')}/{mapping.get('record')} in {mapping.get('xml_namespace') or 'no namespace'}")
    saved = await e2e.agreed(generation.build_translation_xslt, ctx=ctx, mapping=mapping, stream_ids=[raw], build=build,
                             name=f'{feed}-Events', include_xslt=True)
    e2e.check(saved['ok'] and saved.get('saved'), f"saved: {saved.get('problems')}")
    declared = etree.fromstring(saved['xslt'].encode()).get('xpath-default-namespace')
    e2e.check(declared == namespace, f"the XSLT reads the input in {declared or 'no namespace'}")
    try:
        await translation.create_xslt(ctx, build, f'{feed}-HAND', HAND)
        hand = ''
    except ToolError as e:
        hand = str(e)
    if namespace:
        e2e.check('namespace records:2' in hand, f"a hand-written XSLT reading records bare is refused: {hand[:150]}")
    else:
        e2e.check(not hand, f"a hand-written XSLT reading its own records bare is saved: {hand[:150]}")
    pipeline = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Events',
                                template_uuid=candidates['Event Data (XML)']['uuid'],
                                set_properties=[pipeline_writes.PropertyValue(
                                    element='translationFilter', name='xslt', doc_uuid=saved['saved']['uuid'],
                                    doc_type='XSLT')])

    print('  3. validation')
    stepped = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    e2e.check(stepped['verdict'] == 'clean' and stepped['records_stepped'] == 3,
              f"{stepped['records_stepped']} records stepped clean, validated in Stroom against event-logging: "
              f"{[(g['class'], g.get('reason')) for g in stepped['groups']]}")
    output = (await stepping.step_pipeline(ctx, pipeline['uuid'], raw, 0))['elements']['translationFilter']['output']
    valid = await validation.validate_events(ctx, output)
    e2e.check(valid['valid'] and 'event-logging' in valid['schema'], f"its output is Events, valid against {valid['schema']}")
    record = sample[sample.index('<records'):]
    checked = await validation.validate_events(ctx, record)
    if namespace:
        e2e.check(checked['schema'] == 'file://records-v2.0.xsd',
                  f"a source record in records:2 is validated against the records schema: {checked['schema']}, "
                  f"{checked['error_count']} errors (its elements aren't records:2's data)")
    else:
        e2e.check(checked['schema'] is None and 'Not an Events document' in checked['errors'][0]['message'],
                  f"a source record in no namespace is said not to be Events: {checked['errors'][0]['message'][:120]}")

    print('  4. the index')
    await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'], stream_ids=[raw])
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [raw])
    e2e.check(done['gate'] == 'pass', f"processed into one Events stream: {done['streams']} {done['problems']}")
    events = done['streams'][0]['events']
    index_name = f'E2E-RECORDS-{tag.upper()}-{stamp}'
    wrong = await refusal(indexing.draft_index_mapping, ctx=ctx, backend='lucene', index_name=index_name,
                          convention='stroom-flat', events_stream_ids=[raw])
    e2e.check('not Events' in wrong, f"the raw stream is refused as the Events to index: {wrong[:120]}")
    draft = await indexing.draft_index_mapping(ctx, 'lucene', index_name, 'stroom-flat', events)
    plan = FieldPlan.model_validate(draft['plan'])
    user_field = next(f.name for f in plan.fields if f.source == 'EventSource/User/Id')
    index = await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=build, backend='lucene', name=index_name,
                             time_field=plan.time_field)
    await indexing.set_index_fields(ctx, index['uuid'], plan)
    xslt = await translation.create_xslt(ctx, build, f'{index_name}-XSLT', plan.xslt(), index_plan=plan)
    template = next(c for c in (await templates.find_pipeline_templates(ctx, 'indexing'))['candidates']
                    if c['backend'] == 'lucene')
    indexer = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=build, name=f'{index_name} - Indexing',
                               template_uuid=template['uuid'], xslt_uuid=xslt['uuid'], index_uuid=index['uuid'])
    stepped = await stepping.step_sample(ctx, indexer['uuid'], events)
    e2e.check(stepped['verdict'] == 'clean', f"the indexing pipeline steps the Events clean: {stepped['verdict']}")
    written = (await stepping.step_pipeline(ctx, indexer['uuid'], events[0], 0))['elements']
    out = next(v['output'] for k, v in written.items() if 'records:2' in (v.get('output') or ''))
    valid = await validation.validate_events(ctx, out)
    e2e.check(valid['valid'] and valid['schema'] == 'file://records-v2.0.xsd',
              f"its output is records:2, valid against {valid['schema']}: {valid['errors'][:1]}")
    await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=indexer['uuid'], stream_ids=events,
                     source_pipeline_uuid=pipeline['uuid'])
    gate = await processing_writes.wait_for_processing(ctx, indexer['uuid'], events, expect_events=False)
    e2e.check(gate['gate'] == 'pass', f"indexed with no Error stream: {gate['streams']} {gate['problems']}")
    dash = await indexing.create_verification_dashboard(ctx, build, f'{index_name}-VERIFY', index['uuid'], 'lucene',
                                                        ['StreamId', 'EventId', plan.time_field, user_field])
    found = await indexing.run_test_searches(ctx, dash['uuid'], events, 3,
                                             exact=[{'field': user_field, 'value': 'bob'}], pipeline_uuid=indexer['uuid'])
    e2e.check(found['passed'], f"every record indexed, and found by its user: "
                               f"{[(c['check'], c['returned'], c['pass']) for c in found['checks']]}")


async def main():
    stroom = StroomGateway(e2e.target_settings())
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    try:
        for label, (sample, namespace) in SOURCES.items():
            await run(ctx, stroom, label, sample, namespace, e2e.STAMP)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
