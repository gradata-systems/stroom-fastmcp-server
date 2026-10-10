"""Version history: a table in each XSLT's description (its Documentation tab) and a version control block at the end
of each doc, one row a build, written when the build is promoted and previewed as Unreleased until then; edits made
by hand found and recorded as such. The XSLT's code is left alone; the server's own blocks in the description are
hidden, after a note asking people to leave them."""
from datetime import datetime, timezone

from utils import mappingstore, versionlog, xsltversion
from utils.mappingstore import normalise_xslt

CODE = '<?xml version="1.1" encoding="UTF-8"?>\n<xsl:stylesheet version="3.0"><xsl:template match="/"/></xsl:stylesheet>'
CHANGED = CODE.replace('match="/"', 'match="/Events"')
DAY1, DAY2 = datetime(2026, 10, 10, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc)


def saved(description: str, previous: dict | None, change: str, code: str, when: datetime, by: str = 'stroom-mcp') -> str:
    """The description as a save leaves it: the change pending, the history previewing it."""
    pending = xsltversion.with_pending(description, previous, 'peter', change, by, when, code=code)
    return xsltversion.preview(pending, code, when)


def test_a_builds_saves_become_one_row_when_it_is_promoted_and_are_previewed_until_then():
    first = saved('Acme events', None, 'Created from its mapping', CODE, DAY1, 'stroom-mcp 0.16.45')
    second = saved(first, {'data': CODE}, "Mapping changes: rule 'logon' replaced", CHANGED, DAY1,
                   'stroom-mcp 0.16.45, model claude-haiku-5-5 (as the agent said)')
    # Asked for by the user: what a build has done, seen before it is released, without a row a save.
    assert second.startswith('Acme events\n\n## Version history') and xsltversion.history(second) == []
    [preview] = [line for line in second.splitlines() if line.startswith('| Unreleased |')]
    assert "Created from its mapping; Mapping changes: rule 'logon' replaced" in preview
    assert len(xsltversion.pending_of(second)['entries']) == 2
    released = xsltversion.consolidate(second, CHANGED, DAY1)
    [row] = xsltversion.history(released)
    assert (row['version'], row['date'], row['author'], row['how']) == ('1', '2026-10-10', 'peter', 'agent')
    assert row['change'] == "Created from its mapping; Mapping changes: rule 'logon' replaced"
    assert 'model claude-haiku-5-5' in row['by'] and row['digest'] == xsltversion._digest(CHANGED)
    assert '| Unreleased |' not in released and xsltversion.pending_of(released) is None
    assert mappingstore.NOTE not in released          # no hidden block left: no note
    assert released.startswith('Acme events\n\n## Version history')


def test_the_servers_blocks_are_hidden_after_a_note_and_read_back_exactly():
    # Asked for by the user: the mapping (1,400 lines of JSON for one translation) buried the Documentation tab.
    payload = {'mapping': {'extract': [{'regex': '(?:^|\\s)key=--([^"]*)"', 'names': ['a--b']}]}, 'schema_version': '3.5.2'}
    described = mappingstore.with_mapping('Acme events', 'translation', payload)
    described = saved(described, None, 'Created', CODE, DAY1)
    head, hidden = described.split(mappingstore.NOTE)
    assert head.startswith('Acme events\n\n## Version history') and '| Unreleased |' in head
    assert hidden.count('<!-- stroom-mcp ') == 2 and '--' not in hidden.replace('<!--', '').replace('-->', '')
    assert mappingstore.read_mapping(described) == ('translation', payload)       # '--' back exactly
    assert mappingstore.without_block(described, None) == head.strip()
    # Written before, shown between --- markers: still read, and hidden when next written.
    shown = ('Acme events\n\n--- stroom-mcp translation mapping (generated; change the mapping and regenerate, rather '
             'than the XSLT) ---\n{"mapping": {"input": "json"}}\n--- end of stroom-mcp mapping ---')
    assert mappingstore.read_mapping(shown) == ('translation', {'mapping': {'input': 'json'}})
    rewritten = mappingstore.with_mapping(shown, 'translation', payload)
    assert '--- stroom-mcp' not in rewritten and mappingstore.read_mapping(rewritten) == ('translation', payload)


