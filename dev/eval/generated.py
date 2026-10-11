"""Sample files too large to paste, made the same way every time (a case's `files: [{generate: {kind, ...}}]`)."""
import random
from datetime import datetime, timedelta, timezone

USERS = ['alice', 'bob', 'carol', 'dave', 'erin', 'frank', 'grace', 'heidi', 'ivan', 'judy']
SITES = [('https://intranet.example.org/', 'business'), ('https://news.example.com/world', 'news'),
         ('https://mail.example.org/inbox', 'webmail'), ('https://files.example.net/share/q3.xlsx', 'file-sharing'),
         ('https://cdn.example.com/lib.js', 'technology'), ('http://198.51.100.40/update.bin', 'uncategorised'),
         ('https://social.example.com/feed', 'social-media'), ('https://search.example.com/?q=stroom', 'search')]
METHODS = ['GET'] * 8 + ['POST', 'CONNECT']
STATUSES = [200] * 12 + [204, 301, 302, 304, 403, 404, 407, 502]


def proxy_access(day: str, records: int, seed: int) -> str:
    """A web proxy's access log for one day, CSV with a header: one record per request, about 120 bytes each."""
    rng = random.Random(seed)
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    lines = ['time,client_ip,user,method,url,status,bytes,category,action']
    for n in range(records):
        at = start + timedelta(seconds=n * 86400 / records)
        url, category = rng.choice(SITES)
        status = rng.choice(STATUSES)
        action = 'blocked' if status in (403, 407) else 'allowed'
        lines.append(f"{at.strftime('%Y-%m-%dT%H:%M:%S')}Z,10.{rng.randint(0, 3)}.{rng.randint(0, 254)}.{rng.randint(1, 254)},"
                     f"{rng.choice(USERS)},{rng.choice(METHODS)},{url},{status},{rng.randint(200, 900000)},{category},{action}")
    return '\n'.join(lines) + '\n'


def secretserver_audit(records: int, seed: int, plain_head: int = 400) -> str:
    """Delinea Secret Server audit events, as a user's 1,000-record export (seen in production): Windows event XML
    fragments, one a line, alike but for the message in Data, which names the kind of event. Case 35's 33 records
    are the kinds; the first `plain_head` records are only logons and secret views, so the rest of the kinds start
    about 280,000 characters in, past what one read of a stream's head gets."""
    import re
    from pathlib import Path

    import yaml
    case = Path(__file__).parent / 'cases' / '35_xml_fragments_secretserver_messages.yaml'
    templates = yaml.safe_load(case.read_text(encoding='utf-8'))['sample'].splitlines()
    head_kinds = [t for t in templates if re.search(r'Event: \[(User|Secret)\] Action: \[(Login|Login Failed|Logout|'
                                                    r'View)\]', t)]
    rng = random.Random(seed)
    start = datetime(2026, 9, 27, 23, 48, 10, tzinfo=timezone.utc)
    lines = []
    for n in range(records):
        template = rng.choice(head_kinds) if n < plain_head else templates[(n - plain_head) % len(templates)]
        at = start + timedelta(seconds=97 * n)
        line = re.sub(r'SystemTime="[^"]*"', f'SystemTime="{at:%Y-%m-%dT%H:%M:%S}.0000000Z"', template)
        line = re.sub(r'<EventRecordID>\d+</EventRecordID>', f'<EventRecordID>{12001 + n}</EventRecordID>', line)
        line = re.sub(r'<Data>[A-Z][a-z]{2} \d{2} \d{4} \d{2}:\d{2}:\d{2}', f'<Data>{at:%b %d %Y %H:%M:%S}', line)
        lines.append(re.sub(r'\(Item Id: \d+\)', f'(Item Id: {219 + n})', line))
    return '\n'.join(lines) + '\n'


