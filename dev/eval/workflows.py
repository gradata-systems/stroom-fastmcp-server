"""Agent-level cases for the workflows beyond onboarding, run by run_agent.py --workflow.

Each workflow sets up the starting state on the local stack (through the server's own tools, as the e2e suites do),
names the prompt the agent starts from and what the user asks, gives the scripted user the facts it may answer
from, and scores the outcome from Stroom, whatever the agent says about it. A reference run (--reference) does
the workflow through the tools directly, without a model: it checks the setup and the scorer cost nothing.

    uv run python dev/eval/run_agent.py --workflow document_index              # the agent, on Haiku
    uv run python dev/eval/run_agent.py --workflow document_index --reference  # no model: setup and scorer
"""
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

ES = 'http://127.0.0.1:19200'


@dataclass
class Workflow:
    id: str
    prompt: str
    setup: Callable[[Any, str], Awaitable[dict[str, Any]]]
    reference: Callable[[Any, dict[str, Any]], Awaitable[None]]
    score: Callable[[Any, dict[str, Any]], Awaitable[tuple[list[str], list[str]]]]
    done: str           # when the work is finished, for the scripted user
    promote: str        # the user's answer when asked whether or where to promote


# --- document_index: an index another system loads, documented and promoted beside its index doc ---
LEGACY = [
    {'@timestamp': '2026-09-20T08:00:00Z', 'user': {'name': 'alice'}, 'source': {'ip': '10.3.0.1'},
     'http': {'status': 200}, 'message': 'GET /index.html'},
    {'@timestamp': '2026-09-21T09:30:00Z', 'user': {'name': 'bob'}, 'source': {'ip': '10.3.0.2'},
     'http': {'status': 404}, 'message': 'GET /missing'},
    {'@timestamp': '2026-09-22T10:15:00Z', 'user': {'name': 'carol'}, 'source': {'ip': '10.3.0.3'},
     'http': {'status': 500}, 'message': 'POST /upload'},
    {'@timestamp': '2026-09-23T11:45:00Z', 'user': {'name': 'alice'}, 'source': {'ip': '10.3.0.1'},
     'message': 'health check'},
]
LEGACY_FIELDS = ['StreamId', 'EventId', '@timestamp', 'user.name', 'source.ip', 'http.status', 'message']
MAPPING = {'mappings': {'properties': {
    'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}, '@timestamp': {'type': 'date'},
    'user': {'properties': {'name': {'type': 'keyword'}}}, 'source': {'properties': {'ip': {'type': 'ip'}}},
    'http': {'properties': {'status': {'type': 'long'}}}, 'message': {'type': 'text'}}}}
PURPOSE = ("It holds the web access logs of the edge proxy in front of the intranet portal, loaded by the proxy "
           "team's own shipper. The SOC searches it when investigating suspicious access to the portal.")


async def document_index_setup(ctx, stamp: str) -> dict[str, Any]:
    import e2e_translation as e2e
    from e2e_elastic_handover import live_cluster
    from tools import builds, feeds, indexing
    stroom = ctx.lifespan_context['stroom']
    index, feed = f'eval-legacy-web-{stamp}', f'EVAL-LEGACY-WEB-{stamp}'
    setup, folder = f'eval-docindex-setup-{stamp}', f'System/E2E Production/eval-legacy-{stamp}'
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=setup, name=feed)
    stream = (await feeds.upload_sample(ctx, feed, '\n'.join(json.dumps(d) for d in LEGACY)))['stream_id']
    async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
        if (await es.put(f'/{index}', json=MAPPING)).status_code != 200:
            raise RuntimeError(f'could not create Elasticsearch index {index}')
        bulk = ''.join(json.dumps({'index': {}}) + '\n' + json.dumps({'StreamId': stream, 'EventId': n + 1, **d}) + '\n'
                       for n, d in enumerate(LEGACY))
        loaded = (await es.post(f'/{index}/_bulk?refresh=true', content=bulk,
                                headers={'Content-Type': 'application/x-ndjson'})).json()
        if loaded.get('errors'):
            raise RuntimeError(f'bulk load failed: {str(loaded)[:300]}')
    cluster = await live_cluster(stroom)
    await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=setup, backend='elasticsearch', name=index,
                     time_field='@timestamp', index_name=index, cluster_uuid=cluster['uuid'])
    await e2e.agreed(builds.promote_build, ctx=ctx, build=setup, destinations={'ElasticIndex': folder, 'Feed': folder})
    return {'index': index, 'folder': folder, 'build': f'eval-docidx-{stamp}',
            'prompt_args': {'index': index},
            'request': (f"Please document our existing Elasticsearch index `{index}` in Stroom. Use the build "
                        f"`eval-docidx-{stamp}`, give me the link to the draft, and put it beside the index doc once "
                        f"I've agreed."),
            'facts': f"The index is `{index}`, its Elastic Index doc is in {folder}. {PURPOSE} Put the documentation "
                     f"beside the index doc."}


