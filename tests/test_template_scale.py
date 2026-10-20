"""Elasticsearch index templates at the scale seen in testing (20 October), in every way one is made: about 55 fields,
the user's example template mapping them and more, composed of several component templates pasted from Kibana. At
that size the review reply ran to 10,000 characters and was shown cut short, the confirmation form ran to 2,000 and
the client declined it, the components were corrupted as the agent re-sent them, and ecs@mappings, asked for, was
composed with every ECS field still mapped. Each scenario goes through the tools themselves, from the template
proposed to the template agreed, and the same things are checked at every step (_agreed)."""
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from config import Settings
from tools import indexing
from utils import ecs
from utils.consent import ConsentStore, form_message
from utils.fieldplan import ELASTIC, Discovery, FieldPlan, PlannedField
from utils.mappingstore import read_agreed_template
from utils.templatecheck import ecs_duplicates, json_xml_documents, parse_component_templates, parse_template, read_mapping

SETTINGS = Settings(_env_file=None, stroom_url='https://stroom.example', dev_no_auth=True, stroom_api_key='k')
INDEX = 'ecs-acme-v2'
ECS_FIELDS = [
    'source.ip', 'source.port', 'source.bytes', 'source.domain', 'source.mac', 'destination.ip', 'destination.port',
    'destination.bytes', 'destination.domain', 'user.name', 'user.id', 'user.domain', 'user.email', 'user.full_name',
    'host.name', 'host.hostname', 'host.ip', 'host.os.name', 'host.os.version', 'event.action', 'event.category',
    'event.code', 'event.outcome', 'event.dataset', 'event.module', 'event.provider', 'event.severity',
    'event.duration', 'network.protocol', 'network.transport', 'network.direction', 'network.bytes', 'process.name',
    'process.pid', 'process.executable', 'url.full', 'url.domain', 'url.path', 'http.request.method',
    'http.response.status_code', 'observer.name', 'observer.vendor', 'observer.product', 'rule.name', 'rule.id',
    'file.name', 'file.path']
OWN = [('acme.ticket', 'keyword'), ('acme.zone', 'keyword'), ('acme.policy', 'keyword'), ('acme.session_id', 'keyword'),
       ('acme.risk', 'long'), ('acme.flagged', 'boolean')]
VALUES = {'keyword': 'x1', 'ip': '10.0.0.1', 'long': '7', 'date': '2026-10-20T08:00:00.000Z', 'double': '1.5',
          'boolean': 'true', 'text': 'hello', 'id': '3'}
COMPONENTS = ('org-ecs-base', 'org-users', 'org-ilm', 'org-dynamic')


def _field(name: str, type_: str) -> PlannedField:
    return PlannedField(name=name, type=type_, source=f"EventDetail/Data[@Name='{name}']/@Value")


