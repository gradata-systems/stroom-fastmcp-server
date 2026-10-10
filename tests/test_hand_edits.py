"""Edits made by hand in Stroom's editor survive the agent's next change. Asked for by the user: an indexing XSLT
generated from its plan, edited by hand to write another ECS field (http.request.body.bytes), was regenerated from the
plan at the agent's next change, and the edit was gone. Now a save whose code undoes an edit made since the server saved
the XSLT is refused, saying what the edit changed, until it is carried into the plan (or the user says to drop it)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError
from saxonche import PySaxonProcessor

from tools import translation
from utils.fieldplan import FieldPlan, PlannedField
from utils.handedit import lost, outline

FIELDS = [PlannedField(name='StreamId', type='id', source='@StreamId'),
          PlannedField(name='EventId', type='id', source='@EventId'),
          PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'),
          PlannedField(name='user.name', type='keyword', source='EventSource/User/Id'),
          PlannedField(name='url.original', type='keyword', source='EventDetail/*/Resource/URL'),
          PlannedField(name='http.request.method', type='keyword', source='EventDetail/*/Resource/HTTPMethod')]
ACTION = PlannedField(name='event.action', type='keyword', source='EventDetail/*/Action')
BYTES = PlannedField(name='http.request.body.bytes', type='long', source='EventDetail/*/Resource/InboundSize')
# The user's edit, in their own style: no xsl:if, and inside the request object the generator wrote.
METHOD = '<string key="method"><xsl:value-of select="EventDetail/*/Resource/HTTPMethod" /></string>'
EDITED_IN = METHOD + ('<map key="body"><number key="bytes"><xsl:value-of select="EventDetail/View/Resource/InboundSize"/>'
                      '</number></map>')

EVENTS = """<Events xmlns="event-logging:3"><Event StreamId="7" EventId="1">
<EventTime><TimeCreated>2026-10-11T09:00:00.000Z</TimeCreated></EventTime>
<EventSource><User><Id>alice</Id><Name>Alice Smith</Name></User></EventSource>
<EventDetail><View><Action>View</Action><Resource><URL>https://intranet/a</URL><HTTPMethod>GET</HTTPMethod>
<InboundSize>512</InboundSize></Resource></View></EventDetail></Event></Events>"""


def plan(*more: PlannedField, fields: list[PlannedField] = FIELDS) -> FieldPlan:
    return FieldPlan(backend='elasticsearch', index_name='ecs-acme-v1', time_field='@timestamp', convention='ecs',
                     fields=[*fields, *more])


def run(xslt: str) -> str:
    with PySaxonProcessor(license=False) as proc:
        exe = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=xslt)
        return exe.transform_to_string(xdm_node=proc.parse_xml(xml_text=EVENTS))


class Stroom:
    """Stroom's XSLT docs: what the server saves, and what a user changes in Stroom's editor."""
    def __init__(self):
        self.docs = {'x-1': {'type': 'XSLT', 'uuid': 'x-1', 'name': 'ACME-INDEX-XSLT', 'data': '', 'description': ''}}
        self.settings = SimpleNamespace(event_logging_version='4.1.0')

    async def get_doc(self, kind, uuid):
        return dict(self.docs[uuid])

    async def put_doc(self, doc, version=None):
        self.docs[doc['uuid']] = {**doc, 'version': f"v{len(doc['data'])}"}
        return dict(self.docs[doc['uuid']])

    def edit_by_hand(self, old: str, new: str, uuid: str = 'x-1') -> None:
        assert old in self.docs[uuid]['data']
        self.docs[uuid] = {**self.docs[uuid], 'data': self.docs[uuid]['data'].replace(old, new, 1), 'updateUser': 'jane'}


@pytest.fixture
def stroom():
    fake = Stroom()
    guard = SimpleNamespace(check_managed=AsyncMock(), tag=AsyncMock())
    with patch.object(translation, 'gateway_from', lambda ctx: fake), \
            patch.object(translation, 'guard_from', lambda ctx: guard), \
            patch.object(translation, '_checked', AsyncMock()):
        yield fake


async def save(index_plan: FieldPlan, **kw):
    return await translation.save_xslt(SimpleNamespace(), 'acme-v1', 'ACME-INDEX-XSLT', index_plan=index_plan,
                                       uuid='x-1', **kw)


async def test_a_field_added_by_hand_is_kept_until_carried_into_the_plan(stroom):
    await save(plan())
    stroom.edit_by_hand(METHOD, EDITED_IN)
    edited = stroom.docs['x-1']['data']
    # The agent's next change, from the plan as it was saved: the edit would be gone. Refused, saying what it was.
    with pytest.raises(ToolError) as e:
        await save(plan(ACTION), change='event.action, asked for by the user')
    said = str(e.value)
    assert "was edited by hand since the server saved it (by jane)" in said
    assert 'no longer writes number bytes' in said and 'no longer reads EventDetail/View/Resource/InboundSize' in said
    assert '+ <number key="bytes">' in said and 'carry it into the index plan' in said and 'discard_hand_edit' in said
    assert stroom.docs['x-1']['data'] == edited
    # Carried into the plan, in the generator's own style, the change saves, and the field is still written.
    await save(plan(ACTION, BYTES.model_copy(update={'source': 'EventDetail/View/Resource/InboundSize'})))
    out = run(stroom.docs['x-1']['data'])
    assert '<number key="bytes">512</number>' in out and '<string key="action">View</string>' in out
    # Saved since: the next change is the agent's own again.
    await save(plan(ACTION))
    assert 'bytes' not in stroom.docs['x-1']['data']


async def test_a_field_removed_or_a_source_changed_by_hand_is_kept_too(stroom):
    await save(plan())
    url = stroom.docs['x-1']['data']
    start = url.index('<xsl:if test="EventDetail/*/Resource/URL">')
    stroom.edit_by_hand(url[start:url.index('</xsl:if>', start) + len('</xsl:if>')], '')
    with pytest.raises(ToolError, match=r'writes map url again.*writes string original again'):
        await save(plan(ACTION))
    without_url = [f for f in FIELDS if f.name != 'url.original']
    await save(plan(ACTION, fields=without_url))
    assert 'Resource/URL' not in stroom.docs['x-1']['data']
    # A field's source changed by hand: the same name written, from another element.
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    with pytest.raises(ToolError, match=r'no longer reads EventSource/User/Name.*reads EventSource/User/Id again'):
        await save(plan(fields=without_url))
    named = [f.model_copy(update={'source': 'EventSource/User/Name'}) if f.name == 'user.name' else f for f in without_url]
    await save(plan(fields=named))
    assert '<string key="name">Alice Smith</string>' in run(stroom.docs['x-1']['data'])


async def test_the_user_can_say_to_drop_their_edit(stroom):
    await save(plan())
    stroom.edit_by_hand(METHOD, EDITED_IN)
    await save(plan(ACTION), discard_hand_edit=True)
    assert 'bytes' not in stroom.docs['x-1']['data']


async def test_no_edit_by_hand_no_refusal(stroom):
    # Changes the agent makes one after another are its own: only an edit since the server's last save is kept.
    await save(plan())
    await save(plan(ACTION))
    await save(plan(BYTES))
    assert 'action' not in stroom.docs['x-1']['data'] and 'bytes' in stroom.docs['x-1']['data']


async def test_an_xslt_written_by_the_agent_keeps_an_edit_made_by_hand_since(stroom):
    # No plan to regenerate from: everything the XSLT writes and reads now must still be written and read.
    hand = plan().xslt()
    await translation.save_xslt(SimpleNamespace(), 'acme-v1', 'ACME-INDEX-XSLT', code=hand, uuid='x-1')
    stroom.edit_by_hand(METHOD, EDITED_IN)
    with pytest.raises(ToolError, match='no longer writes number bytes'):
        await translation.save_xslt(SimpleNamespace(), 'acme-v1', 'ACME-INDEX-XSLT', code=hand, uuid='x-1')
    await translation.save_xslt(SimpleNamespace(), 'acme-v1', 'ACME-INDEX-XSLT', uuid='x-1',
                                code=stroom.docs['x-1']['data'].replace('<map key="url">', '<map key="url"><!-- kept -->'))


async def test_a_translation_edited_by_hand_is_kept_through_build_translation_xslt(stroom):
    from tests.test_xsltgen import SCHEMA, mapping
    from tools import generation
    from utils.xsltgen import generate
    m = mapping()
    with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)):
        await translation.update_xslt(SimpleNamespace(), 'x-1', generate(m, SCHEMA, '4.1.0')['xslt'], mapping=m)
        stroom.edit_by_hand('</Authenticate>', '<Data Name="device_class" Value="{data[@name=\'class\']/@value}"/>'
                                               '</Authenticate>')
        with pytest.raises(ToolError) as e:
            await translation.update_xslt(SimpleNamespace(), 'x-1', generate(m, SCHEMA, '4.1.0')['xslt'], mapping=m)
        assert 'no longer writes Data device_class' in str(e.value) and "rebuild_mapping uuid='x-1'" in str(e.value)
        # rebuild_mapping carries the edit into the mapping, proven on the sample: its save is not refused.
        await translation.update_xslt(SimpleNamespace(), 'x-1', generate(m, SCHEMA, '4.1.0')['xslt'], mapping=m,
                                      discard_hand_edit=True)


def test_how_an_edit_is_written_does_not_count():
    generated = plan(BYTES).xslt()
    # Brackets round a whole test, whitespace, xsl:if or not: the same fields from the same elements.
    restyled = generated.replace('test="EventDetail/*/Action"', 'test="( EventDetail/*/Action )"')
    assert outline(generated)[1] == outline(restyled)[1]
    # A template's field, read relative to its element, reads the same path.
    assert lost('<x/>', '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform"><xsl:value-of '
                        'select="EventDetail/View/Resource/URL"/></xsl:stylesheet>',
                '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform"><xsl:template match="Resource">'
                '<xsl:value-of select="URL"/></xsl:template></xsl:stylesheet>') == []


async def test_build_status_says_an_index_xslt_was_edited_by_hand(stroom):
    # Before, only a translation's hand edit was reported: an index or CEF plan's said nothing until it was undone.
    from tools import builds
    from utils.mappingstore import read_mapping
    await save(plan())

    async def drift():
        doc = stroom.docs['x-1']
        kind, payload = read_mapping(doc['description'])
        return await builds._plan_drift(SimpleNamespace(), {'kind': kind, 'payload': payload, 'xslt': doc})
    assert await drift() is None
    stroom.edit_by_hand(METHOD, EDITED_IN)
    said = await drift()
    assert said.startswith('its XSLT was edited by hand since the server saved it, and differs from what its index plan')
    assert 'carry it into the index plan' in said and '+ <number key="bytes">' in said


# A field the agent's change changes that the user edited by hand too: theirs to decide, field by field (asked for by
# the user: "keep their hand edit or overwrite it with the proposed field").
EMAIL = [f.model_copy(update={'source': 'EventSource/User/Email'}) if f.name == 'user.name' else f for f in FIELDS]


def asking(*answers: str) -> SimpleNamespace:
    """A call's context: no forms (the agent asks), or forms the user answers, in turn."""
    from utils.consent import ConsentStore
    if not answers:
        return SimpleNamespace(lifespan_context={'consent': ConsentStore(use_elicitation=False)})
    return SimpleNamespace(lifespan_context={'consent': ConsentStore()}, elicit=AsyncMock(
        side_effect=[SimpleNamespace(action='accept', data=a) for a in answers]))


