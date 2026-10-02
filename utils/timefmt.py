"""Timestamp patterns: infer a Java (stroom:format-date) pattern from a value, and check a pattern against values.

The profiler used to know ten shapes; anything else was typed as a string and the model guessed a pattern, which
stroom:format-date then failed silently, leaving TimeCreated empty. Here a value is read left to right into
pattern letters, and any pattern (inferred or the model's) is turned back into a regex and tried on the sample's
values before the XSLT is stepped.
"""
import re

MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
_MONTH_FULL = {'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october',
               'november', 'december'}
# Pieces of a timestamp, tried in order at each position. Each gives pattern text, or a callable deciding it.
_ZONE_NAME = re.compile(r'(?:UTC|GMT|Z(?![a-z])|[A-Z]{3,4}T|[A-Z][a-z]+/[A-Z][A-Za-z_]+)')


def infer_time_pattern(value: str) -> str | None:
    """A Java date pattern that parses `value`, e.g. '28/Sep/2026:10:00:00 +0000' -> 'dd/MMM/yyyy:HH:mm:ss Z';
    None when the value does not look like a date and time. Epoch values give 'epoch seconds' / 'epoch milliseconds'."""
    text = value.strip()
    if re.fullmatch(r'1\d{9}', text):
        return 'epoch seconds'
    if re.fullmatch(r'1\d{12}', text):
        return 'epoch milliseconds'
    if re.fullmatch(r'\d{14}', text):
        return 'yyyyMMddHHmmss'
    if re.fullmatch(r'\d{8}', text):
        return 'yyyyMMdd'
    if re.fullmatch(r'\d{8}T\d{6}(\.\d+)?Z?', text):
        frac = re.search(r'\.(\d+)', text)
        return "yyyyMMdd'T'HHmmss" + (f".{'S' * len(frac.group(1))}" if frac else '') + ('X' if text.endswith('Z') else '')
    out, i, n = [], 0, len(text)
    seen_date = seen_time = False
    while i < n:
        rest = text[i:]
        m = re.match(r'\d+', rest)
        if m:
            digits = m.group(0)
            if not seen_date and len(digits) == 4:
                out.append('yyyy')
                # yyyy-MM-dd or yyyy/MM/dd follows
                dm = re.match(r'([-/.])(\d{1,2})\1(\d{1,2})', rest[4:])
                if dm:
                    out += [dm.group(1), 'MM' if len(dm.group(2)) == 2 else 'M', dm.group(1), 'dd' if len(dm.group(3)) == 2 else 'd']
                    i += 4 + len(dm.group(0))
                else:
                    i += 4
                seen_date = True
                continue
            if not seen_date and len(digits) <= 2:
                # day first (dd/MM/yyyy, dd-MMM-yyyy, 'Sep 28' handled under letters), else MM/dd/yyyy
                dm = re.match(r'(\d{1,2})([-/.])(\d{1,2})\2(\d{2,4})', rest)
                if dm:
                    a, sep, b, year = dm.groups()
                    first_is_day = int(a) > 12 or int(b) <= 12 and int(a) <= 12 and sep != '/'
                    d, mo = ('dd', 'MM') if first_is_day else ('MM', 'dd')
                    out += [d if len(a) == 2 else d[0], sep, mo if len(b) == 2 else mo[0], sep, 'yyyy' if len(year) == 4 else 'yy']
                    i += len(dm.group(0))
                    seen_date = True
                    continue
                dm = re.match(r'(\d{1,2})[ -/]([A-Za-z]{3,})[ -/,]+(\d{2,4})', rest)
                if dm and dm.group(2).lower()[:3] in MONTHS:
                    sep1 = rest[len(dm.group(1))]
                    sep2 = rest[len(dm.group(1)) + 1 + len(dm.group(2)):dm.end(3) - len(dm.group(3))]
                    out += ['dd' if len(dm.group(1)) == 2 else 'd', sep1, 'MMM' if len(dm.group(2)) == 3 else 'MMMM', sep2,
                            'yyyy' if len(dm.group(3)) == 4 else 'yy']
                    i += len(dm.group(0))
                    seen_date = True
                    continue
                if out and out[-1] == ' ' and len(out) >= 2 and out[-2] in ('MMM', 'MMMM'):
                    out.append('dd' if len(digits) == 2 else 'd')  # 'Sep 28' / 'Sep  8'
                    i += len(digits)
                    # a year may follow after the time (RFC 3164 has none; 'Sep 28 10:00:00 2026' has)
                    continue
            if not seen_time and len(digits) <= 2:
                tm = re.match(r'(\d{1,2}):(\d{2})(?::(\d{2}))?(?:([.,])(\d{1,9}))?', rest)
                if tm:
                    hour, _, sec, frac_sep, frac = tm.groups()
                    ampm = re.match(r'\s?([AaPp][Mm])', rest[tm.end():])
                    h = ('hh' if len(hour) == 2 else 'h') if ampm else ('HH' if len(hour) == 2 else 'H')
                    out += [h, ':', 'mm'] + ([':', 'ss'] if sec else []) + ([frac_sep, 'S' * len(frac)] if frac else [])
                    i += tm.end()
                    if ampm:
                        out += [' ' if ampm.group(0).startswith(' ') else '', 'a']
                        i += ampm.end()
                    seen_time = True
                    continue
            if seen_time and len(digits) == 4 and not any(p == 'yyyy' for p in out):
                out.append('yyyy')   # 'Sep 28 10:00:00 2026'
                i += 4
                seen_date = True
                continue
            return None
        m = re.match(r'[A-Za-z]+', rest)
        if m:
            word = m.group(0)
            low = word.lower()
            if seen_time and (_ZONE_NAME.match(rest) or low in ('z',)):
                zm = _ZONE_NAME.match(rest)
                out.append('X' if word == 'Z' else 'z')   # X parses a literal Z as UTC, as a zone offset would be
                i += zm.end() if zm else 1
                continue
            if word == 'T' and seen_date and not seen_time:
                out.append("'T'")
                i += 1
                continue
            if low[:3] in MONTHS and (len(word) == 3 or low in _MONTH_FULL) and not seen_date:
                # 'Sep 28 ...' or 'September 28, 2026': month, day, maybe year
                dm = re.match(r'[A-Za-z]+\s+(\d{1,2})(?:,?\s+(\d{4}))?', rest)
                if dm:
                    out += ['MMM' if len(word) == 3 else 'MMMM', ' ' * (dm.start(1) - len(word)) or ' ',
                            'dd' if len(dm.group(1)) == 2 else 'd']
                    if dm.group(2):
                        out += [rest[dm.end(1):dm.start(2)], 'yyyy']
                    i += dm.end()
                    seen_date = True
                    continue
                return None
            if low[:3] in DAYS and (len(word) == 3 or len(word) > 5):
                out.append('EEE' if len(word) == 3 else 'EEEE')
                i += len(word)
                continue
            return None
        ch = text[i]
        if ch in '+-' and seen_time and re.match(r'[+-]\d{2}:?\d{2}', rest):
            out.append('XXX' if ':' in rest[:6] else 'Z')
            i += 6 if ':' in rest[:6] else 5
            continue
        if ch in ' -/:.,T':
            out.append(ch)
            i += 1
            continue
        return None
    if not seen_date:
        return None
    pattern = ''.join(out).strip()
    # Letters that are not pattern letters must be quoted; here only T and Z appear, already quoted.
    return pattern