def cef_gateway(records: int, seed: int, plain_head: int = 3000) -> str:
    """A secure web gateway's syslog CEF lines (case 22's source), as a day's file: the first `plain_head` lines are
    only logons and allowed web requests, so failed logons, logoffs, blocked requests and malware start about
    550,000 characters in. The extension's keys vary with the kind of event."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 2, 9, 0, 0, tzinfo=timezone.utc)
    users = ['alice', 'bob', 'carol', 'dave', 'erin']
    sites = [('https://example.com/report.pdf', '93.184.216.34', 443, 'business'),
             ('http://198.51.100.40/update.bin', '198.51.100.40', 80, 'malware'),
             ('https://news.example.org/today', '203.0.113.80', 443, 'news')]
    lines = []
    for n in range(records):
        at = start + timedelta(seconds=7 * n)
        kind = rng.choice(['1001', '2001']) if n < plain_head else ['1001', '1002', '1003', '2001', '2002', '3001'][n % 6]
        user, src = rng.choice(users), f'10.1.{rng.randint(0, 9)}.{rng.randint(2, 250)}'
        url, dst, port, cat = rng.choice(sites)
        name, sev, ext = {
            '1001': ('User authenticated', 3, f'suser={user} src={src} act=login outcome=success msg=SAML login from the VPN'),
            '1002': ('Authentication failed', 6, f'suser={user} src=203.0.113.{rng.randint(2, 250)} act=login outcome=failure msg=Bad password'),
            '1003': ('User logged out', 1, f'suser={user} src={src} act=logout msg=Session ended'),
            '2001': ('Web request allowed', 1, f'suser={user} src={src} dst={dst} dpt={port} request={url} act=allow cat={cat}'),
            '2002': ('Web request blocked', 5, f'suser={user} src={src} dst={dst} dpt={port} request={url} act=block cat=malware'),
            '3001': ('Malware detected', 9, f'suser={user} src={src} fname=update.bin fileHash=44d88612fea8a8f36de82e1278abb02f '
                                            f'act=quarantine msg=EICAR test file'),
        }[kind]
        host = 'swg02' if kind == '3001' else 'swg01'
        lines.append(f"<134>{at:%b} {at.day:2d} {at:%H:%M:%S} {host} CEF:0|Acme|SecureGateway|5.2|{kind}|{name}|{sev}|{ext}")
    return '\n'.join(lines) + '\n'


def json_alerts_one_line(records: int, seed: int, plain_head: int = 1500) -> str:
    """IDS alerts (case 02's source) as one JSON array on a single line, as a SIEM export writes it: Stroom holds the
    stream as one record of one line. The first `plain_head` alerts are scans and policy alerts only; the exploit and
    malware ones start about 300,000 characters in."""
    import json
    rng = random.Random(seed)
    start = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
    sigs = [('ET SCAN Nmap Scripting Engine', 2), ('ET POLICY curl User-Agent', 3)]
    late = sigs + [('ET EXPLOIT Possible SQL injection', 1), ('ET MALWARE Known C2 beacon', 1)]
    alerts = []
    for n in range(records):
        sig, sev = rng.choice(sigs) if n < plain_head else late[n % len(late)]
        alerts.append({'ts': f"{start + timedelta(seconds=13 * n):%Y-%m-%dT%H:%M:%S}Z", 'sensor': f'ids-{1 + n % 2}',
                       'sig': sig, 'sev': sev, 'src': f'10.1.{rng.randint(0, 9)}.{rng.randint(2, 250)}',
                       'dst': rng.choice(['10.1.1.20', '93.184.216.34', '10.1.2.10']), 'dport': rng.choice([22, 80, 443])})
    return json.dumps(alerts) + '\n'


def bluecoat_access(records: int, seed: int, restart_at: int = 2000) -> str:
    """A Blue Coat ProxySG access log (W3C extended format, as the user showed one): directive lines name the
    software, the device (#Remark) and the columns (#Fields:). At `restart_at` the proxy is upgraded and the log
    restarts: new directives, the columns reordered and one added. Quoted values hold spaces."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    first = ('date time time-taken c-ip cs-username cs-auth-group x-exception-id sc-filter-result cs-categories '
             'cs(Referer) sc-status s-action cs-method rs(Content-Type) cs-uri-scheme cs-host cs-uri-port cs-uri-path '
             'cs-uri-query cs(User-Agent) s-ip sc-bytes cs-bytes').split()
    second = ['date', 'time', 'c-ip', 'cs-username', 'cs-auth-group', 'sc-filter-result', 'x-exception-id',
              'cs-categories', 'sc-status', 's-action', 'cs-method', 'cs-uri-scheme', 'cs-host', 'cs-uri-port',
              'cs-uri-path', 'cs-uri-query', 'cs(User-Agent)', 'x-virus-id', 's-ip', 'sc-bytes', 'cs-bytes',
              'time-taken', 'cs(Referer)', 'rs(Content-Type)']
    users = [('jsmith', 'Employees'), ('admin', 'Administrators'), ('guest', 'Guests'), ('jbloggs', 'Employees')]
    sites = [('www.example.com', '/index.html', '"Business/Economy"', 'text/html'),
             ('api.example.com', '/v1/status', '"Technology/Internet"', 'application/json'),
             ('malware.example.net', '/payload.exe', '"Malicious Sources"', 'application/octet-stream'),
             ('news.example.org', '/today', '"News/Media"', 'text/html')]

    def header(software: str, columns: list[str], at: datetime) -> list[str]:
        return [f'#Software: {software}', '#Version: 1.0', f'#Start-Date: {at:%Y-%m-%d %H:%M:%S}',
                f'#Date: {at:%Y-%m-%d %H:%M:%S}', '#Fields: ' + ' '.join(columns),
                '#Remark: 4111140007 "proxy01" "192.168.1.10" "main"']

    lines = header('SGOS 6.7.5.12', first, start)
    columns = first
    for n in range(records):
        at = start + timedelta(seconds=3 * n)
        if n == restart_at:
            lines += [''] + header('SGOS 7.3.1', second, at)
            columns = second
        user, group = rng.choice(users)
        host, path, category, ctype = rng.choice(sites)
        denied = 'Malicious' in category or (user == 'guest' and rng.random() < 0.5)
        row = {'date': f'{at:%Y-%m-%d}', 'time': f'{at:%H:%M:%S}', 'time-taken': str(rng.randint(1, 900)),
               'c-ip': f'192.168.1.{100 + rng.randint(0, 50)}', 'cs-username': user, 'cs-auth-group': group,
               'x-exception-id': 'policy_denied' if denied else '-', 'sc-filter-result': 'DENIED' if denied else 'OBSERVED',
               'cs-categories': category, 'cs(Referer)': '-', 'sc-status': '403' if denied else '200',
               's-action': 'TCP_DENIED' if denied else rng.choice(['TCP_NC_MISS', 'TCP_HIT']),
               'cs-method': rng.choice(['GET', 'GET', 'POST']), 'rs(Content-Type)': ctype,
               'cs-uri-scheme': 'http' if 'malware' in host else 'https', 'cs-host': host,
               'cs-uri-port': '80' if 'malware' in host else '443', 'cs-uri-path': path, 'cs-uri-query': '-',
               'cs(User-Agent)': rng.choice(['"Mozilla/5.0 (Windows NT 10.0; Win64; x64)"', '"curl/8.4.0"']),
               'x-virus-id': 'EICAR-Test-File' if 'malware' in host else '-',
               's-ip': '192.168.1.10', 'sc-bytes': str(rng.randint(200, 90000)), 'cs-bytes': str(rng.randint(200, 3000))}
        lines.append(' '.join(row[c] for c in columns))
    return '\n'.join(lines) + '\n'


def cloudtrail(records: int, seed: int, plain_head: int = 500) -> str:
    """An AWS CloudTrail log file: one JSON object, pretty-printed, whose Records array holds the events. The first
    `plain_head` are console logins and S3 reads only; object deletes, user creation, policy changes and denied
    calls come after. sourceIPAddress is an AWS service's name, not an address, for calls a service made."""
    import json
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    users = ['alice', 'bob', 'carol']
    early = ['ConsoleLogin', 'GetObject']
    late = early + ['DeleteObject', 'CreateUser', 'PutBucketPolicy', 'GetObject']
    out = []
    for n in range(records):
        name = rng.choice(early) if n < plain_head else late[n % len(late)]
        user = rng.choice(users)
        event = {'eventVersion': '1.08',
                 'userIdentity': {'type': 'IAMUser', 'principalId': f'AIDA{n:08d}', 'userName': user,
                                  'arn': f'arn:aws:iam::123456789012:user/{user}', 'accountId': '123456789012'},
                 'eventTime': f'{start + timedelta(seconds=11 * n):%Y-%m-%dT%H:%M:%S}Z',
                 'eventSource': {'ConsoleLogin': 'signin.amazonaws.com', 'CreateUser': 'iam.amazonaws.com'}.get(
                     name, 's3.amazonaws.com'),
                 'eventName': name, 'awsRegion': 'us-east-1',
                 'sourceIPAddress': rng.choice(['203.0.113.5', '198.51.100.7']) if name != 'PutBucketPolicy'
                 else 'cloudformation.amazonaws.com',
                 'userAgent': 'aws-cli/2.15.0', 'eventID': f'0f{n:030d}', 'eventType': 'AwsApiCall',
                 'recipientAccountId': '123456789012'}
        if name == 'ConsoleLogin':
            event['eventType'] = 'AwsConsoleSignIn'
            event['responseElements'] = {'ConsoleLogin': 'Failure' if n % 9 == 0 else 'Success'}
        elif name in ('GetObject', 'DeleteObject', 'PutBucketPolicy'):
            event['requestParameters'] = {'bucketName': rng.choice(['finance', 'hr']), 'key': f'reports/q{n % 4 + 1}.xlsx'}
        elif name == 'CreateUser':
            event['requestParameters'] = {'userName': f'svc-{n}'}
        if n >= plain_head and n % 13 == 0:
            event['errorCode'], event['errorMessage'] = 'AccessDenied', 'User is not authorized to perform this action'
        out.append(event)
    return json.dumps({'Records': out}, indent=2) + '\n'


def java_app_log(records: int, seed: int, plain_head: int = 1200) -> str:
    """A Java (Spring Boot, logback) application log: one entry a record, starting with its time, an ERROR's stack
    trace on the lines after it (3 to 14 'at' lines, a 'Caused by' and '... n more'). The first `plain_head` entries
    are logins, logouts and invoices only; failed logins, payment errors and configuration changes come after."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    users = ['alice', 'bob', 'carol', 'dave']
    early = ['login', 'invoice', 'logout']
    late = early + ['failed', 'payment', 'config']
    lines = []
    for n in range(records):
        at = start + timedelta(milliseconds=2731 * n)
        stamp = f"{at:%Y-%m-%d %H:%M:%S},{at.microsecond // 1000:03d}"
        kind = rng.choice(early) if n < plain_head else late[n % len(late)]
        user, thread = rng.choice(users), f'http-nio-8080-exec-{rng.randint(1, 9)}'
        if kind == 'login':
            lines.append(f'{stamp} INFO  [{thread}] com.acme.auth.LoginController - User {user} logged in from '
                         f'10.0.{rng.randint(0, 9)}.{rng.randint(2, 250)}')
        elif kind == 'logout':
            lines.append(f'{stamp} INFO  [{thread}] com.acme.auth.LoginController - User {user} logged out')
        elif kind == 'invoice':
            lines.append(f'{stamp} INFO  [{thread}] com.acme.billing.InvoiceService - Invoice {1000 + n} created for '
                         f'customer {user}')
        elif kind == 'failed':
            lines.append(f'{stamp} WARN  [{thread}] com.acme.auth.LoginController - Login failed for user {user} from '
                         f'203.0.113.{rng.randint(2, 250)}: bad credentials')
        elif kind == 'config':
            lines.append(f'{stamp} INFO  [main] com.acme.admin.ConfigService - Setting session.timeout changed from '
                         f'{rng.choice([15, 20])} to 30 by admin')
        else:
            lines.append(f'{stamp} ERROR [{thread}] com.acme.billing.PaymentGateway - Payment failed for invoice '
                         f'{1000 + n} (customer {user})')
            lines.append('java.net.SocketTimeoutException: Read timed out')
            frames = ['java.base/java.net.SocketInputStream.socketRead0(Native Method)',
                      'java.base/java.net.SocketInputStream.read(SocketInputStream.java:168)',
                      'com.acme.billing.PaymentGateway.charge(PaymentGateway.java:88)',
                      'com.acme.billing.InvoiceService.settle(InvoiceService.java:142)',
                      'org.springframework.aop.framework.ReflectiveMethodInvocation.proceed(ReflectiveMethodInvocation.java:186)']
            lines += [f'\tat {f}' for f in frames[:rng.randint(3, 5)]]
            lines.append('Caused by: java.io.IOException: Connection reset by peer')
            lines += [f'\tat {f}' for f in frames[:rng.randint(1, 3)]]
            lines.append(f'\t... {rng.randint(10, 60)} more')
    return '\n'.join(lines) + '\n'


def ticket_csv(records: int, seed: int) -> str:
    """A ticketing system's CSV export: a header, quoted values holding commas, doubled quotes and new lines (a
    comment over two or three lines), and empty trailing columns."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    users = ['alice', 'bob', 'carol', 'dave']
    lines = ['time,ticket,user,action,priority,comment']
    for n in range(records):
        action = ['create', 'update', 'close', 'reopen'][n % 4] if n >= records // 3 else rng.choice(['create', 'update'])
        comment = rng.choice(['', 'printer offline, again', 'user said ""it worked yesterday""',
                              'restarted the spooler\nand cleared the queue', 'escalated to network\nsee INC-2201\nwaiting'])
        quoted = f'"{comment}"' if comment and (',' in comment or '"' in comment or '\n' in comment) else comment
        lines.append(f"{start + timedelta(seconds=37 * n):%Y-%m-%dT%H:%M:%S}Z,TCK-{1000 + n % 300},{rng.choice(users)},"
                     f"{action},{rng.choice(['P1', 'P2', 'P3'])},{quoted}")
    return '\n'.join(lines) + '\n'


