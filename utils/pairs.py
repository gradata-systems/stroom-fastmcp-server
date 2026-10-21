"""Key/value pairs in a text field: the regex that reads one key's value, for each way a text writes its pairs.

A mapping's pairs entry names a text field, its style and its keys; it stands for an extract entry a key, with these
regexes (as the draft writes them, and as the XSLT folds them into one function called with the key). Seen in
testing: a Fortigate body's 60 keys as 60 extract entries were a third of a 50 KB mapping the agent had to send whole.
"""
import re

# A key follows the text's start or a separator (; | ( ) ] or a spaced dash) and is one to four words before ': '
# (then its value, or [value]); or one word before '=' (value, or "value"). Seen in production: Secret Server's audit
# text in a Windows event's Data, "... Event: [User] Action: [Login] By User: domain.com\priya.patel (Item Id: 219);
# Source IP: 192.0.2.45", its pairs parted by ;, | or ' - ' from record to record.
BOUND = r'(?:^|[;|()\]]|\s-)\s*'
WORDS = r'[A-Za-z][A-Za-z0-9_-]*(?: [A-Za-z][A-Za-z0-9_-]*){0,3}'
# The value runs to the next separator: a dash only where a key or a [ follows it ("Secret: Firewall - Edge Admin;").
PAIR_REGEX = {
    'colon': BOUND + '{key}: (.*?)(?:\\s*[;|()]|\\s+-\\s+(?:\\[|' + WORDS + ': )|\\s*$)',
    'bracketed': BOUND + '{key}: \\[([^\\]]*)\\]',
    'equals': '(?:^|[\\s,;]){key}=([^\\s,;"]*)',
    'quoted': '(?:^|[\\s,;]){key}="([^"]*)"',
}
STYLES = tuple(PAIR_REGEX)


def pair_regex(style: str, key: str) -> str:
    """The regex reading one key's value, its first group the value."""
    return PAIR_REGEX[style].replace('{key}', key.replace('.', '\\.'))


def key_name(key: str) -> str:
    """The field a key's value is, by default: the key in snake case ('Source IP' -> source_ip)."""
    return re.sub(r'[^A-Za-z0-9]+', '_', key).strip('_').lower()


def pair_of(regex: str) -> tuple[str, str] | None:
    """(style, key) for a regex pair_regex writes, else None."""
    for style, template in PAIR_REGEX.items():
        before, after = template.split('{key}')
        if regex.startswith(before) and regex.endswith(after) and len(regex) > len(before) + len(after):
            key = regex[len(before):len(regex) - len(after)].replace('\\.', '.')
            if pair_regex(style, key) == regex and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_. -]*', key):
                return style, key
    return None
