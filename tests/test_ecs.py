"""The Elastic Common Schema as the server knows it (utils/ecs, conventions/ecs_fields.json): asked for by the user, as
it had known ECS only through the few names its convention profile mapped."""
from pathlib import Path

import pytest
import yaml
from lxml import etree
from saxonche import PySaxonProcessor

from utils import ecs
from utils.eventschema import EventSchema
from utils.fieldplan import FieldPlan, PlannedField

ROOT = Path(__file__).resolve().parents[1]
FIELD_MAP = yaml.safe_load((ROOT / 'conventions' / 'ecs.yaml').read_text(encoding='utf-8'))['field_map']
SCHEMAS = {v: EventSchema.parse((ROOT / 'tests' / 'fixtures' / f'event-logging-v{v}.xsd').read_bytes())
           for v in ('3.5.2', '4.1.0')}


def test_the_bundled_schema_is_whole():
    assert len(ecs.schema()['fields']) > 2000 and 'process' in ecs.schema()['field_sets']
    assert ecs.field('event.outcome')['allowed'] == ['failure', 'success', 'unknown']


@pytest.mark.parametrize('path', FIELD_MAP)
def test_every_mapping_is_an_ecs_field_of_its_type_from_an_event_logging_path(path):
    spec = FIELD_MAP[path]
    assert ecs.field(spec['name']) and ecs.check(spec['name'], spec['type']) is None, spec
    # A wildcard stands for one element: the path must exist for at least one action that has it.
    actions = {'Network/*': ['Permit', 'Deny', 'Connect'],
               'EventDetail/*': ['Authenticate', 'Process', 'View', 'Create', 'Delete', 'Alert', 'Network']}
    concrete = [path]
    for wildcard, names in actions.items():
        concrete = [c.replace(wildcard, wildcard.replace('*', n), 1) for c in concrete for n in names] \
            if wildcard in path else concrete
    for version, schema in SCHEMAS.items():
        resolved = []
        for candidate in concrete:
            try:
                schema.resolve(candidate)
                resolved.append(candidate)
            except Exception:
                continue
        assert resolved, f"{path}: in no action of event-logging {version}"


def test_a_plan_is_checked_against_ecs_field_by_field():
    assert ecs.check('source.port', 'keyword') == "source.port is typed keyword, where ECS " + ecs.version() + \
        " has long (plan type long)"
    said = ecs.check('user.nmae', 'keyword')
    assert "isn't an ECS" in said and 'did you mean user.name' in said
    # Outside ECS's field sets: a field of the user's own, which ECS allows. Stroom's own fields are exempt.
    assert ecs.check('acme.ticket', 'keyword') is None and ecs.check('StreamId', 'id') is None
    assert ecs.check('message', 'text') is None and ecs.check('process.command_line', 'keyword') is None
    plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v1', time_field='@timestamp', convention='ecs',
                     fields=[PlannedField(name='source.port', type='keyword', source='EventSource/Client/Port')])
    assert plan.convention_problems() and not plan.model_copy(update={'convention': None}).convention_problems()


def test_every_top_level_name_neither_ecs_nor_a_declared_namespace_is_reported():
    # Seen: an ECS plan drafted authenticate.*, authorise.*, copy.* and create.*, the check passed them as custom
    # fields, and the agent told the user only its own secret.* and secretserver.* fields weren't ECS.
    fields = [PlannedField(name='message', type='text', source='EventDetail/Description')] + [
        PlannedField(name=n, type='keyword', source='EventDetail/TypeId') for n in (
            'StreamId', 'EventId', 'tags', 'labels.team', 'user.name', 'authenticate.user.id',
        'authenticate.user.domain', 'copy.source.object.id', 'secretserver.ticket', 'secret.name')]
    plan = FieldPlan(backend='elasticsearch', index_name='stroom-ecs-secretserver-v1', time_field='@timestamp',
                     convention='ecs', fields=fields, custom_namespaces=['secretserver'])
    said = plan.convention_problems()
    assert [p.split(' ')[0] for p in said] == ['authenticate.*', 'copy.*', 'secret.*']
    assert said[0].startswith('authenticate.* (authenticate.user.id, authenticate.user.domain): authenticate is '
                              "neither an ECS") and '(secretserver.authenticate.user.id)' in said[0]
    declared = plan.model_copy(update={'custom_namespaces': ['secretserver', 'secret', 'authenticate', 'copy']})
    assert declared.convention_problems() == []


