"""Samples must be data, not paths; the Data Splitter spec is inferred from the sample."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from tools import feeds, generation, plan
from utils.dsgen import infer_spec
from utils.samples import as_named_samples, why_not_data

PATHS = (r'c:\Users\pkimb\Documents\stroom-mcp\sample-data\fortios\001_1.json, '
         r'c:\Users\pkimb\Documents\stroom-mcp\sample-data\fortios\001_2.json')
FORTIOS = ('date=2026-10-01 time=10:00:00 devname="fw01" srcip=10.0.0.1 dstip=203.0.113.5 dstport=443 action="accept"\n'
           'date=2026-10-01 time=10:00:05 devname="fw01" srcip=10.0.0.2 dstip=203.0.113.6 dstport=80 action="deny"\n')


def test_paths_and_references_are_not_samples():
    assert 'is a file path' in why_not_data(PATHS)
    assert 'is a file path' in why_not_data(r'c:\logs\fw.log')
    assert 'is a file path' in why_not_data('/var/log/syslog')
    assert 'is a file path' in why_not_data('#file:fortios')
    assert 'is empty' in why_not_data('   ')
    assert why_not_data(FORTIOS) is None
    assert why_not_data('2026-10-01T10:00:00Z,alice,login\n') is None   # data with commas is not a path list
    assert why_not_data('{"path": "c:\\\\x\\\\y.json"}') is None          # JSON mentioning a path is data
    with pytest.raises(ToolError, match='cannot read the client'):
        as_named_samples({'fortios': PATHS})
    assert list(as_named_samples([FORTIOS, FORTIOS])) == ['sample 1', 'sample 2']
    assert as_named_samples(None, FORTIOS) == {'sample': FORTIOS}


async def test_the_tools_refuse_paths_with_the_instruction_to_pass_the_text():
    with pytest.raises(ToolError, match='read the file and pass its text'):
        await feeds.profile_sample(None, samples={'fortios': PATHS})
    with pytest.raises(ToolError, match='read the file and pass its text'):
        await feeds.upload_sample(None, 'FEED', r'c:\Users\x\001_1.json')
    guard = SimpleNamespace(build_folder=AsyncMock(return_value={'_path': 'p', 'uuid': 'f'}))
    with patch.object(plan, 'guard_from', lambda c: guard):
        with pytest.raises(ToolError, match='cannot read the client'):
            await plan.start_onboarding(None, 'FortiOS', {'fortios': PATHS})
        with patch('tools.instructions.applicable_instructions', AsyncMock(return_value={'instructions': []})):
            result = await plan.start_onboarding(None, 'FortiOS', [FORTIOS, FORTIOS])   # a list of texts is fine
    assert result['profile']['format'] == 'key=value' and result['text_converter'].startswith('needed')


async def test_the_splitter_spec_is_inferred_from_the_sample():
    result = await generation.build_data_splitter(None, sample=FORTIOS)
    assert result['inferred'] and result['spec'] == {'kind': 'key_value', 'delimiter': ' ', 'quote': '"'}
    assert result['records'] == 2 and 'srcip' in [f['field'] for f in result['fields']] and result['unmatched_count'] == 0
    assert result['first_records'][0]['action'] == 'accept'
    csv = await generation.build_data_splitter(None, sample='time,user,action\n2026-10-01T10:00:00Z,alice,login\n')
    assert csv['spec'] == {'kind': 'delimited', 'header': True}
    syslog = await generation.build_data_splitter(None, sample='<34>Sep 28 10:00:00 fw01 fortigate: user=alice action=login src=10.0.0.1\n' * 3)
    assert syslog['spec']['kind'] == 'syslog' and syslog['spec']['body'] == {'kind': 'key_value', 'delimiter': ' '}
    assert syslog['first_records'][0]['user'] == 'alice'
    with pytest.raises(ToolError, match='needs no Data Splitter'):
        await generation.build_data_splitter(None, sample='{"a": 1}\n{"a": 2}\n')
    with pytest.raises(ToolError, match='give spec, a regex'):
        await generation.build_data_splitter(None, sample='something odd happened here\nand again there\n')
    with pytest.raises(ToolError, match="spec is not a splitter spec .* For key=value data it looks like"):
        await generation.build_data_splitter(None, sample=FORTIOS, spec={'kind': 'regex'})
    with pytest.raises(ToolError, match='read the file and pass its text'):
        await generation.build_data_splitter(None, sample=PATHS)
    spec, _ = infer_spec('a|b\nc|d\n')
    assert spec.kind == 'delimited' and spec.delimiter == '|'


# Gemma 4 on vLLM, onboarding a CSV: the file's text cut at ':' and ',' into an object, inside a list.
BROKEN = [{
    '"sample_firewall_logs_20.csv"': '"timestamp',
    'device,event_type,severity,src_ip\\n2026-10-01T09': '00:12+10:00',
    'FW-EDGE-01,TRAFFIC,INFO,192.0.2.10\\n2026-10-01T09': '01:05+10:00',
}]


async def test_samples_cut_up_by_the_tool_call_reach_the_server_and_are_explained():
    from fastmcp import Client, FastMCP
    server = FastMCP('t', lifespan=None)
    server.tool(feeds.profile_sample)
    async with Client(server) as client:
        result = await client.call_tool('profile_sample', {'samples': BROKEN}, raise_on_error=False)
    text = ' '.join(c.text for c in result.content if getattr(c, 'text', None))
    assert result.is_error and 'samples arrived broken' in text and 'list of strings' in text


def test_a_list_of_named_texts_and_doubly_escaped_newlines_are_read():
    assert as_named_samples([{'fw.log': FORTIOS}, FORTIOS]) == {'fw.log': FORTIOS, 'sample 2': FORTIOS}
    assert as_named_samples(['a,b\\n1,2\\n3,4']) == {'sample 1': 'a,b\n1,2\n3,4'}
    assert as_named_samples({'fw.json': '{"msg": "a\\nb"}\n{"msg": "c"}'})['fw.json'].count('\\n') == 1  # JSON escapes kept
