"""Onboarding translations end to end against the local Docker stack, driving the real tools.

    uv run python dev/e2e_translation.py
    E2E_TARGET=live E2E_STAMP=... uv run python dev/e2e_translation.py   # the read/write instance in .ai/secrets

Against live, everything is named with the run stamp and promoted into a workspace folder, so
dev/e2e_cleanup.py can remove the run afterwards.

1. CSV, JSON, XML and syslog samples each go from sample to valid Events: feed, upload, template,
   converter and XSLT, pipeline, stepping, processing (exactly one Events stream per raw stream),
   validation and documentation.
2. A reported field fix: draft change, compare_outputs shows only that field changing, save, reprocess (one task;
   Stroom supersedes the earlier output, leaving exactly one Events stream).
3. Promotion of the CSV build, then an in-place fix through a working copy written back on promotion.

Confirmations and approvals are granted here the way a user would, by passing the returned id back.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from security.guard import guard_from  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import (builds, feeds, pipeline_writes, processing_writes, stepping, streams, templates,  # noqa: E402
                   translation, validation)
from tools.pipeline_writes import PropertyValue  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

LIVE = os.environ.get('E2E_TARGET') == 'live'
VERSION = '3.5.2' if LIVE else '4.1.0'  # the local content pack has 4.1.0; live pipelines use 3.5.2
# One stamp for every script in a run, so the run can be found and cleaned up.
def run_stamp() -> str:
    """The stamp a run puts in the names of what it creates: the time and the process, so suites started in the same
    second don't share names (seen running four at a time: a feed of the same name, a build both promoted into)."""
    return f"{time.strftime('%H%M%S')}{os.getpid() % 1000:03d}"


STAMP = os.environ.get('E2E_STAMP') or run_stamp()


def target_settings() -> 'Settings':
    """The local stack, or with E2E_TARGET=live the instance in .ai/secrets."""
    if LIVE:
        secrets = env(ROOT / '.ai' / 'secrets')
        url, key = secrets['STROOM_URL'], secrets['STROOM_API_KEY']
    else:
        url, key = 'http://127.0.0.1:18080', env(ROOT / 'dev' / 'stroom' / '.env')['STROOM_ADMIN_API_KEY']
    return Settings(_env_file=None, stroom_url=url, dev_no_auth=True, stroom_api_key=key, oidc_issuer_url='-',
                    oidc_audience='-', public_base_url='-', event_logging_version=VERSION)
EVENT_TAIL = """
      <EventDetail>
        <TypeId>{type_id}</TypeId>
        <Description>{description}</Description>
        <Authenticate>
          <Action>Logon</Action>
          <User><Id>{user}</Id></User>
          <Outcome><Success>{success}</Success></Outcome>
        </Authenticate>
      </EventDetail>"""


def xslt(default_ns: str, root_match: str, record_match: str, time_expr: str, host: str, user: str, ip: str,
         success: str, type_id: str = 'Logon', description: str = 'User logon') -> str:
    return f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="{default_ns}" xmlns="event-logging:3" xmlns:stroom="stroom"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="3.0">
  <xsl:template match="{root_match}">
    <Events xsi:schemaLocation="event-logging:3 file://event-logging-v{VERSION}.xsd" Version="{VERSION}">
      <xsl:apply-templates />
    </Events>
  </xsl:template>
  <xsl:template match="{record_match}">
    <Event>
      <EventTime><TimeCreated><xsl:value-of select="{time_expr}" /></TimeCreated></EventTime>
      <EventSource>
        <System><Name>E2E</Name><Environment>Dev</Environment></System>
        <Generator>e2e-translation</Generator>
        <Device><HostName><xsl:value-of select="{host}" /></HostName><IPAddress><xsl:value-of select="{ip}" /></IPAddress></Device>
        <User><Id><xsl:value-of select="{user}" /></Id></User>
      </EventSource>{EVENT_TAIL.format(type_id=type_id, description=description,
                                       user=f'<xsl:value-of select="{user}" />'.replace('<Id>', ''),
                                       success=f'<xsl:value-of select="{success}" />')}
    </Event>
  </xsl:template>