@pytest.mark.parametrize('index_name, namespace', [
    ('stroom-ecs-secretserver-v1', 'secretserver'), ('ecs-keycloak-v1', 'keycloak'),
    ('ecs-microsoft-windows{_suffix}v1', 'microsoft_windows'), ('ecs-host-v2', 'host_custom'), ('ecs-v1', 'custom')])
def test_a_sources_namespace_is_named_after_the_index(index_name, namespace):
    assert ecs.source_namespace(index_name) == namespace


@pytest.mark.parametrize('data_name, expected', [
    ('source_ip', 'source.ip'), ('SourceIp', 'source.ip'), ('Source-IP', 'source.ip'), ('user_full_name', 'user.full_name'),
    ('http_request_method', 'http.request.method'), ('UserAgentOriginal', 'user_agent.original'),
    # An abbreviation or a name of the source's own is not guessed at.
    ('src_ip', None), ('dstport', None), ('session_id', None)])
def test_a_data_elements_name_is_an_ecs_field_only_written_another_way(data_name, expected):
    assert ecs.for_data_name(data_name) == expected


EVENTS = """<Events xmlns="event-logging:3">
<Event StreamId="7" EventId="1"><EventTime><TimeCreated>2026-10-01T09:00:00.000Z</TimeCreated></EventTime>
  <EventDetail><Authenticate><Action>Logon</Action><Outcome><Success>false</Success></Outcome></Authenticate></EventDetail></Event>
<Event StreamId="7" EventId="2"><EventTime><TimeCreated>2026-10-01T09:01:00.000Z</TimeCreated></EventTime>
  <EventDetail><Authenticate><Action>Logon</Action><Outcome><Success>true</Success></Outcome></Authenticate></EventDetail></Event>
<Event StreamId="7" EventId="3"><EventTime><TimeCreated>2026-10-01T09:02:00.000Z</TimeCreated></EventTime>
  <EventDetail><Authenticate><Action>Logoff</Action></Authenticate></EventDetail></Event>
</Events>"""


def test_outcome_is_written_as_ecs_has_it_and_left_out_with_none():
    # event-logging's Success is true or false; ECS's event.outcome is success or failure. No Outcome is success in
    # event-logging, but ECS's own reading is left to the user: the field is left out.
    plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v1', time_field='@timestamp', convention='ecs', fields=[
        PlannedField(name='StreamId', type='id', source='@StreamId'), PlannedField(name='EventId', type='id', source='@EventId'),
        PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
        PlannedField(name='event.action', type='keyword', source='EventDetail/*/Action'),
        PlannedField(name='event.outcome', type='keyword', source='EventDetail/*/Outcome/Success', transform='outcome')])
    assert plan.convention_problems() == []
    with PySaxonProcessor(license=False) as proc:
        out = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=plan.xslt()).transform_to_string(
            xdm_node=proc.parse_xml(xml_text=EVENTS))
    ns = {'j': 'http://www.w3.org/2005/xpath-functions'}
    documents = etree.fromstring(out.encode()).findall('j:map', ns)
    outcomes = [d.findtext("j:map[@key='event']/j:string[@key='outcome']", namespaces=ns) for d in documents]
    assert outcomes == ['failure', 'success', None]


