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
from utils.profile import XML_FRAGMENT_WRAPPER, profile
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
    assert "analyze-string(string((json-to-xml(*[@key='payload'])/*/*[@key='msg'])[1])" in result['xslt']
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


def test_the_wrapper_is_checked_and_the_parser_can_be_swapped():
    translation._check_converter('XML_FRAGMENT', XML_FRAGMENT_WRAPPER)
    with pytest.raises(ToolError, match='&fragment;'):
        translation._check_converter('XML_FRAGMENT', '<records xmlns="records:2"/>')
    layers = [{'pipelineData': {
        'elements': {'add': [{'id': 'xmlParser', 'type': 'XMLParser'}, {'id': 'splitFilter', 'type': 'SplitFilter'},
                             {'id': 'translationFilter', 'type': 'XSLTFilter'}]},
        'links': {'add': [{'from': 'xmlParser', 'to': 'splitFilter'}, {'from': 'splitFilter', 'to': 'translationFilter'}]}}}]
    data, new_id, old_type = swap_parser(merge_layers(layers), 'XMLFragmentParser')
    assert (new_id, old_type) == ('xmlFragmentParser', 'XMLParser')
    assert data == {'elements': {'add': [{'id': 'xmlFragmentParser', 'type': 'XMLFragmentParser'}],
                                 'remove': [{'id': 'xmlParser', 'type': 'XMLParser'}]},
                    'links': {'add': [{'from': 'xmlFragmentParser', 'to': 'splitFilter'}],
                              'remove': [{'from': 'xmlParser', 'to': 'splitFilter'}]}}
    # The child's layer applied over the template gives the swapped chain.
    merged = merge_layers(layers + [{'pipelineData': data}])
    assert {e['id'] for e in merged['elements']} == {'xmlFragmentParser', 'splitFilter', 'translationFilter'}
    with pytest.raises(ToolError, match='replace_parser must be one of'):
        swap_parser(merge_layers(layers), 'XSLTFilter')
    assert etree.fromstring(XML_FRAGMENT_WRAPPER.replace('&fragment;', '<Event/>').encode()) is not None
