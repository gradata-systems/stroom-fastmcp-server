"""How generated XSLTs are written, and the gates around them, against the local Stroom stack (see dev/stroom).

    uv run python dev/e2e_xslt_style.py

Driving the real tools, as the agent would:

1. Layouts: one mapping saved in each layout (modes, the default; named; inline). Each steps clean in Stroom, and
   all three give exactly the same Events. Conversions used by several elements (a time format, strip_domain) are
   the XSLT's own xsl:functions, which Stroom runs.
2. The processing gate: once the XSLT is replaced after processing, wait_for_processing fails until the new code
   steps clean.
3. Shared functions: a sibling pipeline, written in the house style (each part a mode template), calls a shared
   XSLT's functions and a shared named template from inside part templates. describe_template lists the functions
   with their namespace and calls, and places the template where its part is applied (EventSource/Device). A mapping
   calling the functions through a functions entry steps clean, with the functions' values in the Events; one
   calling them without the entry is refused.
4. Unknown: kept for traffic only after a refusal names the action elements, with keep_unknown, confirmed by the
   user; a hand-written XSLT's Unknown block says what the records hold; and Unknown can't be accepted as a benign
   error in the documentation.
5. The naming choice: get_field_conventions asks in a form and gives the next call.
"""
import asyncio
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from e2e_generator import FIREWALL, MAPPINGS  # noqa: E402
from e2e_shared_xslt import COMMON_EVENT, SHARED_EVENT, fixture_template, shared_doc  # noqa: E402

# A template of this suite's own: shared with e2e_shared_xslt, its children from both suites' runs filled the
# survey's ten, those of the other suite sorting first, and this run's sibling went unread.
TEXT_TEMPLATE = 'E2E Style Text'
from fastmcp.exceptions import ToolError  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import (builds, feeds, generation, indexing, pipeline_writes, processing_writes, stepping,  # noqa: E402
                   templates, translation, validation)
from tools.pipeline_writes import PropertyValue  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402
from utils.xsltgen import TranslationMapping, generate  # noqa: E402

XSL = 'http://www.w3.org/1999/XSL/Transform'
NS = {'xsl': XSL, 'e': 'event-logging:3'}
COMMON_FUNCTIONS = 'E2E-Common-Functions-V1'
FUNCTIONS_NS = 'urn:e2e:functions'
SHARED_FUNCTIONS = f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xmlns:xsl="{XSL}" xmlns:e2e="{FUNCTIONS_NS}" xmlns:xs="http://www.w3.org/2001/XMLSchema" version="3.0">
  <!-- How urgent a severity is, the same way in every translation. -->
  <xsl:function name="e2e:severityLevel" as="xs:string">
    <xsl:param name="severity" as="xs:string?" />
    <xsl:sequence select="if ($severity = ('ERROR', 'CRITICAL')) then 'high' else 'normal'" />
  </xsl:function>
  <xsl:function name="e2e:isInternal" as="xs:boolean">
    <xsl:param name="ip" as="xs:string?" />
    <xsl:sequence select="starts-with(string($ip), '192.0.2.')" />
  </xsl:function>
</xsl:stylesheet>
"""
# The sibling, in the house style seen live: each part a template rule with its own mode. Only read, never run.
SIBLING = f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="records:2" xmlns="event-logging:3" xmlns:stroom="stroom"
    xmlns:e2e="{FUNCTIONS_NS}" xmlns:xsl="{XSL}" version="3.0">
  <xsl:import href="{COMMON_EVENT}" />
  <xsl:import href="{COMMON_FUNCTIONS}" />
  <xsl:template match="records"><Events><xsl:apply-templates select="record" mode="event" /></Events></xsl:template>
  <xsl:template match="node()" mode="event">
    <Event>
      <EventTime><TimeCreated><xsl:value-of select="data[@name='when']/@value" /></TimeCreated></EventTime>
      <EventSource>
        <System><Name>Other</Name><Environment>Dev</Environment></System>
        <Generator>other</Generator>
        <xsl:apply-templates select="." mode="eventSourceDevice" />
      </EventSource>
      <EventDetail><TypeId>Other</TypeId><Unknown><xsl:apply-templates select="." mode="commonData" /></Unknown></EventDetail>
    </Event>
  </xsl:template>
  <xsl:template match="node()" mode="eventSourceDevice">
    <xsl:call-template name="eventSourceDevice"><xsl:with-param name="ip" select="data[@name='src']/@value" /></xsl:call-template>
  </xsl:template>
  <xsl:template match="node()" mode="commonData">
    <Data Name="level" Value="{{e2e:severityLevel(data[@name='severity']/@value)}}" />
  </xsl:template>
</xsl:stylesheet>
"""
# The firewall sample with domain users, for strip_domain.
SAMPLE = FIREWALL['sample'].replace(',admin,', ',admin@corp.example,')
TIME = "yyyy-MM-dd'T'HH:mm:ssXXX"


