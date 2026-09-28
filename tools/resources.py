"""MCP resources (reference guides) and prompts (packaged workflows)."""
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError

GUIDES = Path(__file__).resolve().parents[1] / 'knowledge' / 'guides'


def register(mcp: FastMCP) -> None:
    @mcp.resource('stroom://guide/{name}', mime_type='text/markdown',
                  description="Working guides: event-logging, xslt, data-splitter, json-input, indexing.")
    def guide(name: str) -> str:
        path = GUIDES / f'{name}.md'
        if not path.is_file() or path.parent != GUIDES:
            raise ResourceError(f"No guide '{name}'. Guides: {', '.join(sorted(p.stem for p in GUIDES.glob('*.md')))}")
        return path.read_text(encoding='utf-8')

    @mcp.resource('stroom://guides', mime_type='text/markdown', description="Index of the working guides.")
    def guides() -> str:
        lines = ['# Guides', '']
        for path in sorted(GUIDES.glob('*.md')):
            title = path.read_text(encoding='utf-8').splitlines()[0].lstrip('# ')
            lines.append(f'- `stroom://guide/{path.stem}`: {title}')
        return '\n'.join(lines)

    @mcp.prompt(description="Evaluate and document an existing events pipeline (read-only).")
    def evaluate_events_pipeline(pipeline: str, sample_size: int = 50, source_docs: str = '') -> str:
        docs = f"\n\nThe user supplied this source documentation; use it for field meanings and event types:\n{source_docs}" \
            if source_docs else ''
        return f"""Evaluate the Stroom events pipeline "{pipeline}" and write a report. Change nothing in Stroom.

1. Describe the pipeline: find it (find_documents), then describe_pipeline for its template chain,
   elements, what it overrides or removes, and its reference data. get_document its XSLT and text converter.
   processing_status shows which feeds its processor filters cover.
2. Sample the data: find_streams for recent Raw Events on each of those feeds, and the Events and Error
   streams the pipeline produced from them (get_stream_children). Step up to {sample_size} raw records
   with step_sample, and look at one or two records in detail with step_pipeline.
3. Map the translation: describe_translation on its XSLT. Compare input_fields with the fields in the
   raw data (read_stream, profile_sample) to find inputs that are never used.
4. Inventory the events: summarise_events on its Events streams.
5. Measure conformance: validate_events and check_event_quality on sample Events records;
   summarise_errors on recent raw streams it processed. Give rates, e.g. "97% valid".
6. Suggest improvements, prioritised, each with the rule or field it fixes, the share of events affected,
   and a draft XSLT change.

Report sections: Purpose and data; Processing; Field mapping; Event types; Schema conformance; Suggestions.
Read stroom://guide/event-logging and stroom://guide/xslt first if you need them.{docs}"""
