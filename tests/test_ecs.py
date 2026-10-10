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


def test_an_ecs_plans_index_template_composes_elastics_ecs_mappings():
    # Asked by the user: the recommended way of composing an ECS index template. The plan's own properties stay,
    # setting each planned field's type over the component's.
    fields = [PlannedField(name='source.ip', type='ip', source='EventSource/Client/IPAddress')]
    ecs_plan = FieldPlan(backend='elasticsearch', index_name='ecs-acme-v1', time_field='@timestamp', fields=fields,
                         convention='ecs')
    body = ecs_plan.elastic_template('ecs-acme-v1')['body']
    assert body['composed_of'] == ['ecs@mappings']
    assert body['template']['mappings']['properties']['source']['properties']['ip'] == {'type': 'ip'}
    other = ecs_plan.model_copy(update={'convention': 'stroom-flat'}).elastic_template('acme-v1')['body']
    assert 'composed_of' not in other
