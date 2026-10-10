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


def test_cascade_seen_on_a_live_instance_is_blocking_with_the_cause_under_review():
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


def test_a_call_to_a_function_nobody_has_points_back_to_the_mapping():
    # Seen (Gemma 4 31B): a mapping's fields given as extract(...) calls; each step said only "Fatal error".
    stored = {'severity': 'FATAL_ERROR', 'elementId': {'id': 'translationFilter'}, 'message':
              'XsltPool - Cannot find a 2-argument function named Q{http://www.w3.org/2005/xpath-functions}extract()'}
    [group] = triage([from_stored_error(stored)], RULES, {'translationFilter'})['groups']
    assert group['class'] == 'blocking' and "the mapping's extract list" in group['reason']


def test_clean_when_no_markers():
    assert triage([], RULES, set())['verdict'] == 'clean'


def test_an_elasticsearch_bulk_failure_is_split_into_the_documents_it_rejected():
    # As Stroom reported it: one FATAL message holding the whole bulk response; one of three documents rejected.
    import json
    from pathlib import Path
    from utils.triage import ErrorRules, marker, triage
    response = {'errors': True, 'items': [
        {'create': {'_id': 'a', 'status': 201, 'result': 'created'}},
        {'create': {'_id': 'b', 'status': 400, 'error': {
            'type': 'document_parsing_exception',
            'reason': '[1:117] object mapping for [user] tried to parse field [user] as object, but found a concrete value'}}},
        {'create': {'_id': 'c', 'status': 201, 'result': 'created'}}], 'took': 600}
    rules = ErrorRules.load(Path(__file__).parents[1] / 'error_rules.yaml')
    result = triage([marker('FATAL', 'elasticIndexingFilter',
                            f'Bulk indexing request failed: BulkResponse: {json.dumps(response)}')],
                    rules, {'elasticIndexingFilter'})
    [group] = result['groups']
    assert result['verdict'] == 'blocking' and group['count'] == 1
    assert group['examples'][0]['message'].startswith(
        'Elasticsearch rejected document 2 of 3 in a bulk request (document_parsing_exception): ')
    assert group['reason'].startswith('A field is an object in some records and a plain value in others')


def test_an_error_the_user_accepted_is_benign_with_their_reason_and_matches_its_kind():
    from pathlib import Path
    from utils.accepted import block, entry, merge, read_accepted
    from utils.triage import ErrorRules, marker, triage
    rules = ErrorRules.load(Path(__file__).parents[1] / 'error_rules.yaml')
    accepted = [entry('decorationFilter', 'No HR record for user svc-backup', 'Service accounts have no HR record',
                      '2026-10-04', matches='No HR record for user svc-*')]
    markers = [marker('ERROR', 'decorationFilter', 'Log - No HR record for user svc-deploy'),  # same kind, another value
               marker('ERROR', 'decorationFilter', 'Lookup failed: no map named HR')]       # another kind
    groups = {g['examples'][0]['message']: g for g in triage(markers, rules, set(), accepted=accepted)['groups']}
    known = groups['Log - No HR record for user svc-deploy']
    assert known['class'] == 'benign' and known.get('accepted') and 'Service accounts have no HR record' in known['reason']
    assert groups['Lookup failed: no map named HR']['class'] == 'review' and not groups['Lookup failed: no map named HR'].get('accepted')
    # Kept in the doc's text, and read back; a newer entry for the same kind replaces the older.
    text = f"## Errors\n\n{block(accepted)}\n"
    assert read_accepted(text) == accepted
    newer = entry('decorationFilter', 'No HR record for user svc-x', 'newer', '2026-10-05', matches='No HR record for user svc-*')
    assert [e['reason'] for e in merge(accepted, [newer])] == ['newer']

    # Without matches, the example is the kind: another user name is another error.
    exact = [entry('decorationFilter', 'No HR record for user svc-backup', 'x', '2026-10-04')]
    other = triage([marker('ERROR', 'decorationFilter', 'No HR record for user svc-deploy')], rules, set(), accepted=exact)
    assert other['groups'][0]['class'] == 'review'