PAN_TRAFFIC = ['future_use1', 'receive_time', 'serial', 'type', 'subtype', 'future_use2', 'generated_time', 'src',
               'dst', 'natsrc', 'natdst', 'rule', 'srcuser', 'dstuser', 'app', 'vsys', 'from', 'to', 'inbound_if',
               'outbound_if', 'logset', 'future_use3', 'sessionid', 'repeatcnt', 'sport', 'dport', 'natsport',
               'natdport', 'flags', 'proto', 'action', 'bytes', 'bytes_sent', 'bytes_received', 'packets', 'start',
               'elapsed', 'category', 'future_use4', 'seqno', 'actionflags', 'srcloc', 'dstloc', 'future_use5',
               'pkts_sent', 'pkts_received', 'session_end_reason']


def palo_alto_traffic(records: int, seed: int, plain_head: int = 1500) -> str:
    """Palo Alto firewall TRAFFIC logs as syslog sends them: an RFC 3164 header, then the log's CSV, positional
    (47 columns, no header anywhere). The first `plain_head` sessions are allowed web traffic only; denied, dropped
    and reset sessions come after."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    users = ['corp\\alice', 'corp\\bob', 'corp\\carol', '']
    lines = []
    for n in range(records):
        at = start + timedelta(seconds=5 * n)
        stamp = f'{at:%Y/%m/%d %H:%M:%S}'
        subtype, action = ('end', 'allow') if n < plain_head else [('end', 'allow'), ('deny', 'deny'),
                                                                   ('drop', 'drop'), ('end', 'reset-both')][n % 4]
        dport = rng.choice([443, 80, 22, 3389])
        row = {'future_use1': '1', 'receive_time': stamp, 'serial': '012801000001', 'type': 'TRAFFIC',
               'subtype': subtype, 'future_use2': '2561', 'generated_time': stamp,
               'src': f'10.0.{rng.randint(0, 9)}.{rng.randint(2, 250)}',
               'dst': rng.choice(['93.184.216.34', '198.51.100.40', '203.0.113.80']), 'natsrc': '203.0.113.1',
               'natdst': '0.0.0.0', 'rule': 'allow-web' if action == 'allow' else 'block-risky',
               'srcuser': rng.choice(users), 'dstuser': '', 'app': {443: 'ssl', 80: 'web-browsing', 22: 'ssh',
                                                                    3389: 'ms-rdp'}[dport],
               'vsys': 'vsys1', 'from': 'trust', 'to': 'untrust', 'inbound_if': 'ethernet1/2',
               'outbound_if': 'ethernet1/1', 'logset': 'log-fwd', 'future_use3': stamp, 'sessionid': str(40000 + n),
               'repeatcnt': '1', 'sport': str(rng.randint(40000, 65000)), 'dport': str(dport), 'natsport': '0',
               'natdport': '0', 'flags': '0x400000', 'proto': 'tcp', 'action': action,
               'bytes': str(rng.randint(0, 90000)), 'bytes_sent': str(rng.randint(0, 9000)),
               'bytes_received': str(rng.randint(0, 80000)), 'packets': str(rng.randint(1, 90)), 'start': stamp,
               'elapsed': str(rng.randint(0, 600)), 'category': rng.choice(['any', 'news', 'malware']),
               'future_use4': '0', 'seqno': str(1000 + n), 'actionflags': '0x0', 'srcloc': '10.0.0.0-10.255.255.255',
               'dstloc': 'United States', 'future_use5': '0', 'pkts_sent': str(rng.randint(1, 40)),
               'pkts_received': str(rng.randint(0, 50)),
               'session_end_reason': {'allow': 'tcp-fin', 'deny': 'policy-deny', 'drop': 'policy-deny',
                                      'reset-both': 'threat'}[action]}
        lines.append(f"<14>{at:%b} {at.day:2d} {at:%H:%M:%S} pa-fw01 " + ','.join(row[c] for c in PAN_TRAFFIC))
    return '\n'.join(lines) + '\n'


def tilde_audit(records: int, seed: int) -> str:
    """A home-grown access log: fields separated by ' ~ ', no header, a result of OK or FAIL."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    lines = []
    for n in range(records):
        action = rng.choice(['LOGIN', 'LOGIN', 'LOGOUT', 'PASSWORD_CHANGE'])
        result = 'FAIL' if action == 'LOGIN' and rng.random() < 0.2 else 'OK'
        lines.append(f"{start + timedelta(seconds=19 * n):%Y-%m-%d %H:%M:%S} ~ {rng.choice(['alice', 'bob', 'carol'])} ~ "
                     f"{action} ~ {result} ~ 10.0.{rng.randint(0, 9)}.{rng.randint(2, 250)}")
    return '\n'.join(lines) + '\n'