async def document_index_reference(ctx, prepared: dict[str, Any]) -> None:
    """As an agent following the prompt would: locate, survey, draft (confirmed), promote beside the index doc."""
    import e2e_translation as e2e
    from tools import builds, explorer
    found = await explorer.find_documents(ctx, prepared['index'], ['ElasticIndex'])
    doc = next(d for d in found['documents'] if d['name'] == prepared['index'])
    await explorer.describe_document(ctx, 'ElasticIndex', doc['uuid'])
    await e2e.agreed(builds.write_documentation, ctx=ctx, build=prepared['build'], index_uuid=doc['uuid'],
                     change='Created', markdown=f"## Purpose and data\n\n{PURPOSE} Each document is one request to the "
                                                f"portal: who made it, from which address, what was asked for and the "
                                                f"HTTP status it got.\n")
    await e2e.agreed(builds.promote_build, ctx=ctx, build=prepared['build'], destinations={})


async def document_index_score(ctx, prepared: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The documentation beside the index doc: every field in the table, the Data surveyed summary, and the agent's
    own Purpose and data prose, built on what the user said of the index's purpose (the agent has to ask)."""
    import e2e_translation as e2e
    stroom = ctx.lifespan_context['stroom']
    problems, notes = [], []
    found = (await stroom.find_documents(prepared['index'], ['Documentation'], 20)).get('values') or []
    docs = [v for v in found if v['docRef']['name'] == prepared['index']]
    beside = [v for v in docs if (v.get('path') or '').replace(' / ', '/') == prepared['folder']]
    if not docs:
        return [f"no Documentation named '{prepared['index']}'"], notes
    if not beside:
        problems.append(f"not promoted beside the index doc ({prepared['folder']}): at "
                        f"{[(v.get('path') or '') for v in docs]}")
    text = (await stroom.get_doc('Documentation', (beside or docs)[0]['docRef']['uuid'])).get('data') or ''
    section = text.split('## Field mapping')[1].split('\n## ')[0] if '## Field mapping' in text else ''
    rows = e2e.field_rows(section)
    missing = [f for f in LEGACY_FIELDS if f not in rows]
    if missing:
        problems.append(f"field table lacks {missing}")
    if '### Data surveyed' not in text:
        problems.append('no Data surveyed summary')
    purpose = text.split('## Purpose and data')[1].split('### Data surveyed')[0] if '## Purpose and data' in text else ''
    prose = ' '.join(purpose.split())
    if len(prose) < 150:
        problems.append(f"Purpose and data is thin ({len(prose)} characters): {prose[:120]!r}")
    # The user knows why the index exists (the facts): the agent must ask, not infer a purpose from the data.
    if not re.search(r'proxy|portal|\bSOC\b', prose, re.I):
        problems.append("Purpose and data does not use what the user said of the index's purpose (the edge proxy in "
                        "front of the intranet portal, searched by the SOC): the agent did not ask, or did not listen")
    notes.append(f"purpose: {prose[:200]!r}")
    return problems, notes


# --- shared: a pipeline made directly in Stroom, outside any workspace, as production content is ---
def _canonical(event: Any) -> set[tuple[str, str]]:
    """An event as its leaf values and Data, path by path: two events that say the same thing compare equal however
    their XSLT was written."""
    from lxml import etree
    out = set()
    for node in event.iter():
        if not isinstance(node.tag, str):
            continue
        path = '/'.join(etree.QName(a).localname for a in reversed(list(node.iterancestors())) if isinstance(a.tag, str))
        name = etree.QName(node).localname
        if name == 'Data':
            out.add((f'{path}/Data[{node.get("Name")}]', node.get('Value') or ''))
        elif len(node) == 0 and (node.text or '').strip():
            out.add((f'{path}/{name}', node.text.strip()))
    return out


async def production_pipeline(ctx, stamp: str, label: str, streams: list[str], xslt_code: str) -> dict[str, Any]:
    """A feed, text converter, XSLT and pipeline (a child of the text template) made directly in Stroom in
    System/E2E Production/<label>-<stamp>, outside any build, its streams processed by its own processor filter."""
    import asyncio
    import e2e_translation as e2e
    from e2e_evaluate_and_fix import _create
    from security.guard import guard_from
    from tools import processing_writes
    stroom = ctx.lifespan_context['stroom']
    system = await guard_from(ctx).system_node()
    top = next((v['docRef'] for v in (await stroom.find_documents('E2E Production', ['Folder'], 10)).get('values') or []
                if v['docRef']['name'] == 'E2E Production'), None)
    top = top or await _create(stroom, 'Folder', 'E2E Production', {k: v for k, v in system.items() if not k.startswith('_')})
    folder = await _create(stroom, 'Folder', f'{label}-{stamp}', top)
    feed_name = f'EVAL-{label.upper()}-{stamp}'
    feed = await stroom.get_doc('Feed', (await _create(stroom, 'Feed', feed_name, folder))['uuid'])
    feed.update(encoding='UTF-8', streamType='Raw Events')
    await stroom.put_doc(feed)
    tc = await stroom.get_doc('TextConverter', (await _create(stroom, 'TextConverter', f'{feed_name}-Splitter', folder))['uuid'])
    tc.update(converterType='DATA_SPLITTER', data=e2e.CSV_SPLITTER)
    tc = await stroom.put_doc(tc)
    xslt = await stroom.get_doc('XSLT', (await _create(stroom, 'XSLT', f'{feed_name}-Events', folder))['uuid'])
    xslt['data'] = xslt_code
    xslt = await stroom.put_doc(xslt)
    template = next(v['docRef'] for v in (await stroom.find_documents('Event Data (Text)', ['Pipeline'], 50))['values']
                    if v['docRef']['name'] == 'Event Data (Text)')
    pipeline = await stroom.get_doc('Pipeline', (await _create(stroom, 'Pipeline', f'{feed_name}-Events', folder))['uuid'])
    entity = lambda d: {'entity': {'type': d['type'], 'uuid': d['uuid'], 'name': d['name']}}  # noqa: E731
    pipeline['parentPipeline'] = template
    pipeline['pipelineData'] = {'properties': {'add': [
        {'element': 'dsParser', 'name': 'textConverter', 'value': entity(tc)},
        {'element': 'translationFilter', 'name': 'xslt', 'value': entity(xslt)}]}}
    pipeline = await stroom.put_doc(pipeline)
    for text in streams:
        response = await stroom.datafeed(feed_name, text.encode('utf-8'), {'Type': 'Raw Events'})
        if not response.is_success:
            raise RuntimeError(f'datafeed: {response.text}')
    await asyncio.sleep(2)
    raw = sorted(m['meta']['id'] for m in (await stroom.find_meta(
        [processing_writes._term('Feed', feed_name), processing_writes._term('Type', 'Raw Events')], 10))['values'])
    await processing_writes._create_filter(stroom, pipeline, {'type': 'operator', 'op': 'AND', 'children': [
        processing_writes._term('Feed', feed_name), processing_writes._term('Type', 'Raw Events')]}, 10, 2, None)
    done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], raw)
    return {'feed': feed_name, 'pipeline': pipeline, 'xslt': xslt, 'raw': raw, 'processed': done,
            'folder': f'System/E2E Production/{label}-{stamp}'}


