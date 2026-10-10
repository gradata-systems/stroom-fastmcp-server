"""Reading a generated XSLT back into a mapping (utils/xsltread): the mapping lost, or out of step with an XSLT changed
by hand. Asked for by the user, with test coverage of hand edits that add a field or stop writing one. The tool that
proves a rebuilt mapping in Stroom before saving it is covered by dev/e2e_restyle.py."""
import pytest
from lxml import etree

import tempfile
from pathlib import Path

from saxonche import PySaxonProcessor

from tests import test_extract, test_sources
from tests.test_xsltgen import (BASE, FIREWALL, LOGON, RECORDS, SCHEMA, SHARED_DEVICE, SHARED_FUNCTIONS, firewall_mapping,
                                mapping, transform)
from utils.mappingstore import normalise_xslt
from utils.xsltgen import TranslationMapping, generate
from utils.xsltread import canonical, read, rebuild

# Plain Saxon has no Stroom functions: format-date stubbed alike in both XSLTs, so they compare.
STUBS = ''.join(f'<xsl:function name="stroom:format-date">{"".join(f"<xsl:param name={chr(34)}p{n}{chr(34)}/>" for n in range(k))}'
                f'<xsl:sequence select="concat(\'D:\', {", ".join(f"$p{n}" for n in range(k))})"/></xsl:function>'
                for k in (1, 2, 3))
# Reference data and dictionaries stubbed alike in both XSLTs: a lookup finds a document of the map and key, a
# dictionary is key=value lines.
REFERENCE_STUBS = ('<xsl:function name="stroom:lookup"><xsl:param name="map"/><xsl:param name="key"/><xsl:document>'
                   '<details xmlns=""><org>{$map}:{$key}</org><phone>p-{$key}</phone></details></xsl:document></xsl:function>'
                   '<xsl:function name="stroom:dictionary"><xsl:param name="name"/><xsl:sequence select="concat($name, '
                   "'&#10;CORP\\alice = Alice A&#10;bob@corp.example=Bob B&#10;WS01 = Workstation 1&#10;ws02=Workstation 2')\"/>"
                   '</xsl:function>')
JSON_RECORD = ('<array xmlns="http://www.w3.org/2013/XSL/json"><map><string key="time">2026-09-28T10:00:00.000Z</string>'
               '<string key="action">login</string><string key="host">h</string><map key="user"><string key="name">dave'
               '</string></map><string key="result">ok</string></map></array>')
