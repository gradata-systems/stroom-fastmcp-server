"""A Documentation doc's title is its name. Seen in production: the title said V1.2 on the doc of the V1.1 pipeline."""
from tools.builds import titled


def test_the_agents_title_gives_way_to_the_docs_name():
    assert titled('# ACME-V1.2-Events\n\n## Purpose and data\nA door.', 'ACME-V1.1-Events') == \
        '# ACME-V1.1-Events\n\n## Purpose and data\nA door.'


def test_a_doc_without_a_title_gets_one_and_sections_stay():
    assert titled('## Purpose and data\nA door.', 'ACME') == '# ACME\n\n## Purpose and data\nA door.'
    # Only the first line, and only a level-one heading: a later '# ...' line (a comment in a code block) stays.
    assert titled('Intro\n# not a title', 'ACME') == '# ACME\n\nIntro\n# not a title'