async def stepped_events(ctx, pipeline_uuid: str, raw: list[int], texts: list[str]) -> tuple[dict[str, Any], list[Any]]:
    """step_sample's verdict on the raw streams, and each record's Event (None where a record wrote none), in order:
    each stream's records are its text's lines after the header."""
    from lxml import etree
    from tools import stepping
    sample = await stepping.step_sample(ctx, pipeline_uuid, raw)
    events = []
    for stream, text in zip(raw, texts):
        for record in range(len(text.strip().splitlines()) - 1):
            event = None
            try:
                step = await stepping.step_pipeline(ctx, pipeline_uuid, stream, record)
                output = ((step.get('elements') or {}).get('translationFilter') or {}).get('output') or ''
                event = etree.fromstring(output.encode('utf-8')).find('{event-logging:3}Event') if output else None
            except Exception:  # a record that fails to step, or writes no XML: no Event
                pass
            events.append(event)
    return sample, events


async def fixed_in_place(ctx, prepared: dict[str, Any], edits: list[tuple[str, str]], change: str) -> None:
    """The reference fix, as update_events_pipeline asks for an in-place change: a working copy in the build, its XSLT
    changed, compared with production, documented, promoted (approval) over the production documents."""
    import e2e_translation as e2e
    from tools import builds, pipeline_writes, stepping, translation
    pipeline, build = prepared['pipeline'], prepared['build']
    copy = await e2e.agreed(pipeline_writes.copy_pipeline, ctx=ctx, build=build, source_uuid=pipeline['uuid'],
                            new_name=f"{pipeline['name']}-WORKING", working_copy=True)
    xslt_copy = next(d for d in copy['copied_documents'] if d['type'] == 'XSLT')
    code = prepared['xslt']['data']
    for old, new in edits:
        if code.count(old) != 1:
            raise RuntimeError(f'the reference fix does not apply to the production XSLT: {old[:60]}')
        code = code.replace(old, new)
    await translation.update_xslt(ctx, xslt_copy['uuid'], code)
    await stepping.compare_outputs(ctx, pipeline['uuid'], prepared['raw'], other_pipeline_uuid=copy['uuid'])
    # Its XSLT keeps no mapping (written by hand), so the Field mapping section is written here, as an agent would.
    await builds.write_documentation(ctx, build, copy['uuid'],
                                     f"# {pipeline['name']}\n\n## Purpose and data\n\nEvaluation workflow.\n\n"
                                     f"## Field mapping\n\n| From | To |\n| --- | --- |\n| time | EventTime/TimeCreated |\n"
                                     f"| user | EventSource/User/Id |\n| ip | EventSource/Client/IPAddress |\n", change)
    await e2e.agreed(builds.promote_build, ctx=ctx, build=build, destinations={})


