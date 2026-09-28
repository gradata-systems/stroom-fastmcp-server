# Stroom FastMCP server

MCP server that lets a chat client or agent take a raw data sample and build working Stroom
content for it: a feed, an event-logging translation pipeline, and an indexing (Lucene or Elasticsearch)
pipeline, stepped and verified before anything is promoted. See [docs/DESIGN.md](docs/DESIGN.md).

Status: Phase 1 (read and validate). 21 read-only tools:

| Group | Tools |
| --- | --- |
| Explorer and pipelines | `find_documents`, `get_document`, `describe_pipeline` |
| Templates | `find_pipeline_templates`, `list_template_children`, `describe_template_contract`, `find_similar_translations` |
| Samples | `profile_sample` |
| Streams and errors | `find_streams`, `get_stream_children`, `read_stream`, `get_stream_attributes`, `summarise_errors`, `summarise_events` |
| Stepping | `step_pipeline`, `step_sample` (with unsaved draft code) |
| Processing | `processing_status` |
| Validation | `check_xslt`, `validate_events`, `check_event_quality`, `describe_translation` |

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

The Phase 0 spike (`spike/phase0.py`) proves the risky Stroom APIs against it; results are in
[spike/FINDINGS.md](spike/FINDINGS.md).