def gelf_app(records: int, seed: int) -> str:
    """Graylog GELF messages, one JSON object a line: short_message, an optional full_message over lines, a
    timestamp in epoch seconds with a fraction, a syslog level, and _-prefixed additional fields that vary by kind."""
    import json
    rng = random.Random(seed)
    base = 1791676800.0
    out = []
    for n in range(records):
        kind = rng.choice(['login', 'logout', 'disk'])
        msg = {'version': '1.1', 'host': rng.choice(['app01', 'app02']), 'timestamp': round(base + 3.7 * n, 3)}
        user = rng.choice(['alice', 'bob', 'carol'])
        if kind == 'login':
            msg.update(short_message='User login', level=6, _user=user, _action='login',
                       _src_ip=f'10.0.0.{rng.randint(2, 250)}')
        elif kind == 'logout':
            msg.update(short_message='User logout', full_message=f'User {user} logged out\nsession {n}', level=6,
                       _user=user, _action='logout')
        else:
            msg.update(short_message='Disk almost full', level=4, _disk='/var', _pct=rng.randint(85, 99),
                       _action='disk_alert')
        out.append(json.dumps(msg))
    return '\n'.join(out) + '\n'


def ecs_json(records: int, seed: int) -> str:
    """Events already in the Elastic Common Schema, one JSON object a line, nested as ECS nests them."""
    import json
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    out = []
    for n in range(records):
        kind = rng.choice(['login', 'login', 'file-delete'])
        event = {'@timestamp': f'{start + timedelta(seconds=13 * n):%Y-%m-%dT%H:%M:%S}.000Z',
                 'ecs': {'version': '8.11.0'}, 'host': {'name': rng.choice(['ws01', 'fs01'])},
                 'user': {'name': rng.choice(['alice', 'bob'])}}
        if kind == 'login':
            ok = rng.random() > 0.2
            event['event'] = {'category': ['authentication'], 'type': ['start'], 'action': 'user-login',
                              'outcome': 'success' if ok else 'failure'}
            event['source'] = {'ip': f'10.0.0.{rng.randint(2, 250)}'}
        else:
            event['event'] = {'category': ['file'], 'type': ['deletion'], 'action': 'file-delete', 'outcome': 'success'}
            event['file'] = {'path': f'/srv/finance/q{n % 4 + 1}.xlsx', 'name': f'q{n % 4 + 1}.xlsx'}
        out.append(json.dumps(event))
    return '\n'.join(out) + '\n'