def production_xslt(time_select: str, ip_select: str, detail: str) -> str:
    """A translation as someone wrote it by hand: CSV records (time, user, host, ip, ...) to Events."""
    import e2e_translation as e2e
    return f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="records:2" xmlns="event-logging:3" xmlns:stroom="stroom"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="3.0">
  <xsl:template match="records">
    <Events xsi:schemaLocation="event-logging:3 file://event-logging-v{e2e.VERSION}.xsd" Version="{e2e.VERSION}">
      <xsl:apply-templates />
    </Events>
  </xsl:template>
  <xsl:template match="record">
    <xsl:variable name="t" select="data[@name='time']/@value" />
    <xsl:variable name="ip" select="data[@name='ip']/@value" />
    <xsl:variable name="user" select="data[@name='user']/@value" />
    <Event>
      <EventTime><TimeCreated><xsl:value-of select="{time_select}" /></TimeCreated></EventTime>
      <EventSource>
        <System><Name>Remote Access</Name><Environment>Eval</Environment></System>
        <Generator>vpn</Generator>
        <Device><HostName><xsl:value-of select="data[@name='host']/@value" /></HostName></Device>
        <xsl:if test="$ip != ''"><Client><IPAddress><xsl:value-of select="{ip_select}" /></IPAddress></Client></xsl:if>
        <User><Id><xsl:value-of select="$user" /></Id></User>
      </EventSource>
{detail}
    </Event>
  </xsl:template>