</xsl:stylesheet>
"""


CSV_SPLITTER = """<?xml version="1.1" encoding="UTF-8"?>
<dataSplitter xmlns="data-splitter:3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xsi:schemaLocation="data-splitter:3 file://data-splitter-v3.0.xsd" version="3.0">
  <split delimiter="\\n" maxMatch="1"><group><split delimiter=","><var id="heading" /></split></group></split>
  <split delimiter="\\n"><group><split delimiter=","><data name="$heading$1" value="$1" /></split></group></split>
</dataSplitter>
"""
SYSLOG_SPLITTER = """<?xml version="1.1" encoding="UTF-8"?>
<dataSplitter xmlns="data-splitter:3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xsi:schemaLocation="data-splitter:3 file://data-splitter-v3.0.xsd" version="3.0">
  <split delimiter="\\n">
    <group>
      <regex pattern="^&lt;(\\d+)&gt;1 (\\S+) (\\S+) (\\S+) (\\S+) (\\S+) (\\S+) Accepted (\\S+) for (\\S+) from (\\S+).*$">
        <data name="pri" value="$1" /><data name="time" value="$2" /><data name="host" value="$3" />
        <data name="app" value="$4" /><data name="method" value="$8" /><data name="user" value="$9" />
        <data name="ip" value="$10" />
      </regex>
    </group>
  </split>