def test_an_ecs_plans_index_template_leaves_ecs_fields_to_elastics_ecs_mappings():
    # Asked by the user: composed of ecs@mappings (the recommended way), and relying on it for the standard ECS fields,
    # mapping only those it doesn't. It maps as documents bring fields, so dynamic mapping is on.
    fields = [PlannedField(name='StreamId', type='id', source='@StreamId'),
              PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
              PlannedField(name='source.ip', type='ip', source='EventSource/Client/IPAddress'),
              PlannedField(name='message', type='text', source='EventDetail/Description'),
              PlannedField(name='source.port', type='keyword', source='EventSource/Client/Port'),   # not ECS's type
              PlannedField(name='gen_ai.usage.input_tokens', type='long', source='Data'),         # it maps integer
              PlannedField(name='acme.ticket', type='keyword', source='EventDetail/TypeId')]       # the user's own
    ecs_plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v1', time_field='@timestamp', fields=fields,
                         convention='ecs')
    body = ecs_plan.elastic_template('ecs-acme-v1')['body']
    mappings = body['template']['mappings']
    assert body['composed_of'] == ['ecs@mappings'] and mappings['dynamic'] is True
    properties = mappings['properties']
    # message too: the component maps it as match_only_text, which Stroom can't search (seen in production).
    assert set(properties) == {'StreamId', 'source', 'gen_ai', 'acme', 'message'}
    assert properties['source']['properties'] == {'port': {'type': 'keyword'}} and properties['message']['type'] == 'text'
    assert properties['gen_ai']['properties']['usage']['properties']['input_tokens'] == {'type': 'long'}
    # Every field mapped, for a template built from the user's example (which says what it is composed of).
    full = ecs_plan.elastic_template('ecs-acme-v1', leave_to_component=False)['body']['template']['mappings']
    assert full['dynamic'] is False and full['properties']['source']['properties']['ip'] == {'type': 'ip'}
    other = ecs_plan.model_copy(update={'convention': 'stroom-flat'}).elastic_template('acme-v1')['body']
    assert 'composed_of' not in other and other['template']['mappings']['dynamic'] is False


def test_the_component_is_measured_and_maps_ecs_fields_as_ecs_says():
    # dev/ecs_component.py measured it against Elasticsearch: the few it maps otherwise are mapped by the template.
    assert ecs.component_type('source.ip') == 'ip' and ecs.component_type('message') == 'match_only_text'
    assert ecs.component_type('gen_ai.usage.input_tokens') == 'integer' and ecs.component_type('acme.ticket') is None
    assert ecs.left_to_component('url.original', 'keyword') and not ecs.left_to_component('source.port', 'keyword')
    assert not ecs.left_to_component('data_stream.dataset', 'keyword') and not ecs.left_to_component('StreamId', 'id')
    assert not ecs.left_to_component('message', 'text') and not ecs.left_to_component('error.message', 'text')


def test_documents_are_checked_against_what_the_component_maps():
    from utils.templatecheck import compare, json_xml_documents

    def docs(ip: str) -> list:
        return json_xml_documents('<array xmlns="http://www.w3.org/2005/xpath-functions"><map><number key="StreamId">7'
                                  '</number><string key="@timestamp">2026-10-11T08:00:00.000Z</string>'
                                  f'<map key="source"><string key="ip">{ip}</string></map></map></array>')
    plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v1', time_field='@timestamp', convention='ecs',
                     fields=[PlannedField(name='StreamId', type='id', source='@StreamId'),
                             PlannedField(name='source.ip', type='ip', source='EventSource/Client/IPAddress')])
    body = plan.elastic_template('ecs-acme-v1')['body']
    good = compare(body, docs('10.0.0.1'), 'ecs-acme-v1')
    assert good['compatible'] and good['notes'] == [] and good['pipeline_changes'] == []
    bad = compare(body, docs('ws01'), 'ecs-acme-v1')
    assert not bad['compatible'] and "source.ip is 'ws01', not an IP address" in bad['blocking']