async def save_in(ctx, index_plan: FieldPlan, **kw):
    return await translation.save_xslt(ctx, 'acme-v1', 'ACME-INDEX-XSLT', index_plan=index_plan, uuid='x-1', **kw)


async def test_a_field_changed_by_hand_and_by_the_agent_is_the_users_to_decide(stroom):
    ctx = asking()
    await save_in(ctx, plan())
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    edited = stroom.docs['x-1']['data']
    # The agent's change: user.name from the user's email. Asked, not refused or saved.
    asked = await save_in(ctx, plan(fields=EMAIL))
    assert asked['status'] == 'needs_guidance' and asked['saved'] is None and stroom.docs['x-1']['data'] == edited
    # First, one question: overwrite the XSLT with the change, or decide field by field (asked for by the user).
    assert "changes 1 field the edit changed too: user.name" in asked['hand_edit']['question']
    assert asked['hand_edit']['options'] == {'overwrite': 'Overwrite the XSLT with the change',
                                             'per_field': 'Decide field by field'}
    assert "hand_edit_choices={'*': 'overwrite'}" in asked['hint']
    [question] = asked['hand_edit_collisions']
    assert question['field'] == 'user.name' and question['options'] == {'keep': 'Keep my hand edit',
                                                                        'overwrite': 'Use the proposed field'}
    assert 'added EventSource/User/Name' in question['question'] and 'by jane' in question['question']
    assert 'proposes user.name (keyword) from EventSource/User/Email' in question['question']
    assert 'the plan had user.name (keyword) from EventSource/User/Id' in question['question']
    # They keep their edit: the change to it is left out, and the edit carried, before it saves.
    with pytest.raises(ToolError) as e:
        await save_in(ctx, plan(fields=EMAIL), hand_edit_choices={'user.name': 'keep'})
    assert 'The user keeps their edit of user.name' in str(e.value) and 'no longer reads EventSource/User/Name' in str(e.value)
    named = [f.model_copy(update={'source': 'EventSource/User/Name'}) if f.name == 'user.name' else f for f in FIELDS]
    await save_in(ctx, plan(ACTION, fields=named))
    assert '<string key="name">Alice Smith</string>' in run(stroom.docs['x-1']['data'])


