"""The parsing capabilities the evaluation set exercises, and which cases exercise each (a case's `capabilities:`).

Asked for by the user: the set is organised around what a server must be able to read, not only around source
formats, so a gap is a capability with no case. tests/test_eval.py fails when a capability has neither a case nor a
reason it is pending, and when the README's table is not this one.

    uv run python dev/eval/capabilities.py      # the README table
"""
from typing import Any

CAPABILITIES = {
    'format_recognition': ('Format recognition', 'JSON, XML, CSV, CEF, syslog and free text detected without being told'),
    'schema_inference': ('Schema inference', 'fields and their types inferred from the sample'),
    'field_variability': ('Field variability', 'different fields present for different kinds of event'),
    'delimiters': ('Delimiter detection', 'commas, tabs, pipes, semicolons, spaces and separators of several characters'),
    'escaping': ('Escaping', 'embedded and doubled quotes, escaped separators'),
    'nesting': ('Nested structures', 'JSON objects and arrays, XML elements and attributes'),
    'timestamps': ('Timestamp recognition', 'ISO 8601, epoch (seconds, milliseconds, nanoseconds), syslog, split and custom formats'),
    'type_inference': ('Type inference', 'addresses, integers, booleans, values the schema rejects as written'),
    'multiline': ('Multi-line records', 'stack traces, quoted values over several lines'),
    'event_boundaries': ('Event boundaries', 'where one record ends: fragments, arrays, wrapped objects, several events in one'),
    'optional_fields': ('Optional fields', 'missing, empty and placeholder values'),
    'field_normalization': ('Field normalisation', "a source's names mapped to the schema's (ECS, codes, odd fields)"),
    'schema_variation': ('Schema variation', 'many kinds of event from one source'),
    'malformed_records': ('Malformed records', 'truncation, invalid quoting, missing separators, reported not dropped'),
    'mixed_formats': ('Mixed formats', 'syslog wrapping CEF or CSV, JSON or text inside JSON, text inside XML'),
    'encoding': ('Encoding', 'non-UTF-8 files, Unicode punctuation, control characters'),
    'field_repetition': ('Field repetition', 'repeated keys and elements, arrays of values'),
    'format_evolution': ('Format evolution', 'new or reordered fields, a changed version, mid-file'),
    'large_samples': ('Large samples', 'samples bigger than one read, the kinds of event deep in the file'),
}
# Capabilities with no case yet, and why: the test accepts these, the README shows them.
PENDING = {
    'malformed_records': 'no case yet: a sample with truncated lines, unbalanced quotes and missing separators, whose '
                         'records the server reports rather than drops',
}


def covered(cases: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Each capability's cases, by id."""
    out: dict[str, list[str]] = {key: [] for key in CAPABILITIES}
    for case in cases:
        for key in case.get('capabilities') or []:
            out.setdefault(key, []).append(case['id'])
    return out


def table(cases: list[dict[str, Any]]) -> str:
    """The README's table: each capability, what it means, and its cases (or why it has none)."""
    rows = ['| Capability | What | Cases |', '| --- | --- | --- |']
    for key, ids in covered(cases).items():
        name, what = CAPABILITIES.get(key, (key, ''))
        cell = ', '.join(f'`{i.split("_")[0]}`' for i in sorted(ids)) or f'pending: {PENDING.get(key, "none")}'
        rows.append(f'| {name} | {what} | {cell} |')
    return '\n'.join(rows)


if __name__ == '__main__':
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    import run_eval
    print(table(run_eval.load_cases([])))
