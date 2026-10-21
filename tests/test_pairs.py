"""A mapping's pairs entry: a text field's key/value pairs, each key a field, written once for many keys.

Seen in testing: a Fortigate draft carried its body's 60 keys as 60 extract entries, a regex each, in a mapping of 50 KB
the agent had to send whole."""
import json

import pytest
from pydantic import ValidationError

from tests.test_sample_check_speed import SAMPLE, kv_mapping
from tests.test_xsltgen import SCHEMA
from utils.draftmap import draft_mapping
from utils.pairs import STYLES, key_name, pair_of, pair_regex
from utils.xsltgen import TranslationMapping, compact_pairs, compact_rules, generate


@pytest.mark.parametrize('style', STYLES)
@pytest.mark.parametrize('key', ['srcip', 'dst.port', 'Source IP', 'Item Id'])
def test_a_pairs_regex_is_read_back_as_its_style_and_key(style, key):
    assert pair_of(pair_regex(style, key)) == (style, key)


def test_a_regex_of_another_shape_is_no_pair():
    assert pair_of('^(\\S+) (\\S+)$') is None
    assert pair_of(pair_regex('equals', 'srcip') + 'x') is None


def mapping(**over) -> dict:
    return {**json.loads(kv_mapping().model_dump_json(exclude_none=True, exclude_defaults=True)), 'extract': [], **over}


def test_pairs_become_an_extract_entry_a_key():
    m = TranslationMapping.model_validate(mapping(pairs=[
        {'field': 'body', 'style': 'quoted', 'keys': ['type', 'user']},
        {'field': 'body', 'style': 'equals', 'keys': ['srcip', 'Source IP'], 'names': {'srcip': 'source'}}]))
    assert m.pairs == []
    assert [(e.field, e.regex, e.names) for e in m.extract] == [
        ('body', pair_regex('quoted', 'type'), ['type']), ('body', pair_regex('quoted', 'user'), ['user']),
        ('body', pair_regex('equals', 'srcip'), ['source']), ('body', pair_regex('equals', 'Source IP'), ['source_ip'])]


def test_pairs_in_a_text_an_extraction_gives_are_read_after_it():
    m = TranslationMapping.model_validate(mapping(
        extract=[{'field': 'msg', 'regex': '^(\\S+) (.*)$', 'names': ['host', 'rest']}],
        pairs=[{'field': 'rest', 'style': 'equals', 'keys': ['user']}, {'field': 'body', 'style': 'equals', 'keys': ['a']}]))
    assert [e.names for e in m.extract] == [['a'], ['host', 'rest'], ['user']]


def test_names_for_keys_it_lacks_are_refused():
    with pytest.raises(ValidationError, match="pairs names \\['other'\\], which aren't among its keys"):
        TranslationMapping.model_validate(mapping(pairs=[{'field': 'body', 'style': 'equals', 'keys': ['a'],
                                                          'names': {'other': 'x'}}]))


def test_the_draft_folds_its_key_value_extracts_into_pairs_and_means_the_same():
    drafted = compact_rules(draft_mapping({'fw.jsonl': SAMPLE}, 'Fortigate')['mapping'])
    folded = compact_pairs(drafted)
    assert 'extract' not in folded and {p['style'] for p in folded['pairs']} == {'quoted', 'equals'}
    assert sorted(k for p in folded['pairs'] for k in p['keys']) == sorted(
        key_name(pair_of(e['regex'])[1]) for e in drafted['extract'])
    assert len(json.dumps(folded['pairs'])) < len(json.dumps(drafted['extract'])) / 3
    entries = lambda m: sorted(e.model_dump_json() for e in TranslationMapping.model_validate(m).extract)  # noqa: E731
    assert entries(folded) == entries(drafted)


def test_extract_entries_of_other_shapes_stay_as_written():
    own = {'field': 'msg', 'regex': '^(\\S+) (.*)$', 'names': ['host', 'rest']}
    reads_derived = {'field': 'rest', 'regex': pair_regex('equals', 'user'), 'names': ['user']}
    flagged = {'field': 'body', 'regex': pair_regex('equals', 'a'), 'names': ['a'], 'flags': 'i'}
    paired = {'field': 'body', 'regex': pair_regex('equals', 'b'), 'names': ['bee']}
    folded = compact_pairs({'extract': [own, reads_derived, flagged, paired]})
    assert folded['extract'] == [own, reads_derived, flagged]
    assert folded['pairs'] == [{'field': 'body', 'style': 'equals', 'keys': ['b'], 'names': {'b': 'bee'}}]


def test_the_xslt_from_pairs_is_the_xslt_from_their_extract_entries():
    with_pairs = TranslationMapping.model_validate(mapping(pairs=[{'field': 'body', 'style': 'quoted', 'keys': ['user']}]))
    as_extract = TranslationMapping.model_validate(mapping(extract=[
        {'field': 'body', 'regex': pair_regex('quoted', 'user'), 'names': ['user']}]))
    a, b = generate(with_pairs, SCHEMA, '4.1.0'), generate(as_extract, SCHEMA, '4.1.0')
    assert a['ok'], a['problems']
    assert a['xslt'] == b['xslt']
