"""Extraction regexes are tried on the sample: one that matches nothing says where it stops matching, and why."""
from utils.localcheck import sample_records
from utils.xpathcheck import check_extractions, where_it_stops
from utils.xsltgen import TranslationMapping

# As seen in a test environment: an en dash between the parts, a domain user with a backslash, [[AppServer]].
MESSAGE = ('Sep 27 2026 23:48:10 – User: domain.com\\Bloggs, Joe – [[AppServer]] Event: [User] Action: [Login] '
           'By User: domain.com\\joe.bloggs (Item Id: 219)')
SAMPLE = '\n'.join(
    f'<Event><System><Computer>app01</Computer></System><EventData><Data>{MESSAGE.replace("Login", action)}</Data>'
    f'</EventData></Event>' for action in ('Login', 'Logout', 'Update')) + '\n'
# Written from memory with '-', and the brackets escaped once, as XPath reads them.
DASHED = (r'^(\S+ \d+ \d+ \S+) - User: (.+?) - \[\[(\w+)\]\] Event: \[(\w+)\] Action: \[(\w+)\] '
          r'By User: (\S+) \(Item Id: (\d+)\)$')


def mapping(regex: str) -> TranslationMapping:
    return TranslationMapping.model_validate({
        'input': 'xml_fragments', 'record': 'Event',
        'extract': [{'xpath': 'EventData/Data', 'regex': regex,
                     'names': ['when', 'subject', 'server', 'kind', 'action', 'user', 'item']}],
        'common': [{'path': 'EventSource/User/Id', 'field': 'user'}],
        'events': [{'name': 'logon', 'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'}]}]})


def check(regex: str) -> tuple[list[str], list[str]]:
    m = mapping(regex)
    records, _ = sample_records(m, SAMPLE, None)
    return check_extractions(m, SAMPLE, None, records)


def test_a_regex_that_matches_nothing_says_where_it_stops_and_names_the_character():
    problems, _ = check(DASHED)
    [problem] = problems
    assert problem.startswith("extract[0] (EventData/Data): the regex matches none of the 3 sample texts, so "
                              "['when', 'subject', 'server', 'kind', 'action', 'user', 'item'] would be empty")
    assert 'It matches as far as `Sep 27 2026 23:48:10 ` and stops there' in problem
    assert 'EN DASH (U+2013)' in problem


def test_the_right_regex_passes_and_one_matching_some_is_a_warning():
    assert check(DASHED.replace(' - ', ' – ')) == ([], [])
    problems, warnings = check(DASHED.replace(' - ', ' – ').replace(r'\[(\w+)\] By', r'\[(Login|Logout)\] By'))
    assert not problems and warnings[0].startswith('extract[0] (EventData/Data): the regex matches 2 of the 3')


def test_over_escaping_and_invalid_regexes_are_explained():
    # Escaped twice: \\[ in the regex is a backslash, then a character class.
    problems, _ = check(DASHED.replace(' - ', ' – ').replace(r'\[\[', r'\\[\\['))
    assert problems and 'in a regex matches a backslash character' in problems[0]
    problems, _ = check('^(unclosed')
    assert 'not a valid XPath regular expression' in problems[0]


def test_where_it_stops_on_a_regex_that_matches_but_the_text_goes_on():
    assert where_it_stops('^Sep (\\d+)$', '', ['Sep 27 2026']).startswith('All of it matches, but the text goes on '
                                                                         'after: ` 2026`')
    assert where_it_stops('^Sep (\\d+) x', '', ['Sep 27 2026']).startswith('It matches as far as `Sep 27 ` and stops')
    assert 'drop the $' in where_it_stops('^Sep$', '', ['Sep 27'])


def kv_mapping(*keys: str) -> TranslationMapping:
    return TranslationMapping.model_validate({
        'input': 'json',
        'extract': [{'field': 'body', 'regex': f'(?:^|\s){k}="([^"]*)"', 'names': [k]} for k in keys],
        'common': [{'path': 'EventSource/Device/HostName', 'field': 'host'}],
        'events': [{'name': 'all', 'fields': [{'path': 'EventDetail/TypeId', 'field': keys[0]}]}]})


def test_every_sample_text_is_tried_not_the_first_200():
    # Seen in production: a VPN rule's keys first appeared at the 557th of 3,933 records; reading only the first 200,
    # the check said they matched nothing, and a sound mapping was refused.
    import json
    lines = [{'host': 'fw1', 'body': f'type="traffic" action="deny" n="{n}"'} for n in range(600)]
    lines[557]['body'] = 'type="event" action="negotiate" remip="10.0.0.1" vpntunnel="hq"'
    sample = '\n'.join(json.dumps(line) for line in lines)
    m = kv_mapping('action', 'vpntunnel', 'nothere')
    records, _ = sample_records(m, sample, None)
    assert len(records) == 600
    problems, warnings = check_extractions(m, sample, None, records)
    assert not any('vpntunnel' in p for p in problems)
    assert any("matches 1 of the 600 sample texts; the rest leave ['vpntunnel'] empty" in w for w in warnings)
    # A key no record has is still refused, and the reason says so, not "matches as far as ``".
    [missing] = [p for p in problems if 'nothere' in p]
    assert 'none of the 600 sample texts' in missing and 'None of these texts has `nothere="`' in missing


def test_a_key_that_is_there_says_where_its_value_stops_matching():
    texts = ['eventtime=1790 level="notice" logid="0001"']
    assert where_it_stops(r'(?:^|\s)level=([0-9]+)', '', texts) == \
        'It finds `level=`, then the regex goes on with `([0-9]+)`, the text with `"notice" logid="0001"`.'
