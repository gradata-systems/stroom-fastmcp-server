# Stroom FastMCP server

MCP server that lets a chat client or agent take a raw data sample and build working Stroom
content for it: a feed, an event-logging translation pipeline, and an indexing (Lucene or Elasticsearch)
pipeline, stepped and verified before anything is promoted. See [docs/DESIGN.md](docs/DESIGN.md).

Status: Phase 4 (LangGraph agent in `agent/`, see below). The server has 55 tools:

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
| Diagnosis | `locate_event` (stream and event back to raw part and record), `summarise_fix` (prove a fix, diff, manual steps) |
| Validation | `check_xslt`, `validate_events`, `check_event_quality`, `describe_translation` |
| Generation | `build_translation_xslt` (event-logging XSLT from a field mapping, checked against the schema) |
| Indexing | `get_field_conventions`, `draft_index_mapping`, `create_index_doc`*, `set_index_fields`, `create_indexing_pipeline`*, `create_verification_dashboard`, `run_test_searches` |
| Elasticsearch | `find_elastic_clusters`, `list_index_templates`, `simulate_index_template`, `put_index_template`**, `test_elastic_index` |
| Builds | `start_build`, `list_build`, `write_documentation`, `promote_build`** |

\* needs the user's confirmation, \*\* needs approval: asked through MCP elicitation where the client supports it,
otherwise returned as an id to pass back. Everything is written under `MCP Workspace/<build>` and tagged
`mcp-managed`; only tagged docs can be changed, and promotion moves them into place.

Design decisions (see [docs/DESIGN.md](docs/DESIGN.md#open-questions-risks-and-delivery)):
- Every Stroom call, including datafeed uploads, acts as the signed-in user. The server forwards their Keycloak
  token, whose `aud` must include `stroom` as well as the MCP audience. There is no shared API key.
- Reprocessing is allowed while developing a pipeline in the workspace: `reprocess_streams` takes at most 10 streams
  per call and runs one task at a time, and Stroom supersedes (deletes) the earlier output. Promoted pipelines are
  refused by the write guard, so reprocessing production data is the user's, as is moving readers from one index
  version to the next.
- Elasticsearch indexing runs only through the Stroom indexing pipeline, after the user confirms that the index
  template for the destination index (named in the question) has been written.
- Indexing filters over Events also carry `Pipeline IS_DOC_REF <events pipeline>`, where the events pipeline is the
  one this server built for the source (`source_pipeline_uuid`), so no Events from elsewhere are picked up.

Resources: `stroom://guides`, `stroom://guide/{name}`, `stroom://conventions/{name}`. Prompts: `onboard_data_source`,
`update_events_pipeline`, `update_indexing_pipeline`, `index_event_data`, `create_discovery_index`, `evaluate_events_pipeline`,
`fix_pipeline_issue`.
Field conventions: `conventions/*.yaml`. Elasticsearch (templates only) is optional: `STROOM_MCP_ES_URL`.
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

`dev/e2e_phase2.py` and `dev/e2e_phase3.py` run the Phase 2 and 3 exit tests against it.
`dev/e2e_agent_transport.py` checks the agent against a local server over MCP (no model needed). The Phase 0 spike (`spike/phase0.py`) proves the risky Stroom APIs against it; results are in
[spike/FINDINGS.md](spike/FINDINGS.md).

## LangGraph agent

`agent/` is a LangGraph build agent that uses only this server. Install it with the `agent` extra.

- `graph.py`: one node per workflow step. Each node is a small tool-calling agent with its own tool subset. Routing between nodes is code, not the model: step verdicts, processing gates and search results decide the next node. Retry loops are capped at 5 attempts, after which the agent asks the user for help.
- `gating.py`: when a tool replies `needs_confirmation`/`needs_approval`/`needs_guidance`, the agent raises a LangGraph `interrupt`. If the user agrees, the tool is re-called with the id; a decline returns the user's note to the model.
- `mcp_tools.py`: loads the tools with `fastmcp.Client`. It does not use `langchain-mcp-adapters`, which pins `mcp<2`.
- `auth.py`: signs the person in with Keycloak's device grant and refreshes their token, so the agent acts as them.
- `run.py`: a terminal runner. It needs `AGENT_MCP_URL`, `AGENT_REALM_URL` and `AGENT_CLIENT_ID` (or `AGENT_BEARER`),
  and `AGENT_MODEL`:

```
uv run --extra agent python -m agent.run --sample sample.csv "Onboard Acme VPN logs"
uv run --extra agent python -m agent.run --mode fix_pipeline_issue "Stream 15768876 event 3: the user id is missing"
```

In `fix_pipeline_issue` mode the agent locates the event, confirms the problem, proves a fix, and asks whether
to apply it or give you the manual steps.

For local development, `STROOM_MCP_DEV_NO_AUTH=true` runs the server without Keycloak and calls Stroom with
`STROOM_MCP_STROOM_API_KEY`. The server refuses to start that way unless it is bound to localhost, and refuses the
API key when authentication is on.