async def test_the_user_overwrites_their_edit_with_the_proposed_field(stroom):
    ctx = asking()
    await save_in(ctx, plan())
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    assert (await save_in(ctx, plan(fields=EMAIL)))['status'] == 'needs_guidance'
    await save_in(ctx, plan(fields=EMAIL), hand_edit_choices={'user.name': 'overwrite'})
    assert 'EventSource/User/Email' in stroom.docs['x-1']['data'] and 'User/Name' not in stroom.docs['x-1']['data']


async def test_only_the_colliding_field_is_overwritten_the_rest_of_the_edit_is_kept(stroom):
    # Asked in a form the client shows: the user's answer comes back in the same call.
    await save_in(asking(), plan())
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    stroom.edit_by_hand(METHOD, EDITED_IN)
    ctx = asking('Decide field by field', 'Use the proposed field')
    with pytest.raises(ToolError) as e:
        await save_in(ctx, plan(fields=EMAIL))
    assert ctx.elicit.await_count == 2 and 'field user.name' in ctx.elicit.await_args.args[0]
    assert 'no longer writes number bytes' in str(e.value) and 'User/Name' not in str(e.value).split('What the edit')[0]
    # Answered once for the XSLT as it is: carrying the rest isn't asked again.
    await save_in(ctx, plan(BYTES.model_copy(update={'source': 'EventDetail/View/Resource/InboundSize'}), fields=EMAIL))
    assert ctx.elicit.await_count == 2
    out = run(stroom.docs['x-1']['data'])
    assert '<number key="bytes">512</number>' in out and 'Alice Smith' not in out