</dataSplitter>
"""

CASES = {
    'csv': {
        'template': 'Event Data (Text)',
        'sample': "time,user,host,ip,result\n2026-09-28T10:00:00,alice,ws01,10.0.0.1,ok\n"
                  "2026-09-28T10:05:00,bob,ws02,10.0.0.2,fail\n2026-09-28T10:07:30,carol,ws03,10.0.0.3,ok\n",
        'converter': ('DATA_SPLITTER', CSV_SPLITTER),
        'xslt': xslt('records:2', 'records', 'record',
                     "stroom:format-date(data[@name='time']/@value, 'yyyy-MM-dd''T''HH:mm:ss')",
                     "data[@name='host']/@value", "data[@name='user']/@value", "data[@name='ip']/@value",
                     "data[@name='result']/@value = 'ok'"),
    },
    'json': {
        'template': 'Event Data (JSON)',
        'sample': json.dumps([{'ts': '2026-09-28T11:00:00Z', 'user': 'dave', 'host': 'ws04', 'src': '10.0.1.4', 'ok': True},
                              {'ts': '2026-09-28T11:02:00Z', 'user': 'erin', 'host': 'ws05', 'src': '10.0.1.5', 'ok': False}]),
        'xslt': xslt('http://www.w3.org/2013/XSL/json', '/array', 'map',
                     "stroom:format-date(string[@key='ts'], 'yyyy-MM-dd''T''HH:mm:ssX')", "string[@key='host']",
                     "string[@key='user']", "string[@key='src']", "boolean[@key='ok']"),
    },
    'xml': {
        'template': 'Event Data (XML)',
        'sample': '<logons><logon><when>2026-09-28 12:00:00</when><who>frank</who><host>ws06</host>'
                  '<addr>10.0.2.6</addr><result>SUCCESS</result></logon><logon><when>2026-09-28 12:01:00</when>'
                  '<who>grace</who><host>ws07</host><addr>10.0.2.7</addr><result>FAILURE</result></logon></logons>',
        'xslt': xslt('', 'logons', 'logon', "stroom:format-date(when, 'yyyy-MM-dd HH:mm:ss')", 'host', 'who', 'addr',
                     "result = 'SUCCESS'"),
    },
    'syslog': {
        'template': 'Event Data (Text)',
        'sample': '<38>1 2026-09-28T13:00:00.000Z ws08 sshd 812 - - Accepted password for heidi from 10.0.3.8 port 22 ssh2\n'
                  '<38>1 2026-09-28T13:04:00.000Z ws09 sshd 813 - - Accepted publickey for ivan from 10.0.3.9 port 22 ssh2\n',
        'converter': ('DATA_SPLITTER', SYSLOG_SPLITTER),
        'xslt': xslt('records:2', 'records', 'record',
                     "stroom:format-date(data[@name='time']/@value, 'yyyy-MM-dd''T''HH:mm:ss.SSSX')",
                     "data[@name='host']/@value", "data[@name='user']/@value", "data[@name='ip']/@value", "true()",
                     type_id='SSH-Accepted', description='SSH logon accepted'),
    },
}


def env(path: Path) -> dict[str, str]:
    return {k.strip(): v.strip() for k, v in (l.split('=', 1) for l in path.read_text().splitlines() if '=' in l)}


def check(condition: bool, message: str):
    print(('  PASS ' if condition else '  FAIL ') + message)
    if not condition:
        raise SystemExit(1)


def field_rows(section: str) -> dict[str, list[str]]:
    """The Field mapping tables' rows, by field: the cells after the field's name."""
    rows = {}
    for line in section.splitlines():
        if line.startswith('| `'):
            cells = [c.strip() for c in line.strip().strip('|').split(' | ')]
            rows.setdefault(cells[0].strip('`'), cells[1:])
    return rows


async def documented_to_the_field(stroom, written: dict, fields: list[str], values: dict[str, str],
                                  what: str = 'the indexing pipeline') -> None:
    """The documentation as Stroom keeps it goes down to the field: every field in its own row, each row with what
    the sample gave (values, or that it had none), and the values expected found in their field's row."""
    text = (await stroom.get_doc('Documentation', written['uuid'])).get('data') or ''
    section = text.split('## Field mapping')[1].split('\n## ')[0] if '## Field mapping' in text else ''
    rows = field_rows(section)
    missing = [f for f in fields if f not in rows]
    check(bool(section) and not missing,
          f"{what}'s documentation, as Stroom keeps it, has a row for each of its {len(fields)} fields"
          + (f": missing {missing}" if missing else ''))
    bare = [f for f, cells in rows.items() if not cells or not cells[-1]]
    check(not bare and 'Sample values' in section,
          f"every row ({len(rows)}) says what the sample gave: values, or that it had none" + (f": not {bare}" if bare else ''))
    # The Description column, after the field's name: what the field is. How often and which values are the In
    # sample and Sample values columns' to say, so the description does not repeat them.
    undescribed = [f for f, cells in rows.items() if not cells or not cells[0].strip()]
    repeating = [f for f, cells in rows.items() if cells and any(
        k in cells[0] for k in ('sampled document', 'in the sample', 'distinct value', 'document that has it'))]
    check('| Description |' in section and not undescribed and not repeating,
          f"each field described, without repeating the sample, e.g. {fields[-1]}: {(rows.get(fields[-1]) or [''])[0]!r}"
          + (f": not {undescribed}" if undescribed else '') + (f": repeats the sample {repeating}" if repeating else ''))
    wrong = {f: (rows.get(f) or ['?'])[-1] for f, v in values.items() if f'`{v}' not in (rows.get(f) or [''])[-1]}
    check(not wrong, "with the sample's values in their field's row, e.g. "
                     + ', '.join(f"{f} = {v}" for f, v in list(values.items())[:4]) + (f": not {wrong}" if wrong else ''))
    if 'In sample' in section:
        # Rows of the tables with an In sample column (a discovery doc's explicit fields have none).
        unsaid, counted, header = [], 0, ''
        for line in section.splitlines():
            if line.startswith('| ') and not line.startswith('| `') and '---' not in line:
                header = line
            elif line.startswith('| `') and 'In sample' in header:
                counted += 1
                if not any(k in line for k in ('always', '% of', 'not in the sample', 'not stored', 'not read', 'not surveyed')):
                    unsaid.append(line.split(' | ')[0].strip('| `'))
        check(counted and not unsaid, f"and how often each of the {counted} was populated in the sample"
                                      + (f": not {unsaid}" if unsaid else ''))


