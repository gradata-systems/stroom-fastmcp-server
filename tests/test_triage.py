from pathlib import Path

import pytest

from utils.triage import ErrorRules, Rule, from_stored_error, marker, normalise, triage

RULES = ErrorRules.load(Path(__file__).parents[1] / 'error_rules.yaml')


def m(severity, element, message, record=None):
    return {**marker(severity, element, message), 'record': record}


@pytest.mark.parametrize('severity, element, own, expected', [
    ('ERROR', 'schemaFilter', False, 'blocking'),
    ('FATAL', 'decorationFilter', False, 'blocking'),
    ('ERROR', 'translationFilter', True, 'blocking'),
    ('ERROR', 'decorationFilter', False, 'review'),
    ('WARNING', 'translationFilter', True, 'review'),
    ('WARNING', 'decorationFilter', False, 'benign'),
    ('INFO', 'decorationFilter', False, 'benign'),
])
def test_default_rules(severity, element, own, expected):
    assert RULES.classify(severity, element, own, 'x')[0] == expected


def test_environment_rules_come_first():
    rules = ErrorRules(rules=[Rule(element='decorationFilter', message='lookup', classification='benign',
                                   reason='known miss')] + RULES.rules)
    assert rules.classify('ERROR', 'decorationFilter', False, 'Failed lookup for user x') == ('benign', 'known miss')


def test_messages_that_differ_only_in_values_group_together():
    assert normalise('Failed to parse date: "abc" at index 0') == normalise('Failed to parse date: "xyz" at index 3')


def test_cascade_from_the_live_spike_is_blocking_with_the_cause_under_review():
    # A bad date: the pipeline's own XSLT warns, then schema validation fails twice.
    markers = [
        m('WARNING', 'translationFilter', 'FormatDate - Failed to parse date: "not-a-date"', 'r1'),
        m('ERROR', 'schemaFilter', "Value '' is not facet-valid with respect to pattern", 'r1'),
        m('ERROR', 'schemaFilter', "The value '' of element 'TimeCreated' is not valid.", 'r1'),
    ]
    result = triage(markers, RULES, own_elements={'translationFilter'}, record_count=3)
    assert result['verdict'] == 'blocking'
    assert result['groups_by_class'] == {'blocking': 2, 'review': 1, 'benign': 0}
    assert result['groups'][-1]['element'] == 'translationFilter'


def test_benign_group_on_every_record_is_raised_to_review():
    markers = [m('WARNING', 'decorationFilter', f'No user found for "{u}"', f'r{i}') for i, u in enumerate('abc')]
    group = triage(markers, RULES, own_elements=set(), record_count=3)['groups'][0]
    assert (group['class'], group['count'], group['records_affected']) == ('review', 3, 3)
    assert triage(markers[:2], RULES, set(), record_count=3)['groups'][0]['class'] == 'benign'


def test_stored_error_without_a_real_location():
    stored = {'type': 'storedError', 'severity': 'ERROR', 'elementId': {'id': 'schemaFilter'},
              'location': {'lineNo': -1, 'colNo': -1}, 'message': 'bad'}
    assert from_stored_error(stored) == {'severity': 'ERROR', 'element': 'schemaFilter', 'message': 'bad',
                                         'location': None}


def test_stepping_fatal_errors_are_blocking():
    # Stepping indicators say FATAL_ERROR (an XSLT that doesn't compile): the same as FATAL.
    stored = {'severity': 'FATAL_ERROR', 'elementId': {'id': 'translationFilter'}, 'message': 'XsltPool - Variable x has not been declared'}
    assert triage([from_stored_error(stored)], RULES, {'translationFilter'})['verdict'] == 'blocking'


def test_clean_when_no_markers():
    assert triage([], RULES, set())['verdict'] == 'clean'