def test_an_edit_made_by_hand_since_the_last_row_gets_a_row_of_its_own():
    released = xsltversion.consolidate(saved('', None, 'Created', CODE, DAY1), CODE, DAY1)
    edited = CODE.replace('match="/"', 'match="/Records"')          # in Stroom's editor
    previous = {'data': edited, 'updateUser': 'jane', 'updateTimeMs': int(DAY2.timestamp() * 1000)}
    again = xsltversion.consolidate(saved(released, previous, 'Rule for Checkout events', CHANGED, DAY2), CHANGED, DAY2)
    rows = xsltversion.history(again)
    assert [(r['version'], r['author'], r['how']) for r in rows] == [('1', 'peter', 'agent'), ('2', 'jane', 'by hand'),
                                                                      ('3', 'peter', 'agent')]
    assert rows[1]['change'] == "Changed outside the agent (Stroom's editor)" and rows[1]['date'] == '2026-10-12'
    # Unchanged by hand: no such row.
    quiet = xsltversion.consolidate(saved(released, {'data': CODE}, 'x', CHANGED, DAY2), CHANGED, DAY2)
    assert [r['how'] for r in xsltversion.history(quiet)] == ['agent', 'agent']


def test_an_xslt_written_before_the_history_is_recorded_as_such():
    previous = {'data': CODE, 'updateUser': 'admin', 'updateTimeMs': int(DAY1.timestamp() * 1000)}
    rows = xsltversion.history(xsltversion.consolidate(saved('', previous, 'Working copy changed', CHANGED, DAY2),
                                                       CHANGED, DAY2))
    assert (rows[0]['author'], rows[0]['how'], rows[0]['change']) == ('admin', 'by hand',
                                                                      'Written before the agent kept a history')


def test_a_history_kept_in_the_code_before_is_moved_to_the_description_and_out_of_the_code():
    # 0.16.43 to 0.16.45 kept it as a comment at the start of the code, which every check comparing code had to skip.
    commented = CODE.replace('<xsl:stylesheet', '<!-- stroom-mcp version history\n v1 | 2026-10-10 | peter | agent | '
                             'Created | stroom-mcp 0.16.44 | #1234abcd\nend of version history -->\n<xsl:stylesheet')
    moved = xsltversion.adopt('Acme events', commented)
    assert [(r['version'], r['change'], r['digest']) for r in xsltversion.history(moved)] == [('1', 'Created', '1234abcd')]
    assert xsltversion.strip(commented) == CODE and normalise_xslt(commented) == normalise_xslt(CODE)
    assert xsltversion.adopt(moved, commented) == moved          # once


def test_a_row_holds_no_column_separator_or_line_break():
    pending = xsltversion.with_pending('', None, 'p', 'a -- b | c\nd', '', DAY1, code=CODE)
    assert xsltversion.history(xsltversion.consolidate(pending, CODE, DAY1))[0]['change'] == 'a -- b / c d'


async def test_a_clean_step_holds_whatever_history_the_code_carried():
    # A draft stepped clean is still clean once saved: a history comment an earlier version left in the code is not
    # code (seen in e2e, when saves wrote one).
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from tools import stepping
    commented = CODE.replace('<xsl:stylesheet', '<!-- stroom-mcp version history\n v1 | 2026-10-10 | p | agent | x |  '
                             '| #1234abcd\nend of version history -->\n<xsl:stylesheet')
    stroom = SimpleNamespace(pipeline_layers=AsyncMock(return_value=[]), get_doc=AsyncMock(return_value={'data': commented}))
    docs = [{'element': 'translationFilter', 'inherited_from_template': False, 'doc': {'type': 'XSLT', 'uuid': 'x'}}]
    with patch.object(stepping, 'translation_docs', lambda uuid, layers: docs):
        assert await stepping.code_fingerprint(stroom, 'p') == await stepping.code_fingerprint(stroom, 'p', {'translationFilter': CODE})


