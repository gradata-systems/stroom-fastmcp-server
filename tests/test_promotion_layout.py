"""Promotion's layout, from testing in production: where an amended pipeline's documents would go wasn't plain (a list
in a form), and a new version went beside the old one with nothing to tell them apart (asked for by the user)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tools import builds


def stroom_with(docs: dict[str, dict], folders: dict[str, str]) -> SimpleNamespace:
    """A Stroom whose documents (by uuid) sit in the given folders."""
    async def get_doc(kind, uuid):
        return docs[uuid]

    async def find_documents(name, kinds, limit):
        return {'values': [{'docRef': {'uuid': uuid, 'name': d['name'], 'type': kinds[0]},
                            'path': folders[uuid].replace('/', ' / ')}
                           for uuid, d in docs.items() if d['name'] == name and uuid in folders]}
    return SimpleNamespace(get_doc=AsyncMock(side_effect=get_doc), find_documents=AsyncMock(side_effect=find_documents))


async def test_a_new_version_goes_in_a_version_subfolder_beside_its_predecessor():
    stroom = stroom_with({'old': {'name': 'DELINEA-AUDIT-V1.0-Events'}, 'older': {'name': 'ACME-V2-Events'}},
                         {'old': 'System/Feeds/Events/Delinea', 'older': 'System/Feeds/Events/Acme/V2'})
    docs = [{'type': 'Pipeline', 'uuid': 'new', 'name': 'DELINEA-AUDIT-V1.1-Events', 'version_of': 'old'},
            {'type': 'Pipeline', 'uuid': 'v3', 'name': 'ACME-V3-Events', 'version_of': 'older'},
            {'type': 'Pipeline', 'uuid': 'given', 'name': 'X-V9-Events', 'version_of': 'old'},
            {'type': 'Pipeline', 'uuid': 'plain', 'name': 'Brand-New-Events', 'version_of': None}]
    proposed, beside = await builds._version_folders(stroom, docs, {'given': 'System/Elsewhere'})
    assert proposed == {'new': 'System/Feeds/Events/Delinea/V1.1',
                        'v3': 'System/Feeds/Events/Acme/V3'}     # beside V2's folder, not inside it


async def test_the_layout_is_a_tree_of_the_destination_folders_with_new_ones_marked():
    stroom = stroom_with({'prod': {'name': 'ACME-V1.0-Events'}}, {'prod': 'System/Feeds/Events/Acme'})
    plan = [{'action': 'move', 'target': 'System/Feeds/Events/Acme/V1.1',
             'doc': {'type': 'Pipeline', 'name': 'ACME-V1.1-Events'}},
            {'action': 'move', 'target': 'System/Feeds/Events/Acme/V1.1',
             'doc': {'type': 'XSLT', 'name': 'ACME-V1.1-Events'}},
            {'action': 'write back', 'target': 'prod', 'doc': {'type': 'Pipeline', 'name': 'ACME-V1.0-Events'}},
            {'action': 'keep', 'target': 'left in the workspace', 'doc': {'type': 'Feed', 'name': 'ACME-MCP-TEST'}}]
    tree = await builds.promotion_tree(stroom, plan, ['System/Feeds/Events/Acme/V1.1'])
    assert tree == ('System/Feeds/Events/Acme/\n'
                    '├── V1.1/  (new)\n'
                    '│   ├── ACME-V1.1-Events  (Pipeline)\n'
                    '│   └── ACME-V1.1-Events  (XSLT)\n'
                    '└── ACME-V1.0-Events  (Pipeline: updated from its working copy, backed up first)\n\n'
                    'Left in the workspace: ACME-MCP-TEST (Feed)')


def test_a_translation_is_filed_under_what_it_writes():
    # Asked for by the user: Events, or Records for a pipeline writing Records streams. Live: STROOM_PROXY-RECEIVE-EVENTS
    # (its own stream type Raw Events) is made Records by 'Stroom Proxy Receive Events', which this server reads as
    # the records stage: so the feed's stream type can't say, and the pipeline's stage does.
    from security.guard import STAGE_ROLES
    assert STAGE_ROLES['translation'] == 'Events' and STAGE_ROLES['records'] == 'Records'


async def test_a_converter_made_before_its_pipeline_moves_to_where_the_pipeline_went():
    from security.guard import WriteGuard
    guard = WriteGuard.__new__(WriteGuard)
    moved = []
    guard._stroom = SimpleNamespace(post=AsyncMock(return_value={'uuid': 'tc'}),
                                    request=AsyncMock(side_effect=lambda *a: moved.append(a)))
    guard.folder_contents = AsyncMock(return_value=[{'type': 'TextConverter', 'uuid': 'tc', 'name': 'X', 'role': 'Events'}])
    guard.role_folder = AsyncMock(return_value={'uuid': 'rec', 'type': 'Folder', 'name': 'Records'})
    assert await guard.move_to_role('b', {'type': 'TextConverter', 'uuid': 'tc'}, 'Records')
    assert moved[0][1] == '/explorer/v2/move' and moved[0][2]['destinationFolder']['uuid'] == 'rec'
    assert not await guard.move_to_role('b', {'type': 'TextConverter', 'uuid': 'tc'}, 'Events')      # already there


def test_an_xslt_is_filed_by_what_it_writes():
    from tools.translation import xslt_role
    assert xslt_role('<Events xmlns="event-logging:3"/>') == 'Events'
    assert xslt_role('<xsl:stylesheet xpath-default-namespace="event-logging:3"><records xmlns="records:2"/>') == 'Indexing'
    assert xslt_role('<xsl:stylesheet xpath-default-namespace="records:2"><records xmlns="records:2"/>') == 'Records'
    assert xslt_role('<array xmlns="http://www.w3.org/2013/XSL/json"/>') == 'Indexing'


def test_a_versions_other_new_documents_join_its_folder():
    # Seen in e2e: the new indexing pipeline went in v2/, its index, documentation and dashboard beside v1.
    joins = builds._joining({'System/Prod/door': {'System/Prod/door/v2'}})
    assert joins == {'System/Prod/door': 'System/Prod/door/v2'}
    # A predecessor already in a version folder: the new one beside it, and documents meant for either place join it.
    assert builds._joining({'System/Prod/door/v2': {'System/Prod/door/v3'}}) == {
        'System/Prod/door/v2': 'System/Prod/door/v3', 'System/Prod/door': 'System/Prod/door/v3'}
    # Two versions meant for one place: neither is guessed.
    assert builds._joining({'System/A': {'System/A/v2', 'System/A/v3'}}) == {}
