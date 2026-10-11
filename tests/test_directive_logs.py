"""Logs whose columns are named by a directive line, not a first line: W3C extended (IIS, Blue Coat) and Zeek. Asked for
by the user: a generic answer, not one limited to Blue Coat, as there are many variants in the wild."""
from lxml import etree

from utils.dsgen import SplitterSpec, dry_run, generate_splitter, infer_spec
from utils.profile import profile
from utils.survey import Shapes, split_records

BLUECOAT = '''#Software: SGOS 6.7.5.12
#Version: 1.0
#Start-Date: 2026-10-11 08:00:00
#Date: 2026-10-11 08:00:00
#Fields: date time time-taken c-ip cs-username cs-auth-group x-exception-id sc-filter-result cs-categories cs(Referer) sc-status s-action cs-method rs(Content-Type) cs-uri-scheme cs-host cs-uri-port cs-uri-path cs-uri-query cs(User-Agent) s-ip sc-bytes cs-bytes
#Remark: 4111140007 "proxy01" "192.168.1.10" "main"

2026-10-11 08:00:01 125 192.168.1.100 jsmith Employees - OBSERVED "Business/Economy" - 200 TCP_NC_MISS GET text/html https www.example.com 443 /index.html - "Mozilla/5.0" 192.168.1.10 12450 820
2026-10-11 08:00:03 2 192.168.1.102 guest Guests policy_denied DENIED "Malicious Sources" - 403 TCP_DENIED GET text/html http malware.example.net 80 /payload.exe - "Mozilla/5.0" 192.168.1.10 1250 420
2026-10-11 08:00:04 210 192.168.1.103 jbloggs Employees - OBSERVED "Business/Economy" - 200 TCP_NC_MISS POST application/json https portal.example.org 443 /api/login - "Mozilla/5.0" 192.168.1.10 850 1450
#Software: SGOS 7.3.1
#Fields: date time c-ip cs-username sc-status
2026-10-11 09:00:00 192.168.1.200 later 404
'''
ZEEK = ('#separator \\x09\n#path\tconn\n#open\t2026-10-11-00-00-00\n#fields\tts\tuid\tid.orig_h\tid.resp_h\tid.resp_p\tproto\n'
        '#types\ttime\tstring\taddr\taddr\tport\tenum\n1791676800.1\tC1\t10.0.0.1\t93.184.216.34\t443\ttcp\n'
        '1791676801.2\tC2\t10.0.0.2\t8.8.8.8\t53\tudp\n1791676802.3\tC3\t10.0.0.3\t10.0.0.9\t22\ttcp\n#close\t2026-10-11\n')


def test_the_columns_come_from_the_directive_in_effect_and_directives_ride_along():
    info = profile(BLUECOAT)
    assert info['format'] == 'delimited' and info['header_directive'] == '#Fields:' and info['delimiter'] == ' '
    assert info['columns'][:3] == ['date', 'time', 'time-taken'] and '#Remark:' in info['directives']
    spec, _ = infer_spec(BLUECOAT)
    run = dry_run(spec, BLUECOAT)
    assert len(run['records']) == 4 and run['unmatched_lines'] == []
    first, last = run['records'][0], run['records'][-1]
    assert first['cs-categories'] == 'Business/Economy' and first['cs(User-Agent)'] == 'Mozilla/5.0'
    assert first['#Software'] == 'SGOS 6.7.5.12' and first['#Remark'].startswith('4111140007 "proxy01"')
    # The log restarted: new columns and a new software version for the lines after it (as Stroom runs it: checked on
    # the local stack, step for step).
    assert last == {**{k: v for k, v in last.items() if k.startswith('#')}, 'date': '2026-10-11', 'time': '09:00:00',
                    'c-ip': '192.168.1.200', 'cs-username': 'later', 'sc-status': '404'}
    assert last['#Software'] == 'SGOS 7.3.1'


def test_zeek_names_its_columns_with_a_tab_separated_fields_directive():
    info = profile(ZEEK)
    assert (info['delimiter'], info['header_directive'], len(info['columns'])) == ('\t', '#fields', 6)
    spec, _ = infer_spec(ZEEK)
    run = dry_run(spec, ZEEK)
    assert [r['proto'] for r in run['records']] == ['tcp', 'udp', 'tcp'] and run['records'][0]['#path'] == 'conn'


def test_the_splitter_sets_the_heading_and_directives_from_their_lines():
    spec = SplitterSpec(kind='delimited', delimiter=' ', header=True, quote='"', comment_prefix='#',
                        header_directive='#Fields:', directives=['#Software:'])
    root = etree.fromstring(generate_splitter(spec).encode())
    ns = {'d': 'data-splitter:3'}
    group = root.find('d:split/d:group', ns)
    patterns = [r.get('pattern') for r in group.findall('d:regex', ns)]
    assert patterns == [r'^\#Fields:\s*(.*)$', r'^\#Software:\s*(.*)$', r'^\#.*$', '^(.+)$']
    assert group.find('d:regex/d:group/d:split/d:var', ns).get('id') == 'heading'
    data = group.findall('d:regex', ns)[-1]
    assert data.find('d:data', ns).attrib == {'name': '#Software', 'value': '$directive0$1'}
    assert data.find('d:group/d:split/d:data', ns).attrib == {'name': '$heading$1', 'value': '$1'}


def test_the_survey_reads_the_data_lines_under_their_columns():
    chunk = split_records(BLUECOAT)
    shapes = Shapes()
    for i in range(len(chunk.records)):
        shapes.add(chunk, i, 1)
    assert len(chunk.records) == 4 and not any(r.startswith('#') for r in chunk.records)
    assert chunk.parsed[1]['s-action'] == 'TCP_DENIED'


def test_a_plain_csv_is_not_taken_for_a_directive_log():
    assert not profile('user,action,host\nalice,login,ws01\nbob,logout,ws02\n').get('header_directive')
    # A comment line that names nothing: still not one.
    assert not profile('# exported 2026-10-11\na b c\n1 2 3\n4 5 6\n').get('header_directive')
