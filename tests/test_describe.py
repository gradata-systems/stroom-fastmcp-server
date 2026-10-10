"""describe_feed's reading of the generated documentation: a field's rows, through its source path, and how to search
it. Against Stroom: dev/e2e_describe.py."""
from tools.describe import _event_kinds, norm, rows_about, search_advice, section, similar, tables, words

INDEX_DOC = """# Acme VPN - indexing

## Purpose and data

Indexes the Acme VPN Events into ecs-vpn-v1.

## Field mapping

Documents for `ecs-vpn-v1` (elasticsearch); the time field is `@timestamp`.

| Index field | Description | Type | From (event-logging path) | In sample | Sample values |
| --- | --- | --- | --- | --- | --- |
| `User.DomainName` | The domain of the user's account, e.g. CORP. | keyword | `EventDetail/*/User/Domain` | 80% of events | `corp.example.com` |
| `User.Id` | The user. | keyword | `EventDetail/*/User/Id` | always | `bob` |
| `Note` | A bar \\| in it.<br>Two lines. | text | `EventDetail/Description` | always | `x` |

## Version control
"""

EVENTS_DOC = """# Acme VPN

## Purpose and data

Logon and logoff records from the Acme VPN concentrators, as JSON lines.

## Field mapping

| XPath | Description | From |
| --- | --- | --- |
| `EventSource/Device/HostName` | The device. | host |

### Event types

| Rule | TypeId | Description | EventDetail |
| --- | --- | --- | --- |
| **logon**<br>action = 'LOGIN' | Logon | | `Authenticate/Action` <- Logon<br>`Authenticate/User/Domain` <- realm<br>`Authenticate/User/Id` <- user |
| **logoff**<br>action = 'LOGOUT' | Logoff | | `Authenticate/User/Domain` <- realm |
"""


def test_names_compare_whatever_their_separators_and_case():
    assert norm('User.DomainName') == norm('user_domain_name') == norm('UserDomainName') == 'userdomainname'


def test_tables_and_sections_are_read_back_from_the_generated_markdown():
    assert section(EVENTS_DOC, 'Purpose and data') == 'Logon and logoff records from the Acme VPN concentrators, as JSON lines.'
    assert '### Event types' in section(EVENTS_DOC, 'Field mapping')
    first, = tables(section(INDEX_DOC, 'Field mapping'))
    assert first[0]['Index field'] == '`User.DomainName`' and first[0]['Type'] == 'keyword'
    assert first[2]['Description'] == 'A bar | in it.\nTwo lines.'       # escaped bars and <br>s undone


def test_a_fields_rows_are_found_in_the_index_doc_and_through_its_source_in_the_events_doc():
    # The user's question: "what is the User.DomainName field about?"
    index_rows = rows_about(INDEX_DOC, 'User.DomainName', [])
    assert [r['Index field'] for r in index_rows] == ['`User.DomainName`']
    assert index_rows[0]['From (event-logging path)'] == '`EventDetail/*/User/Domain`'
    # The events doc names the path without EventDetail/ in each rule's lines: both rules' lines are found, and only
    # those lines, not the rule's other fields.
    event_rows = rows_about(EVENTS_DOC, 'User.DomainName', ['EventDetail/*/User/Domain'])
    assert [r['EventDetail'] for r in event_rows] == ['`Authenticate/User/Domain` <- realm'] * 2
    assert event_rows[0]['Rule'].startswith('**logon**')
    # A path given as the field: the EventSource table's row.
    assert [r['XPath'] for r in rows_about(EVENTS_DOC, 'EventSource/Device/HostName', [])] == ['`EventSource/Device/HostName`']
    assert rows_about(INDEX_DOC, 'Nothing.Here', []) == []


def test_search_advice_follows_what_stroom_was_found_to_do_for_each_type():
    keyword = ' '.join(search_advice('elasticsearch', 'keyword'))
    assert "EQUALS '*x'" in keyword and 'STARTS_WITH and CONTAINS find nothing' in keyword and 'case-sensitive' in keyword
    assert 'CIDR' in ' '.join(search_advice('elasticsearch', 'ip'))
    assert 'keyword sub-field' in ' '.join(search_advice('elasticsearch', 'text'))
    lucene = ' '.join(search_advice('lucene', 'text', 'KEYWORD'))
    assert "EQUALS '*x'" in lucene and 'ENDS_WITH' in lucene
    assert 'ranges' in ' '.join(search_advice('lucene', 'text', 'ALPHA_NUMERIC'))
    assert 'BETWEEN' in ' '.join(search_advice('lucene', 'date'))


def test_the_event_kinds_come_from_the_kept_mapping():
    payload = {'input': 'data_splitter', 'unmatched': 'skip',
               'common': [{'path': 'EventTime/TimeCreated', 'field': 'time'}],
               'events': [{'name': 'logon', 'when': [{'field': 'action', 'equals': 'LOGIN'}],
                           'fields': [{'path': 'EventDetail/TypeId', 'value': 'Logon'},
                                      {'path': 'EventDetail/Authenticate/Action', 'value': 'Logon'}]},
                          {'name': 'noise', 'drop': True, 'when': [{'field': 'action', 'equals': 'PING'}]}]}
    kinds = _event_kinds(payload)
    assert kinds[0].startswith('logon: Authenticate (') and 'LOGIN' in kinds[0]
    assert kinds[1].startswith('noise: left untranslated')


def test_a_misremembered_field_is_answered_with_the_fields_sharing_its_words():
    assert words('User.DomainName') == words('user_domain_name') == words('userDomainName') == {'user', 'domain', 'name'}
    assert words('IPAddress') == {'ip', 'address'}
    fields = ['StreamId', 'UserId', 'AuthenticateUserId', 'UserDomain', 'HostName']
    assert similar('User.DomainName', fields) == ['UserDomain', 'UserId', 'AuthenticateUserId', 'HostName']
    assert similar('Nothing', fields) == []


def test_an_xslts_kept_mapping_is_summarised_unless_asked_for_whole():
    # Seen in production: describe_document returned a 1,400-line kept mapping; the client spilled it to a file, and
    # the agent read it in parts and sent it back whole to regenerate the XSLT.
    from tests.test_xsltgen import mapping
    from tools.explorer import _kept_summary
    from utils.mappingstore import with_mapping
    from utils.xsltversion import with_pending
    payload = {'schema_version': '4.1.0', 'mapping': mapping().model_dump(exclude_none=True, exclude_defaults=True)}
    description = with_pending(with_mapping('Acme VPN events', 'translation', payload), None, 'pk', 'Created', 'x',
                               code='<x/>')
    doc = {'description': description}
    _kept_summary(doc, 'x-1', False)
    assert doc['description'] == 'Acme VPN events' and doc['pending_changes'] == ['Created']
    summary = doc['kept_mapping']
    assert summary['rules'][0].startswith('logon: Authenticate') and summary['common_entries'] == 6
    assert summary['style'] == "the generator's current defaults"
    assert "uuid='x-1' alone regenerates it" in summary['how'] and 'never send it back whole' in summary['how']
    whole = {'description': description}
    _kept_summary(whole, 'x-1', True)
    assert whole['kept_mapping']['mapping'] == payload['mapping']