def fortios_native(records: int, seed: int) -> str:
    """FortiGate's native log lines: key=value pairs, quoted strings, date and time in separate keys with the zone
    in tz, eventtime in nanoseconds, and the kind of event in type and subtype (traffic forward, event system, utm
    ips), each with its own keys."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    lines = []
    for n in range(records):
        at = start + timedelta(seconds=9 * n)
        head = (f'date={at:%Y-%m-%d} time={at:%H:%M:%S} devname="FGT60F" devid="FGT60FTK2000001" '
                f'eventtime={int(at.timestamp()) * 10 ** 9 + n} tz="+0000"')
        kind = rng.choice(['traffic', 'traffic', 'admin', 'ips'])
        if kind == 'traffic':
            lines.append(f'{head} logid="0000000013" type="traffic" subtype="forward" level="notice" vd="root" '
                         f'srcip=10.0.0.{rng.randint(2, 250)} srcport={rng.randint(40000, 65000)} '
                         f'dstip=93.184.216.34 dstport=443 action="{rng.choice(["accept", "deny"])}" policyid=1 '
                         f'service="HTTPS" user="{rng.choice(["alice", "bob"])}"')
        elif kind == 'admin':
            ok = rng.random() > 0.3
            lines.append(f'{head} logid="0100032001" type="event" subtype="system" level="information" vd="root" '
                         f'logdesc="Admin login {"successful" if ok else "failed"}" user="admin" ui="https(10.0.0.9)" '
                         f'action="login" status="{"success" if ok else "failed"}" '
                         f'msg="Administrator admin {"logged in successfully" if ok else "login failed"} from https(10.0.0.9)"')
        else:
            lines.append(f'{head} logid="0419016384" type="utm" subtype="ips" eventtype="signature" level="alert" '
                         f'vd="root" severity="high" srcip=203.0.113.{rng.randint(2, 250)} dstip=10.0.0.20 '
                         f'action="dropped" attack="SQL.Injection"')
    return '\n'.join(lines) + '\n'


DAMAGED_AT = {4: 'cut', 120: 'garbage', 333: 'quote', 512: 'cut'}


def damaged_jsonl(records: int, seed: int) -> str:
    """An app's auth events as JSON lines, some lines damaged as files in the wild are: cut off part-way (a writer
    killed mid-line), a line of garbage (a stack dump in the wrong file), and a value with an unescaped quote. The
    first damaged line is the 5th: read by the JSONParser, the stream stops there and the rest are lost."""
    import json
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    lines = []
    for n in range(records):
        event = {'time': f'{start + timedelta(seconds=17 * n):%Y-%m-%dT%H:%M:%S}Z', 'user': rng.choice(['alice', 'bob', 'carol']),
                 'action': rng.choice(['login', 'login', 'logout', 'password_change']),
                 'result': rng.choice(['success', 'success', 'failure']), 'ip': f'10.0.{rng.randint(0, 9)}.{rng.randint(2, 250)}'}
        line = json.dumps(event)
        damage = DAMAGED_AT.get(n)
        if damage == 'cut':
            line = line[:len(line) // 2]
        elif damage == 'garbage':
            line = 'Exception in thread "main" java.lang.OutOfMemoryError: Java heap space'
        elif damage == 'quote':
            line = line.replace(f'"user": "{event["user"]}"', f'"user": "{event["user"][:1]}"{event["user"][1:]}"')
        lines.append(line)
    return '\n'.join(lines) + '\n'


def soap_gateway(records: int, seed: int) -> str:
    """An API gateway's audit log as JSON lines, each request's SOAP payload kept as XML text in a string field:
    namespaced (soap:, acme:), the user and the outcome only inside it, the operation the kind of event."""
    import json
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    lines = []
    for n in range(records):
        operation = rng.choice(['Login', 'Login', 'GetAccount', 'UpdateAccount'])
        user, ok = rng.choice(['alice', 'bob', 'carol']), rng.random() > 0.15
        body = (f'<acme:{operation} xmlns:acme="urn:acme:accounts:v2"><acme:User>{user}</acme:User>'
                + (f'<acme:Account id="ACC-{1000 + n % 50}"/>' if operation != 'Login' else '')
                + f'<acme:Result>{"success" if ok else "failure"}</acme:Result></acme:{operation}>')
        payload = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Header/>'
                   f'<soap:Body>{body}</soap:Body></soap:Envelope>')
        lines.append(json.dumps({'time': f'{start + timedelta(seconds=23 * n):%Y-%m-%dT%H:%M:%S}Z',
                                 'client_ip': f'10.0.{rng.randint(0, 9)}.{rng.randint(2, 250)}', 'service': 'accounts',
                                 'operation': operation, 'status': 200 if ok else 403, 'payload': payload}))
    return '\n'.join(lines) + '\n'


def mail_gateway(records: int, seed: int) -> str:
    """A mail gateway's delivery log, key=value: one rcpt= per recipient (a key given again), a quoted subject, and
    the action taken (deliver, quarantine, reject)."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    people = ['bob@example.com', 'carol@example.com', 'dave@example.org', 'erin@example.net']
    lines = []
    for n in range(records):
        recipients = rng.sample(people, rng.randint(1, 3))
        action = rng.choice(['deliver', 'deliver', 'deliver', 'quarantine', 'reject'])
        lines.append(f"time={start + timedelta(seconds=29 * n):%Y-%m-%dT%H:%M:%S}Z host=mx01 msgid=<{n}@mail.example.com> "
                     f"from={rng.choice(['alice@example.com', 'frank@example.com'])} "
                     + ' '.join(f'rcpt={r}' for r in recipients)
                     + f' subject="{rng.choice(["Q3 report", "Lunch?", "Invoice 4471"])}" size={rng.randint(800, 900000)} '
                       f'action={action}')
    return '\n'.join(lines) + '\n'


