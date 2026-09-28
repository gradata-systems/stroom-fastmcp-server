# Stroom FastMCP server

MCP server that lets a chat client or agent take a raw data sample and build working Stroom
content for it: a feed, an event-logging translation pipeline, and an indexing (Lucene or Elasticsearch)
pipeline, stepped and verified before anything is promoted. See [docs/DESIGN.md](docs/DESIGN.md).

Status: Phase 2 (build and fix translations). 40 tools:

| Group | Tools |
| --- | --- |
| Explorer and pipelines | `find_documents`, `get_document`, `describe_pipeline` |
| Templates | `find_pipeline_templates`, `list_template_children`, `describe_template_contract`, `find_similar_translations` |
| Samples and feeds | `profile_sample`, `create_feed`*, `upload_sample`, `record_source_notes` |
| Translation | `create_text_converter`, `update_text_converter`, `create_xslt`, `update_xslt` |
| Pipelines | `create_pipeline`*, `copy_pipeline`*, `set_pipeline_property` |
| Streams and errors | `find_streams`, `get_stream_children`, `read_stream`, `get_stream_attributes`, `summarise_errors`, `summarise_events` |
| Stepping | `step_pipeline`, `step_sample`, `compare_outputs` (with unsaved draft code) |
| Processing | `processing_status`, `create_processor_filter`**, `set_processor_filter_enabled`**, `reprocess_streams`**, `wait_for_processing` |
| Validation | `check_xslt`, `validate_events`, `check_event_quality`, `describe_translation` |
| Builds | `start_build`, `list_build`, `write_documentation`, `promote_build`** |

\* needs the user's confirmation, \*\* needs approval: asked through MCP elicitation where the client supports it,
otherwise returned as an id to pass back. Everything is written under `MCP Workspace/<build>` and tagged
`mcp-managed`; only tagged docs can be changed, and promotion moves them into place.

Resources: `stroom://guides` and `stroom://guide/{name}`. Prompt: `evaluate_events_pipeline`.
Error triage rules: `error_rules.yaml`. Template sources: `access_policy.yaml`.

To call a tool directly during development (no MCP client or Keycloak):
`uv run python dev/try_tool.py find_pipeline_templates stage=translation` against the local stack,
or `--live` for the instance in `.ai/secrets` (read-only tools only).

## Running

```
cp .env.example .env   # fill in
uv run python main.py
```

Tests: `uv run pytest`.

## Local Stroom for development

`dev/stroom` runs Stroom v7.13 and MySQL in Docker, bound to localhost. Destructive tests run
here, never against a shared instance.

```
cd dev/stroom && ./init-env.sh && docker compose up -d
```

`dev/e2e_phase2.py` runs the Phase 2 exit test against it. The Phase 0 spike (`spike/phase0.py`) proves the risky Stroom APIs against it; results are in
[spike/FINDINGS.md](spike/FINDINGS.md).