def test_an_example_composing_ecs_mappings_leaves_ecs_fields_to_it():
    from utils.templatecheck import from_example
    plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v2', time_field='@timestamp', convention='ecs',
                     fields=[PlannedField(name='StreamId', type='id', source='@StreamId'),
                             PlannedField(name='source.ip', type='ip', source='EventSource/Client/IPAddress'),
                             PlannedField(name='host.name', type='keyword', source='EventSource/Device/HostName')])
    planned = plan.elastic_template('ecs-acme-v2', leave_to_component=False)['body']
    example = {'index_patterns': ['ecs-acme-v1*'], 'composed_of': ['ecs@mappings'],
               'template': {'mappings': {'properties': {'host': {'properties': {'name': {'type': 'keyword',
                                                                                          'ignore_above': 256}}}}}}}
    body, notes = from_example(planned, example, {})
    properties = body['template']['mappings']['properties']
    # source.ip to the component; host.name as the example maps it; nothing said about ecs@mappings not being given.
    assert 'source' not in properties and properties['host']['properties']['name']['ignore_above'] == 256
    assert body['template']['mappings']['dynamic'] is True and not any('not given' in n for n in notes)
    assert any("left to the component templates (they map them): ['source.ip']" in n for n in notes)
    # An example without it: every field mapped, as before.
    plain, _ = from_example(planned, {**example, 'composed_of': []}, {})
    assert plain['template']['mappings']['properties']['source']['properties']['ip'] == {'type': 'ip'}



def test_an_ecs_plan_composes_ecs_mappings_whatever_the_example_composes():
    # A regression seen in testing: built from a sibling's index mapping (which never shows composed_of, and had
    # dynamic false), an ECS plan's template mapped every field explicitly, dynamic false, without ecs@mappings.
    from utils.templatecheck import from_example
    plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v2', time_field='@timestamp', convention='ecs',
                     fields=[PlannedField(name='StreamId', type='id', source='@StreamId'),
                             PlannedField(name='source.ip', type='ip', source='EventSource/Client/IPAddress'),
                             PlannedField(name='host.name', type='keyword', source='EventSource/Device/HostName'),
                             PlannedField(name='acme.ticket', type='keyword', source='EventDetail/TypeId')])
    planned = plan.elastic_template('ecs-acme-v2', leave_to_component=False)['body']
    for dynamic in ('false', 'strict', False):
        mapping = {'template': {'mappings': {'dynamic': dynamic, 'properties': {
            'StreamId': {'type': 'long'}, 'host': {'properties': {'name': {'type': 'keyword', 'ignore_above': 256}}}}}}}
        body, notes = from_example(planned, mapping, {}, ecs_base=True)
        mappings = body['template']['mappings']
        assert body['composed_of'] == ['ecs@mappings'] and mappings['dynamic'] is True
        # source.ip left to the component; host.name kept as the example maps it; the user's own field mapped here.
        assert 'source' not in mappings['properties'] and mappings['properties']['host']['properties']['name'] == {
            'type': 'keyword', 'ignore_above': 256} and 'acme' in mappings['properties']
        assert any('composed of ecs@mappings' in n for n in notes) and any('dynamic mapping on' in n for n in notes)
    # First, ahead of the example's own components, which override it.
    body, _ = from_example(planned, {'composed_of': ['org-base'], 'template': {'mappings': {}}},
                           {'org-base': {'template': {'settings': {'number_of_shards': 1}}}}, ecs_base=True)
    assert body['composed_of'] == ['ecs@mappings', 'org-base']
    # Standing instructions excluding it win; a plan not following ECS is untouched.
    excluded, _ = from_example(planned, {'template': {'mappings': {'dynamic': False}}}, {}, exclude=['ecs@mappings'],
                               ecs_base=True)
    assert 'composed_of' not in excluded and excluded['template']['mappings']['dynamic'] is False
    plain, _ = from_example(planned, {'template': {'mappings': {'dynamic': False}}}, {})
    assert 'composed_of' not in plain and plain['template']['mappings']['dynamic'] is False


def test_an_ecs_component_template_elasticsearch_lacks_is_refused():
    # Seen in testing: a proposed template composed ecs@settings, which Elasticsearch doesn't have.
    from fastmcp.exceptions import ToolError
    from tools.indexing import components_wanted
    with pytest.raises(ToolError, match='ecs@mappings is its only ECS one'):
        components_wanted({'composed_of': ['ecs@mappings', 'ecs@settings']}, {}, ['ecs@settings'])
    # Given, it exists on the user's cluster.
    assert components_wanted({'composed_of': ['ecs@settings']}, {'ecs@settings': {}}) is None