async def agreed(call, **kwargs):
    """Call a gated tool, then call again with the id it returned, as a user agreeing would."""
    ids, asked = {}, None
    while True:
        try:
            result = await call(**kwargs, **ids)
        except ToolError as e:
            if 'issued for a different request' not in str(e) or asked is None:
                raise
            # Seen once with four suites running: say what changed between asking and agreeing.
            again = await call(**kwargs)
            changed = {k: (asked['details'].get(k), (again.get('details') or {}).get(k))
                       for k in set(asked['details']) | set(again.get('details') or {})
                       if asked['details'].get(k) != (again.get('details') or {}).get(k)}
            raise ToolError(f"{e} -- details changed between asking and agreeing: {changed}") from e
        if not (isinstance(result, dict) and str(result.get('status', '')).startswith('needs_')):
            return result
        if result['status'] == 'needs_review':
            # Shown to the user in the chat (an index template), then confirmed in a form.
            print(f"    needs_review: {(result.get('dev_tools') or '').splitlines()[0]}")
            kwargs['reviewed'] = True
            continue
        key = 'confirmation_id' if result['status'] == 'needs_confirmation' else 'approval_id'
        print(f"    {result['status']}: {result['summary']}")
        ids[key], asked = result[key], result


async def onboard(ctx, fmt: str, case: dict, stamp: str) -> dict:
    print(f'\n### {fmt}')
    build = f'e2e-{fmt}-{stamp}'
    feed_name = f'E2E-{fmt.upper()}-{stamp}'
    profile = await feeds.profile_sample(ctx, case['sample'])
    print(f"  profile: {profile['format']}; parser: {profile.get('suggested_parser')}")
    await agreed(feeds.create_feed, ctx=ctx, build=build, name=feed_name)
    upload = await feeds.upload_sample(ctx, feed_name, case['sample'])
    raw = upload['stream_id']
    check(raw is not None, f"sample uploaded as stream {raw}")
    candidates = (await templates.find_pipeline_templates(ctx, 'translation'))['candidates']
    template = next(c for c in candidates if c['name'] == case['template'])
    props = []
    if 'converter' in case:
        tc = await translation.create_text_converter(ctx, build, feed_name, *case['converter'])
        props.append(PropertyValue(element='dsParser', name='textConverter', doc_uuid=tc['uuid'], doc_type='TextConverter'))
    x = await translation.create_xslt(ctx, build, f'{feed_name}-Events', case['xslt'])
    props.append(PropertyValue(element='translationFilter', name='xslt', doc_uuid=x['uuid'], doc_type='XSLT'))
    if fmt == 'json':
        props.append(PropertyValue(element='jsonParser', name='addRootObject', value=False))
    pipeline = await agreed(pipeline_writes.create_pipeline, ctx=ctx, build=build, name=f'{feed_name}-Events',
                            template_uuid=template['uuid'], set_properties=props)
    sample = await stepping.step_sample(ctx, pipeline['uuid'], [raw])
    if sample['verdict'] != 'clean':
        print(json.dumps(sample['groups'], indent=1)[:2000])
    check(sample['verdict'] == 'clean', f"stepped {sample['records_stepped']} records clean")
    await agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'], stream_ids=[raw])
    gate = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], [raw])
    check(gate['gate'] == 'pass', f"one Events stream per raw stream: {gate['streams']}")
    events_id = gate['streams'][0]['events'][0]
    record = (await streams.read_stream(ctx, events_id, 0, 1))['records'][0]
    valid = await validation.validate_events(ctx, record)
    check(valid['valid'], f"Events valid against {valid['schema']}")
    quality = await validation.check_event_quality(ctx, record)
    check(quality['ok'], 'event quality checks pass')
    doc = await builds.write_documentation(ctx, build, pipeline['uuid'],
                                           f"# {pipeline['name']}\n\n## Purpose and data\n\n{fmt} sample for the translation e2e test.\n\n"
                                           # Written by hand (no mapping kept with the XSLT), so the section is too.
                                           f"## Field mapping\n\n| XPath | From |\n| --- | --- |\n"
                                           f"| `EventSource/User/Id` | the user field |\n",
                                           'Created')
    stored = await ctx.lifespan_context['stroom'].get_doc('Documentation', doc['uuid'])
    text = stored.get('data') or ''
    # The body (data) is what the Stroom UI shows; documentation is the Documentation tab.
    check('translation e2e test' in text and '## Version control' in text and not stored.get('documentation'),
          f"documentation written to the doc's body: {len(text)} chars")
    return {'build': build, 'feed': feed_name, 'raw': raw, 'pipeline': pipeline, 'xslt': x, 'doc': doc}