RESULT = {'ok': 'true', 'fail': 'false'}
CASES = {
    'maps': (mapping(events=[{'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': LOGON + [
        {'path': 'EventDetail/Authenticate/Data', 'data_name': 'outcome', 'field': 'result', 'map': RESULT}]},
        {'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'},
                                     {'path': 'EventDetail/Unknown/Data', 'data_name': 'kind', 'field': 'action',
                                      'map': {'login': 'Logon'}, 'default': 'Other'}]}]), RECORDS),
    # One key and no default: no steps at all, the value guarded by its key (seen in dev/e2e_shared_xslt).
    'a map of one key': (mapping(events=[{'name': 'logon', 'fields': [f for f in LOGON if 'Success' not in f['path']] + [
        {'path': 'EventDetail/Authenticate/Outcome/Success', 'field': 'result', 'map': {'ok': 'true'}}]}]), RECORDS),
    'time formats': (mapping(common=[{'path': 'EventTime/TimeCreated', 'field': 'time', 'time_format': "dd/MM/yyyy HH:mm:ss",
                                      'timezone': '+10:00'}] + BASE[1:], events=[{'name': 'logon', 'fields': LOGON + [
        {'path': 'EventDetail/Authenticate/Data', 'data_name': 'seen', 'field': 'time', 'time_format': "dd/MM/yyyy HH:mm:ss",
         'timezone': '+10:00'},
        {'path': 'EventDetail/Authenticate/Data', 'data_name': 'stamp', 'field': 'sid', 'time_format': 'epoch_ms'}]}]),
                     RECORDS),
    'dropped records': (mapping(events=[{'name': 'keepalive', 'drop': True, 'when': [{'field': 'action', 'equals': 'keepalive'}]},
                                        {'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': LOGON}]),
                        RECORDS),
    'json': (mapping(input='json', common=BASE[:4] + [{'path': 'EventSource/User/Id', 'field': 'user.name'},
                                                      {'path': 'EventSource/Device/HostName', 'field': 'host'}]), JSON_RECORD),
    'extractions': (test_extract.mapping(), test_extract.JSON_LINES),
    'key=value functions': (firewall_mapping(), FIREWALL),
    'key=value conditions': (firewall_mapping().model_copy(update={'events': [
        r.model_copy(update={'when': [r.when[0].model_copy(update={'field': 'dstintfrole',
                                                                   'equals': 'wan' if r.name == 'deny' else 'lan'})]})
        for r in firewall_mapping().events]}), FIREWALL),
    # A value map keyed by a key=value field, no transform: its key written normalize-space(...) once inlined, and
    # read as the field, not trimmed (seen in dev/e2e_restyle: read as transform 'trim').
    'a map keyed by an extracted field': (firewall_mapping().model_copy(update={'events': [
        r.model_copy(update={'fields': r.fields + [r.fields[1].model_copy(update={
            'path': r.fields[1].path.replace('Source/Device/IPAddress', 'Source/TransportProtocol'), 'field': 'proto',
            'map': {'6': 'TCP', '17': 'UDP'}, 'default': 'Other'})]}) for r in firewall_mapping().events]}), FIREWALL),
    'reference data and dictionaries': (TranslationMapping.model_validate(test_sources.MAPPING), test_sources.RECORDS),
    'named layout': (mapping(style={'layout': 'named'}), RECORDS),
    'inline layout': (mapping(style={'layout': 'inline'}), RECORDS),
}


def events(code: str, records: str) -> list[bytes]:
    if 'stroom:format-date' in code:
        code = code.replace('</xsl:stylesheet>', STUBS + '</xsl:stylesheet>')
    if 'stroom:lookup(' in code or 'stroom:dictionary(' in code:
        code = code.replace('</xsl:stylesheet>', REFERENCE_STUBS + '</xsl:stylesheet>')
    return [etree.tostring(e, method='c14n') for e in transform(code, records).findall('{event-logging:3}Event')]


