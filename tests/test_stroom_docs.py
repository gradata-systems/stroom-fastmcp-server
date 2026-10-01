"""Documentation docs: the body (data) is what the Stroom UI shows; documentation is a separate field."""
from utils.stroom import body_text, set_body_text


def test_the_body_is_read_first_then_the_field_earlier_versions_wrote():
    assert body_text({'data': '# Body', 'documentation': 'tab'}) == '# Body'
    assert body_text({'documentation': '# Written by 0.6.0'}) == '# Written by 0.6.0'
    assert body_text({}) == ''


def test_writing_the_body_moves_text_an_earlier_version_left_in_the_wrong_field():
    misplaced = {'documentation': '# Old', 'data': None}
    set_body_text(misplaced, '# New')
    assert misplaced == {'documentation': None, 'data': '# New'}
    # A doc with a body keeps whatever is in its other field.
    kept = {'documentation': 'Notes', 'data': '# Old'}
    set_body_text(kept, '# New')
    assert kept == {'documentation': 'Notes', 'data': '# New'}