def unicode_app(records: int, seed: int) -> str:
    """An app's log, key=value, written as a terminal shows it: a byte order mark at the start, user names in many
    scripts (José Álvarez, Zoë, 山田太郎, Ελένη), and messages coloured with ANSI escape codes (ESC [32m ... ESC [0m)."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    users = [('jalvarez', 'José Álvarez'), ('zoe', 'Zoë Brontë'), ('tyamada', '山田太郎'), ('eleni', 'Ελένη Παππά')]
    lines = []
    for n in range(records):
        uid, name = rng.choice(users)
        ok = rng.random() > 0.2
        colour, word = ('\x1b[32m', 'OK') if ok else ('\x1b[31m', 'FAILED')
        lines.append(f'time={start + timedelta(seconds=31 * n):%Y-%m-%dT%H:%M:%S}Z user={uid} name="{name}" '
                     f'action={rng.choice(["login", "logout"])} result={"success" if ok else "failure"} '
                     f'msg="{colour}{word}\x1b[0m for {name}"')
    return '﻿' + '\n'.join(lines) + '\n'


def postgres_log(records: int, seed: int) -> str:
    """A PostgreSQL server log with statement logging on: each entry starts with its time and the session's
    [pid] user@database, a statement runs over several indented lines, and connections and disconnections are
    entries of their own."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    users = ['alice', 'bob', 'svc_report']
    lines = []
    for n in range(records):
        at = start + timedelta(milliseconds=4137 * n)
        stamp = f'{at:%Y-%m-%d %H:%M:%S}.{at.microsecond // 1000:03d} UTC'
        user, pid = rng.choice(users), 4000 + rng.randint(0, 50)
        kind = rng.choice(['connect', 'select', 'select', 'update', 'delete', 'disconnect'])
        head = f'{stamp} [{pid}] {user}@finance'
        if kind == 'connect':
            lines.append(f'{head} LOG:  connection authorized: user={user} database=finance application_name=psql')
        elif kind == 'disconnect':
            lines.append(f'{head} LOG:  disconnection: session time: 0:0{rng.randint(1, 9)}:12.345 user={user} '
                         f'database=finance host=10.0.0.{rng.randint(2, 250)}')
        elif kind == 'select':
            lines += [f'{head} LOG:  statement: SELECT a.id, a.name, sum(t.amount)', '\tFROM accounts a',
                      '\tJOIN transactions t ON t.account_id = a.id', f'\tWHERE a.region = \'north\' AND t.day = \'2026-10-1{n % 9}\'',
                      '\tGROUP BY a.id, a.name;']
        elif kind == 'update':
            lines += [f'{head} LOG:  statement: UPDATE accounts', f"\tSET credit_limit = {rng.randint(1, 9)}000",
                      f'\tWHERE id = {rng.randint(100, 999)};']
        else:
            lines += [f'{head} LOG:  statement: DELETE FROM transactions', f'\tWHERE id = {rng.randint(1000, 9999)};']
    return '\n'.join(lines) + '\n'


