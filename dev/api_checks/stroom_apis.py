"""Checks of the Stroom APIs the server relies on, built for the Stroom UI, against the local Docker stack.

    uv run python dev/api_checks/stroom_apis.py            # all steps, in order
    uv run python dev/api_checks/stroom_apis.py step       # one step (names below); state is kept in dev/api_checks/out/state.json

Every step prints what it learned. Nothing here talks to a shared Stroom.
"""
import json
import sys
import time
from pathlib import Path

from stroom_client import Stroom, show

OUT = Path(__file__).parent / 'out'
STATE_FILE = OUT / 'state.json'

FEED = 'APICHECK-AUTH-V1'  # default feed name rule: ^[A-Z0-9_-]{3,}$
SAMPLE = b"""time,user,host,result
2026-09-28T10:00:00,alice,ws01,ok
2026-09-28T10:05:00,bob,ws02,fail
2026-09-28T10:07:30,carol,ws03,ok
"""

DATA_SPLITTER = """<?xml version="1.1" encoding="UTF-8"?>
<dataSplitter xmlns="data-splitter:3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xsi:schemaLocation="data-splitter:3 file://data-splitter-v3.0.xsd" version="3.0">
  <split delimiter="\\n" maxMatch="1">
    <group>
      <split delimiter=",">
        <var id="heading" />
      </split>
    </group>
  </split>
  <split delimiter="\\n">
    <group>
      <split delimiter=",">
        <data name="$heading$1" value="$1" />
      </split>
    </group>
  </split>
</dataSplitter>
"""

XSLT = """<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="records:2" xmlns="event-logging:3" xmlns:stroom="stroom"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="3.0">
  <xsl:template match="records">
    <Events xsi:schemaLocation="event-logging:3 file://event-logging-v4.1.0.xsd" Version="4.1.0">
      <xsl:apply-templates />
    </Events>
  </xsl:template>
  <xsl:template match="record">
    <Event>
      <EventTime>
        <TimeCreated><xsl:value-of select="stroom:format-date(data[@name='time']/@value, 'yyyy-MM-dd''T''HH:mm:ss')" /></TimeCreated>
      </EventTime>
      <EventSource>
        <System><Name>APICHECK</Name><Environment>Dev</Environment></System>
        <Generator>api-checks</Generator>
        <Device><HostName><xsl:value-of select="data[@name='host']/@value" /></HostName></Device>
        <User><Id><xsl:value-of select="data[@name='user']/@value" /></Id></User>
      </EventSource>
      <EventDetail>
        <TypeId>Logon</TypeId>
        <Description>User logon</Description>
        <Authenticate>
          <Action>Logon</Action>
          <User><Id><xsl:value-of select="data[@name='user']/@value" /></Id></User>
          <Outcome><Success><xsl:value-of select="data[@name='result']/@value = 'ok'" /></Success></Outcome>
        </Authenticate>
      </EventDetail>
    </Event>
  </xsl:template>
</xsl:stylesheet>
"""
# Same XSLT with an element the schema does not allow, to prove draft-code overrides reach validation.
BROKEN_XSLT = XSLT.replace('<Generator>api-checks</Generator>', '<NotAnElement>x</NotAnElement>')

s = Stroom()


def load() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save(state: dict):
    OUT.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1))


def ref(doc: dict) -> dict:
    return {'type': doc['type'], 'uuid': doc['uuid'], 'name': doc['name']}


# --- steps -----------------------------------------------------------------------------------

def workspace(st):
    """Create MCP Workspace/<build> folders; find the templates the build uses."""
    system = s.system_node()
    ws = next((d for d in s.find('MCP Workspace', ['Folder']) if d['name'] == 'MCP Workspace'), None)
    ws_node = s.post('/explorer/v2/getFromDocRef', ref(ws)) if ws else s.create('Folder', 'MCP Workspace', system)
    build = s.create('Folder', f'api-checks-{int(time.time())}', ws_node)
    templates = {d['name']: ref(d) for d in s.find('*', ['Pipeline']) if 'Template Pipelines' in (d['path'] or '')}
    st.update(workspace=ws_node, build=build, template=templates['Event Data (Text)'])
    show('build folder node', {k: build.get(k) for k in ('type', 'uuid', 'name')})


def feed(st):
    """Create the feed in the build folder and set its stream type and encoding."""
    node = s.create('Feed', FEED, st['build'])
    doc = s.get(f"/feed/v1/{node['uuid']}")
    doc.update(streamType='Raw Events', encoding='UTF-8', description='Stroom API checks feed')
    doc = s.put(f"/feed/v1/{node['uuid']}", doc)
    st['feed'] = ref(doc)
    show('feed', {k: doc.get(k) for k in ('name', 'uuid', 'streamType', 'encoding')})