_LETTER = {
    'y': lambda n: r'\d{4}' if n >= 4 else r'\d{2}', 'u': lambda n: r'\d{4}' if n >= 4 else r'\d{2}',
    'M': lambda n: r'\d{1,2}' if n <= 2 else r'[A-Za-z]{3}' if n == 3 else r'[A-Za-z]{3,}',
    'L': lambda n: r'\d{1,2}' if n <= 2 else r'[A-Za-z]{3,}',
    'd': lambda n: r'\d{1,2}' if n == 1 else r'[ 0-3]?\d', 'D': lambda n: r'\d{1,3}',
    'H': lambda n: r'\d{1,2}', 'k': lambda n: r'\d{1,2}', 'K': lambda n: r'\d{1,2}', 'h': lambda n: r'\d{1,2}',
    'm': lambda n: r'\d{1,2}', 's': lambda n: r'\d{1,2}', 'S': lambda n: r'\d{1,9}', 'n': lambda n: r'\d+', 'N': lambda n: r'\d+',
    'a': lambda n: r'(?:[AaPp]\.?[Mm]\.?)', 'E': lambda n: r'[A-Za-z]{3,}', 'e': lambda n: r'\d|[A-Za-z]{3,}', 'c': lambda n: r'\d|[A-Za-z]{3,}',
    'z': lambda n: r'[A-Za-z][A-Za-z0-9/_+:-]*', 'O': lambda n: r'GMT(?:[+-]\d{1,2}(?::\d{2})?)?',
    'V': lambda n: r'[A-Za-z][A-Za-z0-9/_+-]*', 'v': lambda n: r'[A-Za-z][A-Za-z ]*',
    'X': lambda n: r'(?:Z|[+-]\d{2}(?::?\d{2})?)', 'x': lambda n: r'[+-]\d{2}(?::?\d{2})?', 'Z': lambda n: r'[+-]\d{4}|Z|GMT[+-]\d{2}:\d{2}',
    'G': lambda n: r'[A-Za-z]+', 'Q': lambda n: r'\d|Q\d|[A-Za-z ]+', 'q': lambda n: r'\d|Q\d', 'w': lambda n: r'\d{1,2}',
    'W': lambda n: r'\d', 'F': lambda n: r'\d', 'Y': lambda n: r'\d{4}' if n >= 4 else r'\d{2}', 'p': lambda n: r' *',
}