</xsl:stylesheet>
"""


# --- fix_errors: a production pipeline writing Error streams; the user says which records fail ---
ERROR_STREAMS = [
    "time,user,host,ip,result\n"
    "2026-09-28T10:00:00,alice,vpn01,10.0.0.1,ok\n"
    "2026-09-28T10:05:00.250,bob,vpn01,10.0.0.2,ok\n"
    "2026-09-28T10:07:30,carol,vpn02,2001:DB8::7,fail\n",
    "time,user,host,ip,result\n"
    "2026-09-29T09:00:00.5,dave,vpn02,10.0.0.4,ok\n"
    "2026-09-29T09:12:00,erin,vpn01,FE80::A1,ok\n"
    "2026-09-29T09:20:00,frank,vpn01,10.0.0.6,fail\n",
]
LOGON_DETAIL = """      <EventDetail>
        <TypeId>VPN-LOGON</TypeId>
        <Description>VPN logon</Description>
        <Authenticate>
          <Action>Logon</Action>
          <User><Id><xsl:value-of select="$user" /></Id></User>
          <Outcome><Success><xsl:value-of select="data[@name='result']/@value = 'ok'" /></Success></Outcome>
        </Authenticate>
      </EventDetail>"""
# Seconds only (a fraction of a second fails to parse), and the address as written (upper-case IPv6 fails the schema).
BROKEN_TIME = "stroom:format-date($t, 'yyyy-MM-dd''T''HH:mm:ss')"
FIXED_TIME = ("stroom:format-date(concat(if (contains($t, '.')) then substring-before($t, '.') else $t, '.', "
              "substring(concat(substring-after($t, '.'), '000'), 1, 3)), 'yyyy-MM-dd''T''HH:mm:ss.SSS')")
# What the fixed pipeline must give: every record an Event, the fractions kept, the IPv6 addresses in lower case.
ERROR_TIMES = {'bob': '2026-09-28T10:05:00.250Z', 'dave': '2026-09-29T09:00:00.500Z', 'alice': '2026-09-28T10:00:00.000Z'}
ERROR_IPS = {'carol': '2001:db8::7', 'erin': 'fe80::a1', 'alice': '10.0.0.1'}


async def fix_errors_setup(ctx, stamp: str) -> dict[str, Any]:
    prod = await production_pipeline(ctx, stamp, 'vpnerr', ERROR_STREAMS,
                                     production_xslt(BROKEN_TIME, '$ip', LOGON_DETAIL))
    errors = [e for s in prod['processed']['streams'] for e in s.get('errors') or []]
    if not errors:
        raise RuntimeError(f"setup: the production pipeline wrote no Error streams: {prod['processed']['streams']}")
    name = prod['pipeline']['name']
    return {**prod, 'build': f'eval-fixerr-{stamp}', 'error_streams': errors,
            'prompt_args': {'pipeline': name, 'issue': ("It's been writing Error streams. Records whose time has a "
                                                        "fraction of a second fail, and so do records from IPv6 clients.")},
            'request': (f"Our VPN logon pipeline `{name}` (feed {prod['feed']}) has been writing Error streams "
                        f"(e.g. {errors[0]}). From what I've seen, records whose time has a fraction of a second fail, "
                        f"and so do records from IPv6 clients. Please find out why and fix it, in place (keep the "
                        f"names), using the build `eval-fixerr-{stamp}`. Promote the fix once I've approved it; don't "
                        f"reprocess anything."),
            'facts': (f"The pipeline is `{name}`, in {prod['folder']}; its feed is {prod['feed']}. Change it in place: "
                      f"the same names, not a new version. The time is when the client logged on, in UTC; a fraction "
                      f"of a second should be kept. Promote once the fix is proven; don't reprocess.")}


async def fix_errors_reference(ctx, prepared: dict[str, Any]) -> None:
    await fixed_in_place(ctx, prepared, [(f'select="{BROKEN_TIME}"', f'select="{FIXED_TIME}"'),
                                         ('<xsl:value-of select="$ip" />', '<xsl:value-of select="lower-case($ip)" />')],
                         'Times with a fraction of a second are read whole; IPv6 addresses in lower case, as the schema '
                         'has them')


async def fix_errors_score(ctx, prepared: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The production pipeline (in place, so its own uuid) steps every record of the feed's streams clean, each an
    Event with its time's fraction kept and its address as the schema has it."""
    problems, notes = [], []
    sample, events = await stepped_events(ctx, prepared['pipeline']['uuid'], prepared['raw'], ERROR_STREAMS)
    if sample['verdict'] != 'clean':
        problems.append(f"stepping the production pipeline: {sample['verdict']}: "
                        f"{[(g.get('reason'), (g.get('examples') or [{}])[0].get('message', '')[:120]) for g in sample['groups']][:3]}")
    found = [e for e in events if e is not None]
    if len(found) != 6:
        problems.append(f"{len(found)} of 6 records gave an Event")
    evt = '{event-logging:3}'
    by_user = {e.findtext(f'{evt}EventSource/{evt}User/{evt}Id'): e for e in found}
    for user, when in ERROR_TIMES.items():
        got = by_user.get(user).findtext(f'{evt}EventTime/{evt}TimeCreated') if user in by_user else None
        if got != when:
            problems.append(f"{user}'s event time is {got!r}, not {when}")
    for user, ip in ERROR_IPS.items():
        got = by_user.get(user).findtext(f'.//{evt}Client/{evt}IPAddress') if user in by_user else None
        if got != ip:
            problems.append(f"{user}'s client address is {got!r}, not {ip}")
    notes.append(f"stepped {sample.get('records_stepped')} records: {sample['verdict']}")
    return problems, notes


