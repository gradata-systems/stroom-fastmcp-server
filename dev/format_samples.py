"""Samples of the formats sources send, for dev/e2e_formats.py: each as a source would write it, a few records."""

SAMPLES = {
    # Delimited text
    'csv_quoted': (
        'time,user,src_ip,action,message\n'
        '2026-10-01T08:00:00Z,alice,10.0.0.5,login,"Logged in, from the VPN"\n'
        '2026-10-01T08:05:00Z,"o\'brien, pat",10.0.0.6,logout,"Said ""bye"" and left"\n'
        '2026-10-01T08:07:00Z,carol,10.0.0.7,login,Plain message\n'),
    'tsv': (
        'time\tuser\tsrc_ip\taction\tmessage\n'
        '2026-10-01T08:00:00Z\talice\t10.0.0.5\tlogin\tLogged in, from the VPN\n'
        '2026-10-01T08:05:00Z\tbob\t10.0.0.6\tlogout\tLogged out\n'
        '2026-10-01T08:07:00Z\tcarol\t10.0.0.7\tlogin\t\n'),
    'pipe': (
        'time|user|src_ip|action|message\n'
        '2026-10-01T08:00:00Z|alice|10.0.0.5|login|Logged in, from the VPN\n'
        '2026-10-01T08:05:00Z|bob|10.0.0.6|logout|Logged out\n'
        '2026-10-01T08:07:00Z|carol|10.0.0.7|login|\n'),
    'csv_noheader': (
        '2026-10-01T08:00:00Z,alice,10.0.0.5,login\n'
        '2026-10-01T08:05:00Z,bob,10.0.0.6,logout\n'
        '2026-10-01T08:07:00Z,carol,10.0.0.7,login\n'),
    # Syslog
    'syslog3164_freeform': (
        '<38>Oct  1 08:00:00 bastion01 sshd[2211]: Accepted password for alice from 10.0.0.5 port 52211 ssh2\n'
        '<38>Oct  1 08:05:00 bastion01 sshd[2215]: Failed password for bob from 203.0.113.9 port 40022 ssh2\n'
        '<38>Oct  1 08:07:00 bastion01 sshd[2219]: Accepted publickey for carol from 10.0.0.7 port 52219 ssh2\n'),
    'syslog5424_kv': (
        '<134>1 2026-10-01T08:00:00.000Z fw01 firewall 411 - - action=allow src=10.0.0.5 dst=93.184.216.34 dport=443 proto=tcp user=alice\n'
        '<134>1 2026-10-01T08:05:00.000Z fw01 firewall 411 - - action=deny src=203.0.113.9 dst=10.0.0.10 dport=3389 proto=tcp user=-\n'
        '<134>1 2026-10-01T08:07:00.000Z fw02 firewall 412 - - action=allow src=10.0.0.7 dst=8.8.8.8 dport=53 proto=udp user=carol\n'),
    # CEF, plain and inside syslog: header fields split on |, the extension is key=value with spaces in values.
    'cef': (
        'CEF:0|Acme|Gateway|2.1|100|User logged in|3|rt=Oct 01 2026 08:00:00 UTC suser=alice src=10.0.0.5 act=login msg=Logged in from the VPN\n'
        'CEF:0|Acme|Gateway|2.1|101|User logged out|3|rt=Oct 01 2026 08:05:00 UTC suser=bob src=10.0.0.6 act=logout msg=Session ended\n'
        'CEF:0|Acme|Gateway|2.1|102|Login failed|7|rt=Oct 01 2026 08:07:00 UTC suser=carol src=203.0.113.9 act=failed msg=Bad password\n'),
    'syslog_cef': (
        '<134>Oct  1 08:00:00 gw01 CEF:0|Acme|Gateway|2.1|100|User logged in|3|suser=alice src=10.0.0.5 act=login msg=Logged in from the VPN\n'
        '<134>Oct  1 08:05:00 gw01 CEF:0|Acme|Gateway|2.1|101|User logged out|3|suser=bob src=10.0.0.6 act=logout msg=Session ended\n'
        '<134>Oct  1 08:07:00 gw01 CEF:0|Acme|Gateway|2.1|102|Login failed|7|suser=carol src=203.0.113.9 act=failed msg=Bad password\n'),
    # key=value with quoted values (FortiGate style)
    'kv_quoted': (
        'date=2026-10-01 time=08:00:00 devname="fw01" type="traffic" action="accept" srcip=10.0.0.5 dstip=93.184.216.34 user="alice"\n'
        'date=2026-10-01 time=08:05:00 devname="fw01" type="traffic" action="deny" srcip=203.0.113.9 dstip=10.0.0.10 user="bob smith"\n'
        'date=2026-10-01 time=08:07:00 devname="fw02" type="traffic" action="accept" srcip=10.0.0.7 dstip=8.8.8.8 user="carol"\n'),
    # JSON
    'json_lines': (
        '{"ts": "2026-10-01T08:00:00Z", "user": {"name": "alice"}, "src": "10.0.0.5", "event": "login"}\n'
        '{"ts": "2026-10-01T08:05:00Z", "user": {"name": "bob"}, "src": "10.0.0.6", "event": "logout"}\n'
        '{"ts": "2026-10-01T08:07:00Z", "user": {"name": "carol"}, "src": "10.0.0.7", "event": "login"}\n'),
    'json_array_nested': (
        '[{"time": "2026-10-01T08:00:00Z", "actor": {"user": "alice", "ip": "10.0.0.5"}, "action": "login", "tags": ["vpn"]},\n'
        ' {"time": "2026-10-01T08:05:00Z", "actor": {"user": "bob", "ip": "10.0.0.6"}, "action": "logout", "tags": []},\n'
        ' {"time": "2026-10-01T08:07:00Z", "actor": {"user": "carol", "ip": "10.0.0.7"}, "action": "login", "tags": ["office"]}]\n'),
    # XML documents
    'xml_records': (
        '<?xml version="1.0" encoding="UTF-8"?>\n<records>\n'
        '<record><time>2026-10-01T08:00:00Z</time><user>alice</user><ip>10.0.0.5</ip><action>login</action></record>\n'
        '<record><time>2026-10-01T08:05:00Z</time><user>bob</user><ip>10.0.0.6</ip><action>logout</action></record>\n'
        '<record><time>2026-10-01T08:07:00Z</time><user>carol</user><ip>10.0.0.7</ip><action>login</action></record>\n'
        '</records>\n'),
    'xml_attributes': (
        '<Audit system="badge">\n'
        '<Entry time="2026-10-01T08:00:00Z" user="alice" ip="10.0.0.5" action="login"/>\n'
        '<Entry time="2026-10-01T08:05:00Z" user="bob" ip="10.0.0.6" action="logout"/>\n'
        '<Entry time="2026-10-01T08:07:00Z" user="carol" ip="10.0.0.7" action="login"/>\n'
        '</Audit>\n'),
    'xml_namespaced': (
        '<log:Events xmlns:log="urn:acme:log:1">\n'
        '<log:Event><log:Time>2026-10-01T08:00:00Z</log:Time><log:User>alice</log:User><log:Ip>10.0.0.5</log:Ip><log:Action>login</log:Action></log:Event>\n'
        '<log:Event><log:Time>2026-10-01T08:05:00Z</log:Time><log:User>bob</log:User><log:Ip>10.0.0.6</log:Ip><log:Action>logout</log:Action></log:Event>\n'
        '<log:Event><log:Time>2026-10-01T08:07:00Z</log:Time><log:User>carol</log:User><log:Ip>10.0.0.7</log:Ip><log:Action>login</log:Action></log:Event>\n'
        '</log:Events>\n'),
    # XML fragments that declare their own namespace (Windows event XML, say)
    'xml_fragments_ns': (
        '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System><EventID>4624</EventID><TimeCreated SystemTime="2026-10-01T08:00:00.000Z"/><Computer>dc01</Computer></System><EventData><Data Name="TargetUserName">alice</Data><Data Name="IpAddress">10.0.0.5</Data></EventData></Event>\n'
        '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System><EventID>4634</EventID><TimeCreated SystemTime="2026-10-01T08:05:00.000Z"/><Computer>dc01</Computer></System><EventData><Data Name="TargetUserName">bob</Data><Data Name="IpAddress">10.0.0.6</Data></EventData></Event>\n'
        '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System><EventID>4624</EventID><TimeCreated SystemTime="2026-10-01T08:07:00.000Z"/><Computer>dc01</Computer></System><EventData><Data Name="TargetUserName">carol</Data><Data Name="IpAddress">10.0.0.7</Data></EventData></Event>\n'),
}