def upload(st):
    """POST the sample to /stroom/datafeed and find the Raw Events stream it created."""
    r = s.datafeed(FEED, SAMPLE)
    show('datafeed response', {'status': r.status_code, 'body': r.text[:300], 'headers': dict(r.headers)})
    r.raise_for_status()
    metas = s.wait('raw stream', lambda: s.find_meta(('Feed', 'EQUALS', FEED), ('Type', 'EQUALS', 'Raw Events')))
    st['raw_ids'] = [m['id'] for m in metas]
    show('raw streams', metas)


def content(st):
    """Create the text converter and XSLT in the build folder."""
    tc = s.create('TextConverter', FEED, st['build'])
    doc = s.get(f"/textConverter/v1/{tc['uuid']}")
    doc.update(converterType='DATA_SPLITTER', data=DATA_SPLITTER)
    st['text_converter'] = ref(s.put(f"/textConverter/v1/{tc['uuid']}", doc))
    x = s.create('XSLT', f'{FEED}-Events', st['build'])
    doc = s.get(f"/xslt/v1/{x['uuid']}")
    doc['data'] = XSLT
    st['xslt'] = ref(s.put(f"/xslt/v1/{x['uuid']}", doc))
    show('content', {'text_converter': st['text_converter'], 'xslt': st['xslt']})


def pipeline(st):
    """Create a child of the template that sets only the text converter and XSLT; read its layers back."""
    p = s.create('Pipeline', f'{FEED}-Events', st['build'])
    doc = s.get(f"/pipeline/v1/{p['uuid']}")
    doc['parentPipeline'] = st['template']
    doc['pipelineData'] = {'properties': {'add': [
        {'element': 'dsParser', 'name': 'textConverter', 'value': {'entity': st['text_converter']}},
        {'element': 'translationFilter', 'name': 'xslt', 'value': {'entity': st['xslt']}},
    ]}}
    doc = s.put(f"/pipeline/v1/{p['uuid']}", doc)
    st['pipeline'] = ref(doc)
    layers = s.post('/pipeline/v1/fetchPipelineLayers', st['pipeline'])
    own = s.post('/pipeline/v1/fetchPipelineJson', st['pipeline'])
    show('layers (source pipelines)', [l['sourcePipeline']['name'] for l in layers])
    show("child's own pipeline JSON", own['json'], 800)


