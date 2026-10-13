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
    # The predecessor in a role subfolder: documents given the source's folder join the version too.
    assert builds._joining({'System/Prod/door/Indexing': {'System/Prod/door/Indexing/v2'}}) == {
        'System/Prod/door/Indexing': 'System/Prod/door/Indexing/v2', 'System/Prod/door': 'System/Prod/door/Indexing/v2'}
    # Two versions meant for one place: neither is guessed.
    assert builds._joining({'System/A': {'System/A/v2', 'System/A/v3'}}) == {}


async def _review(docs, destinations, sources=(), stages=None, dashboard_searches=None, **kw):
    """promote_build's first reply (needs_review) for a build of these documents."""
    async def get_doc(kind, uuid):
        if kind == 'Dashboard':
            return {'dashboardConfig': {'components': [{'type': 'query', 'settings': {'dataSource': {
                'uuid': dashboard_searches}}}]}}
        return {'name': uuid}
    stroom = SimpleNamespace(get_doc=AsyncMock(side_effect=get_doc), find_documents=AsyncMock(return_value={'values': []}))
    guard = SimpleNamespace(resolve_folder=AsyncMock(side_effect=lambda path: ({'_path': path}, [])))
    with patch.object(builds, '_build_docs', AsyncMock(return_value=docs)), \
            patch.object(builds, 'guard_from', lambda c: guard), \
            patch('tools.plan.source_feeds', AsyncMock(return_value=list(sources))), \
            patch.object(builds, '_shape_stage', AsyncMock(side_effect=lambda s, u: (stages or {}).get(u, 'translation'))), \
            patch.object(builds, 'promotion_processing', AsyncMock(return_value=[])), \
            patch.object(builds, 'build_checks', AsyncMock(return_value=[])):
        return await builds.promote_build(SimpleNamespace(lifespan_context={'stroom': stroom}), 'b',
                                          destinations=destinations, **kw)


def _doc(kind, uuid, name, role=None):
    return {'type': kind, 'uuid': uuid, 'name': name, 'path': 'x', 'working_copy_of': None, 'version_of': None,
            'role': role}


async def test_role_subfolders_are_kept_under_each_destination_unless_the_user_says_not():
    # Asked for by the user: the build's Events and Indexing subfolders in production too, as they confirm.
    docs = [_doc('Feed', 'f', 'ACME'), _doc('Pipeline', 'ev', 'ACME-Events', 'Events'),
            _doc('XSLT', 'x', 'ACME-Events', 'Events'), _doc('Pipeline', 'ix', 'ACME-Indexing', 'Indexing')]
    shown = await _review(docs, {'Feed': 'System/Feeds/Acme', 'Pipeline': 'System/Feeds/Acme', 'XSLT': 'System/Feeds/Acme'})
    assert shown['layout'] == ('System/Feeds/Acme/\n'
                               '├── ACME  (Feed)\n'
                               '├── Events/\n'
                               '│   ├── ACME-Events  (Pipeline)\n'
                               '│   └── ACME-Events  (XSLT)\n'
                               '└── Indexing/\n'
                               '    └── ACME-Indexing  (Pipeline)')
    assert 'whether to keep the build\'s role subfolders' in shown['hint'] and 'role_subfolders' in shown['hint']
    flat = await _review(docs, {'Feed': 'System/Feeds/Acme', 'Pipeline': 'System/Feeds/Acme', 'XSLT': 'System/Feeds/Acme'},
                         role_subfolders=False)
    assert 'Events/' not in flat['layout'] and 'Indexing/' not in flat['layout']
    # A folder given for one document is where it goes, as given.
    given = await _review(docs, {'Feed': 'System/Feeds/Acme', 'Pipeline': 'System/Feeds/Acme', 'XSLT': 'System/Feeds/Acme',
                                 'ix': 'System/Elastic Indices/Acme'})
    assert 'Elastic Indices/\n    └── Acme/\n        └── ACME-Indexing  (Pipeline)' in given['layout']


async def test_a_build_on_an_existing_feed_leaves_its_test_feed_in_the_workspace():
    # Seen in VS Code: onboarding from a feed already in Stroom, the agent copied records into a test feed (named like
    # any other), and promotion would have made it production with the pipeline's filter selecting it.
    docs = [_doc('Feed', 'f', 'ACME-COPY'), _doc('Pipeline', 'ev', 'ACME-Events', 'Events')]
    shown = await _review(docs, {'Feed': 'System/Feeds/Acme', 'Pipeline': 'System/Feeds/Acme'}, sources=['ACME'])
    assert shown['layout'].endswith('Left in the workspace: ACME-COPY (Feed)')
    # Promoted when the user gives it a folder of its own (a new version of the feed, say).
    moved = await _review(docs, {'f': 'System/Feeds/Acme', 'Pipeline': 'System/Feeds/Acme'}, sources=['ACME'])
    assert 'Left in the workspace' not in moved['layout']


async def test_a_dashboard_on_an_index_from_elsewhere_goes_with_the_builds_indexing_pipeline():
    # Seen in production: a new indexing version verified through the existing index doc; its dashboard had no place.
    docs = [_doc('Dashboard', 'dash', 'ACME-VERIFY', 'Indexing'), _doc('Pipeline', 'ix', 'ACME-V2-Indexing', 'Indexing')]
    docs[1]['version_of'] = 'v1'
    with patch.object(builds, '_version_folders', AsyncMock(return_value=({'ix': 'System/Indices/Acme/V2'},
                                                                           {'System/Indices/Acme': {'System/Indices/Acme/V2'}}))):
        shown = await _review(docs, {}, stages={'ix': 'indexing'}, dashboard_searches='prod-index', role_subfolders=False)
    assert shown['layout'] == ('System/Indices/Acme/V2/\n'
                               '├── ACME-V2-Indexing  (Pipeline)\n'
                               '└── ACME-VERIFY  (Dashboard)')


async def test_a_build_of_one_role_gets_no_role_subfolder():
    # An index documented, or a new indexing version: all of it Indexing, so a subfolder would only add a level (and
    # put a doc the user placed in a folder of their choosing one level down).
    docs = [_doc('Documentation', 'd', 'ACME-Index', 'Indexing'), _doc('Pipeline', 'ix', 'ACME-Indexing', 'Indexing')]
    shown = await _review(docs, {'Documentation': 'System/Docs', 'Pipeline': 'System/Docs'})
    assert 'Indexing/' not in shown['layout'] and 'role_subfolders' not in shown['hint']
