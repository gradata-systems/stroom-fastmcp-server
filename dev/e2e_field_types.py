"""Every field type the index plans support, single and multi-valued, indexed into Lucene and into Elasticsearch and
verified through Stroom, against the local Docker stack (Elasticsearch: the stack's elastic profile).

    uv run python dev/e2e_field_types.py

Asked for by the user: every supported data type covered end to end, including fields of several values (Stroom shows
them comma-separated in a dashboard) and fields set not to be indexed. Before, a field of several values was written
by the indexing XSLT as one value of them joined by spaces: two ports as the number "80 443".

1. A JSON source whose Events hold, per record, a keyword, a text, a long, a double, a boolean, an ip and a date, and
   repeated Data elements for tags, ports, peer addresses and times (two, one, or none per record), and a secret.
2. Lucene: the plan drafted with a field of each type, those the schema lets repeat marked multiple; the index doc,
   the secret set not to be indexed; the indexing XSLT saved from the plan, stepped, processed; verify_index with a
   check per type, multi-valued ones by one of their values, each hit traced; a check on the secret refused.
3. Elasticsearch: the same, with the user's example template mapping the secret "index": false; the template built,
   agreed and applied; the stored documents hold arrays; verify_index as for Lucene; the secret's check refused.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from config import Settings  # noqa: E402
from e2e_elastic_handover import ES, FIXTURE_TEMPLATE, _request, fixtures, live_cluster  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from tools import indexing, processing_writes, stepping, templates, translation  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import FieldPlan, PlannedField  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

check, agreed = e2e.check, e2e.agreed
RECORDS = [
    {'ts': '2026-10-20T08:00:00Z', 'user': 'alice', 'host': 'ws01', 'src': '10.0.0.1', 'port': 22, 'ratio': 0.25,
     'ok': True, 'msg': 'Connection accepted quickly', 'tags': ['vpn', 'admin'], 'ports': [80, 443],
     'peers': ['10.1.0.1', '10.1.0.2'], 'seen': ['2026-10-19T01:00:00Z', '2026-10-19T02:00:00Z'], 'secret': 's1'},
    {'ts': '2026-10-20T08:05:00Z', 'user': 'bob', 'host': 'ws02', 'src': '10.0.0.2', 'port': 8080, 'ratio': 0.75,
     'ok': False, 'msg': 'Connection refused', 'tags': ['vpn'], 'ports': [8443], 'peers': ['10.1.0.3'],
     'seen': ['2026-10-18T05:00:00Z'], 'secret': 's2'},
    {'ts': '2026-10-20T08:10:00Z', 'user': 'carol', 'host': 'ws03', 'src': '10.0.0.3', 'port': 443, 'ratio': 1.5,
     'ok': True, 'msg': 'Session accepted', 'tags': [], 'ports': [], 'peers': [], 'seen': [], 'secret': 's3'}]
WHEN = "stroom:format-date({of}, 'yyyy-MM-dd''T''HH:mm:ssX')"
TRANSLATION = f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="http://www.w3.org/2013/XSL/json" xmlns="event-logging:3" xmlns:stroom="stroom"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="3.0">
  <xsl:template match="/array">
    <Events xsi:schemaLocation="event-logging:3 file://event-logging-v{e2e.VERSION}.xsd" Version="{e2e.VERSION}">
      <xsl:apply-templates />
    </Events>
  </xsl:template>
  <xsl:template match="map">
    <Event>
      <EventTime><TimeCreated><xsl:value-of select="{WHEN.format(of="string[@key='ts']")}" /></TimeCreated></EventTime>
      <EventSource>
        <System><Name>E2E</Name><Environment>Dev</Environment></System>
        <Generator>e2e-field-types</Generator>
        <Device><HostName><xsl:value-of select="string[@key='host']" /></HostName>
                <IPAddress><xsl:value-of select="string[@key='src']" /></IPAddress></Device>
        <User><Id><xsl:value-of select="string[@key='user']" /></Id></User>
      </EventSource>
      <EventDetail>
        <TypeId>Logon</TypeId>
        <Description><xsl:value-of select="string[@key='msg']" /></Description>
        <Authenticate>
          <Action>Logon</Action>
          <User><Id><xsl:value-of select="string[@key='user']" /></Id></User>
          <Outcome><Success><xsl:value-of select="boolean[@key='ok']" /></Success></Outcome>
          <Data Name="port" Value="{{number[@key='port']}}" />
          <Data Name="ratio" Value="{{number[@key='ratio']}}" />
          <Data Name="secret" Value="{{string[@key='secret']}}" />
          <xsl:for-each select="array[@key='tags']/string"><Data Name="tag" Value="{{.}}" /></xsl:for-each>
          <xsl:for-each select="array[@key='ports']/number"><Data Name="ports" Value="{{.}}" /></xsl:for-each>
          <xsl:for-each select="array[@key='peers']/string"><Data Name="peer" Value="{{.}}" /></xsl:for-each>
          <xsl:for-each select="array[@key='seen']/string">
            <Data Name="seen" Value="{{{WHEN.format(of='.')}}}" />
          </xsl:for-each>
        </Authenticate>
      </EventDetail>
    </Event>
  </xsl:template>
</xsl:stylesheet>
"""
CASE = {'template': 'Event Data (JSON)', 'sample': json.dumps(RECORDS), 'xslt': TRANSLATION}
DATA = "EventDetail/Authenticate/Data[@Name='{}']/@Value"
# (name, type, source) beside the convention's own fields; the multi-valued ones are those of several values above.
EXTRA = [('port', 'long', DATA.format('port')), ('ratio', 'double', DATA.format('ratio')),
         ('ok', 'boolean', 'EventDetail/Authenticate/Outcome/Success'), ('tag', 'keyword', DATA.format('tag')),
         ('ports', 'long', DATA.format('ports')), ('peer', 'ip', DATA.format('peer')),
         ('seen', 'date', DATA.format('seen')), ('secret', 'keyword', DATA.format('secret'))]