async def field_fix(ctx, csv: dict):
    print('\n### field fix (CSV): report says the event should carry the host IP as Client/IPAddress')
    original = (await ctx.lifespan_context['stroom'].get_doc('XSLT', csv['xslt']['uuid']))['data']
    draft = original.replace('<User><Id><xsl:value-of select="data[@name=\'user\']/@value" /></Id></User>\n      </EventSource>',
                             '<Client><IPAddress><xsl:value-of select="data[@name=\'ip\']/@value" /></IPAddress></Client>\n'
                             '        <User><Id><xsl:value-of select="data[@name=\'user\']/@value" /></Id></User>\n      </EventSource>')
    check(draft != original, 'draft differs from the saved XSLT')
    diff = await stepping.compare_outputs(ctx, csv['pipeline']['uuid'], [csv['raw']], draft_code={'translationFilter': draft})
    paths = [f['path'] for f in diff['fields_changed']]
    print(f"    changed paths: {paths}")
    check(paths == ['Event/EventSource/Client/IPAddress'], 'diff limited to the reported field')
    step = await stepping.step_sample(ctx, csv['pipeline']['uuid'], [csv['raw']], draft_code={'translationFilter': draft})
    check(step['verdict'] == 'clean', 'draft steps clean')
    await translation.update_xslt(ctx, csv['xslt']['uuid'], draft)
    try:
        await processing_writes.create_processor_filter(ctx, csv['pipeline']['uuid'], stream_ids=[csv['raw']])
        refused = ''
    except ToolError as e:
        refused = str(e)
    check('use reprocess_streams' in refused, f"a second plain filter is refused: {refused}")
    run = await agreed(processing_writes.reprocess_streams, ctx=ctx, pipeline_uuid=csv['pipeline']['uuid'],
                       stream_ids=[csv['raw']])
    check(run.get('max_tasks') == 1, f"reprocess filter {run.get('filter_id')} runs one task at a time")
    gate = await processing_writes.wait_for_processing(ctx, csv['pipeline']['uuid'], [csv['raw']], filter_id=run['filter_id'])
    check(gate['gate'] == 'pass', f"the reprocess made exactly one new Events stream: {gate['streams']}")
    everything = await processing_writes.wait_for_processing(ctx, csv['pipeline']['uuid'], [csv['raw']], timeout_seconds=5)
    check(everything['streams'][0]['events'] == gate['streams'][0]['events'],
          "Stroom marked the earlier Events stream superseded (deleted), leaving one")