# --- change_event_type: a working pipeline; the user wants one kind it writes as Unknown written properly ---
TYPE_STREAMS = [
    "time,user,host,ip,action\n"
    "2026-09-30T08:00:00,alice,ws01,10.1.0.1,LOGON\n"
    "2026-09-30T08:10:00,alice,ws01,10.1.0.1,PWCHANGE\n"
    "2026-09-30T08:20:00,bob,ws02,10.1.0.2,LOGON\n"
    "2026-09-30T08:25:00,bob,ws02,10.1.0.2,PWCHANGE\n"
    "2026-09-30T09:00:00,alice,ws01,10.1.0.1,LOGOFF\n"
    "2026-09-30T09:05:00,carol,ws03,10.1.0.3,SCREENLOCK\n",
]
TYPE_DETAIL = """      <EventDetail>
        <TypeId><xsl:value-of select="data[@name='action']/@value" /></TypeId>
        <xsl:choose>
          <xsl:when test="data[@name='action']/@value = ('LOGON', 'LOGOFF')">
            <Description>Workstation logon or logoff</Description>
            <Authenticate>
              <Action><xsl:value-of select="if (data[@name='action']/@value = 'LOGON') then 'Logon' else 'Logoff'" /></Action>
              <User><Id><xsl:value-of select="$user" /></Id></User>
            </Authenticate>
          </xsl:when>
          <xsl:otherwise>
            <Description>Other workstation activity</Description>
            <Unknown><Data Name="action" Value="{data[@name='action']/@value}" /></Unknown>
          </xsl:otherwise>
        </xsl:choose>
      </EventDetail>"""
PWCHANGE = """          <xsl:otherwise>
            <Description>Other workstation activity</Description>"""
PWCHANGE_FIXED = """          <xsl:when test="data[@name='action']/@value = 'PWCHANGE'">
            <Description>Password changed</Description>
            <Authenticate>
              <Action>ChangePassword</Action>
              <User><Id><xsl:value-of select="$user" /></Id></User>
            </Authenticate>
          </xsl:when>
          <xsl:otherwise>
            <Description>Other workstation activity</Description>"""
SECONDS = "stroom:format-date($t, 'yyyy-MM-dd''T''HH:mm:ss')"


async def change_event_type_setup(ctx, stamp: str) -> dict[str, Any]:
    prod = await production_pipeline(ctx, stamp, 'wsact', TYPE_STREAMS, production_xslt(SECONDS, '$ip', TYPE_DETAIL))
    errors = [e for s in prod['processed']['streams'] for e in s.get('errors') or []]
    if errors:
        raise RuntimeError(f"setup: the production pipeline should work, but wrote Error streams {errors}")
    # What every record gives now: the records the change is not about must give the same after it.
    _, before = await stepped_events(ctx, prod['pipeline']['uuid'], prod['raw'], TYPE_STREAMS)
    name = prod['pipeline']['name']
    return {**prod, 'build': f'eval-evtype-{stamp}', 'before': [_canonical(e) if e is not None else None for e in before],
            'prompt_args': {'pipeline': name, 'issue': ("PWCHANGE records come out as Unknown events. They're "
                                                        "password changes: they should be Authenticate events, with "
                                                        "the action ChangePassword.")},
            'request': (f"Our workstation activity pipeline `{name}` (feed {prod['feed']}) writes the PWCHANGE records "
                        f"as Unknown events. They're users changing their own passwords, so they should be "
                        f"Authenticate events with the action ChangePassword. Please change that, in place (keep the "
                        f"names), using the build `eval-evtype-{stamp}`, and promote it once I've approved it. Nothing "
                        f"else about the events should change. Don't reprocess anything."),
            'facts': (f"The pipeline is `{name}`, in {prod['folder']}; its feed is {prod['feed']}. Change it in place: "
                      f"the same names, not a new version. PWCHANGE is a user changing their own password. Only "
                      f"PWCHANGE is to change; leave the other actions as they are, SCREENLOCK included. Promote once "
                      f"it's proven; don't reprocess.")}


async def change_event_type_reference(ctx, prepared: dict[str, Any]) -> None:
    await fixed_in_place(ctx, prepared, [(PWCHANGE, PWCHANGE_FIXED)], 'PWCHANGE written as a password change')