def style_mapping(layout: str | None = None, system_name: str = 'E2E') -> dict:
    """The firewall mapping, with strip_domain on two users and the time format twice: both become functions."""
    m = copy.deepcopy(MAPPINGS['firewall'])
    for f in m['common']:
        if f['path'] == 'EventSource/User/Id':
            f['transform'] = 'strip_domain'
        if f['path'] == 'EventSource/System/Name':
            f['value'] = system_name
    for rule in m['events']:
        for f in rule['fields']:
            if f['path'] == 'EventDetail/Authenticate/User/Id':
                f['transform'] = 'strip_domain'
        if rule['name'] == 'traffic':
            rule['fields'].append({'path': 'EventDetail/Network/Open/Data', 'data_name': 'logged', 'field': 'timestamp',
                                   'time_format': TIME})
    if layout:
        m['style'] = {'layout': layout}
    return m


async def pipeline_for(ctx, build: str, name: str, template_uuid: str, tc_uuid: str, xslt_uuid: str) -> dict:
    return await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=name, template_uuid=template_uuid,
                            set_properties=[PropertyValue(element='dsParser', name='textConverter', doc_uuid=tc_uuid,
                                                          doc_type='TextConverter'),
                                            PropertyValue(element='translationFilter', name='xslt', doc_uuid=xslt_uuid,
                                                          doc_type='XSLT')])


async def events_of(ctx, pipeline_uuid: str, raw: int, records: int) -> list[etree._Element]:
    """Each record's Event, as Stroom writes it."""
    out = []
    for record in range(records):
        output = (await stepping.step_pipeline(ctx, pipeline_uuid, raw, record))['elements']['translationFilter']['output']
        out += etree.fromstring(output.encode()).findall('e:Event', NS)
    return out


def canonical(event: etree._Element) -> bytes:
    event = copy.deepcopy(event)
    for el in event.iter():
        if el.text is not None and not el.text.strip():
            el.text = None
        if el.tail is not None and not el.tail.strip():
            el.tail = None
    return etree.tostring(event, method='c14n')


async def main():
    settings = e2e.target_settings()
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    try:
        await run(ctx, stroom, e2e.STAMP)
        print('\nALL PASSED')
    finally:
        await stroom.close()


