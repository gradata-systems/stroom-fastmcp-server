"""A case's reference solution checked offline, with no Stroom and no model: what build_translation_xslt checks
before it saves (the mapping generates against the schema; the sample is read as the server's Data Splitter reads
it; every field and time format fits the records; the extractions match), plus the record count the case expects.
Stepping and processing in Stroom are run_eval.py --reference's.

    uv run python dev/eval/offline.py [case ...]
"""
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_eval as ev  # noqa: E402
from utils.dsgen import SplitterSpec, dry_run, infer_spec  # noqa: E402
from utils.eventschema import EventSchema  # noqa: E402
from utils.localcheck import check_mapping, sample_records  # noqa: E402
from utils.xpathcheck import check_extractions, check_xpaths  # noqa: E402
from utils.xsltgen import TranslationMapping, generate  # noqa: E402

SCHEMA_FILE = ev.ROOT / 'tests' / 'fixtures' / 'event-logging-v4.1.0.xsd'
_schema: EventSchema | None = None


def schema() -> EventSchema:
    global _schema
    if _schema is None:
        _schema = EventSchema.parse(SCHEMA_FILE.read_bytes())
    return _schema


def texts_of(case: dict[str, Any]) -> list[str]:
    """The sample as the server reads its streams: the files whole (decoded as the feed would be), else the samples."""
    if case.get('files'):
        encodings = {f['name']: f.get('encoding') or 'utf-8' for f in case['files']}
        return [data.decode(encodings[name]) for name, data in ev.case_files(case)]
    return ev.samples_of(case)


def check(case: dict[str, Any]) -> dict[str, Any]:
    """{'problems', 'warnings', 'records', 'splitter'}: problems is empty when the reference would be saved."""
    reference = case['reference']
    mapping = TranslationMapping.model_validate(reference['mapping'])
    generated = generate(mapping, schema(), '4.1.0')
    problems, warnings = list(generated['problems']), list(generated.get('warnings') or [])
    texts = texts_of(case)
    splitter = None
    if mapping.input == 'data_splitter':
        given = reference.get('splitter')
        if given == 'infer':
            splitter, _ = infer_spec(texts[0])
            if splitter is None:
                problems.append('the server infers no Data Splitter from the sample')
        elif isinstance(given, dict):
            splitter = SplitterSpec.model_validate(given)
        elif reference.get('converter'):
            # A hand-written converter (the first cases): Stroom reads it, not the server; nothing to check here.
            return {'problems': problems, 'warnings': warnings, 'records': None, 'splitter': None}
    records, note = sample_records(mapping, texts, splitter)
    checked = check_mapping(mapping, records)
    problems += checked['problems']
    warnings += checked['warnings'] + ([note] if note else []) + check_xpaths(mapping, texts, splitter)
    extraction_problems, extraction_warnings = check_extractions(mapping, texts, splitter, records)
    problems += extraction_problems
    warnings += extraction_warnings
    counted = len(records)
    if case.get('files') and splitter is not None:
        # The server reads a sample's first records only: files are counted whole, as Stroom will split them.
        counted = sum(len(dry_run(splitter, t.strip('﻿\r\n '), max_records=10 ** 7)['records']) for t in texts)
    if counted != case['expected']['records']:
        problems.append(f"{counted} records read from the sample, expected {case['expected']['records']}")
    return {'problems': problems, 'warnings': warnings, 'records': counted,
            'splitter': splitter.model_dump(exclude_none=True) if splitter else None}


if __name__ == '__main__':
    failed = 0
    cases = ev.load_cases(sys.argv[1:])
    for case in cases:
        result = check(case)
        failed += bool(result['problems'])
        print(f"{'FAIL' if result['problems'] else 'ok  '} {case['id']}: {result['records']} records")
        for p in result['problems']:
            print(f"    - {p}")
        for w in result['warnings']:
            print(f"    . {w}")
    print(f"{len(cases) - failed} of {len(cases)} cases pass offline")
    raise SystemExit(1 if failed else 0)
