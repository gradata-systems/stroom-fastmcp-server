"""Elasticsearch template hand-over against the local Stroom stack (see dev/stroom), without Elasticsearch,
or with it (--live).

    uv run python dev/e2e_elastic_handover.py
    uv run python dev/e2e_elastic_handover.py --live    # needs: docker compose --profile elastic up -d

The local stack has no Elasticsearch indexing template, so this adds a fixture one (the local 'Indexing'
template with an ElasticIndexingFilter in place of the Lucene one) and an Elastic Cluster doc pointing
nowhere. The documents come from stepping the indexing XSLT, so no Elasticsearch server is needed.

1. Stage 1 on the CSV sample (as in the translation suite).
2. An ES indexing pipeline from the fixture template; the agent's template proposal, self-checked against
   the documents the pipeline writes.
3. A user's changed template (a renamed field, a stricter type, dynamic strict) is checked: not compatible,
   with the pipeline changes it needs.
4. Indexing is refused until an index template is agreed. A standalone example (no component templates) is
   built from first; then the user's example composed of a component template: the template for the new index
   follows its conventions, and the user confirms it. Then they correct it (a priority), and the correction is confirmed and kept instead.
5. Once the user says the admin has committed it, the approval starts the indexing filter (here disabled again
   afterwards, as there is no Elasticsearch).

--live then runs the whole workflow against Elasticsearch 9 (the local stack's elastic profile): the user's
example is a sibling index's template in Stroom-style PascalCase with explicit objects (User.Id, TypeId) and a
component template, holding fields this source lacks and lacking some it has. The plan is named from it, the
built template agreed, then applied as the cluster admin would (component templates first); Elasticsearch's own
composition (_simulate_index) is compared with ours; indexing starts; the index holds every event, and its
mapping is the template's, with nothing added dynamically; and Stroom searches it. Then a standalone example
(no component templates): the template built from it is resolved by Elasticsearch (_index_template/_simulate)
exactly as built.

Then structure: Events whose user has an Id, a Name and an EmailAddress, indexed with an example that maps
user: {id, name, emailAddress}. The plan takes those names (the id is user.id, not the convention's user.name)
and the document Elasticsearch stores keeps the structure: "user": {"id", "name", "emailAddress"}, not flat
"user.id" keys.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dev'))

import e2e_translation as e2e  # noqa: E402
from config import Settings  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from utils.mappingstore import read_agreed_template, read_mapping  # noqa: E402
from tools import builds, indexing, processing_writes, stepping, templates, translation  # noqa: E402
from tools.plan import build_status  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.fieldplan import FieldPlan, PlannedField  # noqa: E402
from utils.templatecheck import compose  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402
from searching import paired  # noqa: E402

FIXTURE_TEMPLATE = 'E2E Events to Elasticsearch'
FIXTURE_CLUSTER = 'E2E_LOCAL_ES'
LIVE_CLUSTER = 'E2E_LIVE_ES'
ES = 'http://127.0.0.1:19200'          # the elastic profile's node, from here; Stroom reaches it as elasticsearch:9200

# A sibling source's index template, as the user would paste it from Dev Tools: Stroom-style names with explicit
# objects, a field this source lacks (Keyfob.Serial, User.Name) and none for some it has (Description).
LIVE_EXAMPLE = """PUT _index_template/stroom-keyfob-v1
{"index_patterns": ["stroom-keyfob-v1*"], "priority": 300, "composed_of": ["e2e-stroom-base"],
 "template": {"settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
  "aliases": {"keyfob": {}},
  "mappings": {"dynamic": "strict", "properties": {
   "TypeId": {"type": "keyword", "ignore_above": 512},
   "User": {"type": "object", "properties": {"Id": {"type": "keyword", "ignore_above": 512},
                                             "Name": {"type": "keyword", "ignore_above": 512}}},
   "Device": {"type": "object", "properties": {"HostName": {"type": "keyword", "ignore_above": 512},
                                               "IPAddress": {"type": "ip"}}},
   "Keyfob": {"type": "object", "properties": {"Serial": {"type": "keyword", "ignore_above": 512}}}}}}}"""
# The same conventions in a standalone template: no component templates, so it maps every field itself.
LIVE_STANDALONE = """PUT _index_template/stroom-badge-v1
{"index_patterns": ["stroom-badge-v1*"], "priority": 300,
 "template": {"settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
  "mappings": {"dynamic": "strict", "properties": {
   "StreamId": {"type": "long"}, "EventId": {"type": "long"}, "@timestamp": {"type": "date"},
   "TypeId": {"type": "keyword", "ignore_above": 256},
   "User": {"type": "object", "properties": {"Id": {"type": "keyword", "ignore_above": 256}}},
   "Badge": {"type": "object", "properties": {"Number": {"type": "keyword", "ignore_above": 256}}}}}}}"""
LIVE_COMPONENT = """PUT _component_template/e2e-stroom-base
{"template": {"mappings": {"properties": {"StreamId": {"type": "long"}, "EventId": {"type": "long"},
                                          "@timestamp": {"type": "date"}}}}}"""


async def with_json_schema_filter(stroom: StroomGateway, ref: dict) -> dict:
    """An Elasticsearch fixture template validating its XSLT's output as the live one does: a schema filter of group
    JSON between the XSLT and the indexing filter. The group holds two schemas (json.xsd, xpath-functions.xsd), so
    output that does not say which it follows (xsi:schemaLocation) fails every record. Added once, kept after."""
    doc = await stroom.get(f"/pipeline/v1/{ref['uuid']}")
    data = doc.get('pipelineData') or {}
    elements = (data.get('elements') or {}).setdefault('add', [])
    if not elements or any(e['id'] == 'schemaFilter' for e in elements):
        return ref
    elements.append({'id': 'schemaFilter', 'type': 'SchemaFilter'})
    data.setdefault('properties', {}).setdefault('add', []).append(
        {'element': 'schemaFilter', 'name': 'schemaGroup', 'value': {'string': 'JSON'}})
    links = data.setdefault('links', {}).setdefault('add', [])
    for link in links:
        if link['from'] == 'xsltFilter' and link['to'] == 'elasticIndexingFilter':
            link['to'] = 'schemaFilter'
    links.append({'from': 'schemaFilter', 'to': 'elasticIndexingFilter'})
    await stroom.request('PUT', f"/pipeline/v1/{doc['uuid']}", doc)
    return ref


async def fixtures(stroom: StroomGateway) -> tuple[dict, dict]:
    """The fixture ES indexing template (beside 'Indexing') and a cluster doc, created once."""
    found = await stroom.find_all_documents('Indexing', ['Pipeline'])
    lucene = next(v for v in found if v['docRef']['name'] == 'Indexing')
    existing = {v['docRef']['name']: v['docRef'] for v in
                (await stroom.find_documents('E2E*', ['Pipeline', 'ElasticCluster'], 20))['values']}
    parent = await stroom.post('/explorer/v2/find', {
        'filter': {'includedTypes': ['Folder'], 'nameFilter': 'Template Pipelines', 'requiredPermissions': ['VIEW']},
        'pageRequest': {'offset': 0, 'length': 5}})
    destination = parent['values'][0]['docRef'] if parent.get('values') else None
    if FIXTURE_TEMPLATE not in existing:
        node = await stroom.post('/explorer/v2/create', {'docType': 'Pipeline', 'docName': FIXTURE_TEMPLATE,
                                                         'destinationFolder': destination, 'permissionInheritance': 'DESTINATION'})
        doc = await stroom.get(f"/pipeline/v1/{node['docRef']['uuid'] if 'docRef' in node else node['uuid']}")
        source = (await stroom.get(f"/pipeline/v1/{lucene['docRef']['uuid']}"))['pipelineData']
        data = json.loads(json.dumps(source).replace('"indexingFilter"', '"elasticIndexingFilter"')
                          .replace('"IndexingFilter"', '"ElasticIndexingFilter"'))
        doc['pipelineData'] = data
        doc['description'] = 'Fixture for dev/e2e_elastic_handover.py'
        await stroom.request('PUT', f"/pipeline/v1/{doc['uuid']}", doc)
        existing[FIXTURE_TEMPLATE] = {'type': 'Pipeline', 'uuid': doc['uuid'], 'name': FIXTURE_TEMPLATE}
    if FIXTURE_CLUSTER not in existing:
        node = await stroom.post('/explorer/v2/create', {'docType': 'ElasticCluster', 'docName': FIXTURE_CLUSTER,
                                                         'destinationFolder': destination, 'permissionInheritance': 'DESTINATION'})
        ref = node.get('docRef', node)
        existing[FIXTURE_CLUSTER] = {'type': 'ElasticCluster', 'uuid': ref['uuid'], 'name': FIXTURE_CLUSTER}
    return await with_json_schema_filter(stroom, existing[FIXTURE_TEMPLATE]), existing[FIXTURE_CLUSTER]


def change_template(body: dict) -> dict:
    """What a user might send back: user.name renamed to user.id, host.name made an ip, dynamic strict."""
    changed = json.loads(json.dumps(body))
    props = changed['template']['mappings']['properties']
    props['user']['properties']['id'] = props['user']['properties'].pop('name')
    props['host']['properties']['name'] = {'type': 'ip'}
    changed['template']['mappings']['dynamic'] = 'strict'
    return changed


async def main():
    local = e2e.env(ROOT / 'dev' / 'stroom' / '.env')
    settings = Settings(_env_file=None, stroom_url='http://127.0.0.1:18080', dev_no_auth=True,
                        stroom_api_key=local['STROOM_ADMIN_API_KEY'], event_logging_version=e2e.VERSION)
    stroom = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': stroom, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'), 'consent': ConsentStore(use_elicitation=False)})
    stamp = e2e.run_stamp()
    try:
        template_ref, cluster = await fixtures(stroom)
        csv = await e2e.onboard(ctx, 'csv', e2e.CASES['csv'], stamp)
        events = (await processing_writes.wait_for_processing(ctx, csv['pipeline']['uuid'], [csv['raw']]))['streams'][0]['events']

        print('\n### Elasticsearch indexing pipeline')
        candidates = (await templates.find_pipeline_templates(ctx, 'indexing'))['candidates']
        es_template = next(c for c in candidates if c['name'] == FIXTURE_TEMPLATE)
        e2e.check(es_template['backend'] == 'elasticsearch', 'fixture template reads as an Elasticsearch template')
        index = f'e2e-acme-{stamp}-v1'
        offered = await indexing.draft_index_mapping(ctx, 'elasticsearch', index, 'ecs', events)
        e2e.check(offered.get('drafted') is False and offered['options'][0]['option'].startswith('example index template'),
                  'a convention alone is not drafted: the user is offered their example first')
        # This user has no example: they say so in the confirmation.
        draft = await e2e.agreed(indexing.draft_index_mapping, ctx=ctx, backend='elasticsearch', index_name=index,
                                 convention='ecs', events_stream_ids=events, without_example=True)
        plan = FieldPlan.model_validate(draft['plan'])
        # As the agent does: the indexing XSLT saved from its plan, which is kept with it for the documentation.
        xslt = await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=plan)
        pipeline = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=csv['build'], name=f'{index} - Indexing',
                                   template_uuid=es_template['uuid'],
                                   xslt_uuid=xslt['uuid'], index_name=index,
                                   cluster_uuid=cluster['uuid'])
        sample = await stepping.step_sample(ctx, pipeline['uuid'], events)
        print(f"    stepping: {sample['verdict']}; groups: {[(g['class'], g['element'], g['count']) for g in sample['groups']]}")
        e2e.check(sample['verdict'] == 'clean', "stepped clean through the template's JSON schema filter (as live)")
        # The live template's schema filter (group JSON) needs the output to say which schema it follows.
        saved_xslt = (await stroom.get_doc('XSLT', xslt['uuid'])).get('data') or ''
        undeclared = saved_xslt.replace(
            ' xsi:schemaLocation="http://www.w3.org/2005/xpath-functions file://xpath-functions.xsd"', '')
        bare = await stepping.step_sample(ctx, pipeline['uuid'], events, draft_code={'xsltFilter': undeclared})
        reasons = ' '.join(g['reason'] for g in bare['groups'])
        e2e.check(undeclared != saved_xslt and bare['verdict'] == 'blocking' and 'xsi:schemaLocation' in reasons
                  and 'Do not change or remove the schema filter' in reasons,
                  "without its schema declaration, every record fails as on live, and triage says how to fix the output")

        print('\n### index field documentation, with the values from the sample')
        doc = await builds.write_documentation(ctx, csv['build'], pipeline['uuid'],
                                               '## Purpose and data\n\nCSV events indexed into Elasticsearch.\n',
                                               'Created', stream_ids=events)
        section = doc.get('field_mapping') or ''
        print('    ' + '\n    '.join(section.splitlines()[:8]))
        e2e.check('| Index field | Description | Type | From (event-logging path) | In sample | Sample values |' in section,
                 'the index fields, each with its event-logging path, how often it is populated and its sampled values')
        e2e.check('Sample values are what the 3 documents written from the sample got' in section
                 and '`alice`' in section and '`bob`' in section, 'sampled values from the documents the pipeline writes')
        user_field = next(f.name for f in plan.fields if f.source == 'EventSource/User/Id')
        await e2e.documented_to_the_field(stroom, doc, [f.name for f in plan.fields], {user_field: 'alice'})

        print('\n### the proposed template')
        unasked = await indexing.propose_index_template(ctx, pipeline['uuid'], plan, events)
        e2e.check(unasked.get('needs') == 'example_template' and 'dev_tools' not in unasked,
                  'without an example nothing is built to commit: the user is asked for theirs')
        # This user has none: built from the plan, shown for them to confirm (not confirmed yet).
        proposal = await indexing.propose_index_template(ctx, pipeline['uuid'], plan, events, without_example=True)
        e2e.check(proposal.get('status') == 'needs_review', 'built from the plan alone, shown to the user to review first')
        dev_tools = proposal['dev_tools']
        proposal['template'] = json.loads(dev_tools.split(chr(10), 1)[1])
        print('    ' + dev_tools.splitlines()[0])
        e2e.check(proposal['template']['index_patterns'] == [f'{index}*'], "template covers the pipeline's index")
        e2e.check(proposal['self_check']['compatible'] and proposal['self_check']['documents_checked'] == 3,
                 f"proposal fits the {proposal['self_check']['documents_checked']} documents the pipeline writes: "
                 f"{proposal['self_check']['blocking']}")

        print("\n### the user's changed template")
        changed = change_template(proposal['template'])
        check = await indexing.check_index_template(ctx, pipeline['uuid'],
                                                    f"PUT _index_template/{proposal['template_name']}\n{json.dumps(changed)}", events)
        for change in check['pipeline_changes']:
            print(f"    change: {change['field']}: {change['change']}")
        fields = {c['field'] for c in check['pipeline_changes']}
        e2e.check(not check['compatible'] and {'user.name', 'host.name'} <= fields,
                 f"not compatible, with the rename and the type change flagged: {check['blocking']}")
        same = await indexing.check_index_template(ctx, pipeline['uuid'], json.dumps(proposal['template']), events)
        e2e.check(same['compatible'], 'the unchanged proposal checks as compatible')

        print('\n### no template agreed yet: indexing is refused')
        try:
            await processing_writes.create_processor_filter(ctx, pipeline['uuid'], stream_ids=events,
                                                            source_pipeline_uuid=csv['pipeline']['uuid'])
            refused = ''
        except ToolError as e:
            refused = str(e)
        e2e.check(refused.startswith('No Elasticsearch index template has been agreed'), f"refused: {refused[:90]}")
        e2e.check(f'propose_index_template pipeline_uuid={pipeline["uuid"]}' in refused and 'example_template=' in refused,
                  'the refusal names the call that agrees it, with the example the user gave')

        print('\n### stepped clean is not indexed: the plan waits for the template (seen in VS Code)')
        status = await build_status(ctx, csv["build"])
        state = {s['step']: s['state'] for s in status['steps']}
        e2e.check(state['index_template'] == 'to do' and state['indexed'] == 'to do'
                  and status['next']['step'] not in ('index_documented', 'promoted'),
                  f"template and indexing still to do, promotion not next: {status['next']['step']}")
        e2e.check(any('no Elasticsearch index template agreed' in c for c in status['before_promotion'])
                  and any('has not been indexed and verified' in c for c in status['before_promotion']),
                  "promotion's approval would warn of both")
        started_wait = time.monotonic()
        waited = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], events, timeout_seconds=60,
                                                             expect_events=False)
        e2e.check(time.monotonic() - started_wait < 30 and 'no processor filter' in waited['problems'][0],
                  'no processor filter: nothing to wait for, said at once')

        print('\n### a standalone example: no component templates')
        standalone = ('PUT _index_template/ecs-standalone-v1\n' + json.dumps({
            'index_patterns': ['ecs-standalone-v1*'], 'priority': 120,
            'template': {'mappings': {'dynamic': 'strict', 'properties': {
                'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}, '@timestamp': {'type': 'date'},
                'user': {'properties': {'name': {'type': 'keyword', 'ignore_above': 128}}}}}}}))
        alone = await indexing.propose_index_template(ctx, pipeline['uuid'], plan, events, example_template=standalone)
        built = json.loads(alone['dev_tools'].split('\n', 1)[1])
        props = built['template']['mappings']['properties']
        notes = alone.get('from_example') or []
        e2e.check(alone.get('status') == 'needs_review' and 'composed_of' not in built and built['priority'] == 120
                 and not alone.get('component_templates')
                 and not any('composed_of' in n and 'ecs@mappings' not in n for n in notes),
                 'built with no component templates, none asked for')
        # The plan follows ECS: Elastic's recommended base is suggested, the user's example still followed as it is.
        e2e.check(any("doesn't compose ecs@mappings" in n for n in notes),
                  'the example not composing ecs@mappings is said, not changed')
        e2e.check(props['StreamId'] == {'type': 'long'} and props['@timestamp'] == {'type': 'date'}
                 and props['user']['properties']['name'] == {'type': 'keyword', 'ignore_above': 128}
                 and props['host']['properties']['name'] == {'type': 'keyword', 'ignore_above': 128},
                 "every field in the template itself, in the example's types and keyword style")

        print("\n### the template from the user's example, confirmed by the user")
        example = ('PUT _index_template/ecs-sibling-v1\n' + json.dumps({
            'index_patterns': ['ecs-sibling-v1*'], 'priority': 300, 'composed_of': ['ecs-base'],
            'template': {'settings': {'index': {'number_of_shards': 2}}, 'aliases': {'sibling': {}},
                         'mappings': {'dynamic': 'strict', 'properties': {'user': {'properties': {
                             'name': {'type': 'keyword', 'ignore_above': 256}}}}}}}))
        base = 'PUT _component_template/ecs-base\n' + json.dumps({'template': {'mappings': {'properties': {
            '@timestamp': {'type': 'date'}}}}})
        asked = await indexing.propose_index_template(ctx, pipeline['uuid'], plan, events, example_template=example,
                                                      component_templates=[base])
        e2e.check(asked.get('status') == 'needs_review' and asked['dev_tools'].startswith(f'PUT _index_template/{index}\n'),
                 "the template is shown to the user first, in full, to review before they confirm it")
        final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], plan=plan,
                                events_stream_ids=events, example_template=example, component_templates=[base])
        body = final['template']
        print('    ' + '; '.join(final['from_example']))
        e2e.check(body['index_patterns'] == [f'{index}*'] and body['composed_of'] == ['ecs-base']
                 and body['template']['settings']['index']['number_of_shards'] == 2 and 'aliases' not in body['template'],
                 "the new index's pattern, with the example's components and settings, without its aliases")
        e2e.check(body['template']['mappings']['dynamic'] == 'strict' and '@timestamp' not in body['template']['mappings']['properties'],
                 "the example's mapping parameters, and fields its components map left to them")
        e2e.check(final['self_check']['compatible'], f"the final template fits the documents: {final['self_check']['blocking']}")
        kept = read_agreed_template((await stroom.get_doc('Pipeline', pipeline['uuid'])).get('description'))
        e2e.check(final.get('agreed') and kept and kept['dev_tools'] == final['dev_tools'], 'agreed, and kept with the pipeline')

        print("\n### the user's correction, confirmed instead")
        corrected = f"PUT _index_template/{index}\n{json.dumps({**body, 'priority': 400})}"
        fixed = await e2e.agreed(indexing.check_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], template=corrected,
                                events_stream_ids=events, component_templates=[base])
        kept = read_agreed_template((await stroom.get_doc('Pipeline', pipeline['uuid'])).get('description'))
        e2e.check(fixed.get('agreed') and '"priority": 400' in kept['dev_tools'], 'the correction is the agreed template now')
        state = {s['step']: s['state'] for s in (await build_status(ctx, csv["build"]))['steps']}
        e2e.check(state['index_template'] == 'done' and state['indexed'] == 'to do', 'agreed; indexing still to do')

        print('\n### the admin has committed it: indexing starts')
        first = await processing_writes.create_processor_filter(ctx, pipeline['uuid'], stream_ids=events,
                                                                source_pipeline_uuid=csv['pipeline']['uuid'])
        e2e.check(first.get('status') == 'needs_approval' and first['summary'].startswith(
                 f"The agreed index template '{index}' for Elasticsearch index '{index}' is committed to cluster "
                 f"{cluster['name']}: start indexing"), f"the approval asks whether it is committed: {first.get('summary')}")
        started = await processing_writes.create_processor_filter(ctx, pipeline['uuid'], stream_ids=events,
                                                                  source_pipeline_uuid=csv['pipeline']['uuid'],
                                                                  approval_id=first['approval_id'])
        stored = await stroom.get(f"/processorFilter/v1/{started['filter_id']}")
        e2e.check(stored.get('enabled') is True, f"filter {started['filter_id']} created enabled")
        # No Elasticsearch here: stop it again, so it doesn't fail in the background.
        await e2e.agreed(processing_writes.set_processor_filter_enabled, ctx=ctx, filter_id=started['filter_id'], enabled=False)

        print("\n### an ECS field added by hand in Stroom's editor is kept through the agent's next change")
        # Asked by the user: the indexing XSLT edited by hand to write event.created too, then the plan regenerated
        # at the agent's next change, and the edit was gone.
        doc = await stroom.get_doc('XSLT', xslt['uuid'])
        created = '<string key="created"><xsl:value-of select="EventTime/TimeCreated" /></string>'
        code = doc['data']
        code = (code.replace('<map key="event">', '<map key="event">' + created, 1) if '<map key="event">' in code else
                code.replace('<xsl:template match="Event">\n    <map>', '<xsl:template match="Event">\n    <map>'
                             '<map key="event">' + created + '</map>', 1))
        e2e.check(created in code, 'the edit made')
        await stroom.put_doc({**doc, 'data': code})
        status = await build_status(ctx, csv['build'])
        e2e.check(any('edited by hand since the server saved it' in c and 'carry it into the index plan' in c
                      for c in status['before_promotion']), 'build_status says the XSLT was edited by hand')
        try:
            await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=plan, uuid=xslt['uuid'],
                                        change='Saved again from its plan')
            refused = ''
        except ToolError as e:
            refused = str(e)
        print(f'    {refused[:220]}')
        e2e.check(refused.startswith('Not saved:') and 'no longer writes string created' in refused,
                  'regenerating from the plan without the edit is refused, saying what it would undo')
        e2e.check(created in (await stroom.get_doc('XSLT', xslt['uuid']))['data'], 'the edit is still in Stroom')
        carried = plan.model_copy(update={'fields': [*plan.fields, PlannedField(name='event.created', type='date',
                                                                               source='EventTime/TimeCreated')]})
        await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=carried, uuid=xslt['uuid'],
                                    change='event.created, added by hand, carried into the plan')
        after = await stroom.get_doc('XSLT', xslt['uuid'])
        e2e.check('key="created"' in after['data'] and read_mapping(after['description'])[1]['fields'][-1]['name']
                  == 'event.created', 'carried into the plan, saved: the field written, and kept in the plan')
        stepped = await stepping.step_sample(ctx, pipeline['uuid'], events)
        e2e.check(stepped['verdict'] == 'clean', f"stepped clean with the carried field: {stepped['verdict']}")
        status = await build_status(ctx, csv['build'])
        e2e.check(not any('edited by hand since the server saved it' in c for c in status['before_promotion']),
                  'and no longer reported as edited by hand')

        print("\n### a field edited by hand that the agent's change changes too: the user decides")
        doc = await stroom.get_doc('XSLT', xslt['uuid'])
        mine = doc['data'].replace(created, created.replace('EventTime/TimeCreated', 'current-dateTime()'), 1)
        e2e.check(mine != doc['data'], "the field's source changed by hand: when the event was indexed")
        await stroom.put_doc({**doc, 'data': mine})
        proposed = carried.model_copy(update={'fields': [
            f.model_copy(update={'source': 'EventDetail/*/Outcome/Success'}) if f.name == 'event.created' else f
            for f in carried.fields]})
        asked = await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=proposed, uuid=xslt['uuid'])
        fields = [q['field'] for q in asked.get('hand_edit_collisions') or []]
        print(f"    {(asked.get('hand_edit_collisions') or [{}])[0].get('question', asked)}")
        e2e.check(asked.get('status') == 'needs_guidance' and fields == ['event.created']
                  and 'Overwrite the XSLT with the change' in json.dumps(asked.get('hand_edit')),
                  f"asked first whether to overwrite the XSLT or decide field by field, then per field: {fields}")
        # The user overwrites in one step.
        await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=proposed, uuid=xslt['uuid'],
                                    hand_edit_choices={'*': 'overwrite'}, change='event.created from the outcome')
        after = (await stroom.get_doc('XSLT', xslt['uuid']))['data']
        e2e.check('current-dateTime()' not in after and 'Outcome/Success' in after,
                  'the user chose to overwrite: saved over their edit')

        print("\n### a key renamed by hand is its field still: field by field, the user keeps their edit")
        doc = await stroom.get_doc('XSLT', xslt['uuid'])
        await stroom.put_doc({**doc, 'data': doc['data'].replace('<string key="created">', '<string key="created_at">', 1)})
        back = proposed.model_copy(update={'fields': [
            f.model_copy(update={'source': 'EventTime/TimeCreated'}) if f.name == 'event.created' else f
            for f in proposed.fields]})
        asked = await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=back, uuid=xslt['uuid'])
        fields = [q['field'] for q in asked.get('hand_edit_collisions') or []]
        e2e.check(asked.get('status') == 'needs_guidance' and fields == ['event.created'],
                  f"the renamed key (created_at) is linked to event.created, and asked about: {fields}")
        try:
            await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=back, uuid=xslt['uuid'],
                                        hand_edit_choices={'event.created': 'keep'})
            refused = ''
        except ToolError as e:
            refused = str(e)
        e2e.check('The user keeps their edit of event.created' in refused and 'string created_at' in refused,
                  'kept: the change to it left out, the rename to carry into the plan')
        renamed = proposed.model_copy(update={'fields': [
            f.model_copy(update={'name': 'event.created_at'}) if f.name == 'event.created' else f
            for f in proposed.fields]})
        await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=renamed, uuid=xslt['uuid'],
                                    change='event.created renamed event.created_at, as the user did by hand')
        after = (await stroom.get_doc('XSLT', xslt['uuid']))['data']
        e2e.check('key="created_at"' in after and 'key="created"' not in after,
                  'the rename carried into the plan: saved, written as event.created_at')
        if '--live' in sys.argv:
            await live(ctx, stroom, csv, events, es_template, stamp)
            await live_structure(ctx, stroom, es_template, stamp)
        print('\nALL PASSED')
    finally:
        await stroom.close()


def _request(text: str) -> tuple[str, dict]:
    """A Dev Tools request: ('_index_template/name', body)."""
    line, body = text.split('\n', 1)
    return line.split(' ', 1)[1].strip(), json.loads(body)


def _properties(node: dict) -> dict:
    """A mapping's properties with object nodes reduced to their properties, for comparing what Elasticsearch
    holds (it omits "type": "object" once applied) with what was built."""
    out = {}
    for name, spec in (node or {}).items():
        if 'properties' in spec:
            out[name] = {k: v for k, v in spec.items() if k not in ('type', 'properties')}
            out[name]['properties'] = _properties(spec['properties'])
        else:
            out[name] = spec
    return out


async def live_cluster(stroom: StroomGateway) -> dict:
    found = (await stroom.find_documents(LIVE_CLUSTER, ['ElasticCluster'], 5)).get('values') or []
    if found:
        ref = found[0]['docRef']
    else:
        parent = await stroom.post('/explorer/v2/find', {
            'filter': {'includedTypes': ['Folder'], 'nameFilter': 'Template Pipelines', 'requiredPermissions': ['VIEW']},
            'pageRequest': {'offset': 0, 'length': 5}})
        node = await stroom.post('/explorer/v2/create', {
            'docType': 'ElasticCluster', 'docName': LIVE_CLUSTER, 'permissionInheritance': 'DESTINATION',
            'destinationFolder': parent['values'][0]['docRef'] if parent.get('values') else None})
        ref = node.get('docRef', node)
    doc = await stroom.get_doc('ElasticCluster', ref['uuid'])
    doc['connection'] = {**(doc.get('connection') or {}), 'connectionUrls': ['http://elasticsearch:9200'],
                         'useAuthentication': False}
    await stroom.request('PUT', f"/elasticCluster/v1/{ref['uuid']}", doc)
    return {'type': 'ElasticCluster', 'uuid': ref['uuid'], 'name': LIVE_CLUSTER}


async def live(ctx, stroom: StroomGateway, csv: dict, events: list[int], es_template: dict, stamp: str) -> None:
    import httpx
    async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
        version = (await es.get('/')).json()['version']['number']
        print(f'\n### live: Elasticsearch {version}')
        e2e.check(version.startswith('9.'), f'Elasticsearch 9 at {ES}')
        cluster = await live_cluster(stroom)
        index = f'stroom-door-{stamp}-v1'

        print("\n### the plan, named from the user's example")
        draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', index, 'ecs', events,
                                                   example_template=LIVE_EXAMPLE, component_templates=[LIVE_COMPONENT])
        plan = FieldPlan.model_validate(draft['plan'])
        names = {f.source: f.name for f in plan.fields}
        for note in draft['from_example']:
            print(f'    {note}')
        e2e.check(names.get('EventSource/User/Id') == 'User.Id' and names.get('EventDetail/TypeId') == 'TypeId'
                 and names.get('EventSource/Device/HostName') == 'Device.HostName',
                 f"the example's names: User.Id for the user, TypeId for the event type: {sorted(names.values())}")
        e2e.check(all(n in ('StreamId', 'EventId', '@timestamp') or n[:1].isupper() for n in names.values()),
                 "every other field named in the example's PascalCase")
        xslt = await translation.save_xslt(ctx, csv['build'], f'{index}-XSLT', index_plan=plan)
        pipeline = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=csv['build'],
                                   name=f'{index} - Indexing', template_uuid=es_template['uuid'],
                                   xslt_uuid=xslt['uuid'], index_name=index, cluster_uuid=cluster['uuid'])
        sample = await stepping.step_sample(ctx, pipeline['uuid'], events)
        e2e.check(sample['verdict'] == 'clean', f"stepped clean: {sample['verdict']}")

        print('\n### the template, built and agreed')
        final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], plan=plan,
                                events_stream_ids=events, example_template=LIVE_EXAMPLE,
                                component_templates=[LIVE_COMPONENT])
        props = final['template']['template']['mappings']['properties']
        print('    ' + json.dumps(props))
        e2e.check(final.get('agreed') and props['User'].get('type') == 'object'
                 and props['User']['properties']['Id'] == {'type': 'keyword', 'ignore_above': 512}
                 and props['TypeId'] == {'type': 'keyword', 'ignore_above': 512},
                 "agreed: the example's objects and field types for User.Id and TypeId")
        e2e.check('Keyfob' not in props and 'StreamId' not in props, "the example's own fields and the component's not repeated")

        print('\n### the cluster admin applies it')
        for text in (LIVE_COMPONENT, final['dev_tools']):
            path, body = _request(text)
            response = await es.put(f'/{path}', json=body)
            e2e.check(response.status_code == 200, f'PUT {path}: {response.status_code} {response.text[:200]}')
        simulated = (await es.post(f'/_index_template/_simulate_index/{index}')).json()['template']['mappings']
        ours, _ = compose(final['template'], {'e2e-stroom-base': _request(LIVE_COMPONENT)[1]})
        e2e.check(_properties(simulated['properties']) == _properties(ours['template']['mappings']['properties'])
                 and simulated.get('dynamic') == 'strict',
                 "Elasticsearch composes the templates as check_index_template does")

        print('\n### indexing')
        started = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                                  stream_ids=events, source_pipeline_uuid=csv['pipeline']['uuid'])
        done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], events, expect_events=False,
                                                           filter_id=started['filter_id'])
        print(f"    {done.get('status') or done.get('state')}: {json.dumps(done)[:300]}")
        await es.post(f'/{index}/_refresh')
        count = (await es.get(f'/{index}/_count')).json().get('count')
        e2e.check(count == 3, f'the index holds the 3 events: {count}')
        actual = (await es.get(f'/{index}/_mapping')).json()[index]['mappings']
        e2e.check(_properties(actual['properties']) == _properties(simulated['properties']),
                 "the index's mapping is the template's: nothing added dynamically")
        hit = (await es.post(f'/{index}/_search', json={'query': {'term': {'User.Id': 'alice'}}})).json()
        e2e.check(hit['hits']['total']['value'] == 1 and hit['hits']['hits'][0]['_source']['TypeId'] == 'Logon',
                 'User.Id and TypeId searchable as indexed')

        print('\n### a standalone example: Elasticsearch resolves the built template as built')
        alone = await indexing.propose_index_template(ctx, pipeline['uuid'], plan, events,
                                                      example_template=LIVE_STANDALONE)
        built = json.loads(alone['dev_tools'].split('\n', 1)[1])
        e2e.check('composed_of' not in built and built['template']['mappings']['properties']['User']['properties']['Id']
                 == {'type': 'keyword', 'ignore_above': 256}
                 and built['template']['mappings']['properties']['Device']['type'] == 'object',
                 "built from it alone: its types, objects and keyword style")
        # The agreed template for this index is applied already, with the same pattern and priority, which
        # Elasticsearch refuses even to simulate: one higher.
        response = await es.post('/_index_template/_simulate', json={**built, 'priority': built['priority'] + 1})
        e2e.check(response.status_code == 200, f'simulated: {response.status_code} {response.text[:200]}')
        resolved = response.json()['template']['mappings']
        e2e.check(_properties(resolved['properties']) == _properties(built['template']['mappings']['properties'])
                 and resolved.get('dynamic') == 'strict', 'Elasticsearch resolves it with nothing added or lost')

        print('\n### searched through Stroom and in Elasticsearch, each hit traced to its event')
        doc = await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=csv['build'], backend='elasticsearch',
                              name=index, time_field=plan.time_field, index_name=index, cluster_uuid=cluster['uuid'])
        await paired(ctx, es, csv['build'], index, doc['uuid'], events, 3,
                     ['StreamId', 'EventId', '@timestamp', 'User.Id', 'TypeId'], [
                         ('User.Id', 'EQUALS', 'bob', 1, {'term': {'User.Id': 'bob'}}),
                         ('User.Id', 'EQUALS', 'Bob', 0, {'term': {'User.Id': 'Bob'}}),
                         ('User.Id', 'IN', 'alice,carol', 2, {'terms': {'User.Id': ['alice', 'carol']}}),
                         ('User.Id', 'EQUALS', 'a*', 1, {'wildcard': {'User.Id': 'a*'}}),
                         ('TypeId', 'EQUALS', 'Logon', 3, {'term': {'TypeId': 'Logon'}}),
                         ('Device.HostName', 'EQUALS', 'ws02', 1, {'term': {'Device.HostName': 'ws02'}}),
                         ('Device.IPAddress', 'EQUALS', '10.0.0.2', 1, {'term': {'Device.IPAddress': '10.0.0.2'}}),
                         ('Device.IPAddress', 'EQUALS', '10.0.0.0/24', 3, {'term': {'Device.IPAddress': '10.0.0.0/24'}}),
                         ('Description', 'EQUALS', 'logon', 3, {'match': {'Description': 'logon'}}),
                         ('@timestamp', 'BETWEEN', '2026-09-28T10:04:00.000Z,2026-09-28T10:08:00.000Z', 2,
                          {'range': {'@timestamp': {'gte': '2026-09-28T10:04:00.000Z', 'lte': '2026-09-28T10:08:00.000Z'}}}),
                     ], pipeline_uuid=pipeline['uuid'])
        ref = {'type': 'Pipeline', 'uuid': pipeline['uuid'], 'name': pipeline['name']}
        status = await build_status(ctx, csv['build'])
        # The build's first indexing pipeline (without Elasticsearch, above) was never indexed: still to do, for it.
        points_elsewhere = (status['next']['step'] != 'indexed'
                            or status['next']['call']['arguments'].get('pipeline_uuid') != pipeline['uuid'])
        e2e.check(await stepping.verified(ctx, ref) and points_elsewhere,
                  'this pipeline is indexed once verify_index passed; the plan points at what is not')

        print('\n### documented')
        written = await builds.write_documentation(ctx, csv['build'], pipeline['uuid'],
                                                   '## Purpose and data\n\nCSV logons indexed into Elasticsearch.\n',
                                                   'Created', stream_ids=events)
        section = written.get('field_mapping') or ''
        e2e.check(f"Elasticsearch index template `{index}`, agreed with the user" in section
                 and e2e.field_rows(section)['User.Id'][1:3] == ['keyword', '`EventSource/User/Id`']
                 and '`alice`' in section,
                 'the field mapping names the agreed template, with each field and its sampled values')
        await e2e.documented_to_the_field(stroom, written, [f.name for f in plan.fields],
                                          {'User.Id': 'alice', 'Device.HostName': 'ws01'})


PEOPLE_SAMPLE = ("time,user,name,email,host,ip,result\n"
                 "2026-09-28T10:00:00,john.smith1,John Smith,john.smith1@email.com,ws01,10.0.0.1,ok\n"
                 "2026-09-28T10:05:00,jane.doe2,Jane Doe,jane.doe2@email.com,ws02,10.0.0.2,fail\n")
PEOPLE_EXAMPLE = """PUT _index_template/people-sibling-v1
{"index_patterns": ["people-sibling-v1*"], "priority": 300,
 "template": {"settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
  "mappings": {"dynamic": "strict", "properties": {
   "StreamId": {"type": "long"}, "EventId": {"type": "long"}, "@timestamp": {"type": "date"},
   "user": {"properties": {"id": {"type": "keyword"}, "name": {"type": "keyword"}, "emailAddress": {"type": "keyword"}}},
   "host": {"properties": {"name": {"type": "keyword"}, "ip": {"type": "ip"}}},
   "event": {"properties": {"code": {"type": "keyword"}}}}}}}"""


def people_case() -> dict:
    """The CSV case, with the user's name and email address in the Events as well as their id."""
    case = dict(e2e.CASES['csv'], sample=PEOPLE_SAMPLE)
    # The EventSource user (the Authenticate element has one too, indented deeper).
    user = "\n        <User><Id><xsl:value-of select=\"data[@name='user']/@value\" /></Id></User>"
    assert case['xslt'].count(user) == 1
    case['xslt'] = case['xslt'].replace(user, (
        "\n        <User><Id><xsl:value-of select=\"data[@name='user']/@value\" /></Id>"
        "<Name><xsl:value-of select=\"data[@name='name']/@value\" /></Name>"
        "<EmailAddress><xsl:value-of select=\"data[@name='email']/@value\" /></EmailAddress></User>"))
    return case