def _step_all(st, code=None, label=''):
    """Step every record of the raw stream; return per-record translation output and indicators."""
    doc = s.get(f"/pipeline/v1/{st['pipeline']['uuid']}")
    criteria = {'expression': {'type': 'operator', 'op': 'AND', 'children': [
        {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': str(st['raw_ids'][0])}]}}
    request = {'pipelineDoc': doc, 'criteria': criteria, 'stepType': 'FIRST', 'stepSize': 1, 'timeout': 30000,
               'code': code or {}}
    records, session, started = [], None, time.time()
    while time.time() - started < 300:
        result = s.post('/stepping/v1/step', request)
        session = result.get('sessionUuid')
        if not result.get('complete'):
            # Still running: poll the same session (it expires after 10 s without a request).
            request = {**request, 'sessionUuid': session, 'stepType': 'REFRESH'}
            time.sleep(1)
            continue
        if not result.get('foundRecord'):
            break
        elements = (result.get('stepData') or {}).get('elementMap') or {}
        errors = {eid: (e.get('indicators') or {}).get('uniqueErrorSet') or []
                  for eid, e in elements.items() if (e.get('indicators') or {}).get('errorCount')}
        records.append({'location': result.get('foundLocation'), 'errors': errors,
                        'translation_output': (elements.get('translationFilter') or {}).get('output', '')[:600]})
        # A session ends when its step completes (SteppingService removes it); the session id only
        # polls an incomplete step. Each new step is a fresh request from the last found location.
        request = {k: v for k, v in request.items() if k != 'sessionUuid'}
        request.update(stepType='FORWARD', stepLocation=result.get('foundLocation'))
        session = None
        if result.get('generalErrors'):
            show(f'{label} general errors', result['generalErrors'])
    if session:
        s.post('/stepping/v1/terminateStepping', {**request, 'sessionUuid': session})
    return records


def step(st):
    """Step every record with the saved XSLT, then with broken draft code that is never saved."""
    good = _step_all(st, label='saved')
    show(f'stepped {len(good)} records with saved code; errors per record', [r['errors'] for r in good])
    show('first record translation output', good[0]['translation_output'] if good else None)
    bad = _step_all(st, code={'translationFilter': BROKEN_XSLT}, label='draft')
    show(f'stepped {len(bad)} records with broken draft code; errors in first record', bad[0]['errors'] if bad else None, 2500)
    st['stepping'] = {'saved_records': len(good), 'saved_errors': sum(bool(r['errors']) for r in good),
                      'draft_records': len(bad), 'draft_errors': sum(bool(r['errors']) for r in bad)}


def process(st):
    """Create a processor filter on the sample stream ids; check exactly one Events child per raw stream."""
    request = {'pipeline': st['pipeline'], 'processorType': 'PIPELINE', 'enabled': True, 'priority': 10,
               'autoPriority': False, 'reprocess': False, 'export': False, 'maxProcessingTasks': 0,
               'queryData': {'dataSource': {'type': 'StreamStore', 'uuid': '0', 'name': 'StreamStore'},
                             'expression': {'type': 'operator', 'op': 'OR', 'children': [
                                 {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': str(i)}
                                 for i in st['raw_ids']]}}}
    f = s.post('/processorFilter/v1', request)
    st['filter_id'] = f['id']
    show('processor filter', {k: f.get(k) for k in ('id', 'enabled', 'priority', 'pipelineName')})
    for raw in st['raw_ids']:
        events = s.wait(f'Events child of {raw}', lambda: s.find_meta(('Parent Id', 'EQUALS', raw),
                                                                      ('Type', 'EQUALS', 'Events')), timeout=300)
        errors = s.find_meta(('Parent Id', 'EQUALS', raw), ('Type', 'EQUALS', 'Error'))
        st.setdefault('events_ids', []).extend(m['id'] for m in events)
        show(f'raw {raw}: Events children {len(events)}, Error children {len(errors)}',
             [{'id': m['id'], 'type': m['typeName']} for m in events + errors])
    data = s.post('/data/v1/fetch', {'sourceLocation': {'metaId': st['events_ids'][0], 'partIndex': 0,
                                                        'recordIndex': 0, 'childType': None},
                                     'displayMode': 'TEXT', 'recordCount': 1})
    show('first Events record', data.get('data'), 1200)


def documentation(st):
    """Create a Documentation doc beside the pipeline and read it back."""
    node = s.create('Documentation', st['pipeline']['name'], st['build'])
    doc = s.get(f"/documentation/v1/{node['uuid']}")
    show('empty Documentation doc fields', list(doc.keys()))
    text = '# APICHECK-AUTH-V1.0-Events\n\n## Purpose and data\n\nStroom API checks: CSV logons to event-logging.\n'
    doc['documentation'] = text
    saved = s.put(f"/documentation/v1/{node['uuid']}", doc)
    back = s.get(f"/documentation/v1/{node['uuid']}")
    st['documentation'] = ref(saved)
    show('round trip ok', back.get('documentation') == text)


def dashboard(st):
    """Round-trip an existing dashboard's JSON into a new workspace dashboard."""
    source = next(iter(s.find('*', ['Dashboard'])), None)
    if source is None:
        show('no dashboards to copy')
        return
    src = s.get(f"/dashboard/v1/{source['uuid']}")
    node = s.create('Dashboard', f"{FEED}-VERIFY", st['build'])
    doc = s.get(f"/dashboard/v1/{node['uuid']}")
    doc['dashboardConfig'] = src['dashboardConfig']
    s.put(f"/dashboard/v1/{node['uuid']}", doc)
    back = s.get(f"/dashboard/v1/{node['uuid']}")
    st['dashboard'] = ref(back)
    comps = [(c['type'], c.get('name')) for c in back['dashboardConfig']['components']]
    show(f"copied dashboard config from {source['name']}; components", comps)
    show('query component settings (shape)', next((c['settings'] for c in back['dashboardConfig']['components']
                                                   if c['type'] == 'query'), None), 1500)


def promote(st):
    """Move the feed from the build folder to a sibling folder and confirm its UUID is unchanged."""
    target = s.create('Folder', f"promoted-{int(time.time())}", st['workspace'])
    feed_node = s.post('/explorer/v2/getFromDocRef', st['feed'])
    moved = s.call('PUT', '/explorer/v2/move', {'explorerNodes': [feed_node], 'destinationFolder': target,
                                                'permissionInheritance': 'DESTINATION'})
    info = s.post('/explorer/v2/info', st['feed'])
    show('move response (shape)', moved, 600)
    show('feed after move', {'uuid': info['explorerNode']['uuid'], 'same_uuid': info['explorerNode']['uuid'] == st['feed']['uuid']})
    after = s.find(FEED, ['Feed'])
    show('feed path after move', [d['path'] for d in after])


STEPS = [workspace, feed, upload, content, pipeline, step, process, documentation, dashboard, promote]

if __name__ == '__main__':
    wanted = sys.argv[1:] or [f.__name__ for f in STEPS]
    state = load()
    for fn in STEPS:
        if fn.__name__ in wanted:
            print(f'\n##### {fn.__name__}: {fn.__doc__.strip().splitlines()[0]}')
            try:
                fn(state)
            finally:
                save(state)