async def test_a_constant_changed_by_hand_and_in_the_mapping_is_asked_about(stroom):
    from tests.test_xsltgen import SCHEMA, mapping
    from tools import generation
    from utils.xsltgen import generate
    m = mapping()
    prod = m.model_copy(deep=True)
    next(e for e in prod.common if e.path == 'EventSource/System/Environment').value = 'Prod'
    ctx = asking()
    with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)):
        await translation.update_xslt(ctx, 'x-1', generate(m, SCHEMA, '4.1.0')['xslt'], mapping=m)
        stroom.edit_by_hand('<Environment>Dev</Environment>', '<Environment>Test</Environment>')
        asked = await translation.update_xslt(ctx, 'x-1', generate(prod, SCHEMA, '4.1.0')['xslt'], mapping=prod)
        [question] = asked['hand_edit_collisions']
        assert question['field'] == 'common: EventSource/System/Environment' and 'added Environment = Test' in question['question']
        # Another change, to a field the edit left alone: no question, but the edit must still be carried.
        other = m.model_copy(deep=True)
        next(e for e in other.common if e.path == 'EventSource/Generator').value = 'vpnd2'
        with pytest.raises(ToolError, match='no longer writes Environment = Test'):
            await translation.update_xslt(ctx, 'x-1', generate(other, SCHEMA, '4.1.0')['xslt'], mapping=other)
        await translation.update_xslt(ctx, 'x-1', generate(prod, SCHEMA, '4.1.0')['xslt'], mapping=prod,
                                      hand_edit_choices={'common: EventSource/System/Environment': 'overwrite'})
        assert '<Environment>Prod</Environment>' in stroom.docs['x-1']['data']