def test_dynamic_off_stops_the_component_and_says_so():
    from utils.templatecheck import compare, json_xml_documents
    plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v1', time_field='@timestamp', convention='ecs',
                     fields=[PlannedField(name='source.ip', type='ip', source='EventSource/Client/IPAddress')])
    body = plan.elastic_template('ecs-acme-v1')['body']
    body['template']['mappings']['dynamic'] = 'strict'
    docs = json_xml_documents('<array xmlns="http://www.w3.org/2005/xpath-functions"><map>'
                              '<string key="@timestamp">2026-10-11T08:00:00.000Z</string><map key="source">'
                              '<string key="ip">10.0.0.1</string></map></map></array>')
    said = compare(body, docs, 'ecs-acme-v1')
    assert said['blocking'] == ['source.ip: an ECS field left to ecs@mappings, but dynamic is strict, so it isn\'t '
                                'mapped and documents are rejected']
    assert 'dynamic back to true' in said['pipeline_changes'][0]['change']


def test_a_field_stroom_cant_list_is_said_when_a_template_maps_it_so():
    # Seen in production: Stroom leaves match_only_text (message, under ecs@mappings) out of an Elastic Index doc's
    # fields, so no search on it found anything.
    from utils.templatecheck import compare, json_xml_documents
    docs = json_xml_documents('<array xmlns="http://www.w3.org/2005/xpath-functions"><map><number key="StreamId">7'
                              '</number><string key="message">Connection Failed</string></map></array>')
    body = {'index_patterns': ['ecs-acme-v1*'], 'composed_of': ['ecs@mappings'],
            'template': {'mappings': {'dynamic': True, 'properties': {'StreamId': {'type': 'long'}}}}}
    left = compare(body, docs, 'ecs-acme-v1')
    assert left['compatible'] and any('message: mapped as match_only_text' in n for n in left['notes'])
    assert next(c for c in left['pipeline_changes'] if c['field'] == 'message')['change'].startswith(
        "map 'message' in the template as text")
    body['template']['mappings']['properties']['message'] = {'type': 'text'}
    assert compare(body, docs, 'ecs-acme-v1')['notes'] == []


def test_an_earlier_templates_ecs_fields_mapped_as_the_component_does_are_left_to_it():
    # Asked for by the user: an amended indexing pipeline's template, built from v1's, mapped the ECS base fields
    # again, explicitly. Mapped just as ecs@mappings does, they are left to it; mapped otherwise, they are kept.
    from utils.templatecheck import from_example
    plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v2', time_field='@timestamp', convention='ecs',
                     fields=[PlannedField(name='source.ip', type='ip', source='EventSource/Client/IPAddress'),
                             PlannedField(name='host.name', type='keyword', source='EventSource/Device/HostName'),
                             PlannedField(name='user.name', type='keyword', source='EventSource/User/Id')])
    planned = plan.elastic_template('ecs-acme-v2', leave_to_component=False)['body']
    v1 = {'index_patterns': ['ecs-acme-v1*'], 'composed_of': ['ecs@mappings'], 'template': {'mappings': {'properties': {
        'source': {'properties': {'ip': {'type': 'ip'}}},
        'host': {'properties': {'name': {'type': 'keyword', 'ignore_above': 1024}}},
        'user': {'properties': {'name': {'type': 'keyword', 'ignore_above': 256}}}}}}}
    body, notes = from_example(planned, v1, {})
    properties = body['template']['mappings']['properties']
    assert 'source' not in properties and 'host' not in properties                 # as ecs@mappings maps them
    assert properties['user']['properties']['name'] == {'type': 'keyword', 'ignore_above': 256}    # an override
    assert body['composed_of'] == ['ecs@mappings']
