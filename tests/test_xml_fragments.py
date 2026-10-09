"""XML fragments: several root elements (one <Event> per line), read by an XMLFragmentParser inside its wrapper."""
from pathlib import Path

import pytest
import yaml
from fastmcp.exceptions import ToolError
from lxml import etree

from tests.test_xsltgen import SCHEMA, VALIDATOR, transform
from tools import translation
from tools.pipeline_writes import swap_parser
from tools.pipelines import merge_layers
from utils.profile import XML_FRAGMENT_WRAPPER, profile, wrapper_root
from utils.survey import sample_text, split_records
from utils.xsltgen import TranslationMapping, generate

NS = {'e': 'event-logging:3'}
CASES = Path(__file__).resolve().parents[1] / 'dev' / 'eval' / 'cases'
FRAGMENTS = yaml.safe_load((CASES / '12_xml_fragments_events.yaml').read_text(encoding='utf-8'))
EMBEDDED = yaml.safe_load((CASES / '13_json_embedded_message.yaml').read_text(encoding='utf-8'))


def test_profile_recognises_fragments_and_says_how_they_are_read():
    result = profile(FRAGMENTS['sample'])
    assert (result['format'], result['record_element'], result['records'], result['namespace']) == ('xml fragments', 'Event', 3, None)
    fields = {f['field']: f for f in result['fields']}
    assert fields['TimeCreated@SystemTime']['type'].startswith('timestamp') and fields['Computer']['fill_rate'] == 100
    assert result['text_converter']['type'] == 'XML_FRAGMENT' and '&fragment;' in result['text_converter']['code']
    assert result['xslt_input']['mapping'] == {'input': 'xml_fragments', 'xml_namespace': 'records:2', 'record': 'Event'}
    assert 'replace_parser' in result['parser']
    # One element is a document; a document stays a document
    assert profile('<logs><entry><who>a</who></entry></logs>')['format'] == 'xml'


def test_survey_splits_fragments_into_records_and_writes_them_back_as_lines():
    chunk = split_records(FRAGMENTS['sample'])
    assert chunk.format == 'xml fragments' and len(chunk.records) == 3
    assert chunk.parsed[0]['EventID'] == '4624' and chunk.parsed[0]['TimeCreated@SystemTime'].startswith('2026-10-01')
    assert sample_text(chunk, chunk.records[:2]).count('<Event>') == 2
    cut = split_records(FRAGMENTS['sample'][:-40], truncated=True)
    assert len(cut.records) == 2   # the cut-off last fragment is dropped


