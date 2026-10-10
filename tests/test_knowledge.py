"""The guides, prompts and server instructions name tools and stroom: functions that exist. Knowledge lives in several
places; this keeps them from drifting apart from the code."""
import re
from pathlib import Path

from main_tools import TOOL_MODULES
from tools import resources
from tools.validation import STROOM_FUNCTIONS

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {t.__name__ for m in TOOL_MODULES for t in m.ALL_TOOLS}
GUIDES = {p.stem: p.read_text(encoding='utf-8') for p in (ROOT / 'knowledge' / 'guides').glob('*.md')}
PROMPTS = {'instructions': resources.SERVER_INSTRUCTIONS}
# Words that look like tool names (snake_case, a known verb first) but are not tools.
NOT_TOOLS = {'data_splitter', 'xml_fragments', 'json_layout', 'xpath_default_namespace', 'time_format', 'any_of',
             'in_dictionary', 'data_name', 'xml_namespace', 'key_xpath', 'pair_separator', 'body_field',
             'strip_domain', 'event_logging_path', 'type_id', 'event_detail', 'stream_type', 'stream_ids',
             'loader_pipeline', 'replace_parser', 'output_type', 'source_pipeline_uuid', 'draft_code', 'pipeline_uuid',
             'index_uuid', 'records_per_stream', 'skip_stream_ids', 'known_signatures', 'expect_events', 'filter_id',
             'events_stream_ids',
             'source_confirmation_id', 'confirmation_id', 'approval_id', 'field_mapping', 'pipeline_link', 'extra_fields',
             'index_name', 'timestamp_field', 'source_docs', 'sample_size', 'input_fields', 'working_copy', 'set_properties',
             'dev_tools', 'pipeline_properties', 'xslt_input', 'text_converter', 'parser_properties', 'variable_min_reads',
             'inline_map_max_keys', 'snake_case', 'manual_steps', 'expected_paths', 'raw_stream', 'path_population',
             'sample_check', 'fields_seen', 'converter_type', 'reference_data', 'index_patterns', 'event_types',
             'max_chars_per_stream', 'max_parts_per_stream', 'max_records', 'ignore_warnings', 'index_plan', 'drop_when',
             'for_each', 'mark_rules', 'reuse_existing_docs', 'accept_parser_mismatch', 'build_status'}
VERBS = ('find', 'get', 'list', 'describe', 'create', 'update', 'copy', 'set', 'build', 'check', 'validate', 'step',
         'compare', 'profile', 'upload', 'record', 'start', 'write', 'promote', 'reprocess', 'wait', 'summarise',
         'survey', 'locate', 'read', 'run', 'propose', 'draft', 'test', 'processing', 'onboard', 'index', 'evaluate',
         'fix', 'generate')


def tool_like(text: str) -> set[str]:
    words = set(re.findall(r'\b([a-z]+_[a-z_]+)\b', text))
    return {w for w in words if w.split('_')[0] in VERBS and w not in NOT_TOOLS}


def test_guides_and_instructions_name_tools_that_exist():
    prompts = {p.__name__: p.__wrapped__ if hasattr(p, '__wrapped__') else p for p in []}
    for name, text in {**GUIDES, **PROMPTS}.items():
        unknown = tool_like(text) - TOOLS - {'onboard_data_source', 'onboard_existing_feed', 'update_events_pipeline',
                                             'update_indexing_pipeline', 'index_event_data', 'create_discovery_index',
                                             'evaluate_events_pipeline', 'fix_pipeline_issue', 'document_index'}
        assert not unknown, (name, unknown)


def test_guides_name_stroom_functions_that_exist():
    for name, text in GUIDES.items():
        used = set(re.findall(r'stroom:([A-Za-z][A-Za-z0-9-]*)\(', text)) - {'json-parse'}   # named as the non-example
        assert used <= STROOM_FUNCTIONS, (name, used - STROOM_FUNCTIONS)
    assert 'json-parse' not in STROOM_FUNCTIONS


def test_the_guide_index_lists_every_guide():
    source = re.sub(r'"\s*\n\s*"', '', (ROOT / 'tools' / 'resources.py').read_text(encoding='utf-8'))
    listed = set(re.findall(r'Working guides: ([^"]+)"', source)[0].rstrip('.').split(', '))
    assert listed == set(GUIDES), (listed ^ set(GUIDES))


# The docs list what the code has: a tool, an e2e suite, an evaluation case or workflow added without them fails here,
# so a release can't go out with the docs behind (dev/release.py runs these).
PROMPT_NAMES = {'onboard_data_source', 'onboard_existing_feed', 'update_events_pipeline', 'update_indexing_pipeline',
                'index_event_data', 'create_discovery_index', 'evaluate_events_pipeline', 'fix_pipeline_issue',
                'document_index', 'forward_events_as_cef', 'review_cef_pipeline', 'check_feed_coverage'}
DESIGN = (ROOT / 'docs' / 'DESIGN.md').read_text(encoding='utf-8')


def test_the_design_lists_every_tool_and_only_tools_and_prompts():
    rows = set(re.findall(r'^\| `([a-z_]+)`', DESIGN, re.M))
    assert TOOLS - rows == set(), f"tools DESIGN's catalogue lacks: {sorted(TOOLS - rows)}"
    assert rows - TOOLS - PROMPT_NAMES == set(), f"DESIGN rows that are no tool or prompt: {sorted(rows - TOOLS - PROMPT_NAMES)}"


def test_the_design_lists_every_e2e_suite():
    suites = {p.stem for p in (ROOT / 'dev').glob('e2e_*.py')} - {'e2e_cleanup'}   # a utility, not a suite
    missing = sorted(s for s in suites if f'dev/{s}.py' not in DESIGN)
    assert not missing, f"e2e suites DESIGN's table lacks: {missing}"
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    missing = sorted(s for s in suites if f'dev/{s}.py' not in readme)
    assert not missing, f"e2e suites the README's table lacks: {missing}"


def test_the_evaluation_readme_lists_every_case_and_workflow():
    import sys
    sys.path.insert(0, str(ROOT / 'dev' / 'eval'))
    import workflows
    readme = (ROOT / 'dev' / 'eval' / 'README.md').read_text(encoding='utf-8')
    cases = [p.stem for p in (ROOT / 'dev' / 'eval' / 'cases').glob('*.yaml')]
    assert not [c for c in cases if f'`{c}`' not in readme], 'evaluation cases the README lacks'
    assert not [w for w in workflows.WORKFLOWS if f'`{w}`' not in readme], 'workflows the README lacks'