def _base() -> list[PlannedField]:
    return [PlannedField(name='StreamId', type='id', source='@StreamId'),
            PlannedField(name='EventId', type='id', source='@EventId'),
            PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated')]


def ecs_plan(own: list[tuple[str, str]] = OWN, ecs_fields: list[str] = ECS_FIELDS, **extra) -> FieldPlan:
    fields = _base() + [_field(n, ecs.plan_type(n)) for n in ecs_fields] + [_field(n, t) for n, t in own]
    return FieldPlan(backend='elasticsearch', index_name=INDEX, time_field='@timestamp', fields=fields,
                     convention='ecs', custom_namespaces=['acme'], **extra)


def _nest(flat: dict[str, dict]) -> dict:
    out: dict = {}
    for path, spec in flat.items():
        node = out
        *parents, leaf = path.split('.')
        for part in parents:
            node = node.setdefault(part, {'properties': {}})['properties']
        node[leaf] = spec
    return out


def _documents(fields: list[tuple[str, str]]) -> list[dict]:
    """The documents the indexing XSLT writes: each field (name, plan type) with a value of its type."""
    def xml(node: dict, key: str | None = None) -> str:
        attr = f' key="{key}"' if key else ''
        if isinstance(node, dict):
            return f'<map{attr}>' + ''.join(xml(v, k) for k, v in node.items()) + '</map>'
        kind, value = node
        return f'<{kind}{attr}>{value}</{kind}>'
    tree: dict = {}
    for name, type_ in fields:
        node = tree
        *parents, leaf = name.split('.')
        for part in parents:
            node = node.setdefault(part, {})
        kind = {'long': 'number', 'id': 'number', 'double': 'number', 'boolean': 'boolean'}.get(type_, 'string')
        node[leaf] = (kind, VALUES[type_])
    return json_xml_documents('<array xmlns="http://www.w3.org/2005/xpath-functions">' + xml(tree) + '</array>')


def _components() -> dict[str, dict]:
    network = {n: {'type': ELASTIC[ecs.plan_type(n)]} for n in ECS_FIELDS if n.startswith(('source.', 'destination.'))}
    users = {n: {'type': 'keyword'} for n in ECS_FIELDS if n.startswith('user.')}
    return {'org-ecs-base': {'template': {'mappings': {'properties': _nest(network)}}},
            'org-users': {'template': {'mappings': {'properties': _nest(users)}}},
            'org-ilm': {'template': {'settings': {'index': {'lifecycle': {'name': 'logs-90d'}}}}},
            'org-dynamic': {'template': {'mappings': {'dynamic_templates': [
                {'strings': {'match_mapping_type': 'string', 'mapping': {'type': 'keyword'}}}]}}}}


def kibana(components: dict[str, dict]) -> str:
    """Kibana Dev Tools' answers to a GET _component_template/<name> for each, pasted as one text."""
    return '\n'.join(f"# GET _component_template/{name} 200 OK\n" + json.dumps(
        {'component_templates': [{'name': name, 'component_template': body}]}, indent=2)
        for name, body in components.items())


def legacy_example(composed: bool = True, flat: bool = False, dynamic: str = 'strict', ecs_base: bool = False) -> dict:
    """The user's legacy template: every planned field mapped (every third keyword with the old ignore_above 256, an
    override of ecs@mappings), 20 more fields it alone writes, settings; composed of COMPONENTS, which map the
    source, destination and user fields, or (composed=False) mapping those itself; ecs_base: composed of ecs@mappings
    first, as an ECS example that references it is."""
    owned = ECS_FIELDS if not composed else [n for n in ECS_FIELDS if not n.startswith(('source.', 'destination.', 'user.'))]
    fields = {'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}, '@timestamp': {'type': 'date'}}
    for n, name in enumerate(owned):
        kind = ELASTIC[ecs.plan_type(name)]
        fields[name] = {'type': kind, 'ignore_above': 256} if kind == 'keyword' and n % 3 == 0 else {'type': kind}
    fields.update({n: {'type': ELASTIC[t]} for n, t in OWN})
    fields.update({f'legacy.f{n}': {'type': 'keyword'} for n in range(20)})
    mappings = {'dynamic': dynamic, **({'subobjects': False, 'properties': fields} if flat else
                                       {'properties': _nest(fields)})}
    composed_of = (['ecs@mappings'] if ecs_base else []) + (list(COMPONENTS) if composed else [])
    return {'index_patterns': ['ecs-acme-v1*'], 'priority': 300, **({'composed_of': composed_of} if composed_of else {}),
            'template': {'settings': {'index': {'number_of_shards': 2, 'number_of_replicas': 1}}, 'mappings': mappings}}


def request(body: dict, name: str = 'ecs-acme-v1') -> str:
    return f"PUT _index_template/{name}\n{json.dumps(body, indent=2)}"


@contextlib.contextmanager
def harness(monkeypatch, documents: list[dict], ruled: dict | None = None):
    """A Stroom, a consent store and a pipeline whose indexing XSLT writes these documents to INDEX, under standing
    instructions' component template rules (ruled)."""
    monkeypatch.setattr('tools.plan._user', lambda ctx: 'alice')
    saved = {'type': 'Pipeline', 'uuid': 'p1', 'name': 'Acme - Indexing', 'description': 'Indexes Acme.'}

    async def put_doc(doc):
        saved.update(doc)
        return doc
    stroom = SimpleNamespace(settings=SETTINGS, get_doc=AsyncMock(side_effect=lambda t, u: dict(saved)), put_doc=put_doc)
    context = SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': ConsentStore(False)})
    with patch('tools.indexing._destination', AsyncMock(return_value={'index name': INDEX, 'cluster': 'ES_PROD'})), \
            patch('tools.indexing._documents', AsyncMock(return_value=documents)), \
            patch('tools.indexing._require_input', AsyncMock()), \
            patch('tools.indexing.indexing_xslt_digest', AsyncMock(return_value='abc')), \
            patch('tools.indexing.guard_from', return_value=SimpleNamespace(check_managed=AsyncMock())), \
            patch('tools.indexing._kept_example', AsyncMock(return_value=None)), \
            patch('tools.builds.kept_mapping', AsyncMock(return_value=None)), \
            patch('tools.instructions.components_ruled',
                  AsyncMock(return_value=ruled or {'include': [], 'exclude': []})):
        yield context, saved


def _plan_docs(plan: FieldPlan) -> list[dict]:
    return _documents([(f.name, f.type) for f in plan.fields])


async def _agreed(context, saved, plan, tool: str = 'propose', **given) -> tuple[dict, list[str]]:
    """The template proposed (or checked), reviewed, confirmed and agreed, as the agent calls the tools, checking at
    each step what broke at scale: the review reply leads with what to do and is short enough to show whole; the
    template round-trips; the components aren't sent again after the first call, nor the template after its review;
    the form is short and names the review. (body, notes) of the agreed template."""
    async def call(**more):
        if tool == 'propose':
            return await indexing.propose_index_template(context, 'p1', plan, [8], **given, **more)
        return await indexing.check_index_template(context, 'p1', events_stream_ids=[8], **given, **more)
    shown = await call()
    assert shown.get('status') == 'needs_review', {k: shown.get(k) for k in ('status', 'blocking', 'hint', 'missing')}
    assert list(shown)[:2] == ['status', 'hint']
    assert len(json.dumps(shown)) < 20_000, len(json.dumps(shown))
    assert len(shown['dev_tools'].splitlines()) < 150, len(shown['dev_tools'].splitlines())
    name, body = parse_template(shown['dev_tools'])
    notes = shown.get('from_example') or shown.get('notes') or []
    # Never sent again: the components (kept for the session), and for check_index_template the template itself.
    given.pop('component_templates', None)
    if tool == 'check':
        given.pop('template')
    asked = await call(reviewed=shown['review_id'])
    assert asked.get('status') == 'needs_confirmation', asked
    message = form_message(asked['summary'], asked['details'])
    assert len(message) <= 600 and len(asked['details']) <= 20, message
    assert shown['review_id'] in asked['details']['index template']
    done = await call(reviewed=shown['review_id'], confirmation_id=asked['confirmation_id'])
    assert done['agreed'], done
    assert read_agreed_template(saved['description'])['dev_tools'] == shown['dev_tools']
    return body, notes


def _properties(body: dict) -> dict[str, dict]:
    mappings = body['template']['mappings']
    if mappings.get('subobjects') is False:
        return dict(mappings['properties'])
    return {p: s for p, s in read_mapping(mappings).fields.items() if s.get('type') != 'object'}


def _ecs_left(body: dict, plan: FieldPlan) -> list[str]:
    """The plan's ECS fields the template leaves to ecs@mappings."""
    mapped = _properties(body)
    return [f.name for f in plan.fields if f.name not in mapped and ecs.left_to_component(f.name, f.type)]


# 1. The user's example, composed of component templates; with ecs@mappings among them or not. Asked for by the
# user: ecs@mappings only when the user opts for ECS field mapping (the ECS convention, or an example referencing it).
@pytest.mark.parametrize('ecs_base', [True, False])
async def test_an_example_with_component_templates(monkeypatch, ecs_base):
    plan = ecs_plan()
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        example = request(legacy_example(ecs_base=ecs_base))
        asked = await indexing.propose_index_template(context, 'p1', plan, [8], example_template=example)
        # One request per component: the API takes one name or a wildcard, not a list.
        assert asked['status'] == 'needs_components'
        assert asked['request'].splitlines() == [f"GET _component_template/{n}" for n in COMPONENTS]
        body, notes = await _agreed(context, saved, plan, example_template=example,
                                    component_templates=kibana(_components()))
    assert body['composed_of'] == (['ecs@mappings'] if ecs_base else []) + list(COMPONENTS) and body['priority'] == 300
    assert body['template']['mappings']['dynamic'] == (True if ecs_base else 'strict')
    assert ecs_duplicates(body) == []
    mapped = _properties(body)
    if not ecs_base:
        # No ecs@mappings: every field the components don't map is mapped here, as the example maps it.
        assert {f.name for f in plan.fields if not f.name.startswith(('source.', 'destination.', 'user.'))} <= set(mapped)
    # The components' fields are left to them; the overrides (ignore_above 256) and the user's own are here.
    assert not any(p.startswith(('source.', 'destination.', 'user.')) for p in mapped)
    assert mapped['host.name'] == {'type': 'keyword', 'ignore_above': 256} and mapped['acme.risk'] == {'type': 'long'}
    assert not any(p.startswith('legacy.') for p in mapped)
    assert body['template']['settings']['index']['number_of_shards'] == 2
    assert any("can't be weighed" in n and 'org-ilm' in n and 'org-dynamic' in n for n in notes)


# 2. The same example with no component templates: it maps everything itself, unless it references ecs@mappings.
async def test_an_example_without_component_templates(monkeypatch):
    plan = ecs_plan()
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        body, notes = await _agreed(context, saved, plan, example_template=request(legacy_example(composed=False)))
    # Not the user's choice: no ecs@mappings, every field mapped as the example maps it, its strict kept.
    assert 'composed_of' not in body and body['template']['mappings']['dynamic'] == 'strict'
    assert {f.name for f in plan.fields} <= set(_properties(body))


async def test_an_example_referencing_ecs_mappings_leaves_the_ecs_fields_to_it(monkeypatch):
    plan = ecs_plan()
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        body, notes = await _agreed(context, saved, plan,
                                    example_template=request(legacy_example(composed=False, ecs_base=True)))
    assert body['composed_of'] == ['ecs@mappings'] and body['template']['mappings']['dynamic'] is True
    assert ecs_duplicates(body) == [] and len(_ecs_left(body, plan)) > 25
    mapped = _properties(body)
    # Only overrides of ecs@mappings stay: ignore_above 256, or keyword where it maps a wildcard (url.full).
    assert all(s == {'type': 'keyword', 'ignore_above': 256} or ecs.component_type(p) != 'keyword'
               for p, s in mapped.items() if p in ECS_FIELDS and s.get('type') == 'keyword')
    assert any('dynamic mapping on' in n for n in notes)


# 3. No example: ECS, with many fields of the source's own.
async def test_no_example_ecs_with_many_fields_of_the_sources_own(monkeypatch):
    own = [(f'acme.{group}.{n}', t) for group, t in (('session', 'keyword'), ('risk', 'long'), ('flags', 'boolean'),
                                                     ('policy', 'keyword'), ('score', 'double')) for n in range(8)]
    plan = ecs_plan(own=own, ecs_fields=ECS_FIELDS[:15])
    assert plan.convention_problems() == []
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        body, notes = await _agreed(context, saved, plan, without_example=True)
    mapped = _properties(body)
    # No priority: the user's to set, if they want one (asked for by the user).
    assert body['composed_of'] == ['ecs@mappings'] and body['data_stream'] == {} and 'priority' not in body
    assert body['template']['mappings']['dynamic'] is True and ecs_duplicates(body) == []
    # Every field of the source's own mapped as the plan types it; the ECS ones left to the component.
    assert {p: s['type'] for p, s in mapped.items() if p.startswith('acme.')} == {n: ELASTIC[t] for n, t in own}
    assert set(_ecs_left(body, plan)) == {f.name for f in plan.fields
                                          if ecs.left_to_component(f.name, f.type)}
    assert any('built from the field plan alone' in n for n in notes)


# 4. The example is an existing index's mapping (GET <index>/_mapping): no composed_of to follow, dynamic false.
async def test_an_index_mapping_as_the_example(monkeypatch):
    plan = ecs_plan()
    mapping = {'ecs-acme-v1-000001': {'mappings': legacy_example(composed=False, dynamic='false')['template']['mappings']}}
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        body, notes = await _agreed(context, saved, plan, example_template=json.dumps(mapping))
    # A mapping never shows composed_of: followed as it is, ecs@mappings only if the user asks for it (scenario 5).
    assert body['index_patterns'] == [f'{INDEX}*'] and 'composed_of' not in body
    assert body['template']['mappings']['dynamic'] == 'false' and {f.name for f in plan.fields} <= set(_properties(body))


# 5. The user asks for ecs@mappings on a plan that doesn't say it follows ECS (its convention lost).
@pytest.mark.parametrize('with_example', [True, False])
async def test_ecs_mappings_asked_for_by_name(monkeypatch, with_example):
    plan = ecs_plan().model_copy(update={'convention': None, 'custom_namespaces': []})
    given = ({'example_template': request(legacy_example(composed=False, dynamic='false'))} if with_example
             else {'without_example': True})
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        body, notes = await _agreed(context, saved, plan, include_components=['ecs@mappings'], **given)
    assert body['composed_of'][0] == 'ecs@mappings' and body['template']['mappings']['dynamic'] is True
    assert ecs_duplicates(body) == [] and len(_ecs_left(body, plan)) > 25


# 6. Standing instructions exclude ecs@mappings: every field mapped here, the example's dynamic kept.
async def test_ecs_mappings_excluded_by_standing_instructions(monkeypatch):
    plan = ecs_plan()
    ruled = {'include': [], 'exclude': [{'name': 'ecs@mappings', 'from': 'System'}]}
    with harness(monkeypatch, _plan_docs(plan), ruled) as (context, saved):
        body, notes = await _agreed(context, saved, plan, example_template=request(legacy_example(composed=False)))
    assert 'ecs@mappings' not in body.get('composed_of', []) and body['template']['mappings']['dynamic'] == 'strict'
    assert {f.name for f in plan.fields} <= set(_properties(body)) and any('exclude' in n for n in notes)


# 7. A convention other than ECS: no ecs@mappings, the example's types followed.
async def test_a_flat_convention_with_an_example_and_components(monkeypatch):
    names = [('UserId', 'keyword'), ('HostName', 'keyword'), ('SourceIPAddress', 'ip'), ('SourcePort', 'long')] + [
        (f'Custom{n}', 'keyword' if n % 2 else 'long') for n in range(46)]
    fields = _base() + [_field(n, t) for n, t in names]
    plan = FieldPlan(backend='elasticsearch', index_name=INDEX, time_field='@timestamp', fields=fields,
                     convention='stroom-flat')
    example = {'index_patterns': ['acme-v1*'], 'composed_of': ['org-flat'], 'template': {'mappings': {
        'dynamic': 'strict', 'properties': {**{n: {'type': ELASTIC[t]} for n, t in names[:20]},
                                            'StreamId': {'type': 'long'}, 'EventId': {'type': 'long'}}}}}
    flat = {'org-flat': {'template': {'mappings': {'properties': {'@timestamp': {'type': 'date'},
                                                                  **{n: {'type': ELASTIC[t]} for n, t in names[20:]}}}}}}
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        body, notes = await _agreed(context, saved, plan, example_template=request(example),
                                    component_templates=kibana(flat))
    assert body['composed_of'] == ['org-flat'] and body['template']['mappings']['dynamic'] == 'strict'
    assert set(_properties(body)) == {n for n, _ in names[:20]} | {'StreamId', 'EventId'}


# 8. A discovery index: raw records mapped as they come, the example giving its settings and components.
async def test_a_discovery_index_with_an_example_and_components(monkeypatch):
    plan = FieldPlan.for_discovery(INDEX, Discovery(input='json', timestamp_field='time'))
    source = [(f'field{n}', 'keyword' if n % 3 else 'long') for n in range(50)]
    documents = _documents([('StreamId', 'id'), ('EventId', 'id'), ('@timestamp', 'date'), *source])
    with harness(monkeypatch, documents) as (context, saved):
        body, notes = await _agreed(context, saved, plan, example_template=request(legacy_example()),
                                    component_templates=kibana(_components()))
    mapped = _properties(body)
    assert 'ecs@mappings' not in body.get('composed_of', []) and set(COMPONENTS) <= set(body['composed_of'])
    assert not any(p.startswith(('field', 'legacy.')) for p in mapped) and body['template']['mappings']['dynamic'] is True
    assert any('a discovery index' in n for n in notes)


# 9. The example maps dotted names as fields of their own (subobjects: false).
async def test_an_example_with_subobjects_false(monkeypatch):
    plan = ecs_plan()
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        body, notes = await _agreed(context, saved, plan,
                                    example_template=request(legacy_example(composed=False, flat=True, ecs_base=True)))
    mappings = body['template']['mappings']
    assert mappings['subobjects'] is False and body['composed_of'] == ['ecs@mappings'] and mappings['dynamic'] is True
    assert all('properties' not in s for s in mappings['properties'].values()) and 'acme.risk' in mappings['properties']


# 10. A component the user can't give: built without it, and said so.
async def test_a_component_the_user_cant_give(monkeypatch):
    plan = ecs_plan()
    given = {n: c for n, c in _components().items() if n != 'org-users'}
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        body, notes = await _agreed(context, saved, plan, example_template=request(legacy_example()),
                                    component_templates=kibana(given), without_components=['org-users'])
    assert 'org-users' in body['composed_of'] and any('org-users' in n and 'not given' in n for n in notes)


# 11. The user's own template, hand-written: composed of ecs@mappings and mapping the ECS fields again.
async def test_a_hand_written_template_mapping_ecs_fields_twice_is_said_so(monkeypatch):
    plan = ecs_plan()
    body = plan.elastic_template(INDEX, leave_to_component=False)['body']
    body['template']['mappings']['dynamic'] = True
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        agreed, notes = await _agreed(context, saved, plan, tool='check', template=request(body, INDEX),
                                      component_templates=kibana(_components()))
    twice = next(n for n in notes if 'mapped twice' in n)
    assert 'destination.ip' in twice and agreed == body      # the user's own, said, not changed


# 11b. GET output pasted (Kibana's, or Stroom's own tool's): what Elasticsearch sets itself never reaches the PUT.
def _dated(body: dict) -> dict:
    """A template as GET returns it: with the dates Elasticsearch sets itself (9.5)."""
    return {**body, 'created_date_millis': 1792526762556, 'modified_date_millis': 1792526762556}


def _as_got(body: dict) -> dict:
    """A template as GET ?human&include_defaults returns it (as Elasticsearch 9.5 does)."""
    got = {**_dated(body), 'created_date': '2026-10-20T20:06:02.556Z', 'modified_date': '2026-10-20T20:06:02.556Z'}
    got['template'] = {**got.get('template', {}), 'lifecycle': {'enabled': True, 'data_retention': '30d', 'rollover': {
        'min_docs': 1, 'max_age': '1d [automatic]'}}}
    return got


_SET_BY_ELASTICSEARCH = ('created_date', 'modified_date', 'rollover')


@pytest.mark.parametrize('tool', ['propose', 'check'])
async def test_get_output_pasted_leaves_out_what_elasticsearch_sets(monkeypatch, tool):
    plan = ecs_plan()
    components = {n: _as_got(c) for n, c in _components().items()}
    pasted = '\n'.join(f"# GET _component_template/{n} 200 OK\n" + json.dumps(
        {'component_templates': [{'name': n, 'component_template': c}]}) for n, c in components.items())
    if tool == 'propose':
        example = json.dumps({'index_templates': [{'name': 'ecs-acme-v1', 'index_template': _as_got(legacy_example())}]})
        given = {'example_template': example}
    else:
        own = plan.elastic_template(INDEX, leave_to_component=False)['body']
        own.update(composed_of=list(COMPONENTS), version=3, _meta={'owner': 'acme'})
        example = json.dumps({'index_templates': [{'name': INDEX, 'index_template': _as_got(own)}]})
        given = {'template': example}
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        shown = await (indexing.propose_index_template(context, 'p1', plan, [8], component_templates=pasted, **given)
                       if tool == 'propose' else
                       indexing.check_index_template(context, 'p1', events_stream_ids=[8], component_templates=pasted,
                                                     **given))
        assert shown['status'] == 'needs_review', shown
        kept = indexing._given_components(context, [], list(COMPONENTS))
    assert not any(word in shown['dev_tools'] for word in _SET_BY_ELASTICSEARCH), shown['dev_tools']
    assert not any(word in json.dumps(kept) for word in _SET_BY_ELASTICSEARCH)
    assert all(c['template']['lifecycle'] == {'enabled': True, 'data_retention': '30d'} for c in kept.values())
    if tool == 'check':
        # The user's own: all a PUT takes kept, their lifecycle too.
        _, body = parse_template(shown['dev_tools'])
        assert body['version'] == 3 and body['_meta'] == {'owner': 'acme'}
        assert body['template']['lifecycle'] == {'enabled': True, 'data_retention': '30d'}


# 11c. Stroom's elastic_index_templates output pasted whole: the template and its components in one paste.
def stroom_tool(name: str, body: dict, components: dict[str, dict], refused: dict[str, str] = None) -> str:
    """The output of Stroom's elastic_index_templates tool (ElasticIndexTemplatesTool.describe, Stroom gh-5848)."""
    refused = refused or {}
    lines = ["Elastic Index [ecs-sibling-v1](https://stroom.example/stroom/ui?doc=x)", "Index name: `ecs-sibling-v1`",
             "Resolves to: `ecs-sibling-v1-000002` (the newest index or data stream the index name stands for)", "",
             f"Index template that applies: `{name}`",
             "Overlapping templates (they match the index too, but have a lower priority, so they don't apply):",
             "- `ecs-default` (index patterns: `ecs-*`; priority 100)", "",
             "Nothing has been changed in Elasticsearch. Each body below is exactly as Elasticsearch returns it. Show "
             "each template to the user verbatim, with its name just above its code block: don't add, remove or "
             "change anything in it, and don't say whether or how it can be applied to Elasticsearch.", "",
             f"### Index template `{name}`", "```json", json.dumps(_dated(body), indent=2), "```"]
    for n in body.get('composed_of') or []:
        lines += ['', f"### Component template `{n}`"]
        lines += ([f"This component template couldn't be read (FORBIDDEN): the cluster connection's credentials "
                   f"lack the privilege Elasticsearch names: {refused[n]}"] if n in refused else
                  ["```json", json.dumps(_dated(components[n]), indent=2), "```"])
    return '\n'.join(lines)


async def test_stroom_tool_output_pasted_as_the_example(monkeypatch):
    plan = ecs_plan()
    refused = {'org-ilm': 'action [cluster:admin/component_template/get] is unauthorized for user [stroom]'}
    pasted = stroom_tool('ecs-acme-v1', legacy_example(), _components(), refused)
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        # Its components come with it: only the one Stroom couldn't read is asked for.
        asked = await indexing.propose_index_template(context, 'p1', plan, [8], example_template=pasted)
        assert asked['status'] == 'needs_components', asked
        assert asked['request'].splitlines() == ['GET _component_template/org-ilm']
        body, notes = await _agreed(context, saved, plan, example_template=pasted,
                                    component_templates=kibana({'org-ilm': _components()['org-ilm']}))
    assert body['composed_of'] == list(COMPONENTS) and body['priority'] == 300
    assert not any(p.startswith(('source.', 'destination.', 'user.')) for p in _properties(body))
    assert 'created_date' not in json.dumps(body)


async def test_stroom_tool_output_pasted_as_the_users_own_template(monkeypatch):
    plan = ecs_plan()
    own = plan.elastic_template(INDEX, leave_to_component=False)['body']
    own.update(composed_of=list(COMPONENTS), version=3)
    with harness(monkeypatch, _plan_docs(plan)) as (context, saved):
        agreed, _ = await _agreed(context, saved, plan, tool='check', template=stroom_tool(INDEX, own, _components()))
    assert agreed == own        # the user's own, less what Elasticsearch set


def test_stroom_tool_output_refused_template_and_other_text():
    from utils.templatecheck import stroom_tool_templates
    refused = stroom_tool('ecs-acme-v1', legacy_example(), _components()).split('### Index template')[0] + \
        "Index template: couldn't be read (FORBIDDEN): the cluster connection's credentials lack the privilege"
    assert stroom_tool_templates(refused) == []
    with pytest.raises(ValueError, match='not valid JSON'):
        parse_template(refused)
    unread = "### Index template `ecs-acme-v1`\nThis index template couldn't be read (ERROR): timed out"
    with pytest.raises(ValueError, match='GET _index_template/ecs-acme-v1'):
        parse_template(unread)
    assert stroom_tool_templates(request(legacy_example())) == [] and stroom_tool_templates(kibana(_components())) == []


def test_large_components_survive_the_shapes_an_agent_resends_them_in():
    pasted = parse_component_templates([kibana(_components())])
    assert sorted(pasted) == sorted(COMPONENTS)
    responses = [{'component_templates': [{'name': n, 'component_template': b}]} for n, b in pasted.items()]
    # A GET response inside a GET response; an escaped string; a list of the responses as JSON text.
    assert parse_component_templates([{'component_templates': responses}]) == pasted
    assert parse_component_templates([json.dumps(kibana(_components()))]) == pasted
    assert parse_component_templates([json.dumps(responses)]) == pasted


SIBLING = {'name': 'ecs-sibling-v1', 'indexName': 'ecs-sibling-v1', 'fields': [
    {'fldName': n, 'nativeType': ELASTIC[ecs.plan_type(n)]} for n in ECS_FIELDS] + [
    {'fldName': n, 'nativeType': t} for n, t in (('StreamId', 'long'), ('EventId', 'long'), ('@timestamp', 'date'))]}
SIBLING_EVENTS = {'path_population': {'EventTime/TimeCreated': 100.0, 'EventDetail/Data': 100.0},
                  'data_names': {'EventDetail/Data': {n.replace('.', '_'): 100.0 for n in ECS_FIELDS}}}


def read_by_stroom(example: dict, components: dict[str, dict], refused: dict[str, str] = None) -> dict:
    """Stroom's reply to POST elasticIndex/v1/templates (dev/stroom-requests/elastic-index-templates.md)."""
    refused = refused or {}
    return {'indexName': 'ecs-sibling-v1', 'resolvedName': 'ecs-sibling-v1-000002',
            'indexTemplate': {'status': 'OK', 'name': 'ecs-sibling-v1', 'body': _dated(example), 'overlapping': []},
            'componentTemplates': [
                {'name': n, 'status': 'FORBIDDEN', 'message': refused[n]} if n in refused else
                {'name': n, 'status': 'OK', 'body': _dated(components[n])} if n in components else
                {'name': n, 'status': 'NOT_FOUND', 'message': f"Component template '{n}' was not found"}
                for n in example.get('composed_of') or []]}


async def _follow(monkeypatch, read, then=None):
    """draft_index_mapping like_index=, confirmed in its form, and the template proposed with nothing pasted, as the
    agent calls them; read: Stroom's answer to the templates request, or the error it raises. (asked, drafted, body,
    notes, posted), body None when the draft stopped for components (then: the call to make before proposing)."""
    from tests.test_kept_example import Store
    store = Store()
    kept_example = indexing._kept_example
    posted = []
    with harness(monkeypatch, []) as (context, saved):
        stroom = context.lifespan_context['stroom']

        async def get_doc(doc_type, uuid):
            return SIBLING if doc_type == 'ElasticIndex' else await store.get_doc(doc_type, uuid) \
                if doc_type == 'Documentation' else dict(saved)
        stroom.get_doc = AsyncMock(side_effect=get_doc)

        async def put_doc(d):
            return await store.put_doc(d) if d.get('uuid', '').startswith('d') else saved.update(d) or d
        stroom.put_doc = put_doc

        async def post(path, body):
            posted.append((path, body))
            if isinstance(read, Exception):
                raise read
            return read
        stroom.post = post
        guard = SimpleNamespace(check_managed=AsyncMock(), folder_contents=store.folder_contents,
                                create_filled=store.create_filled)
        with patch('tools.indexing.require_events', AsyncMock()), \
                patch('tools.indexing.summarise_events', AsyncMock(return_value=SIBLING_EVENTS)), \
                patch('tools.indexing._build_of_stream', AsyncMock(return_value='b1')), \
                patch('tools.plan.build_of', AsyncMock(return_value='b1')), \
                patch('tools.indexing.guard_from', return_value=guard), \
                patch('tools.indexing.set_body_text', lambda d, text: d.__setitem__('data', text)), \
                patch('tools.indexing._kept_example', kept_example):
            asked = await indexing.draft_index_mapping(context, 'elasticsearch', INDEX, like_index='u1',
                                                       events_stream_ids=[8])
            assert asked['status'] == 'needs_confirmation', asked
            # The user confirms what was read through Stroom, and what wasn't, in a short form.
            assert len(form_message(asked['summary'], asked['details'])) <= 600
            drafted = await indexing.draft_index_mapping(context, 'elasticsearch', INDEX, like_index='u1',
                                                         events_stream_ids=[8], confirmation_id=asked['confirmation_id'])
            if drafted.get('status') == 'needs_components':
                if then is None:
                    return asked, drafted, None, [], posted
                drafted = await indexing.draft_index_mapping(context, 'elasticsearch', INDEX, like_index='u1',
                                                             events_stream_ids=[8], **then)
            plan = FieldPlan.model_validate(drafted['plan'])
            assert {f.name for f in plan.fields} >= set(ECS_FIELDS)
            with patch('tools.indexing._documents', AsyncMock(return_value=_plan_docs(plan))):
                # Proposed with nothing pasted (seen in testing: the user was asked to paste a template anyway).
                body, notes = await _agreed(context, saved, plan)
    return asked, drafted, body, notes, posted


# 12. Following an existing index in Stroom (like_index), on a Stroom that can't read templates: its fields.
async def test_following_an_existing_index_in_stroom(monkeypatch):
    asked, _, body, notes, posted = await _follow(monkeypatch, ToolError('Stroom rejected the request (404): Not Found'))
    assert posted[0] == ('/elasticIndex/v1/templates', {'indexDocRef': {'type': 'ElasticIndex', 'uuid': 'u1',
                                                                        'name': 'ecs-sibling-v1'}})
    assert '50 fields' in asked['details']['read through Stroom']
    # Following an index isn't opting for ECS field mapping: its fields and types, mapped here.
    assert 'composed_of' not in body and set(ECS_FIELDS) <= set(_properties(body)) and 'priority' not in body
    assert any("read through Stroom from Elastic Index doc 'ecs-sibling-v1'" in n and "aren't readable" in n
               for n in notes)


# 13. The same, where Stroom reads the index template its indices take and its component templates (asked for by the
# user: getting them from Kibana to paste was cumbersome).
async def test_following_an_index_whose_template_stroom_reads(monkeypatch):
    example = legacy_example(ecs_base=True)
    asked, drafted, body, notes, _ = await _follow(monkeypatch, read_by_stroom(example, _components()))
    assert asked['details']['read through Stroom'] == ('its index template ecs-sibling-v1, composed of '
                                                       'ecs@mappings, org-ecs-base, org-users, org-ilm, org-dynamic')
    # As if the user had pasted them: the example's composition (ecs@mappings, as it references it), settings and
    # overrides, and nothing asked for or sent again.
    assert body['composed_of'] == ['ecs@mappings', *COMPONENTS] and body['priority'] == 300
    assert body['template']['mappings']['dynamic'] is True and ecs_duplicates(body) == []
    assert _properties(body)['host.name'] == {'type': 'keyword', 'ignore_above': 256}
    assert body['template']['settings']['index']['number_of_shards'] == 2
    assert any('index template and component templates' in n for n in notes)


async def test_a_component_stroom_isnt_allowed_to_read_is_asked_for_alone(monkeypatch):
    # The user warned: reading templates takes Elasticsearch privileges the cluster's credentials may lack.
    refused = {'org-users': 'action [cluster:monitor/component_template/get] is unauthorized for user [stroom]'}
    example = legacy_example(ecs_base=True)
    asked, drafted, body, _, _ = await _follow(monkeypatch, read_by_stroom(example, _components(), refused))
    assert 'org-users' in asked['details']['not readable through Stroom']
    assert drafted['status'] == 'needs_components' and drafted['missing'] == ['org-users']
    assert drafted['request'] == 'GET _component_template/org-users' and 'is unauthorized' in drafted['hint']
    # Pasted, alone: the others aren't asked again, and the template is agreed as the example has it.
    pasted = kibana({'org-users': _components()['org-users']})
    _, _, body, _, _ = await _follow(monkeypatch, read_by_stroom(example, _components(), refused),
                                     then={'component_templates': pasted})
    assert body['composed_of'] == ['ecs@mappings', *COMPONENTS]


async def test_a_template_stroom_isnt_allowed_to_read_falls_back_to_the_index_fields(monkeypatch):
    read = {'indexName': 'ecs-sibling-v1', 'resolvedName': None,
            'indexTemplate': {'status': 'FORBIDDEN', 'message': 'action [indices:admin/index_template/get] is '
                                                                 'unauthorized for user [stroom]'},
            'componentTemplates': []}
    asked, _, body, notes, _ = await _follow(monkeypatch, read)
    assert '50 fields' in asked['details']['read through Stroom']
    assert "may not read index templates" in asked['details']['its index template']
    assert 'composed_of' not in body and set(ECS_FIELDS) <= set(_properties(body))