def test_generated_xslt_reads_fragments_in_the_wrappers_namespace():
    # unmatched: skip, as Stroom's own functions (the warning's stroom:record-no()) only compile inside Stroom
    result = generate(TranslationMapping.model_validate({**FRAGMENTS['reference']['mapping'], 'unmatched': 'skip'}), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    assert 'xpath-default-namespace="records:2"' in result['xslt']
    assert '<xsl:template match="/">' in result['xslt'] and 'select="*/Event" mode="event"' in result['xslt']
    # What the XMLFragmentParser hands on: the fragments inside the wrapper, in its default namespace.
    wrapped = XML_FRAGMENT_WRAPPER.split('&fragment;')[0].split(']>')[1] + FRAGMENTS['sample'] + '</records>'
    events = transform(result['xslt'].replace("stroom:format-date(", "string(").replace(", 'yyyy-MM-dd''T''HH:mm:ss.SSSX')", ')'), wrapped)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    logon, failed, logoff = events.findall('e:Event', NS)
    assert logon.findtext('e:EventSource/e:User/e:Id', namespaces=NS) == 'alice'
    assert logon.findtext('e:EventSource/e:Client/e:IPAddress', namespaces=NS) == '10.0.0.1'
    assert failed.findtext('.//e:Authenticate/e:Outcome/e:Success', namespaces=NS) == 'false'
    assert logoff.findtext('.//e:Authenticate/e:Action', namespaces=NS) == 'Logoff'
    assert logoff.find('e:EventSource/e:Client', NS) is None
    missing = generate(TranslationMapping.model_validate({**FRAGMENTS['reference']['mapping'], 'record': None}), SCHEMA, '4.1.0')
    assert not missing['ok'] and 'xml_fragments input needs record' in missing['problems'][0]


def test_a_message_inside_json_inside_a_json_array_is_extracted_through_an_xpath():
    result = generate(TranslationMapping.model_validate({**EMBEDDED['reference']['mapping'], 'unmatched': 'skip'}), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    # Read through the guarded helper: an empty payload, or one that isn't JSON, gives nothing, not a fatal error.
    assert "analyze-string(string((mcp:json-to-xml(*[@key='payload'])/*/*[@key='msg'])[1])" in result['xslt']
    assert '<xsl:function name="mcp:json-to-xml"' in result['xslt']
    record = ('<map><string key="host">gw01</string><string key="payload">{"ts": "2026-10-01T10:00:00.000Z", '
              '"msg": "user=alice action=LOGIN src=10.0.0.1 result=failure"}</string></map>')
    xslt = result['xslt'].replace("stroom:format-date(", "string(").replace(", 'yyyy-MM-dd''T''HH:mm:ssX')", ')')
    events = transform(xslt, f'<array xmlns="http://www.w3.org/2013/XSL/json">{record}</array>')
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    [event] = events.findall('e:Event', NS)
    assert event.findtext('e:EventSource/e:User/e:Id', namespaces=NS) == 'alice'
    assert event.findtext('e:EventSource/e:Client/e:IPAddress', namespaces=NS) == '10.0.0.1'
    assert event.findtext('.//e:Authenticate/e:Outcome/e:Success', namespaces=NS) == 'false'
    assert event.findtext('e:EventDetail/e:Description', namespaces=NS) == 'user=alice action=LOGIN src=10.0.0.1 result=failure'
    # A payload that is empty, or isn't JSON, gives nothing to read: the record still goes through, no fatal error
    # (seen in a test environment as "empty sequence" fatal errors, until json-to-xml was guarded by hand).
    for payload in ('', 'not JSON at all', '{"truncated": '):
        odd = record.replace(record[record.index('{"ts"'):record.index('</string></map>')], payload)
        transform(xslt, f'<array xmlns="http://www.w3.org/2013/XSL/json">{odd}{record}</array>')


def test_the_wrapper_is_checked_and_the_parser_can_be_swapped():
    translation._check_converter('XML_FRAGMENT', XML_FRAGMENT_WRAPPER)
    with pytest.raises(ToolError, match='&fragment;'):
        translation._check_converter('XML_FRAGMENT', '<records xmlns="records:2"/>')
    layers = [{'pipelineData': {
        'elements': {'add': [{'id': 'Source', 'type': 'Source'}, {'id': 'xmlParser', 'type': 'XMLParser'},
                             {'id': 'splitFilter', 'type': 'SplitFilter'}, {'id': 'translationFilter', 'type': 'XSLTFilter'}]},
        'links': {'add': [{'from': 'Source', 'to': 'xmlParser'}, {'from': 'xmlParser', 'to': 'splitFilter'},
                          {'from': 'splitFilter', 'to': 'translationFilter'}]}}}]
    data, new_id, old_type = swap_parser(merge_layers(layers), 'XMLFragmentParser')
    assert (new_id, old_type) == ('xmlFragmentParser', 'XMLParser')
    assert data == {'elements': {'add': [{'id': 'xmlFragmentParser', 'type': 'XMLFragmentParser'}],
                                 'remove': [{'id': 'xmlParser', 'type': 'XMLParser'}]},
                    'links': {'add': [{'from': 'Source', 'to': 'xmlFragmentParser'},
                                      {'from': 'xmlFragmentParser', 'to': 'splitFilter'}],
                              'remove': [{'from': 'Source', 'to': 'xmlParser'}, {'from': 'xmlParser', 'to': 'splitFilter'}]}}
    # The child's layer applied over the template gives the swapped chain, fed from Source (seen: the new parser was
    # linked onwards only, so nothing fed it, the UI didn't show it and stepping failed).
    merged = merge_layers(layers + [{'pipelineData': data}])
    assert {e['id'] for e in merged['elements']} == {'Source', 'xmlFragmentParser', 'splitFilter', 'translationFilter'}
    from tools.pipelines import chain_order
    assert chain_order(merged['elements'], merged['links'])[:2] == ['Source', 'xmlFragmentParser']
    with pytest.raises(ToolError, match='replace_parser must be one of'):
        swap_parser(merge_layers(layers), 'XSLTFilter')
    # A template with no Source element (Event Data (XML), locally and live): the new parser is fed from a Source
    # added as Stroom's own children write it (seen: the UI showed Source linked to nothing).
    legacy = [{'pipelineData': {
        'elements': {'add': [{'id': 'xmlParser', 'type': 'XMLParser'}, {'id': 'splitFilter', 'type': 'SplitFilter'}]},
        'links': {'add': [{'from': 'xmlParser', 'to': 'splitFilter'}]}}}]
    data, _, _ = swap_parser(merge_layers(legacy), 'XMLFragmentParser')
    assert data['elements']['add'] == [{'id': 'Source', 'type': 'Source'}, {'id': 'xmlFragmentParser', 'type': 'XMLFragmentParser'}]
    assert data['links']['add'] == [{'from': 'Source', 'to': 'xmlFragmentParser'}, {'from': 'xmlFragmentParser', 'to': 'splitFilter'}]
    merged = merge_layers(legacy + [{'pipelineData': data}])
    assert chain_order(merged['elements'], merged['links'])[:3] == ['Source', 'xmlFragmentParser', 'splitFilter']
    assert etree.fromstring(XML_FRAGMENT_WRAPPER.replace('&fragment;', '<Event/>').encode()) is not None


LIVE_WRAPPER = """<?xml version="1.1" encoding="UTF-8"?>
<!DOCTYPE records [<!ENTITY fragment SYSTEM "fragment">]>
<Events
  xmlns="event-logging:3"
  xmlns:stroom="stroom"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="event-logging:3 file://event-logging-v3.4.2.xsd"
  Version="3.4.2">
  &fragment;
</Events>

<!--<records xmlns="records:2">-->
<!--&fragment;-->
<!--</records>-->
"""


def _ctx(converters: dict[str, tuple[str, str, str]]):
    """A Stroom holding these text converters: {uuid: (name, path, type and code as 'TYPE|code')}."""
    from types import SimpleNamespace

    class Stroom:
        settings = SimpleNamespace(event_logging_version='3.5.2', workspace_folder='MCP Workspace')

        async def find_documents(self, name, types, limit, offset=0):
            return {'values': [{'docRef': {'type': 'TextConverter', 'uuid': u, 'name': n}, 'path': p}
                               for u, (n, p, _) in converters.items()]
                    + [{'docRef': {'type': 'Folder', 'uuid': 'f', 'name': 'Feeds'}, 'path': 'System'}]}

        async def get_doc(self, kind, uuid):
            if uuid not in converters:
                raise ToolError('Document not found')
            kind, code = converters[uuid][2].split('|', 1)
            return {'converterType': kind, 'data': code}

    return SimpleNamespace(lifespan_context={'stroom': Stroom()})


def test_the_wrapper_root_and_its_namespace_are_read_past_comments():
    assert wrapper_root(XML_FRAGMENT_WRAPPER) == ('records', 'records:2')
    assert wrapper_root(LIVE_WRAPPER) == ('Events', 'event-logging:3')
    assert wrapper_root('<!DOCTYPE x [<!ENTITY fragment SYSTEM "fragment">]><logs>&fragment;</logs>') == ('logs', '')


async def test_fragments_follow_the_environments_own_wrapper():
    # Seen on live: every fragment wrapper is an <Events> one in event-logging:3; the server proposed records:2.
    ctx = _ctx({'w1': ('Event Logging v3.4.2 Fragments', 'System / Format Handling', f'XML_FRAGMENT|{LIVE_WRAPPER}'),
                'ds': ('CSV', 'System / Feeds', 'DATA_SPLITTER|<dataSplitter/>'),
                'own': ('Mine', 'System / MCP Workspace / b', f'XML_FRAGMENT|{XML_FRAGMENT_WRAPPER}'),
                'gone': ('Deleted', 'System', 'unused|')})
    setup = await translation.with_fragment_setup(ctx, profile(FRAGMENTS['sample']))
    # Saved as the environment's own, tidied: its commented-out older version left behind.
    from utils.profile import clean_wrapper
    assert setup['text_converter']['code'] == clean_wrapper(LIVE_WRAPPER) and '<!--' not in clean_wrapper(LIVE_WRAPPER)
    assert setup['text_converter']['environment'] == {'name': 'Event Logging v3.4.2 Fragments', 'uuid': 'w1',
                                                      'path': 'System / Format Handling'}
    assert setup['xslt_input']['mapping'] == {'input': 'xml_fragments', 'xml_namespace': 'event-logging:3', 'record': 'Event'}
    # The build's own converters (in the workspace) are not the environment's convention
    assert 'other_wrappers' not in setup['text_converter']
    # With none in the environment, the standard records:2 one
    plain = await translation.with_fragment_setup(_ctx({}), profile(FRAGMENTS['sample']))
    assert plain['text_converter']['code'] == XML_FRAGMENT_WRAPPER and plain['xslt_input']['namespace'] == 'records:2'


async def test_event_logging_fragments_take_an_events_wrapper_in_the_configured_version():
    events = ('<Event><EventTime><TimeCreated>2026-10-01T10:00:00.000Z</TimeCreated></EventTime>'
              '<EventSource><System><Name>App</Name><Environment>Prod</Environment></System></EventSource>'
              '<EventDetail><TypeId>1</TypeId></EventDetail></Event>') * 2
    profiled = profile(events)
    assert profiled['event_logging'] and profiled['text_converter']['root'] == 'Events'
    setup = await translation.with_fragment_setup(_ctx({}), profiled)
    assert 'event-logging-v3.5.2.xsd' in setup['text_converter']['code']
    assert setup['xslt_input']['namespace'] == 'event-logging:3'
    # A source's own <Event> (Windows' System, EventData) is not event-logging
    assert not profile(FRAGMENTS['sample']).get('event_logging')


def test_generated_xslt_reads_fragments_inside_an_events_wrapper():
    mapping = {**FRAGMENTS['reference']['mapping'], 'unmatched': 'skip', 'xml_namespace': 'event-logging:3'}
    result = generate(TranslationMapping.model_validate(mapping), SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    wrapped = LIVE_WRAPPER.split(']>')[1].split('&fragment;')[0] + FRAGMENTS['sample'] + '</Events>'
    events = transform(result['xslt'].replace("stroom:format-date(", "string(").replace(", 'yyyy-MM-dd''T''HH:mm:ss.SSSX')", ')'), wrapped)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    assert [e.findtext('e:EventSource/e:User/e:Id', namespaces=NS) for e in events.findall('e:Event', NS)][0] == 'alice'


async def test_the_converter_step_gives_fragments_their_wrapper_rather_than_skipping_it():
    # Seen: the plan's converter step called build_data_splitter, which said "no Data Splitter", and none was saved.
    from tools.generation import build_data_splitter
    ctx = _ctx({'w1': ('Event Logging v3.4.2 Fragments', 'System / Format Handling', f'XML_FRAGMENT|{LIVE_WRAPPER}')})
    result = await build_data_splitter(ctx, sample=FRAGMENTS['sample'])
    from utils.profile import clean_wrapper
    assert result['converter_type'] == 'XML_FRAGMENT' and result['converter'] == clean_wrapper(LIVE_WRAPPER)
    assert result['xslt_input']['mapping']['xml_namespace'] == 'event-logging:3'
    assert "xml_namespace 'event-logging:3'" in result['hint']


def test_the_environments_wrapper_is_copied_without_its_comments():
    # Live's 'Event Logging v3.4.2 Fragments' carries an older version of itself commented out (curly quotes and all)
    # and a DOCTYPE named records on an <Events> root: a Delinea build's copy had both.
    from pathlib import Path
    from utils.profile import clean_wrapper, xml_fragment_setup
    live = (Path(__file__).parent / 'fixtures' / 'live_fragment_wrapper_with_comments.xml').read_text(encoding='utf-8')
    assert '<!--' in live and '<!DOCTYPE records' in live
    clean = clean_wrapper(live)
    assert '<!--' not in clean and '“' not in clean and '<!DOCTYPE Events [' in clean and '\n\n' not in clean
    setup = xml_fragment_setup('http://schemas.microsoft.com/win/2004/08/events/event', 'Event',
                               {'code': live, 'name': 'Event Logging v3.4.2 Fragments', 'uuid': 'w', 'path': 'System'})
    assert setup['text_converter']['code'] == clean and setup['text_converter']['root'] == 'Events'
    wrapped = etree.fromstring(clean.replace('<?xml version="1.1" encoding="UTF-8"?>', '')
                               .replace('&fragment;', '<Event xmlns="x"/>').encode())
    assert etree.QName(wrapped).localname == 'Events' and len(wrapped) == 1