def code_of(m) -> str:
    result = generate(m, SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    return result['xslt']


@pytest.mark.parametrize('case', CASES)
def test_a_lost_mapping_is_read_back_from_the_xslt_and_writes_the_same_events(case):
    m, records = CASES[case]
    code = code_of(m)
    rebuilt = rebuild(code, None, None)
    assert rebuilt.problems == [] and rebuilt.raw == [], rebuilt.summary()        # every element read as an idiom
    assert events(code_of(rebuilt.mapping), records) == events(code, records)
    # Each entry read as a field is the one written: no transform added, none lost (a lost mapping may order them
    # differently, and a rule whose condition fixes the field's value writes the value it must have, read as such).
    written = {(f.path, f.data_name): f.model_dump(exclude_defaults=True, exclude={'scope'})
               for f in m.common + [f for r in m.events for f in r.fields] if f.field and not f.data_name}
    for f in rebuilt.mapping.common + [f for r in rebuilt.mapping.events for f in r.fields]:
        if (f.path, f.data_name) in written and f.field:
            # An extraction's name is the mapping's own, lost with it: read back named after its source.
            extracted = {n for e in m.extract for n in e.names}
            drop = {'scope', 'field'} if written[(f.path, f.data_name)]['field'] in extracted else {'scope'}
            assert {k: v for k, v in f.model_dump(exclude_defaults=True).items() if k not in drop} ==                 {k: v for k, v in written[(f.path, f.data_name)].items() if k not in drop}


@pytest.mark.parametrize('case', CASES)
def test_with_the_kept_mapping_every_unchanged_entry_is_kept_and_the_xslt_regenerates_identically(case):
    m, _ = CASES[case]
    code = code_of(m)
    rebuilt = rebuild(code, m, code)
    assert rebuilt.new == [] and rebuilt.removed == [] and rebuilt.reused > 0
    assert normalise_xslt(code_of(rebuilt.mapping)) == normalise_xslt(code)


def test_what_the_xslt_is_read_as():
    code = code_of(mapping())
    reading = read(code)
    logon, other = reading.rules
    assert (logon.name, other.name) == ('logon', 'other') and canonical(logon.test) == "data[@name='action']/@value = 'login'"
    leaves = {(leaf.path, leaf.data_name): leaf for leaf in logon.leaves}
    assert leaves[('EventSource/System/Name', None)].value == 'Acme VPN'          # through the shared template
    assert canonical(leaves[('EventDetail/Authenticate/Data', 'session')].expr) == "data[@name='sid']/@value"
    assert other.test is None and reading.namespace == 'records:2'


HAND_EDITS = {
    # A new field: a Data element written when its value is there.
    'a Data element added': (
        lambda code: code.replace('</Authenticate>', '<xsl:if test="normalize-space(data[@name=\'host\']/@value)">'
                                  '<Data Name="host_seen" Value="{data[@name=\'host\']/@value}"/></xsl:if></Authenticate>', 1),
        ['rule logon: EventDetail/Authenticate/Data Data host_seen'], []),
    # An element no longer written: the user in EventSource, for every rule.
    'an element removed': (
        lambda code: code.replace(code[code.index('<xsl:if test="normalize-space(data[@name=\'user\']/@value)">\n        <User>'):
                                       code.index('</User>', code.index('<EventSource>')) + len('</User>\n      </xsl:if>')], '', 1),
        [], ['rule logon: EventSource/User/Id', 'rule other: EventSource/User/Id']),
    'a constant changed': (lambda code: code.replace('<Action>Logon</Action>', '<Action>Logoff</Action>', 1),
                           ['rule logon: EventDetail/Authenticate/Action'], []),
    'a condition changed': (
        lambda code: code.replace("data[@name='action']/@value = 'login'", "data[@name='action']/@value = ('login', 'keepalive')", 1),
        [], []),
}


@pytest.mark.parametrize('edit', HAND_EDITS)
def test_a_hand_edit_is_carried_into_the_mapping_and_the_regenerated_xslt_writes_what_the_edit_did(edit):
    m = mapping()
    code = code_of(m)
    change, new, removed = HAND_EDITS[edit]
    edited = change(code)
    assert edited != code
    rebuilt = rebuild(edited, m, code)
    assert rebuilt.new == new and rebuilt.removed == removed and rebuilt.problems == []
    regenerated = code_of(rebuilt.mapping)
    assert events(regenerated, RECORDS) == events(edited, RECORDS)
    if edit == 'a condition changed':
        assert rebuilt.mapping.events[0].when[0].one_of == ['login', 'keepalive']      # read back as the idiom
        assert len(events(edited, RECORDS)) == 2 and b'Logon' in events(edited, RECORDS)[1]
    # And it is now in step: read against itself, nothing is new.
    again = rebuild(regenerated, rebuilt.mapping, regenerated)
    assert again.new == [] and again.removed == []


def test_an_edit_a_mapping_cannot_express_shows_in_the_events():
    # A Data element written even when its value is empty: the mapping writes Data only with a value, so the
    # regenerated XSLT differs on the record without a host. rebuild_mapping's proof in Stroom finds exactly this and
    # saves nothing unless the user accepts it.
    m = mapping()
    code = code_of(m)
    edited = code.replace('</Authenticate>', '<Data Name="host_seen" Value="{data[@name=\'host\']/@value}"/></Authenticate>', 1)
    rebuilt = rebuild(edited, m, code)
    assert rebuilt.new == ['rule logon: EventDetail/Authenticate/Data Data host_seen']
    hand, mapped = events(edited, RECORDS), events(code_of(rebuilt.mapping), RECORDS)
    assert hand != mapped and b'Name="host_seen" Value=""' in hand[0] and b'host_seen' not in mapped[0]


async def test_the_proof_runs_on_the_pipelines_original_samples_else_its_feeds_newest_streams():
    # Asked for by the user: prove on the original sample data, without the agent having to find it.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from tools import rebuild as tool

    def filter_on(*terms):
        return {'id': 7, 'queryData': {'expression': {'type': 'operator', 'op': 'AND', 'children': list(terms)}}}
    ids = [{'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': v} for v in ('101', '102')]
    feed = {'type': 'term', 'field': 'Feed', 'condition': 'EQUALS', 'value': 'ACME'}
    metas = {101: 'Raw Events', 102: 'Raw Events', 900: 'Raw Events', 901: 'Raw Events'}

    def stroom_with(filters, gone=()):
        async def find_meta(terms, limit, op='AND'):
            if terms[0]['field'] == 'Id':
                wanted = [int(t['value']) for t in terms]
                return {'values': [{'meta': {'id': i, 'typeName': metas[i]}} for i in wanted if i not in gone]}
            return {'values': [{'meta': {'id': i, 'feedName': 'ACME'}} for i in (901, 900)]}
        return SimpleNamespace(processor_filters=AsyncMock(return_value=filters), find_meta=find_meta,
                               get_doc=AsyncMock(return_value={'name': 'ACME-Events'}))
    guard = SimpleNamespace(tags=AsyncMock(return_value=['mcp-build-acme']),
                            folder_contents=AsyncMock(return_value=[{'type': 'Feed', 'name': 'ACME'}]))
    cases = [([filter_on(*ids)], (), [101, 102], 'original sample streams'),
             ([filter_on(*ids), filter_on(feed)], (101, 102), [901, 900], "feed 'ACME' (its sample streams are gone)"),
             ([filter_on(feed)], (), [901, 900], "feed 'ACME' (its sample processor filters are gone)"),
             ([], (), [901, 900], "feed 'ACME' (it has no sample processor filters: only ever stepped)")]
    for filters, gone, expected, said in cases:
        with patch.object(tool, 'gateway_from', lambda ctx, s=stroom_with(filters, gone): s), \
                patch.object(tool, 'guard_from', lambda ctx: guard):
            found, which = await tool.sample_streams(None, 'p-1')
        assert found == expected and said in which, (found, which)


# Reference data lookups and dictionaries, each form the generator writes: read back as the entry that wrote it.
LOOKUPS = {
    'lookup, below the value': {'path': 'EventSource/User/UserDetails/Organisation',
                                'lookup': {'map': 'USER_TO_ORG', 'field': 'user', 'path': 'org'}},
    'lookup, the whole value': {'path': 'EventSource/User/UserDetails/Phone', 'lookup': {'map': 'USER_TO_PHONE', 'field': 'user'}},
    'lookup, an xpath key': {'path': 'EventSource/User/UserDetails/Phone',
                             'lookup': {'map': 'HOST_PHONE', 'xpath': "upper-case(data[@name='hostname']/@value)",
                                        'path': 'details/phone'}},
    'lookup into Data': {'path': 'EventSource/Device/Data', 'data_name': 'site', 'lookup': {'map': 'HOST_SITE', 'field': 'hostname'}},
    'dictionary, with a default': {'path': 'EventSource/User/Name', 'field': 'user', 'dictionary': 'User names', 'default': 'unknown'},
    'dictionary, the first of fields': {'path': 'EventSource/Device/Name', 'any_of': ['hostname', 'host'], 'dictionary': 'Host names'},
}


@pytest.mark.parametrize('form', LOOKUPS)
def test_reference_data_lookups_and_dictionaries_are_read_back_as_the_entries_that_wrote_them(form):
    entry = LOOKUPS[form]
    plain = [e for e in test_sources.MAPPING['common'] if 'lookup' not in e and 'dictionary' not in e]
    m = TranslationMapping.model_validate({**test_sources.MAPPING, 'common': plain + [entry]})
    code = code_of(m)
    rebuilt = rebuild(code, None, None)
    assert rebuilt.problems == [] and rebuilt.raw == [], rebuilt.summary()
    # Data is read into each rule (a lost mapping keeps it there), anything else into common.
    found = [e.model_dump(exclude_defaults=True) for e in rebuilt.mapping.common + [f for r in rebuilt.mapping.events for f in r.fields]
             if (e.path, e.data_name) == (entry['path'], entry.get('data_name'))]
    assert found and all(f == entry for f in found)
    assert events(code_of(rebuilt.mapping), test_sources.RECORDS) == events(code, test_sources.RECORDS)


def imported_events(code: str, imported: dict[str, str]) -> list[bytes]:
    """Run with the imported XSLTs beside it, as Stroom resolves an import by name."""
    with tempfile.TemporaryDirectory() as folder:
        for name, text in imported.items():
            (Path(folder) / name).write_text(text, encoding='utf-8')
        main = Path(folder) / 'main.xsl'
        main.write_text(code, encoding='utf-8')
        with PySaxonProcessor(license=False) as proc:
            out = proc.new_xslt30_processor().compile_stylesheet(stylesheet_file=str(main)).transform_to_string(
                xdm_node=proc.parse_xml(xml_text=RECORDS))
    return [etree.tostring(e, method='c14n') for e in etree.fromstring(out.encode()).findall('{event-logging:3}Event')]


SHARED_DEVICE_OF_HOST = SHARED_DEVICE.replace(
    '<Device><HostName>from-meta</HostName></Device>',
    '<xsl:param name="host"/><Device><HostName><xsl:value-of select="$host"/></HostName></Device>')
WITHOUT_DEVICE = [e for e in BASE if not e['path'].startswith('EventSource/Device')]
IMPORTS = {
    'a shared template': (mapping(common=WITHOUT_DEVICE, shared=[
        {'href': 'Common-Event-V1', 'template': 'eventSourceDevice', 'at': 'EventSource/Device'}]),
                          {'Common-Event-V1': SHARED_DEVICE}),
    'a shared template with parameters': (mapping(common=WITHOUT_DEVICE, shared=[
        {'href': 'Common-Event-V1', 'template': 'eventSourceDevice', 'at': 'EventSource/Device',
         'with_params': {'host': "data[@name='host']/@value"}}]), {'Common-Event-V1': SHARED_DEVICE_OF_HOST}),
    'shared functions': (mapping(events=[
        {'name': 'logon', 'when': [{'field': 'action', 'equals': 'login'}], 'fields': LOGON + [
            {'path': 'EventDetail/Authenticate/Data', 'data_name': 'loud', 'xpath': "gs:shout(data[@name='user']/@value)"}]},
        {'name': 'other', 'fields': [{'path': 'EventDetail/TypeId', 'field': 'action'},
                                     {'path': 'EventDetail/Unknown/Data', 'data_name': 'action', 'field': 'action'}]}],
        functions=[{'href': 'Common Functions', 'prefix': 'gs', 'namespace': 'urn:gs'}]), {'Common Functions': SHARED_FUNCTIONS}),
}


@pytest.mark.parametrize('case', IMPORTS)
def test_imports_are_read_back_from_the_xslt_and_the_xslts_it_imports(case):
    m, imported = IMPORTS[case]
    code = code_of(m)
    rebuilt = rebuild(code, None, None, imported)
    assert rebuilt.problems == [], rebuilt.summary()
    assert [s.model_dump(exclude_defaults=True) for s in rebuilt.mapping.shared] == \
        [s.model_dump(exclude_defaults=True) for s in m.shared]
    assert rebuilt.mapping.functions == m.functions
    assert normalise_xslt(code_of(rebuilt.mapping)) == normalise_xslt(code)
    assert imported_events(code_of(rebuilt.mapping), imported) == imported_events(code, imported)
    # With the mapping kept, its imports are kept as they are.
    kept = rebuild(code, m, code)
    assert kept.mapping.shared == m.shared and kept.mapping.functions == m.functions


def test_a_shared_template_whose_xslt_cannot_be_read_is_a_problem_not_a_guess():
    # Which element it writes is only in the imported XSLT: without it the mapping can't say where the template goes.
    m, _ = IMPORTS['a shared template']
    rebuilt = rebuild(code_of(m), None, None, {})
    assert any('eventSourceDevice' in p and "couldn't be read" in p for p in rebuilt.problems)
