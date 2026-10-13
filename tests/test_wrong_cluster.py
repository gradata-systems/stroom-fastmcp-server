"""A feed or document the user names that isn't in this Stroom (asked for by the user: they may be connected to the
wrong cluster, and the work must stop rather than go on with something else)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from config import Settings
from tools import feeds
from utils.stroom import DocumentNotFound, StroomGateway

API = 'http://stroom.test/api'


def settings(**extra):
    return Settings(stroom_url='http://stroom.test', stroom_api_key='k', dev_no_auth=True, host='127.0.0.1', **extra)


def test_this_stroom_is_named_by_its_cluster_name_when_it_has_one():
    assert settings(cluster_name='PROD').stroom_label == "Stroom 'PROD' (http://stroom.test)"
    assert settings().stroom_label == 'the Stroom at http://stroom.test'


@respx.mock
async def test_a_uuid_not_here_is_refused_saying_which_stroom_this_is():
    respx.get(f'{API}/feed/v1/abc').mock(return_value=httpx.Response(500, json={
        'code': 500, 'message': "Document not found: DocRef{type='Feed', uuid='abc', name='null'}"}))
    stroom = StroomGateway(settings(cluster_name='DEV'))
    with pytest.raises(DocumentNotFound, match=r"No Feed with UUID abc in Stroom 'DEV' \(http://stroom.test\)\. If the "
                                               r"user named it, this server may be connected to a different Stroom"):
        await stroom.get_doc('Feed', 'abc')
    await stroom.close()


def test_the_closest_feed_names_come_first():
    names = ['FORTIGATE-FG60F-V1.2', 'FORTIGATE-FG60F-V1.1', 'KEYCLOAK-V1.0-EVENTS', 'ACS-ADMIN-V1.0-EVENTS', 'FORTI-X']
    assert feeds.closest_feeds('Fortigate-FG60F-V1.3', names) == ['FORTIGATE-FG60F-V1.1', 'FORTIGATE-FG60F-V1.2']
    assert feeds.closest_feeds('Windows-Security', names) == []


def _ctx(choice):
    stroom = SimpleNamespace(
        settings=settings(cluster_name='PROD'),
        get=AsyncMock(return_value=None),                                      # no feed of exactly that name
        find_documents=AsyncMock(return_value={'values': []}),                 # nor in another case
        find_all_documents=AsyncMock(return_value=[{'docRef': {'type': 'Feed', 'name': n}} for n in
                                                   ('KEYCLOAK-V1.0-EVENTS', 'KEYCLOAK-V1.1-EVENTS', 'ACME')]))
    consent = SimpleNamespace(choose=AsyncMock(return_value=choice))
    return SimpleNamespace(lifespan_context={'stroom': stroom, 'consent': consent}), consent


async def test_a_feed_name_not_here_offers_the_three_closest_and_a_way_to_correct_it():
    ctx, consent = _ctx('KEYCLOAK-V1.1-EVENTS')
    assert await feeds.resolve_feed(ctx, 'Keycloak-V1.2-Events') == 'KEYCLOAK-V1.1-EVENTS'
    action, question, options = consent.choose.await_args.args[1:4]
    assert "no feed named 'Keycloak-V1.2-Events' in Stroom 'PROD'" in question
    assert options[-1] == feeds.NOT_LISTED and 'KEYCLOAK-V1.0-EVENTS' in options and len(options) <= 4
    ctx, _ = _ctx(feeds.NOT_LISTED)
    with pytest.raises(ToolError, match="ask them for the feed's exact name"):
        await feeds.resolve_feed(ctx, 'Keycloak-V1.2-Events')
    # No form here: refused, with the candidates for the agent to put to the user.
    ctx, _ = _ctx(None)
    with pytest.raises(ToolError, match="The closest here: KEYCLOAK-V1.0-EVENTS, KEYCLOAK-V1.1-EVENTS.*don't pick one"):
        await feeds.resolve_feed(ctx, 'Keycloak-V1.2-Events')
    # Nothing like it: refused outright, naming the cluster.
    with pytest.raises(ToolError, match="No feed named 'Windows-Security', nor any like it, in Stroom 'PROD'"):
        await feeds.resolve_feed(ctx, 'Windows-Security')