MULTI = {'tag', 'ports', 'peer', 'seen'}


def checks(name, text: str, time_field: str, text_search) -> dict:
    """verify_index's checks: one a type, the multi-valued fields by one of their values (an element of an array)."""
    S = indexing.SearchCheck
    return {
        'exact': [{'field': name('tag'), 'value': 'admin'}, {'field': name('ports'), 'value': '443'},
                  {'field': name('peer'), 'value': '10.1.0.2'}, {'field': name('port'), 'value': '8080'}],
        'time_range': {'field': time_field, 'from': '2026-10-20T08:04:00.000Z', 'to': '2026-10-20T08:11:00.000Z',
                       'expected': 2},
        'searches': [S(field=name('port'), condition='GREATER_THAN', value='100', expected=2),
                     S(field=name('ratio'), condition='GREATER_THAN', value='0.5', expected=2),
                     S(field=name('ok'), value='true', expected=2),
                     S(field=name('tag'), value='vpn', expected=2),                      # one value of two, or the only one
                     S(field=name('tag'), condition='IN', value='vpn,admin', expected=2),
                     S(field=name('ports'), condition='GREATER_THAN', value='1000', expected=1),
                     S(field=name('peer'), value='10.1.0.3', expected=1),
                     S(field=name('seen'), condition='BETWEEN',
                       value='2026-10-19T00:00:00.000Z,2026-10-19T23:59:59.000Z', expected=1),
                     S(field=name('seen'), condition='LESS_THAN', value='2026-10-19T00:00:00.000Z', expected=1),
                     text_search(text)]}


def said(result: dict) -> None:
    for c in result['checks']:
        trace = c.get('trace') or {}
        print(f"    {'ok ' if c['pass'] else 'BAD'} {c['check']}: {c.get('returned')}"
              + (f" (traced to event {trace.get('event')})" if trace.get('traced') else f" (trace: {trace})" if trace else '')
              + (f" [{c['searched_as']}]" if c.get('searched_as') else ''))


async def refused_on(ctx, name: str, **kwargs) -> str:
    try:
        await indexing.verify_index(ctx, exact=[{'field': name, 'value': 's2'}], **kwargs)
    except ToolError as e:
        return str(e)
    return ''


