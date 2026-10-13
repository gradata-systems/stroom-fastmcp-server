"""A Documentation doc's title is its name. Seen in production: the title said V1.2 on the doc of the V1.1 pipeline."""
from tools.builds import titled


def test_the_agents_title_gives_way_to_the_docs_name():
    assert titled('# ACME-V1.2-Events\n\n## Purpose and data\nA door.', 'ACME-V1.1-Events') == \
        '# ACME-V1.1-Events\n\n## Purpose and data\nA door.'


def test_a_doc_without_a_title_gets_one_and_sections_stay():
    assert titled('## Purpose and data\nA door.', 'ACME') == '# ACME\n\n## Purpose and data\nA door.'
    # Only the first line, and only a level-one heading: a later '# ...' line (a comment in a code block) stays.
    assert titled('Intro\n# not a title', 'ACME') == '# ACME\n\nIntro\n# not a title'


def test_sections_written_at_level_one_go_down_a_level():
    # Seen (Haiku, eval): '# Purpose and data' first; the survey went in a '## Purpose and data' of its own above it,
    # and as a pipeline's first line the heading would have been taken for the title.
    from tools.builds import section_levels
    given = '# ACME\n\n# Purpose and data\n\nA door.\n\n## Volumes\n\nFew.\n\n```\n# a comment\n```\n\n# Open items\n\nNone.'
    assert section_levels(given) == ('## Purpose and data\n\nA door.\n\n### Volumes\n\nFew.\n\n```\n# a comment\n```'
                                     '\n\n## Open items\n\nNone.')
    assert titled(section_levels(given), 'ACME').startswith('# ACME\n\n## Purpose and data\n\nA door.')
    # Sections already at '## ', under a title of the agent's or a level-one heading of its own: left as they are.
    for kept in ('# ACME\n\n## Purpose and data\nA door.', '## Purpose and data\n\n# Not a section\nA door.'):
        assert section_levels(kept) == kept