async def change_event_type_score(ctx, prepared: dict[str, Any]) -> tuple[list[str], list[str]]:
    """In production now: each PWCHANGE record an Authenticate event with the action ChangePassword and its user;
    every other record's event exactly as before (SCREENLOCK still Unknown, as the user asked for no other change)."""
    problems, notes = [], []
    sample, after = await stepped_events(ctx, prepared['pipeline']['uuid'], prepared['raw'], TYPE_STREAMS)
    if sample['verdict'] != 'clean':
        problems.append(f"stepping the production pipeline: {sample['verdict']}")
    if len(after) != len(prepared['before']):
        return problems + [f"{len(after)} records stepped, {len(prepared['before'])} before"], notes
    evt = '{event-logging:3}'
    actions = [line.rsplit(',', 1)[1] for line in TYPE_STREAMS[0].splitlines()[1:]]
    for n, (action, old, new) in enumerate(zip(actions, prepared['before'], after)):
        if new is None:
            problems.append(f"record {n} ({action}) wrote no Event")
            continue
        if action == 'PWCHANGE':
            got = new.findtext(f'{evt}EventDetail/{evt}Authenticate/{evt}Action')
            user = new.findtext(f'{evt}EventDetail/{evt}Authenticate/{evt}User/{evt}Id')
            if got != 'ChangePassword' or not user:
                problems.append(f"record {n} (PWCHANGE): Authenticate/Action {got!r}, user {user!r}")
            if new.find(f'{evt}EventDetail/{evt}Unknown') is not None:
                problems.append(f"record {n} (PWCHANGE) is still Unknown")
        elif _canonical(new) != old:
            changed = sorted(_canonical(new) ^ old)[:4]
            problems.append(f"record {n} ({action}) changed, though only PWCHANGE was to: {changed}")
    notes.append(f"stepped {sample.get('records_stepped')} records: {sample['verdict']}")
    return problems, notes


# --- records_output: a source that isn't events, kept as Records and indexed as records ---
ASSETS = ("hostname,ip,os,owner,last_seen\n"
          "ws-0141,10.5.0.41,Windows 11,alice,2026-10-01T08:00:00Z\n"
          "ws-0142,10.5.0.42,Windows 11,bob,2026-10-01T08:05:00Z\n"
          "lnx-db-01,10.6.0.10,Ubuntu 24.04,dba-team,2026-10-01T07:30:00Z\n"
          "lnx-web-03,10.6.0.23,Ubuntu 24.04,web-team,2026-10-01T07:45:00Z\n"
          "mac-0007,10.5.0.77,macOS 15,carol,2026-10-01T09:10:00Z\n"
          "prn-fin-02,10.7.0.12,Printer firmware 4.2,facilities,2026-10-01T06:00:00Z\n")
ASSET_HOSTS = ['lnx-db-01', 'lnx-web-03', 'mac-0007', 'prn-fin-02', 'ws-0141', 'ws-0142']
EXAMPLE_TEMPLATE = ('PUT _index_template/assets-example\n'
                    '{"index_patterns": ["assets-example*"], "template": {"settings": {"index": {"number_of_shards": 1, '
                    '"number_of_replicas": 0}}, "mappings": {"dynamic": true}}}')


async def records_output_setup(ctx, stamp: str) -> dict[str, Any]:
    """A records template of the environment's own (as e2e_stream_types makes it): a parser writing Records."""
    from e2e_stream_types import RECORDS_DATA, RECORDS_TEMPLATE, fixture
    template = await fixture(ctx.lifespan_context['stroom'], ctx, RECORDS_TEMPLATE, RECORDS_DATA)
    build, feed, index = f'eval-assets-{stamp}', f'EVAL-ASSETS-{stamp}', f'eval-assets-{stamp}'
    return {'build': build, 'feed': feed, 'index': index, 'template': template,
            'prompt_args': {'sample': ASSETS, 'timestamp_field': 'last_seen'},
            'request': (f"This is our asset inventory export (CSV, with a header): one record per device, not "
                        f"events, so no event-logging translation. Keep it in Stroom as Records (a pipeline writing "
                        f"Records streams from the raw CSV), then index those Records into Elasticsearch as they are, "
                        f"so we can search the inventory. Use the build `{build}`, the feed `{feed}` and the index "
                        f"`{index}`. Stop once the index is verified and documented; don't promote the build."),
            'facts': (f"The sample is all there is. The index is `{index}`, on the local Elasticsearch cluster. "
                      f"last_seen is the time field, ISO 8601 in UTC. No stream meta to add, nothing to leave out. "
                      f"An example index template from a sibling index:\n{EXAMPLE_TEMPLATE}\nAgree to the proposals "
                      f"that fit this. Don't promote: this is a scratch environment.")}