async def live_structure(ctx, stroom: StroomGateway, es_template: dict, stamp: str) -> None:
    import httpx
    people = await e2e.onboard(ctx, 'people', people_case(), stamp)
    events = (await processing_writes.wait_for_processing(ctx, people['pipeline']['uuid'], [people['raw']]))['streams'][0]['events']
    async with httpx.AsyncClient(base_url=ES, timeout=30) as es:
        cluster = await live_cluster(stroom)
        index = f'people-{stamp}-v1'
        print("\n### structure kept: the user's example maps user: {id, name, emailAddress}")
        draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', index, 'ecs', events,
                                                   example_template=PEOPLE_EXAMPLE)
        plan = FieldPlan.model_validate(draft['plan'])
        names = {f.source: f.name for f in plan.fields}
        e2e.check(names.get('EventSource/User/Id') == 'user.id' and names.get('EventSource/User/Name') == 'user.name'
                 and names.get('EventSource/User/EmailAddress') == 'user.emailAddress',
                 f"the example's names: {sorted(names.values())}")
        e2e.check('<map key="user">' in plan.xslt() and 'key="user.id"' not in plan.xslt(),
                 'the indexing XSLT writes the user as an object')
        xslt = await translation.save_xslt(ctx, people['build'], f'{index}-XSLT', index_plan=plan)
        pipeline = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=people['build'],
                                   name=f'{index} - Indexing', template_uuid=es_template['uuid'],
                                   xslt_uuid=xslt['uuid'], index_name=index, cluster_uuid=cluster['uuid'])
        sample = await stepping.step_sample(ctx, pipeline['uuid'], events)
        e2e.check(sample['verdict'] == 'clean', f"stepped clean: {sample['verdict']}")
        final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=pipeline['uuid'], plan=plan,
                                events_stream_ids=events, example_template=PEOPLE_EXAMPLE)
        e2e.check(final.get('agreed') and final['self_check']['compatible'], 'agreed, and fits the documents')
        path, request = _request(final['dev_tools'])
        e2e.check((await es.put(f'/{path}', json=request)).status_code == 200, f'PUT {path}')
        started = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=pipeline['uuid'],
                                  stream_ids=events, source_pipeline_uuid=people['pipeline']['uuid'])
        done = await processing_writes.wait_for_processing(ctx, pipeline['uuid'], events, expect_events=False,
                                                           filter_id=started['filter_id'])
        e2e.check(done.get('gate') == 'pass', 'indexed with no Error stream')
        await es.post(f'/{index}/_refresh')
        hit = (await es.post(f'/{index}/_search', json={'query': {'term': {'user.id': 'john.smith1'}}})).json()
        source = hit['hits']['hits'][0]['_source'] if hit['hits']['hits'] else {}
        print('    ' + json.dumps(source))
        e2e.check(source.get('user') == {'id': 'john.smith1', 'name': 'John Smith', 'emailAddress': 'john.smith1@email.com'}
                 and not any('.' in k for k in source), 'the stored document keeps the structure: "user": {...}')
        doc = await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=people['build'], backend='elasticsearch',
                              name=index, time_field=plan.time_field, index_name=index, cluster_uuid=cluster['uuid'])
        await paired(ctx, es, people['build'], index, doc['uuid'], events, 2,
                     ['StreamId', 'EventId', '@timestamp', 'user.id', 'user.name'], [
                         ('user.id', 'EQUALS', 'john.smith1', 1, {'term': {'user.id': 'john.smith1'}}),
                         ('user.name', 'EQUALS', 'Jane Doe', 1, {'term': {'user.name': 'Jane Doe'}}),
                         ('user.emailAddress', 'EQUALS', '*@email.com', 2, {'wildcard': {'user.emailAddress': '*@email.com'}}),
                         ('host.ip', 'EQUALS', '10.0.0.2', 1, {'term': {'host.ip': '10.0.0.2'}}),
                     ], pipeline_uuid=pipeline['uuid'])
        print('\n### following an existing index in Stroom, when the user has no template to paste')
        like = await e2e.agreed(indexing.draft_index_mapping, ctx=ctx, backend='elasticsearch',
                                index_name=f'people-{stamp}-v2', events_stream_ids=events, like_index=doc['uuid'])
        names = {f['source']: f['name'] for f in like['plan']['fields']}
        e2e.check(names.get('EventSource/User/Id') == 'user.id' and names.get('EventSource/User/Name') == 'user.name'
                  and any(f"'{index}'" in n for n in like.get('from_example') or []),
                  f"field names follow the existing index, read through Stroom: {sorted(names.values())}")

        print('\n### documented down to the nested fields, with the sample values')
        written = await builds.write_documentation(ctx, people['build'], pipeline['uuid'],
                                                   '## Purpose and data\n\nPeople logons indexed into Elasticsearch.\n',
                                                   'Created', stream_ids=events)
        await e2e.documented_to_the_field(stroom, written, [f.name for f in plan.fields], {
            'user.id': 'john.smith1', 'user.name': 'John Smith', 'user.emailAddress': 'john.smith1@email.com',
            'host.ip': '10.0.0.1'})

        print('\n### subobjects: false: the same events, the index mapping each dotted name as a field of its own')
        flat_index = f'people-flat-{stamp}-v1'
        flat_example = PEOPLE_EXAMPLE.replace('"mappings": {"dynamic": "strict",', '"mappings": {"dynamic": "strict", "subobjects": false,')
        draft = await indexing.draft_index_mapping(ctx, 'elasticsearch', flat_index, 'ecs', events,
                                                   example_template=flat_example)
        flat = FieldPlan.model_validate(draft['plan'])
        e2e.check(flat.subobjects is False and '<map key="user">' in flat.xslt(),
                 'the plan records subobjects: false; documents are still written nested')
        xslt = await translation.save_xslt(ctx, people['build'], f'{flat_index}-XSLT', index_plan=flat)
        flat_pipeline = await e2e.agreed(indexing.create_indexing_pipeline, ctx=ctx, build=people['build'],
                                        name=f'{flat_index} - Indexing', template_uuid=es_template['uuid'],
                                        xslt_uuid=xslt['uuid'], index_name=flat_index, cluster_uuid=cluster['uuid'])
        e2e.check((await stepping.step_sample(ctx, flat_pipeline['uuid'], events))['verdict'] == 'clean', 'stepped clean')
        final = await e2e.agreed(indexing.propose_index_template, ctx=ctx, pipeline_uuid=flat_pipeline['uuid'], plan=flat,
                                events_stream_ids=events, example_template=flat_example)
        mapping = final['template']['template']['mappings']
        e2e.check(mapping.get('subobjects') is False and 'user.id' in mapping['properties'] and 'user' not in mapping['properties'],
                 'the template maps user.id, user.name and user.emailAddress as fields of their own')
        path, request = _request(final['dev_tools'])
        e2e.check((await es.put(f'/{path}', json=request)).status_code == 200, f'PUT {path}')
        started = await e2e.agreed(processing_writes.create_processor_filter, ctx=ctx, pipeline_uuid=flat_pipeline['uuid'],
                                  stream_ids=events, source_pipeline_uuid=people['pipeline']['uuid'])
        done = await processing_writes.wait_for_processing(ctx, flat_pipeline['uuid'], events, expect_events=False,
                                                           filter_id=started['filter_id'])
        e2e.check(done.get('gate') == 'pass', 'indexed with no Error stream')
        doc = await e2e.agreed(indexing.create_index_doc, ctx=ctx, build=people['build'], backend='elasticsearch',
                              name=flat_index, time_field=flat.time_field, index_name=flat_index,
                              cluster_uuid=cluster['uuid'])
        await paired(ctx, es, people['build'], flat_index, doc['uuid'], events, 2,
                     ['StreamId', 'EventId', '@timestamp', 'user.id', 'user.name'], [
                         ('user.id', 'EQUALS', 'john.smith1', 1, {'term': {'user.id': 'john.smith1'}}),
                         ('user.emailAddress', 'EQUALS', 'jane.doe2@email.com', 1,
                          {'term': {'user.emailAddress': 'jane.doe2@email.com'}}),
                     ], pipeline_uuid=flat_pipeline['uuid'])


if __name__ == '__main__':
    asyncio.run(main())