async def test_a_key_renamed_by_hand_is_linked_to_its_field(stroom):
    # Asked for by the user: a key renamed by hand (method -> verb) is still the field it was: where the agent's change
    # is to that field, the user is asked about it, not just refused.
    ctx = asking()
    await save_in(ctx, plan())
    stroom.edit_by_hand('<string key="method">', '<string key="verb">')
    upper = [f.model_copy(update={'source': 'upper-case(EventDetail/*/Resource/HTTPMethod)'})
             if f.name == 'http.request.method' else f for f in FIELDS]
    asked = await save_in(ctx, plan(fields=upper))
    [question] = asked['hand_edit_collisions']
    assert question['field'] == 'http.request.method' and 'added string verb' in question['question']
    # Kept: the change to it left out, the rename carried into the plan.
    with pytest.raises(ToolError, match='The user keeps their edit of http.request.method'):
        await save_in(ctx, plan(fields=upper), hand_edit_choices={'http.request.method': 'keep'})
    verb = [f.model_copy(update={'name': 'http.request.verb'}) if f.name == 'http.request.method' else f for f in FIELDS]
    await save_in(ctx, plan(fields=verb))
    assert '<string key="verb">GET</string>' in run(stroom.docs['x-1']['data'])


async def test_a_key_renamed_by_hand_with_no_change_to_its_field_is_carried(stroom):
    ctx = asking()
    await save_in(ctx, plan())
    stroom.edit_by_hand('<string key="method">', '<string key="verb">')
    with pytest.raises(ToolError, match=r'no longer writes string verb.*writes string method again'):
        await save_in(ctx, plan(ACTION))


async def test_the_user_overwrites_the_xslt_in_one_step(stroom):
    # Asked for by the user: one choice up front to overwrite the XSLT, rather than one a field. Every hand edit goes,
    # the fields the change changes and the rest alike.
    await save_in(asking(), plan())
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    stroom.edit_by_hand(METHOD, EDITED_IN)
    ctx = asking('Overwrite the XSLT with the change')
    await save_in(ctx, plan(fields=EMAIL))
    assert ctx.elicit.await_count == 1 and '(the edit changed 3 other things as well)' in ctx.elicit.await_args.args[0]
    code = stroom.docs['x-1']['data']
    assert 'EventSource/User/Email' in code and 'User/Name' not in code and 'bytes' not in code


async def test_overwriting_in_one_step_from_the_chat(stroom):
    ctx = asking()
    await save_in(ctx, plan())
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    stroom.edit_by_hand('EventSource/User/Id', 'EventSource/User/Name')
    assert (await save_in(ctx, plan(fields=EMAIL)))['status'] == 'needs_guidance'
    await save_in(ctx, plan(fields=EMAIL), hand_edit_choices={'*': 'overwrite'})
    assert 'EventSource/User/Email' in stroom.docs['x-1']['data']