async def records_output_reference(ctx, prepared: dict[str, Any]) -> None:
    """As e2e_stream_types proves it: the raw CSV through a records pipeline, the Records indexed by a discovery plan."""
    import e2e_translation as e2e
    from e2e_elastic_handover import _request, live_cluster
    from tools import feeds, generation, indexing, pipeline_writes, processing_writes, stepping, templates, translation
    from utils.fieldplan import Discovery, FieldPlan
    stroom, build, feed, index = ctx.lifespan_context['stroom'], prepared['build'], prepared['feed'], prepared['index']
    await e2e.agreed(feeds.create_feed, ctx=ctx, build=build, name=feed)
    raw = (await feeds.upload_sample(ctx, feed, ASSETS))['stream_id']
    await generation.build_data_splitter(ctx, stream_ids=[raw], save_as=feed, build=build)
    writer = await e2e.agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed}-Records',
                              template_uuid=prepared['template']['uuid'])
    await stepping.step_sample(ctx, writer['uuid'], [raw])
    await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=writer['uuid'], stream_ids=[raw])
    done = await processing_writes.wait_for_processing(ctx, writer['uuid'], [raw])
    produced = done['streams'][0]['events']
    plan = FieldPlan.model_validate((await indexing.draft_index_mapping(
        ctx, 'elasticsearch', index, discovery=Discovery(input='delimited', timestamp_field='last_seen')))['plan'])
    xslt = await translation.save_xslt(ctx, build, f'{index}-XSLT', index_plan=plan)
    es_template = next(c for c in (await templates.find_pipeline_templates(ctx, 'indexing'))['candidates']
                       if c['backend'] == 'elasticsearch' and c['parser'] == 'XMLParser')
    cluster = await live_cluster(stroom)
    indexer = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=build, name=f'{index} - Indexing',
                               template_uuid=es_template['uuid'], xslt_uuid=xslt['uuid'], index_name=index,
                               cluster_uuid=cluster['uuid'])
    await stepping.step_sample(ctx, indexer['uuid'], produced)
    final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=indexer['uuid'], plan=plan,
                             events_stream_ids=produced, example_template=EXAMPLE_TEMPLATE)
    path, body = _request(final['dev_tools'])
    async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
        await es.put(f'/{path}', json=body)
    await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=indexer['uuid'], stream_ids=produced)
    await processing_writes.wait_for_processing(ctx, indexer['uuid'], produced, expect_events=False)


async def records_output_score(ctx, prepared: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Records streams in the feed holding every record (a pipeline writing Records, not Events), and the index holding
    every record as itself, its hostname among its fields."""
    from tools import streams
    problems, notes = [], []
    records = (await streams.find_streams(ctx, feed=prepared['feed'], stream_type='Records', limit=20))['streams']
    events = (await streams.find_streams(ctx, feed=prepared['feed'], stream_type='Events', limit=20))['streams']
    if not records:
        problems.append(f"no Records stream in {prepared['feed']}" + (f" (Events streams instead: "
                                                                     f"{[s['id'] for s in events]})" if events else ''))
    else:
        read = await streams.read_stream(ctx, stream_id=max(s['id'] for s in records), first_record=0, record_count=1)
        notes.append(f"Records stream {max(s['id'] for s in records)}: {read.get('total_records')} records")
    async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
        await es.post(f"/{prepared['index']}/_refresh")
        counted = (await es.get(f"/{prepared['index']}/_count")).json().get('count')
        hits = (await es.get(f"/{prepared['index']}/_search", params={'size': 50})).json().get('hits', {}).get('hits', [])
    if counted != len(ASSET_HOSTS):
        problems.append(f"index {prepared['index']} holds {counted} documents, not {len(ASSET_HOSTS)}")
    hosts = sorted(str(h['_source'].get('hostname')) for h in hits)
    if hosts != ASSET_HOSTS:
        problems.append(f"the documents' hostnames are {hosts}, not every device's ({ASSET_HOSTS})")
    return problems, notes


WORKFLOWS = {
    'document_index': Workflow(
        id='document_index', prompt='document_index', setup=document_index_setup,
        reference=document_index_reference, score=document_index_score,
        done="the documentation is drafted, the user has its link, and it is promoted beside the index doc",
        promote="Yes, put it beside the index doc."),
    'fix_errors': Workflow(
        id='fix_errors', prompt='update_events_pipeline', setup=fix_errors_setup, reference=fix_errors_reference,
        score=fix_errors_score,
        done="the fix is proven on the feed's streams and promoted to the production pipeline, in place",
        promote="Yes, promote it in place."),
    'change_event_type': Workflow(
        id='change_event_type', prompt='update_events_pipeline', setup=change_event_type_setup,
        reference=change_event_type_reference, score=change_event_type_score,
        done="the change is proven and promoted to the production pipeline, in place",
        promote="Yes, promote it in place."),
    'records_output': Workflow(
        id='records_output', prompt='create_discovery_index', setup=records_output_setup,
        reference=records_output_reference, score=records_output_score,
        done="the inventory is in Records streams, indexed into Elasticsearch, the index verified and documented",
        promote="Don't promote: this is a scratch environment. Stop once the index is verified and documented."),
}