def cri_container_log(records: int, seed: int) -> str:
    """A Kubernetes container's log as the CRI writes it on the node: '<time> <stream> <P|F> <content>', a long
    message split into partial (P) lines at arbitrary characters, its last part a full (F) line. The app writes
    key=value messages; exports carry a long detail that is split."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
    lines = []
    for n in range(records):
        at = start + timedelta(seconds=7 * n)
        action = rng.choice(['login', 'logout', 'export'])
        message = (f'user={rng.choice(["alice", "bob", "carol"])} action={action} '
                   f'result={rng.choice(["success", "success", "failure"])} pod=api-7d9f-{n % 5}')
        if action == 'export':
            message += ' detail=' + 'x' * rng.randint(40, 120) + f' rows={rng.randint(1, 900)}'
        stream = 'stderr' if 'failure' in message else 'stdout'
        parts, rest = [], message
        while action == 'export' and len(rest) > 50:
            cut = rng.randint(20, 50)
            parts.append(rest[:cut])
            rest = rest[cut:]
        parts.append(rest)
        for i, part in enumerate(parts):
            stamp = (at + timedelta(microseconds=i)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            lines.append(f"{stamp} {stream} {'F' if i == len(parts) - 1 else 'P'} {part}")
    return '\n'.join(lines) + '\n'