async def promotion(ctx, csv: dict, stamp: str):
    print('\n### promotion of the CSV build, then an in-place fix through a working copy')
    stroom = ctx.lifespan_context['stroom']
    if LIVE:
        # Keep the run inside the workspace on a shared instance.
        dest = (await guard_from(ctx).build_folder(f'e2e-promoted-{stamp}'))['_path']
    else:
        # Neither folder exists yet: promotion creates both, after approval.
        dest = f'System/E2E Promoted {stamp}/Events'
    everything = {t: dest for t in ('Feed', 'Pipeline', 'XSLT', 'TextConverter', 'Documentation')}
    ref = {'type': 'Pipeline', 'uuid': csv['pipeline']['uuid'], 'name': csv['pipeline']['name']}
    stepped = [t for t in (await stroom.post('/explorer/v2/getFromDocRef', ref)).get('tags') or [] if t.startswith('mcp-stepped-')]
    check(stepped, f"clean steps are recorded on the pipeline: {stepped}")
    # A new context, as after a restart or on another replica: the record is in Stroom, not in memory.
    fresh = SimpleNamespace(lifespan_context=dict(ctx.lifespan_context))
    listed = await builds.list_build(fresh, csv['build'])
    check(listed['before_promotion'] == [], f"stepped clean and documented, nothing outstanding: {listed['before_promotion']}")
    result = await agreed(builds.promote_build, ctx=ctx, build=csv['build'], destinations=everything)
    check('promoted_with_warnings' not in result, 'promoted without warnings')
    check(len(result['promoted']) >= 5, f"promoted: {result['promoted']}")
    if not LIVE:
        check(result['promoted'][:2] == [f'created folder System/E2E Promoted {stamp}',
                                         f'created folder System/E2E Promoted {stamp}/Events'],
              f"missing destination folders created first: {result['promoted'][:2]}")
    text = (await stroom.get_doc('Documentation', csv['doc']['uuid'])).get('data') or ''
    check('translation e2e test' in text, f"promoted documentation keeps its text: {len(text)} chars")
    # The build's changes, one version each: a row of the XSLT's history (its Documentation tab, not its code), a row
    # at the end of its documentation.
    from utils.xsltversion import history
    xslt_doc = await stroom.get_doc('XSLT', csv['xslt']['uuid'])
    rows = history(xslt_doc.get('description'))
    check([r['version'] for r in rows] == ['1'] and rows[0]['how'] == 'agent' and '| 1 |' in text
          and 'version history' not in (xslt_doc.get('data') or ''),
          "version 1 recorded in the XSLT's history (its Documentation, not its code) and the documentation's version control")
    check("removed the build's workspace folder, now empty" in result['promoted']
          and await guard_from(ctx).build_folder(csv['build'], create=False) is None, 'the emptied build folder is removed')
    made = result.get('processing_filters') or []
    check([(f['pipeline'], f['feed'], f['enabled']) for f in made] == [(csv['pipeline']['name'], csv['feed'], False)],
          f"promotion pre-creates the pipeline's filter for its feed, disabled: {made}")
    stored = await stroom.get(f"/processorFilter/v1/{made[0]['filter_id']}")
    check(stored.get('enabled') is False and stored.get('minMetaCreateTimeMs'), 'the filter is disabled and only takes new data')
    info = await stroom.post('/explorer/v2/info', {'type': 'Pipeline', 'uuid': csv['pipeline']['uuid']})
    check(info['explorerNode']['uuid'] == csv['pipeline']['uuid'], 'pipeline kept its UUID')
    tags = (await stroom.post('/explorer/v2/getFromDocRef', {'type': 'Pipeline', 'uuid': csv['pipeline']['uuid'],
                                                             'name': csv['pipeline']['name']})).get('tags') or []
    check('mcp-generated' in tags and 'mcp-managed' not in tags and not any(t.startswith('mcp-stepped-') for t in tags),
          f"promoted pipeline keeps mcp-generated only: {tags}")

    # Documented again after promotion (seen in VS Code): the promoted doc is changed through a working copy, written
    # back by promoting the build again, not a second doc of its name in a new workspace folder.
    redo = await builds.write_documentation(
        ctx, csv['build'], csv['pipeline']['uuid'],
        f"# {csv['pipeline']['name']}\n\n## Purpose and data\n\nReworded after promotion.\n\n"
        f"## Field mapping\n\n| XPath | From |\n| --- | --- |\n| `EventSource/User/Id` | the user field |\n",
        'Purpose reworded after promotion')
    check((redo.get('working_copy_of') or {}).get('uuid') == csv['doc']['uuid'] and 'promote_build' in redo['next'],
          f"a working copy of the promoted doc, to promote: {redo.get('working_copy_of')} {redo.get('next', '')[:100]}")
    again = await agreed(builds.promote_build, ctx=ctx, build=csv['build'], destinations={})
    text = (await stroom.get_doc('Documentation', csv['doc']['uuid'])).get('data') or ''
    named = [v for v in (await stroom.find_documents(csv['doc']['name'], ['Documentation'], 20)).get('values') or []
             if v['docRef']['name'] == csv['doc']['name']]
    check('Reworded after promotion' in text and len(named) == 1
          and any('back over' in line for line in again['promoted']),
          f"written back over the promoted doc, the only one of its name: {again['promoted']}")

    fix_build = f'e2e-fix-{stamp}'
    copy = await agreed(pipeline_writes.copy_pipeline, ctx=ctx, build=fix_build, source_uuid=csv['pipeline']['uuid'],
                        new_name=f"{csv['pipeline']['name']}-WORKING", working_copy=True)
    xslt_copy = next(d for d in copy['copied_documents'] if d['type'] == 'XSLT')
    code = (await stroom.get_doc('XSLT', xslt_copy['uuid']))['data']
    changed = code.replace('<Description>User logon</Description>', '<Description>Interactive user logon</Description>')
    diff = await stepping.compare_outputs(ctx, csv['pipeline']['uuid'], [csv['raw']], draft_code={'translationFilter': changed})
    check([f['path'] for f in diff['fields_changed']] == ['Event/EventDetail/Description'], 'in-place change diff limited to Description')
    await translation.update_xslt(ctx, xslt_copy['uuid'], changed)
    try:
        await translation.update_xslt(ctx, csv['xslt']['uuid'], changed)
        check(False, 'guard refused a direct change to the promoted XSLT')
    except Exception as e:
        check('not created by this server' in str(e) or 'working copy' in str(e), f'guard refused a direct change: {str(e)[:80]}')
    before = (await builds.list_build(ctx, fix_build))['before_promotion']
    check(len(before) == 1 and 'no clean step' in before[0],
          f"the working copy's changed code was compared but never stepped clean: {before}")
    result = await agreed(builds.promote_build, ctx=ctx, build=fix_build, destinations={})
    print(f"    {result['promoted']}")
    check(await guard_from(ctx).build_folder(fix_build, create=False) is None,
          'the fix build folder is removed once its working copies are written back')
    check(result.get('promoted_with_warnings') == before, 'promotion carried the warning the user approved with')
    now = (await stroom.get_doc('XSLT', csv['xslt']['uuid']))['data']
    check('Interactive user logon' in now, 'working copy written back over the production XSLT')
    backups = []
    for _ in range(10):  # explorer search indexes new docs after a short delay
        backups = [v['docRef'] for v in (await stroom.find_documents(f"{csv['xslt']['name']} backup*", ['XSLT'], 10)).get('values') or []]
        if backups:
            break
        await asyncio.sleep(2)
    backup_tags = [(await stroom.post('/explorer/v2/getFromDocRef', b)).get('tags') or [] for b in backups]
    check(backups and all('mcp-generated' in tags for tags in backup_tags), f"the backup copy is tagged mcp-generated: {backup_tags}")


async def main():
    stroom = StroomGateway(target_settings())
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = STAMP
    print(f'RUN STAMP {stamp} against {stroom.settings.stroom_url if LIVE else "the local stack"}')
    only = sys.argv[1:] or list(CASES)
    try:
        built = {fmt: await onboard(ctx, fmt, CASES[fmt], stamp) for fmt in only if fmt in CASES}
        if 'csv' in built:
            await field_fix(ctx, built['csv'])
            await promotion(ctx, built['csv'], stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