def stored_value(listed: list[dict], name: str) -> str:
    return json.dumps(next((f for f in listed if f.get('fldName') == name), None))


async def lucene(ctx, src: dict, events: list[int], stamp: str) -> None:
    print('\n### Lucene')
    stroom = ctx.lifespan_context['stroom']
    index_name = f'E2E-TYPES-{stamp}-INDEX'
    extra = [PlannedField(name=n.capitalize(), type=t, source=s) for n, t, s in EXTRA]
    draft = await indexing.draft_index_mapping(ctx, 'lucene', index_name, 'stroom-flat', events, extra_fields=extra)
    plan = FieldPlan.model_validate(draft['plan'])
    multiple = {f.name for f in plan.fields if f.multiple}
    print(f"    plan: {[(f.name, f.type, f.multiple) for f in plan.fields]}")
    check({n.capitalize() for n in MULTI} <= multiple and not {'UserId', 'EventTime', 'StreamId'} & multiple,
          f"fields of several values marked so from the schema: {sorted(multiple)}")
    index = await agreed(indexing.create_index_doc, ctx=ctx, build=src['build'], backend='lucene', name=index_name,
                         time_field=plan.time_field)
    ref = {'type': 'Index', 'uuid': index['uuid'], 'name': index_name}
    listed = (await stroom.post('/index/v2/findFields', {'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 200}}))['values']
    secret = next(f for f in listed if f['fldName'] == 'Secret')
    # The secret is kept (stored) but not indexed, as a user would set it on the index doc.
    await stroom.request('PUT', '/index/v2/updateField', {'indexDocRef': ref, 'fieldName': 'Secret',
                                                          'indexField': {**secret, 'indexed': False}})
    listed = (await stroom.post('/index/v2/findFields', {'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 200}}))['values']
    check(next(f for f in listed if f['fldName'] == 'Secret').get('indexed') is False,
          f"the secret set not to be indexed: {stored_value(listed, 'Secret')}")
    xslt = await translation.save_xslt(ctx, src['build'], f'{index_name}-XSLT', index_plan=plan)
    template = next(c for c in (await templates.find_pipeline_templates(ctx, 'indexing'))['candidates']
                    if c['backend'] == 'lucene' and c['name'] == 'Indexing')
    pipeline = await agreed(indexing.create_indexing_pipeline, ctx=ctx, build=src['build'], name=f'{index_name} - Indexing',
                            template_uuid=template['uuid'], xslt_uuid=xslt['uuid'], index_uuid=index['uuid'])
    stepped = await stepping.step_pipeline(ctx, pipeline['uuid'], events[0], 0)
    output = next(iter(stepped['elements'].values()))['output']
    check(output.count('name="Ports"') == 2 and output.count('name="Tag"') == 2,
          'stepped: a field of two values written as two values, not one joined by a space')
    sample = await stepping.step_sample(ctx, pipeline['uuid'], events)
    check(sample['verdict'] == 'clean', f"stepped clean: {sample['verdict']}")
    await agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'], stream_ids=events,
                 source_pipeline_uuid=src['pipeline']['uuid'])
    gate = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], events, expect_events=False)
    check(gate['gate'] == 'pass', f"indexed with no Error stream: {gate['streams']}")
    name = lambda n: n.capitalize()
    columns = ['EventTime', 'UserId', *(n.capitalize() for n, _, _ in EXTRA if n != 'secret')]
    common = dict(build=src['build'], index_uuid=index['uuid'], backend='lucene', stream_ids=events,
                  expected_documents=3, fields=columns, pipeline_uuid=pipeline['uuid'])
    asked = checks(name, 'Description', 'EventTime',
                   lambda field: indexing.SearchCheck(field=field, condition='CONTAINS', value='accepted', expected=2))
    result = await agreed(indexing.verify_index, ctx=ctx, **common, **asked)
    said(result)
    check(result['passed'], 'every type verified, multi-valued fields by one of their values, each hit traced')
    refused = await refused_on(ctx, 'Secret', **common)
    check('no search through Stroom finds it' in refused and "isn't indexed" in refused,
          f"a check on the field not indexed is refused, saying why: {refused[:200]}")


async def elasticsearch(ctx, src: dict, events: list[int], stamp: str) -> None:
    import httpx
    print('\n### Elasticsearch')
    stroom = ctx.lifespan_context['stroom']
    es_template = next(c for c in (await templates.find_pipeline_templates(ctx, 'indexing'))['candidates']
                       if c['name'] == FIXTURE_TEMPLATE)
    cluster = await live_cluster(stroom)
    index = f'e2e-types-{stamp}-v1'
    types = {'port': 'long', 'ratio': 'double', 'ok': 'boolean', 'tag': 'keyword', 'ports': 'long', 'peer': 'ip',
             'seen': 'date'}
    acme = {n: {'type': t} for n, t in types.items()}
    acme['secret'] = {'type': 'keyword', 'index': False}          # kept in _source, not searchable
    example = 'PUT _index_template/e2e-types-v0\n' + json.dumps({
        'index_patterns': ['e2e-types-v0*'], 'template': {'settings': {'index': {'number_of_shards': 1,
                                                                                   'number_of_replicas': 0}},
                                                           'mappings': {'properties': {'acme': {'properties': acme}}}}})
    extra = [PlannedField(name=f'acme.{n}', type=t, source=s) for n, t, s in EXTRA]
    draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', index, 'ecs', events, extra_fields=extra,
                                               example_template=example, custom_namespaces=['acme'])
    plan = FieldPlan.model_validate(draft['plan'])
    print(f"    plan: {[(f.name, f.type, f.multiple) for f in plan.fields]}")
    check({f'acme.{n}' for n in MULTI} <= {f.name for f in plan.fields if f.multiple},
          'fields of several values marked so from the schema')
    xslt = await translation.save_xslt(ctx, src['build'], f'{index}-XSLT', index_plan=plan)
    pipeline = await agreed(indexing.create_indexing_pipeline, ctx=ctx, build=src['build'], name=f'{index} - Indexing',
                            template_uuid=es_template['uuid'], xslt_uuid=xslt['uuid'], index_name=index,
                            cluster_uuid=cluster['uuid'])
    stepped = await stepping.step_pipeline(ctx, pipeline['uuid'], events[0], 0)
    output = next(iter(stepped['elements'].values()))['output']
    check('<array key="ports">' in output and '<array key="tag">' in output,
          'stepped: a field of two values written as an array')
    sample = await stepping.step_sample(ctx, pipeline['uuid'], events)
    check(sample['verdict'] == 'clean', f"stepped clean through the JSON schema check: {sample['verdict']}")
    final = await agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], plan=plan,
                         events_stream_ids=events, example_template=example)
    notes = final.get('from_example') or []
    check(final.get('agreed') and final['self_check']['compatible'], 'the template agreed, fitting the documents')
    check(any('acme.secret' in n and 'not searchable' in n for n in final['self_check']['notes']),
          f"the self-check says the secret isn't searchable: {final['self_check']['notes']}")
    async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
        path, body = _request(final['dev_tools'])
        response = await es.put(f'/{path}', json=body)
        check(response.status_code == 200, f'PUT {path}: {response.status_code} {response.text[:200]}')
        started = await agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                               stream_ids=events, source_pipeline_uuid=src['pipeline']['uuid'])
        done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], events, expect_events=False,
                                                           filter_id=started['filter_id'])
        check(done.get('gate') == 'pass', f"indexed with no Error stream: {json.dumps(done)[:300]}")
        await es.post(f'/{index}/_refresh')
        hit = (await es.post(f'/{index}/_search', json={'query': {'term': {'user.name': 'alice'}}})).json()
        stored = hit['hits']['hits'][0]['_source']['acme'] if hit['hits']['hits'] else {}
        print(f"    stored: {json.dumps(stored)}")
        check(stored.get('ports') == [80, 443] and stored.get('tag') == ['vpn', 'admin']
              and stored.get('peer') == ['10.1.0.1', '10.1.0.2'] and stored.get('port') == 22,
              'stored as arrays of their type; a single value as itself')
        hidden = (await es.post(f'/{index}/_search', json={'query': {'term': {'acme.secret': 's2'}}}))
        print(f"    Elasticsearch on acme.secret: {hidden.status_code} {hidden.text[:160]}")
    ref = {'type': 'ElasticIndex', 'uuid': None}
    docs = (await stroom.find_documents(index, ['ElasticIndex'], 5)).get('values') or []
    index_doc = await agreed(indexing.create_index_doc, ctx=ctx, build=src['build'], backend='elasticsearch',
                             name=index, index_name=index, time_field='@timestamp', cluster_uuid=cluster['uuid']) \
        if not docs else docs[0]['docRef']
    ref = {'type': 'ElasticIndex', 'uuid': index_doc['uuid'], 'name': index}
    listed = (await stroom.post('/dataSource/v1/findFields', {'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 300}}))['values']
    print(f"    Stroom lists acme.secret as: {stored_value(listed, 'acme.secret')}")
    name = lambda n: f'acme.{n}'
    message = next((f.name for f in plan.fields if f.source == 'EventDetail/Description'), None)
    columns = ['@timestamp', 'user.name', *(f'acme.{n}' for n, _, _ in EXTRA if n != 'secret')]
    common = dict(build=src['build'], index_uuid=index_doc['uuid'], backend='elasticsearch', stream_ids=events,
                  expected_documents=3, fields=columns, pipeline_uuid=pipeline['uuid'])
    asked = checks(name, message, '@timestamp',
                   lambda field: indexing.SearchCheck(field=field, value='*accepted*', expected=2))
    asked['searches'].append(indexing.SearchCheck(field='acme.tag', condition='IS_NOT_NULL', expected=2))
    result = await agreed(indexing.verify_index, ctx=ctx, **common, **asked)
    said(result)
    check(result['passed'], 'every type verified, multi-valued fields by one of their values, each hit traced')
    check(any(c.get('searched_as') for c in result['checks']), 'IS_NOT_NULL searched as EQUALS * and said so')
    refused = await refused_on(ctx, 'acme.secret', **common)
    check('no search through Stroom finds it' in refused, f"a check on the field not indexed is refused, saying why: {refused[:240]}")

    print('\n### a new index following this one in Stroom (like_index)')
    # Asked for by the user: Stroom reads the index template (and its components) the followed doc's indices take,
    # where it has the templates endpoint (dev/stroom-requests/elastic-index-templates.md); else the doc's fields.
    asked = await indexing.draft_index_mapping(ctx, 'elasticsearch', f'e2e-types-{stamp}-v2', events_stream_ids=events,
                                               like_index=index_doc['uuid'], extra_fields=extra)
    read = (asked.get('details') or {}).get('read through Stroom', '')
    print(f"    read through Stroom: {read}")
    if read.startswith('its index template'):
        check(read.startswith(f'its index template {index}'), f"the index template this index takes, read by Stroom: {read}")
        drafted = await agreed(indexing.draft_index_mapping, ctx=ctx, backend='elasticsearch',
                               index_name=f'e2e-types-{stamp}-v2', events_stream_ids=events,
                               like_index=index_doc['uuid'], extra_fields=extra)
        check(any('index template' in n and 'read through Stroom' in n for n in drafted.get('from_example') or []),
              'drafted from it, as if the user had pasted it')
    else:
        check(asked.get('status') == 'needs_confirmation' and 'fields and their types' in read,
              f"this Stroom can't read index templates: the doc's fields and types instead, said so: {read}")


async def main():
    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], conventions_dir=ROOT / 'conventions')
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = e2e.run_stamp()
    try:
        await fixtures(stroom)
        src = await e2e.onboard(ctx, 'json', CASE, stamp)
        events = (await processing_writes.wait_for_processing(ctx, src['pipeline']['uuid'], [src['raw']]))['streams'][0]['events']
        await lucene(ctx, src, events, stamp)
        await elasticsearch(ctx, src, events, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


if __name__ == '__main__':
    asyncio.run(main())
