"""Version history: a comment at the start of each XSLT and a version control block at the end of each doc, one
line (row) a build, written when the build is promoted; edits made by hand found and recorded as such."""
from datetime import datetime, timezone

from utils import versionlog, xsltversion
from utils.mappingstore import normalise_xslt

CODE = '<?xml version="1.1" encoding="UTF-8"?>\n<xsl:stylesheet version="3.0"><xsl:template match="/"/></xsl:stylesheet>'
CHANGED = CODE.replace('match="/"', 'match="/Events"')
DAY1, DAY2 = datetime(2026, 10, 10, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc)


def test_a_builds_saves_become_one_line_when_it_is_promoted():
    # Created and saved twice while worked on: no line yet, the history unchanged, the changes pending.
    description = xsltversion.with_pending('Acme events', None, 'peter', 'Created from its mapping', 'stroom-mcp 0.16.42', DAY1)
    description = xsltversion.with_pending(description, {'data': CODE}, 'peter', "Mapping changes: rule 'logon' replaced",
                                           'stroom-mcp 0.16.42, model claude-haiku-5-5 (as the agent said)', DAY1)
    assert xsltversion.carry(CHANGED, CODE) == xsltversion.strip(CHANGED) and not xsltversion.rows(CHANGED)
    assert description.startswith('Acme events') and len(xsltversion.pending_of(description)['entries']) == 2
    released = xsltversion.consolidate(CHANGED, xsltversion.pending_of(description), DAY1)
    [line] = xsltversion.rows(released)
    assert (line['version'], line['date'], line['author'], line['how']) == ('1', '2026-10-10', 'peter', 'agent')
    assert line['change'] == "Created from its mapping; Mapping changes: rule 'logon' replaced"
    assert 'model claude-haiku-5-5' in line['by']
    assert released.startswith('<?xml version="1.1" encoding="UTF-8"?>\n<!-- stroom-mcp version history\n v1 | ')
    assert xsltversion.without_pending(description) == 'Acme events'
    # The history isn't code: what the mapping generates still matches, and drift isn't reported for it.
    assert normalise_xslt(released) == normalise_xslt(CHANGED)


def test_an_edit_made_by_hand_since_the_last_line_gets_a_line_of_its_own():
    released = xsltversion.consolidate(CODE, {'base': None, 'entries': [{'author': 'peter', 'change': 'Created'}]}, DAY1)
    edited_by_hand = released.replace('match="/"', 'match="/Records"')        # in Stroom's editor, history and all
    previous = {'data': edited_by_hand, 'updateUser': 'jane', 'updateTimeMs': int(DAY2.timestamp() * 1000)}
    description = xsltversion.with_pending('', previous, 'peter', 'Rule for Checkout events', 'stroom-mcp', DAY2)
    again = xsltversion.consolidate(xsltversion.carry(CHANGED, edited_by_hand), xsltversion.pending_of(description), DAY2)
    lines = xsltversion.rows(again)
    assert [(r['version'], r['author'], r['how']) for r in lines] == [('1', 'peter', 'agent'), ('2', 'jane', 'by hand'),
                                                                       ('3', 'peter', 'agent')]
    assert lines[1]['change'] == "Changed outside the agent (Stroom's editor)" and lines[1]['date'] == '2026-10-12'
    # Unchanged by hand: no such line.
    quiet = xsltversion.with_pending('', {'data': released}, 'peter', 'x', '', DAY2)
    carried = xsltversion.carry(CHANGED, released)        # as update_xslt saves it
    assert [r['how'] for r in xsltversion.rows(xsltversion.consolidate(carried, xsltversion.pending_of(quiet)))] == ['agent', 'agent']


def test_an_xslt_written_before_the_history_is_recorded_as_such():
    previous = {'data': CODE, 'updateUser': 'admin', 'updateTimeMs': int(DAY1.timestamp() * 1000)}
    description = xsltversion.with_pending('', previous, 'peter', 'Working copy changed', '', DAY2)
    lines = xsltversion.rows(xsltversion.consolidate(CHANGED, xsltversion.pending_of(description), DAY2))
    assert [(r['author'], r['how'], r['change']) for r in lines][0] == ('admin', 'by hand',
                                                                          'Written before the agent kept a history')


def test_a_comment_holds_no_double_hyphen_or_separator():
    pending = {'base': None, 'entries': [{'author': 'p', 'change': 'a -- b | c\nd', 'by': ''}]}
    released = xsltversion.consolidate(CODE, pending, DAY1)
    assert '--' not in released.split('<!-- stroom-mcp version history')[1].split('-->')[0]
    assert xsltversion.rows(released)[0]['change'] == 'a - b / c d'


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
    assert versionlog.NONE_YET in fresh and versionlog.rows_of(versionlog.consolidate(fresh, DAY1))[0]['change'] == 'Created'


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