async def run(ctx, stroom: StroomGateway, stamp: str) -> None:
    build, feed = f'style-{stamp}', f'STYLE-{stamp}'
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, SAMPLE))['stream_id']
    template = next(c for c in (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
                    if c['name'] == FIREWALL['template'])
    tc = await translation.create_text_converter(ctx, build, feed, *FIREWALL['converter'])
    records = SAMPLE.count('\n') - 1

    print('\n### 1. one mapping in each layout: the same Events, with the conversions as functions')
    outputs, saved = {}, {}
    for layout in ('modes', 'named', 'inline'):
        mapping = TranslationMapping.model_validate(style_mapping(None if layout == 'modes' else layout))
        result = await e2e.agreed(generation.build_translation_xslt, ctx=ctx, mapping=mapping, stream_ids=[raw],
                                  build=build, name=f'{feed}-{layout}', include_xslt=True)
        e2e.check(result['ok'] and result.get('saved'), f"{layout}: saved with its mapping: {result.get('problems')}")
        sheet = etree.fromstring(result['xslt'].encode())
        functions = sorted(f.get('name') for f in sheet.findall('xsl:function', NS))
        e2e.check(functions == ['mcp:data', 'mcp:parse_time', 'mcp:strip_domain'],   # mcp:data: its Data entries
                  f"{layout}: the time format and strip_domain, each used twice, are functions: {functions}")
        record = sheet.find("xsl:template[@mode='event']", NS)
        if layout == 'modes':
            kinds = [a.get('mode') for a in record.iterfind('.//xsl:apply-templates', NS)]
            parts = {t.get('mode') for t in sheet.findall("xsl:template[@match='node()']", NS)}
            e2e.check(kinds == ['event_type_traffic', 'event_type_logon', 'event_type_logoff', 'event_type_system']
                      and {'event_time', 'event_source'} <= parts and not sheet.findall('xsl:template[@name]', NS),
                      f"modes: each kind and each shared part a mode template, applied to the record: {kinds}")
        elif layout == 'named':
            kinds = [c.get('name') for c in record.iterfind('.//xsl:call-template', NS)]
            e2e.check(kinds == ['event_type_traffic', 'event_type_logon', 'event_type_logoff', 'event_type_system'],
                      f"named: each kind a named template, called: {kinds}")
        else:
            e2e.check(len(record.findall('.//e:Event', NS)) == 4, 'inline: every kind written in the record template')
        pipeline = await pipeline_for(ctx, build, f'{feed}-{layout}-Events', template['uuid'], tc['uuid'],
                                      result['saved']['uuid'])
        sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
        e2e.check(sample['verdict'] == 'clean', f"{layout}: {sample['records_stepped']} records stepped clean in Stroom: "
                                                f"{[(g['class'], g.get('reason')) for g in sample['groups']]}")
        outputs[layout] = await events_of(ctx, pipeline['uuid'], raw, records)
        saved[layout] = (result['saved']['uuid'], pipeline['uuid'], mapping)
    e2e.check(len(outputs['modes']) == records
              and [canonical(e) for e in outputs['modes']] == [canonical(e) for e in outputs['named']]
              == [canonical(e) for e in outputs['inline']], f'all three layouts give the same {records} Events')
    events = outputs['modes']
    valid = await validation.validate_events(ctx, '<Events xmlns="event-logging:3" Version="4.1.0">'
                                             + ''.join(etree.tostring(e, encoding='unicode') for e in events) + '</Events>')
    e2e.check(valid['valid'], f"valid against {valid['schema']}: {valid.get('errors')}")
    logon = next(e for e in events if e.find('.//e:Authenticate', NS) is not None)
    e2e.check(logon.findtext('e:EventSource/e:User/e:Id', namespaces=NS) == 'admin'
              and logon.findtext('.//e:Authenticate/e:User/e:Id', namespaces=NS) == 'admin',
              'mcp:strip_domain, run by Stroom: admin@corp.example is admin in both places')
    logged = events[0].find(".//e:Data[@Name='logged']", NS).get('Value')
    e2e.check(logged == events[0].findtext('e:EventTime/e:TimeCreated', namespaces=NS) == '2026-09-30T23:00:12.000Z',
              f"mcp:parse_time, run by Stroom, gives the event time in both places: {logged}")

    print('\n### 2. the processing gate: a replaced XSLT steps clean before its output counts')
    xslt_uuid, pipeline_uuid, _ = saved['modes']
    await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline_uuid, stream_ids=[raw])
    done = await processing_writes.wait_for_processing(ctx, pipeline_uuid, [raw])
    e2e.check(done['gate'] == 'pass', f"processed: {done['streams']} {done['problems']}")
    changed = TranslationMapping.model_validate(style_mapping(system_name='E2E-2'))
    replaced = await e2e.agreed(generation.build_translation_xslt, ctx=ctx, mapping=changed, stream_ids=[raw],
                                uuid=xslt_uuid)
    e2e.check(replaced['ok'], f"the XSLT replaced: {replaced.get('problems')}")
    gated = await processing_writes.wait_for_processing(ctx, pipeline_uuid, [raw])
    unstepped = [p for p in gated['problems'] if 'has not stepped clean' in p]
    e2e.check(gated['gate'] == 'fail' and unstepped, f"the gate fails on the unstepped code: {gated['problems']}")
    again = await stepping.step_sample(ctx, pipeline_uuid, [raw])
    e2e.check(again['verdict'] == 'clean', 'the new code steps clean')
    after = await processing_writes.wait_for_processing(ctx, pipeline_uuid, [raw])
    e2e.check(not [p for p in after['problems'] if 'has not stepped clean' in p],
              f"and the gate no longer holds it: {after['problems']}")

    print('\n### 3. shared functions: found in the house-style sibling, imported and called')
    await shared_doc(stroom, COMMON_EVENT, SHARED_EVENT)
    await shared_doc(stroom, COMMON_FUNCTIONS, SHARED_FUNCTIONS)
    text_template = await fixture_template(stroom, TEXT_TEMPLATE, 'Event Data (Text)')
    src = f'style-src-{stamp}'
    sibling = await translation.create_xslt(ctx, src, f'STYLE-OTHER-{stamp}-Events', SIBLING)
    sibling_tc = await translation.create_text_converter(ctx, src, f'STYLE-OTHER-{stamp}', *FIREWALL['converter'])
    await pipeline_for(ctx, src, f'STYLE-OTHER-{stamp}-Events', text_template['uuid'], sibling_tc['uuid'], sibling['uuid'])
    described = await templates.describe_template(ctx, text_template['uuid'])
    # Any of the house-style siblings (this run's or an earlier one's: the survey reads a template's first children).
    rows = [r for r in described.get('shared_xslt') or []
            if any(u.startswith('STYLE-OTHER-') for u in (r.get('used_by') or []))]
    function = next((r for r in rows if r.get('function') == 'e2e:severityLevel'), {})
    e2e.check(function.get('namespace') == FUNCTIONS_NS and function.get('href') == COMMON_FUNCTIONS
              and function.get('params') == ['severity as xs:string?'] and function.get('returns') == 'xs:string'
              and function.get('calls') == 1, f"the function the sibling calls, with its namespace: {function}")
    device = next((r for r in rows if r.get('template') == 'eventSourceDevice'), {})
    e2e.check(device.get('at') == ['EventSource/Device'],
              f"the shared template, called in a part's mode template, placed where the part is applied: {device.get('at')}")
    contents = next((r['document_contents'] for r in described['shared_xslt']
                     if (r.get('document_contents') or {}).get('name') == COMMON_FUNCTIONS), {})
    e2e.check(sorted(contents.get('functions') or {}) == ['e2e:isInternal', 'e2e:severityLevel'],
              'and everything the shared XSLT offers, called or not')
    with_functions = copy.deepcopy(MAPPINGS['firewall'])
    for rule in with_functions['events']:
        if rule['name'] == 'system':
            rule['fields'].append({'path': 'EventDetail/Alert/Data', 'data_name': 'level',
                                   'xpath': "e2e:severityLevel(data[@name='severity']/@value)"})
        if rule['name'] == 'traffic':
            rule['fields'].append({'path': 'EventDetail/Network/Open/Data', 'data_name': 'internal',
                                   'xpath': "string(e2e:isInternal(data[@name='src_ip']/@value))"})
    unbound = await generation.build_translation_xslt(ctx, TranslationMapping.model_validate(with_functions),
                                                      stream_ids=[raw])
    e2e.check(not unbound['ok'] and any("no functions entry binds 'e2e'" in p for p in unbound['problems']),
              'calling them without a functions entry is refused')
    with_functions['functions'] = [{'href': function['href'], 'prefix': 'e2e', 'namespace': function['namespace']}]
    result = await e2e.agreed(generation.build_translation_xslt, ctx=ctx,
                              mapping=TranslationMapping.model_validate(with_functions), stream_ids=[raw],
                              build=build, name=f'{feed}-functions', include_xslt=True)
    e2e.check(result['ok'] and result.get('saved'), f"saved: {result.get('problems')}")
    sheet = etree.fromstring(result['xslt'].encode())
    e2e.check([i.get('href') for i in sheet.findall('xsl:import', NS)] == [COMMON_FUNCTIONS]
              and sheet.nsmap.get('e2e') == FUNCTIONS_NS, 'the XSLT imports the shared one and binds its prefix')
    pipeline = await pipeline_for(ctx, build, f'{feed}-functions-Events', template['uuid'], tc['uuid'],
                                  result['saved']['uuid'])
    sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    e2e.check(sample['verdict'] == 'clean', f"steps clean in Stroom, which resolves the import: "
                                            f"{[(g['class'], g.get('reason')) for g in sample['groups']]}")
    events = await events_of(ctx, pipeline['uuid'], raw, records)
    levels = [d.get('Value') for d in (e.find(".//e:Alert/e:Data[@Name='level']", NS) for e in events) if d is not None]
    internal = [d.get('Value') for d in (e.find(".//e:Data[@Name='internal']", NS) for e in events) if d is not None]
    e2e.check(levels == ['normal', 'high', 'normal'] and internal == ['true', 'false'],
              f"the shared functions' values in the Events: levels {levels}, internal {internal}")

    print('\n### 4. Unknown: kept only as the user chooses, and said for what it holds')
    unknown = copy.deepcopy(MAPPINGS['firewall'])
    unknown['events'][0] = {'name': 'traffic', 'when': [{'field': 'event_type', 'equals': 'TRAFFIC'}],
                            'allow_unknown': 'the user wants traffic kept as it came',
                            'fields': [{'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}
    refused = await generation.build_translation_xslt(ctx, TranslationMapping.model_validate(unknown), stream_ids=[raw])
    e2e.check(not refused['ok'] and any("can't be kept as Unknown" in p and 'keep_unknown: true' in p
                                        for p in refused['problems']),
              f"refused first, naming keep_unknown: {[p[:160] for p in refused['problems']]}")
    unknown['events'][0]['keep_unknown'] = True
    keep = dict(ctx=ctx, mapping=TranslationMapping.model_validate(unknown), stream_ids=[raw], build=build,
                name=f'{feed}-unknown')
    asked = await generation.build_translation_xslt(**keep)
    e2e.check(asked.get('status') == 'needs_confirmation' and "the records' values show their action" in asked['summary'],
              f"the user is asked, told what the values suggest: {asked.get('summary')}")
    kept = await generation.build_translation_xslt(**keep, confirmation_id=asked['confirmation_id'])
    e2e.check(kept['ok'] and kept.get('saved'), 'kept as Unknown once the user agrees')
    pipeline = await pipeline_for(ctx, build, f'{feed}-unknown-Events', template['uuid'], tc['uuid'], kept['saved']['uuid'])
    sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    e2e.check(sample['verdict'] == 'clean', 'the agreed Unknown steps clean')
    # The same code saved by hand, without its mapping: nobody agreed to its Unknown.
    schema = await generation.event_schema(ctx, e2e.VERSION)
    code = generate(TranslationMapping.model_validate(unknown), schema, e2e.VERSION)['xslt']
    hand = await translation.create_xslt(ctx, build, f'{feed}-hand', code)
    pipeline = await pipeline_for(ctx, build, f'{feed}-hand-Events', template['uuid'], tc['uuid'], hand['uuid'])
    sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    block = next((g for g in sample['groups'] if 'Unknown' in g.get('reason', '')), {})
    message = (block.get('examples') or [{}])[0].get('message', '')
    e2e.check(sample['verdict'] == 'blocking' and 'they hold' in message and 'ALLOW' in message and 'DENY' in message,
              f"a hand-written XSLT's Unknown is blocked, saying what the records hold: {message[:220]}")
    try:
        await builds.write_documentation(ctx, build=build, pipeline_uuid=pipeline['uuid'], markdown='## Purpose\n\nE2E.',
                                         accept_errors=[{'element': 'translationFilter', 'example': message[:200],
                                                         'reason': 'they are fine'}])
        e2e.check(False, 'Unknown accepted as a benign error')
    except ToolError as e:
        e2e.check("aren't an error to accept" in str(e), f"Unknown isn't accepted as a benign error: {str(e)[:120]}")

    print('\n### 5. the naming choice, in a form')
    picked = []

    async def elicit(message, response_type):
        # The choices, as the form's one field (titled with the question, as VS Code's history shows it) lists them.
        import dataclasses
        import typing
        choices = dataclasses.fields(response_type)[0].type
        if typing.get_origin(choices) is typing.Annotated:
            choices = typing.get_args(choices)[0]
        options = list(typing.get_args(choices))
        picked.append((message, options))
        return SimpleNamespace(action='accept', data=options[-1])
    form_ctx = SimpleNamespace(lifespan_context={**ctx.lifespan_context, 'consent': ConsentStore(use_elicitation=True)},
                               elicit=elicit)
    chosen = await indexing.get_field_conventions(form_ctx, backend='elasticsearch')
    e2e.check(picked and picked[0][1][0] == 'From an index template' and chosen.get('status') == 'chosen'
              and 'draft_index_mapping convention=' in chosen.get('hint', '')
              and "isn't asked again" in chosen.get('hint', ''),
              f"the user picks from a picker, and the reply gives the next call: {picked[0][1] if picked else None} "
              f"-> {chosen.get('choice')}")


if __name__ == '__main__':
    asyncio.run(main())