def test_the_docs_version_control_takes_one_row_a_build_and_keeps_an_older_change_log():
    old = '# Acme\n\n## Purpose\n\nText.\n\n## Change log\n- 2026-10-01: Created\n- 2026-10-03: Mapped CODE\n'
    first = versionlog.with_pending('# Acme\n\n## Purpose\n\nNew text.', old, 'Rule for Checkout events', 'peter (agent)',
                                    'XSLT Acme @1234abcd', DAY1)
    second = versionlog.with_pending('# Acme\n\n## Purpose\n\nNewer text.', first, 'Field mapping regenerated',
                                     'peter (agent)', 'XSLT Acme @5678abcd', DAY1)
    # While worked on: the older log's lines as rows, nothing added, the changes pending.
    assert [r['change'] for r in versionlog.rows_of(second)] == ['Created', 'Mapped CODE']
    assert '## Change log' not in second and 'Newer text.' in second and 'New text.' not in second
    assert len(versionlog.pending_of(second)) == 2
    released = versionlog.consolidate(second, DAY2)
    rows = versionlog.rows_of(released)
    assert [r['version'] for r in rows] == ['1', '2', '3']
    assert rows[-1] == {'version': '3', 'date': '2026-10-12', 'by': 'peter (agent)',
                        'change': 'Rule for Checkout events; Field mapping regenerated', 'code': 'XSLT Acme @5678abcd'}
    assert versionlog.pending_of(released) == [] and versionlog.consolidate(released) is None
    fresh = versionlog.with_pending('# New', '', 'Created', 'peter (agent)')
    assert versionlog.UNRELEASED in fresh and '| Unreleased | 2026-10-10 | peter (agent) | Created |' in fresh
    assert versionlog.rows_of(fresh) == []                     # the preview is never read back as a released row
    released = versionlog.consolidate(fresh, DAY1)
    assert versionlog.rows_of(released)[0]['change'] == 'Created' and 'Unreleased' not in released


async def test_drift_tells_a_generator_upgrade_from_an_edit_made_by_hand():
    # Seen in production after 0.16.43: build_status called a generated XSLT "edited by hand", because the
    # generator's defaults had changed, and the agent went looking for the edit.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from tests.test_xsltgen import SCHEMA, mapping
    from tools import builds, generation
    from utils.xsltgen import generate
    from utils.xsltversion import with_pending
    m = mapping()
    old = generate(mapping(style={'variables': 'top', 'data_values': 'attribute'}), SCHEMA, '4.1.0')['xslt']
    payload = {'schema_version': '4.1.0', 'mapping': m.model_dump(exclude_none=True, exclude_defaults=True)}

    async def drift(code: str, description: str):
        kept = {'payload': payload, 'xslt': {'uuid': 'x-1', 'data': code, 'description': description}}
        with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)):
            return await builds._drift(SimpleNamespace(), kept)
    saved = with_pending('', None, 'pk', 'Created', 'stroom-mcp 0.16.42', code=old)
    upgraded = await drift(old, saved)
    assert 'written by an earlier generator (stroom-mcp 0.16.42) and is unchanged since' in upgraded
    assert "build_translation_xslt uuid='x-1' (no mapping) regenerates it" in upgraded and 'by hand' not in upgraded
    edited = await drift(old.replace('<EventSource>', '<EventSource><!-- mine -->'), saved)
    assert edited.startswith('its XSLT was edited by hand since the server saved it')
    unknown = await drift(old, '')
    assert 'edited by hand, or written by an earlier version of the generator' in unknown
    assert await drift(generate(m, SCHEMA, '4.1.0')['xslt'], saved) is None


