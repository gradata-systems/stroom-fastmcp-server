"""The formats sources send, as the server profiles, splits and drafts them (dev/e2e_formats.py runs them in Stroom)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dev'))

from format_samples import SAMPLES  # noqa: E402
from tests.test_xsltgen import SCHEMA  # noqa: E402
from utils.draftmap import draft_mapping  # noqa: E402
from utils.dsgen import SplitterSpec, dry_run, generate_splitter, infer_spec  # noqa: E402
from utils.profile import profile  # noqa: E402
from utils.xsltgen import TranslationMapping, generate  # noqa: E402


def records(name: str) -> list[dict]:
    spec, _ = infer_spec(SAMPLES[name])
    run = dry_run(spec, SAMPLES[name])
    assert not run['unmatched_lines']
    return run['records']


def rules(name: str, **kwargs) -> dict[str, dict]:
    return {r['name']: r for r in draft_mapping([SAMPLES[name]], **kwargs)['mapping']['events']}


def test_cef_alone_and_after_syslog_is_its_header_then_pairs_whose_values_hold_spaces():
    # Seen: CEF read as key=value, its header fields taken for keys and its values cut at the first space.
    for name in ('cef', 'syslog_cef'):
        assert profile(SAMPLES[name])['format'] == 'cef'
        first = records(name)[0]
        assert {k: first[k] for k in ('cef_vendor', 'cef_product', 'cef_signature_id', 'cef_name', 'cef_severity')} == {
            'cef_vendor': 'Acme', 'cef_product': 'Gateway', 'cef_signature_id': '100', 'cef_name': 'User logged in',
            'cef_severity': '3'}
        assert (first['suser'], first['src'], first['msg']) == ('alice', '10.0.0.5', 'Logged in from the VPN')
    assert records('syslog_cef')[0]['host'] == 'gw01' and records('cef')[0]['rt'] == 'Oct 01 2026 08:00:00 UTC'
    xml = generate_splitter(infer_spec(SAMPLES['syslog_cef'])[0])
    assert '<data name="cef_name" value="$7" />' in xml and '<group value="$9">' in xml and '<group value="$1">' in xml
    assert set(rules('cef')) >= {'login', 'logout'}      # act names the kind


def test_key_value_pairs_are_one_regex_with_one_value_group_quoted_or_not():
    # Seen in Stroom: a split on '=' with $2 (a split has none), then $2$3 (a group that took no part can't be named).
    assert records('kv_quoted')[1] == {'date': '2026-10-01', 'time': '08:05:00', 'devname': 'fw01', 'type': 'traffic',
                                       'action': 'deny', 'srcip': '203.0.113.9', 'dstip': '10.0.0.10', 'user': 'bob smith'}
    xml = generate_splitter(infer_spec(SAMPLES['kv_quoted'])[0])
    assert xml.count('<regex ') == 1 and '<data name="$1" value="$2" />' in xml and 'split delimiter="="' not in xml
    unquoted = SplitterSpec(kind='key_value')
    assert dry_run(unquoted, 'a=1 b=two c=')['records'] == [{'a': '1', 'b': 'two', 'c': ''}]


def test_a_doubled_quote_stays_doubled_as_in_stroom_and_the_draft_unescapes_it():
    assert records('csv_quoted')[1]['message'] == 'Said ""bye"" and left'
    message = next(f for f in draft_mapping([SAMPLES['csv_quoted']])['mapping']['common'] if f.get('field') == 'message')
    assert message['transform'] == 'unescape_quotes'
    from utils.xsltgen import transform_expr
    assert transform_expr('unescape_quotes', '$message[1]') == """replace($message[1], '""', '"')"""


def test_syslog_nil_values_are_left_out_and_never_name_the_kind():
    drafted = draft_mapping([SAMPLES['syslog5424_kv']])
    assert drafted['mapping']['nil_values'] == ['-']
    assert set(r['name'] for r in drafted['mapping']['events']) == {'permitted', 'denied', 'other'}  # not msgid's '-'
    permitted = next(r for r in drafted['mapping']['events'] if r['name'] == 'permitted')
    assert any(f['path'].endswith('Network/Permit/Source/Device/IPAddress') and f['field'] == 'src' for f in permitted['fields'])
    m = TranslationMapping.model_validate(drafted['mapping'])
    assert "[not(normalize-space(.) = ('-'))]" in generate(m, SCHEMA, '4.1.0')['xslt']


def test_the_draft_reads_attributes_nested_keys_windows_eventdata_and_the_agents_splitter():
    assert set(rules('xml_attributes')) == {'login', 'logout', 'other'}          # action="login" on an element
    common = draft_mapping([SAMPLES['json_lines']])['mapping']['common']
    assert {'path': 'EventSource/User/Id', 'field': 'user.name'} in common          # user.name is user_name
    windows = rules('xml_fragments_ns')
    logon = {f['path']: f for f in windows['4624']['fields']}
    assert logon['EventDetail/Authenticate/Action']['value'] == 'Logon'
    assert logon['EventDetail/Authenticate/Outcome/Success']['value'] == 'true'
    assert windows['4634']['fields'][0] == {'path': 'EventDetail/Authenticate/Action', 'value': 'Logoff'}
    named = SplitterSpec(kind='delimited', header=['time', 'user', 'src_ip', 'action'])
    assert set(rules('csv_noheader', splitter=named)) == {'login', 'logout', 'other'}
    assert 'json_fields' not in draft_mapping([SAMPLES['json_array_nested']])['mapping']   # a real array isn't JSON text


def test_the_rule_for_the_rest_always_has_an_action_element():
    # Seen: every field mapped already left the catch-all with nothing, and the whole mapping failed.
    for name in ('csv_quoted', 'json_lines', 'xml_records'):
        other = rules(name)['other']
        assert any(f['path'].startswith('EventDetail/Unknown/') for f in other['fields'])


def test_headerless_quoted_csv_is_one_group_per_column():
    # $1$2 per column (a quoted group or a bare one) is refused by Stroom when one took no part.
    spec = SplitterSpec(kind='delimited', header=['time', 'user', 'message'], quote='"')
    run = dry_run(spec, '2026-10-01T08:00:00Z,"o\'brien, pat","Said ""bye"" and left"\n2026-10-01T08:05:00Z,bob,\n')
    assert run['records'] == [{'time': '2026-10-01T08:00:00Z', 'user': "o'brien, pat", 'message': 'Said ""bye"" and left'},
                              {'time': '2026-10-01T08:05:00Z', 'user': 'bob', 'message': ''}]
    xml = generate_splitter(spec)
    assert '<data name="message" value="$3" />' in xml and '$1$2' not in xml


async def test_a_records_source_is_validated_as_what_it_is_and_never_indexed_as_events():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    import pytest
    from fastmcp.exceptions import ToolError
    from tools import indexing, validation
    ctx = SimpleNamespace(lifespan_context={})
    stroom = SimpleNamespace(settings=SimpleNamespace(event_logging_version='4.1.0'))
    cache = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(validate=lambda doc: True, error_log=[])))
    with patch.object(validation, 'gateway_from', lambda ctx: stroom), patch.object(validation, 'SchemaCache', lambda s: cache):
        own = await validation.validate_events(ctx, '<records><record><user>alice</user></record></records>')
        assert own['schema'] is None and own['errors'][0]['message'].startswith('Not an Events document: its root is '
                                                                                '<records> in no namespace')
        records = await validation.validate_events(ctx, '<records xmlns="records:2"><record/></records>')
        assert records['schema'] == 'file://records-v2.0.xsd' and cache.get.await_args.args[0] == 'file://records-v2.0.xsd'
        checked = await validation.check_events(ctx, '<records xmlns="records:2"><record/></records>')
        assert 'quality rules' in checked['quality']['note']
    raw = SimpleNamespace(find_meta=AsyncMock())
    with patch.object(indexing, '_meta', AsyncMock(return_value={'typeName': 'Raw Events'})), \
            pytest.raises(ToolError, match="is 'Raw Events', not Events: the source's own data"):
        await indexing.require_events(raw, [7])


async def test_a_hand_written_xslt_reading_its_own_records_bare_is_fine_for_xml_in_no_namespace():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from tools import validation
    hand = ('<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="2.0">'
            '<xsl:template match="records"><xsl:apply-templates select="record"/></xsl:template></xsl:stylesheet>')
    with patch.object(validation, '_import_problems', AsyncMock(return_value=[]), create=True):
        flagged = await validation.check_xslt(SimpleNamespace(lifespan_context={}), hand)
        fine = await validation.check_xslt(SimpleNamespace(lifespan_context={}), hand, input_namespace='')
    assert any('namespace records:2' in e for e in flagged['errors'])
    assert not any('namespace records:2' in e for e in fine['errors'])


async def test_a_discovery_pipeline_may_be_checked_over_raw_data_any_other_only_over_events():
    # Seen in e2e: a discovery index (raw JSON indexed as it is) refused its template check for its raw sample.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    import pytest
    from fastmcp.exceptions import ToolError
    from tools import indexing
    ctx = SimpleNamespace(lifespan_context={})
    raw = AsyncMock(side_effect=ToolError("Stream 7 is 'Raw Events', not Events"))
    with patch.object(indexing, 'require_events', raw), patch.object(indexing, 'gateway_from', lambda ctx: None):
        with patch.object(indexing, '_shape', AsyncMock(return_value={'stage': 'discovery'})):
            await indexing._require_input(ctx, 'p', [7])
        # An XML discovery pipeline's parser is an indexing pipeline's too: its plan says what it is.
        await indexing._require_input(ctx, 'p', [7], SimpleNamespace(discovery={'input': 'xml'}))
        with pytest.raises(ToolError, match='not Events'):
            await indexing._require_input(ctx, 'p', [7], SimpleNamespace(discovery=None))
