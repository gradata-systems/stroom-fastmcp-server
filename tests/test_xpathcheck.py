"""xpath inputs are evaluated on the sample records as the translation will see them."""
from utils.dsgen import SplitterSpec
from utils.xpathcheck import check_xpaths, json_xml
from utils.xsltgen import TranslationMapping

BASE = [{'path': 'EventSource/System/Name', 'value': 'App'}, {'path': 'EventSource/System/Environment', 'value': 'Eval'},
        {'path': 'EventSource/Generator', 'value': 'app'}, {'path': 'EventSource/Device/HostName', 'field': 'host'},
        {'path': 'EventDetail/TypeId', 'value': 'x'}]
EMBEDDED = ('[{"host": "app01", "event": "{\\"timestamp\\": \\"2026-10-01T10:00:00Z\\", \\"user\\": \\"alice\\"}"},'
            ' {"host": "app02", "event": "{\\"timestamp\\": \\"2026-10-01T10:05:00Z\\"}"}]')


def mapping(xpath: str, more=(), when=(), **extra) -> TranslationMapping:
    return TranslationMapping.model_validate({
        'input': 'json', 'json_layout': 'array', **extra,
        'common': BASE + [{'path': 'EventTime/TimeCreated', 'xpath': xpath, 'time_format': "yyyy-MM-dd'T'HH:mm:ssX"},
                          *more],
        'events': [{'name': 'any', 'when': list(when),
                    'fields': [{'path': 'EventDetail/Unknown/Data', 'data_name': 'a', 'value': 'b'}]}]})


def test_json_values_are_written_as_the_parser_does():
    assert json_xml({'a': 'x<y', 'n': 2, 'ok': True, 'z': None, 'l': [1]}) == (
        '<map><string key="a">x&lt;y</string><number key="n">2</number><boolean key="ok">true</boolean>'
        '<null key="z"/><array key="l"><number>1</number></array></map>')


def test_an_xpath_selecting_nothing_in_any_record_is_reported():
    assert check_xpaths(mapping("json-to-xml(*[@key='event'])/*/*[@key='timestamp']"), EMBEDDED) == []
    [warning] = check_xpaths(mapping('json-to-xml(event)/timestamp'), EMBEDDED)
    assert warning.startswith("xpath `json-to-xml(event)/timestamp` (used for EventTime/TimeCreated) selects nothing "
                              "in any of the 2 sample records")
    assert "json-to-xml(*[@key='field'])/*/*[@key='name']" in warning


def test_a_value_in_only_some_records_is_fine_and_conditions_are_checked():
    m = mapping("json-to-xml(*[@key='event'])/*/*[@key='timestamp']",
                more=[{'path': 'EventSource/User/Id', 'xpath': "json-to-xml(*[@key='event'])/*/*[@key='user']"}],
                when=[{'xpath': "*[@key='kind']", 'equals': 'x'}])
    [warning] = check_xpaths(m, EMBEDDED)   # user is in one record: fine; there is no kind key at all
    assert warning.startswith("xpath `*[@key='kind']` (used for [any] when)")


def test_data_splitter_records_and_what_is_not_evaluated():
    spec = SplitterSpec.model_validate({'kind': 'delimited', 'delimiter': ',', 'header': True})
    csv = 'time,user\n2026-10-01T10:00:00Z,alice\n'
    ds = TranslationMapping.model_validate({'input': 'data_splitter', 'common': BASE[:-1] + [
        {'path': 'EventTime/TimeCreated', 'xpath': "data[@name='time']/@value", 'time_format': "yyyy-MM-dd'T'HH:mm:ssX"},
        {'path': 'EventDetail/TypeId', 'xpath': "data[@name='kind']/@value"}],
        'events': [{'name': 'any', 'fields': [{'path': 'EventDetail/Unknown/Data', 'data_name': 'a', 'value': 'b'}]}]})
    [warning] = check_xpaths(ds, csv, spec)
    assert "data[@name='kind']/@value" in warning
    # Stroom functions and variables need Stroom; for_each xpaths read items: none of these are evaluated.
    assert check_xpaths(mapping("stroom:meta('Feed')"), EMBEDDED) == []
    assert check_xpaths(mapping('nothing', for_each="*[@key='items']/*"), EMBEDDED) == []


def test_a_regex_replacement_is_checked_though_it_has_dollars():
    # Seen: replace(..., '$1:$2') skipped as if $1 were a variable, so an xpath selecting nothing (a JSON field
    # named as if it were an element) passed: the MAC address it should have written was empty in every event.
    mac = "replace(upper-case(replace({}, '[.:-]', '')), '^(..)(..)(..)(..)(..)(..)$', '$1:$2:$3:$4:$5:$6')"
    sample = '[{"host": "sw1", "time": "2026-10-01T10:00:00Z", "mac": "001a.2b3c.4d5e"}]'
    more = lambda x: [{'path': 'EventSource/Device/MACAddress', 'xpath': x}]  # noqa: E731
    time = "*[@key='time']"
    [warning] = check_xpaths(mapping(time, more=more(mac.format('mac'))), sample)
    assert 'used for EventSource/Device/MACAddress' in warning and 'selects nothing' in warning
    assert check_xpaths(mapping(time, more=more(mac.format("*[@key='mac']"))), sample) == []
    # A variable outside the literals is still the XSLT's to bind: not evaluated here.
    assert check_xpaths(mapping(time, more=more("concat($prefix, *[@key='mac'])")), sample) == []