def pattern_regex(pattern: str) -> re.Pattern | None:
    """A regex matching the strings a Java date pattern parses (roughly: counts of digits and letter words)."""
    out, i = ['^'], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "'":
            end = pattern.find("'", i + 1)
            if end == i + 1:        # '' is a literal quote
                out.append("'")
                i += 2
                continue
            if end < 0:
                return None
            out.append(re.escape(pattern[i + 1:end]))
            i = end + 1
            continue
        if ch.isalpha():
            if ch not in _LETTER:
                return None
            n = 1
            while i + n < len(pattern) and pattern[i + n] == ch:
                n += 1
            out.append(_LETTER[ch](n))
            i += n
            continue
        out.append(re.escape(ch))
        i += 1
    try:
        return re.compile(''.join(out) + '$')
    except re.error:
        return None


def check_time_format(pattern: str, values: list[str]) -> str | None:
    """Why `pattern` does not fit the sample's values, or None when every non-empty value matches it."""
    if pattern in ('epoch_ms', 'epoch_s', 'epoch seconds', 'epoch milliseconds'):
        bad = [v for v in values if v.strip() and not re.fullmatch(r'\d{9,13}', v.strip())]
        return f"time_format {pattern!r} expects digits only; the sample has {bad[:3]}" if bad else None
    regex = pattern_regex(pattern)
    if regex is None:
        return f"time_format {pattern!r} is not a Java date pattern this check understands"
    bad = [v for v in values if v.strip() and not regex.match(v.strip())]
    if not bad:
        return None
    inferred = infer_time_pattern(bad[0])
    return (f"time_format {pattern!r} does not match the sample's values, e.g. {bad[:3]}"
            + (f"; the values look like {inferred!r}" if inferred and inferred != pattern else ''))
