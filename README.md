# Stroom FastMCP server

MCP server that lets an agent take a raw data sample, or a feed that already holds data, and build working
Stroom content for it: a feed, an event-logging translation pipeline, and an indexing (Lucene or Elasticsearch)
pipeline, stepped and verified before anything is promoted. It works with any MCP client that can sign the user
in; it includes no agent or model of its own. See [docs/DESIGN.md](docs/DESIGN.md).

Status: 0.10.0, released as a container image and a Helm chart ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).
The server has 58 tools:

| Group | Tools |
| --- | --- |
| Explorer and pipelines | `find_documents`, `get_document`, `describe_pipeline` |
| Templates | `find_pipeline_templates`, `list_template_children`, `describe_template_contract`, `find_similar_translations` |
| Samples and feeds | `profile_sample`, `create_feed`*, `upload_sample`, `record_source_notes` |
| Translation | `create_text_converter`, `update_text_converter`, `create_xslt`, `update_xslt` |
| Pipelines | `create_pipeline`*, `copy_pipeline`*, `set_pipeline_property` |
| Streams and errors | `find_streams`, `get_stream_children`, `read_stream`, `get_stream_attributes`, `summarise_errors`, `summarise_events` |
| Stepping | `step_pipeline`, `step_sample`, `step_records` (chosen records in place), `compare_outputs` (with unsaved draft code) |
| Processing | `processing_status`, `create_processor_filter`**, `set_processor_filter_enabled`**, `reprocess_streams`**, `wait_for_processing` |
| Standing instructions | `get_instructions` (AGENTS Documentation docs in Stroom, by folder) |
| Sampling | `survey_feed` (kinds of event in an existing feed, stream after stream, or in given sample streams), `set_shape_handling`* (kinds the user leaves untranslated) |
| Diagnosis | `locate_event` (stream and event back to raw part and record), `summarise_fix` (prove a fix, diff, manual steps) |
| Validation | `check_xslt`, `validate_events`, `check_event_quality`, `describe_translation` |
| Generation | `build_translation_xslt` (event-logging XSLT from a field mapping, checked against the schema and the sample), `build_data_splitter` (a Data Splitter from a spec, run on the sample), `build_reference_xslt` (reference-data maps from a mapping) |
| Reference data | `find_reference_data` (maps, feeds and loaders the environment has), `set_pipeline_references`, `create_dictionary`, `update_dictionary` |
| Indexing | `get_field_conventions`, `draft_index_mapping`, `create_index_doc`*, `set_index_fields`, `create_indexing_pipeline`*, `create_verification_dashboard`, `run_test_searches` |
| Elasticsearch | `find_elastic_clusters`, `propose_index_template`, `check_index_template`, `test_elastic_index` |
| Builds | `start_build`, `list_build`, `write_documentation`, `promote_build`** |

\* needs the user's confirmation, \*\* needs approval. The user answers these in a form the client shows, so the
model never holds the answer; a client without forms gets an id to pass back once the user has agreed.

Resources: `stroom://guides`, `stroom://guide/{name}`, `stroom://conventions/{name}`. Prompts (the workflows, e.g. as
slash commands): `onboard_data_source`, `update_events_pipeline`, `update_indexing_pipeline`, `index_event_data`,
`create_discovery_index`, `evaluate_events_pipeline`, `onboard_existing_feed`, `fix_pipeline_issue`.

## Clients

Any MCP client that speaks streamable HTTP and OAuth can use the server: a chat client, an IDE, or an agent
framework. The rules that must hold (the workspace, confirmations and approvals, processing limits, the
Elasticsearch hand-over, checks before promotion) are enforced by the server, not by prompts, so every client gets
them. What a client needs is in [docs/DESIGN.md](docs/DESIGN.md#clients).

Setting up VS Code (the identity provider's client, Stroom trusting the provider, `mcp.json`) is described in
[docs/VSCODE.md](docs/VSCODE.md); other clients need the same kind of client and token audiences. Any OpenID Connect provider
that issues JWT access tokens works; Keycloak is the worked example.

## How it works

- **As the user.** Every Stroom call, including datafeed uploads, acts as the signed-in user. The server forwards
  their access token, whose `aud` must include `stroom` as well as the MCP audience (or one audience both accept). There is no shared API key.
- **In a workspace.** Everything is written under `MCP Workspace/<build>` and tagged `mcp-managed` and
  `mcp-generated`. Only `mcp-managed` docs can be changed; promotion moves them into place and removes
  `mcp-managed`. `mcp-generated` stays, so everything the server created can be found in Stroom by that tag.
- **Checked before promotion.** `list_build` and the promotion approval show what a build's pipelines still lack:
  a clean step of their current code (recorded as `mcp-stepped-*` tags on the pipeline), documentation whose Field
  mapping matches the current mapping and XSLT, and an XSLT that is still what its mapping generates.
- **Documented from the mapping.** The mapping an XSLT was generated from is kept with it; `write_documentation`
  generates the Field mapping section from it over the sample streams (sources and sampled values, exact per-rule
  counts), so the documentation cannot drift from the code.
- **Standing instructions.** A Documentation doc named `AGENTS` in a Stroom folder holds instructions for building
  pipelines there (and below), like an AGENTS.md; see `stroom://guide/agent-instructions`. `get_instructions` returns
  them, and `start_build` and `build_translation_xslt` hand them back too, so a model that skips the step still sees
  them.
- **Audited.** Every tool call, resource read, Stroom request, confirmation, approval and refusal is logged as a
  JSON line with the user behind it: [docs/AUDIT.md](docs/AUDIT.md).

Design decisions (see [docs/DESIGN.md](docs/DESIGN.md#open-questions-risks-and-delivery)):
- Reprocessing is allowed while developing a pipeline in the workspace: `reprocess_streams` takes at most 10 streams
  per call and runs one task at a time, and Stroom supersedes (deletes) the earlier output. Promoted pipelines are
  refused by the write guard, so reprocessing production data is the user's, as is moving readers from one index
  version to the next.
- Processing: sample filters run one task at a time; a translation pipeline only processes the build's own feeds;
  promotion pre-creates each promoted pipeline's filter for new data, disabled, with a link to review and enable it.
- Generate, then check, rather than hand-write: text converters, translations and reference-data XSLTs come from
  specs and mappings; a mapping is checked against the schema and against every sample file (fields no record has,
  time formats the values do not fit) before anything is stepped. Several sample files of one source are profiled
  together, uploaded one stream each, and all stepped.
- Reference data and dictionaries are first-class: `lookup` and `dictionary` sources in the mapping, the Reference
  Data pipeline built from a mapping, and the events pipeline naming the feed as a pipeline reference.
- One record may hold several events (`for_each`), a value may repeat (`repeat`), and records the user wants left
  out are dropped by condition with a reason (`drop_when`), in translations, reference data and indexing alike.
  Indexing Events from a pipeline the server did not build needs the user's confirmation of that pipeline.
- Elasticsearch indexing runs only through the Stroom indexing pipeline, after the user confirms that the index
  template for the destination index (named in the question) has been written.
- The server proposes the index template (a Dev Tools request) and checks any changes the user sends back
  against the candidate indexing pipeline, listing the pipeline changes an incompatible template needs. Once the
  user has committed it, the indexing filter is created disabled and the user gets a link to the pipeline
  (`<stroom>/?action=open-doc&docType=Pipeline&docUuid=...`) to review it and enable the filter.
- Indexing filters over Events also carry `Pipeline IS_DOC_REF <events pipeline>`, where the events pipeline is the
  one this server built for the source (`source_pipeline_uuid`), so no Events from elsewhere are picked up.
- Kinds of event the user chooses to leave untranslated are recorded in the build's survey doc
  (`set_shape_handling`), dropped by an explicit rule in the mapping, and checked as producing no Event.

Configuration: field conventions in `conventions/*.yaml`, error triage rules in `error_rules.yaml`, template sources in
`access_policy.yaml`. Every setting is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#settings).
The server has no Elasticsearch connection or credentials of its own: everything that touches Elasticsearch goes
through Stroom (its Elastic Cluster and Elastic Index docs and its indexing filter), as the user.

## Running

```
cp .env.example .env   # fill in
uv run python main.py
```

TLS is required: the server refuses to start without a certificate unless a proxy in front terminates TLS
(`STROOM_MCP_TLS_TERMINATED_UPSTREAM`), or it listens on localhost (development). `/healthz` answers `ok` and the version (e.g. `ok 0.10.0`) for probes.

For local development, `STROOM_MCP_DEV_NO_AUTH=true` runs the server without sign-in and calls Stroom with
`STROOM_MCP_STROOM_API_KEY`. The server refuses to start that way unless it is bound to localhost, and refuses the
API key when authentication is on.

Tests: `uv run pytest`.

## Development and testing

`dev/stroom` runs Stroom v7.13 and MySQL in Docker, bound to localhost; destructive tests run there.

```
cd dev/stroom && ./init-env.sh && docker compose up -d
```

End-to-end suites, driving the real tools against that stack:

| Script | Covers |
| --- | --- |
| `dev/e2e_phase2.py` | CSV, JSON, XML and syslog samples to valid Events; a field fix and reprocessing; promotion and a working copy written back |
| `dev/e2e_phase3.py` | Indexing on the Lucene backend: field plan, index doc, indexing pipeline, verification searches, a v2 copy |
| `dev/e2e_existing_feed.py` | Building a pipeline from a feed that already holds data: surveys, stepping in place, kinds left untranslated |
| `dev/e2e_generator.py` | Translations generated from field mappings, stepped and validated |
| `dev/e2e_instructions.py` | Standing instructions (AGENTS docs) by folder |
| `dev/e2e_elastic_handover.py` | The Elasticsearch template hand-over, without Elasticsearch |
| `dev/e2e_oauth.py` | Sign-in as an MCP client does it, with the dev Keycloak in `dev/keycloak`, and Stroom trusting it |

To call one tool directly (no MCP client or sign-in): `uv run python dev/try_tool.py find_pipeline_templates
stage=translation`, or `--live` for the instance in `.ai/secrets` (read-only tools only). `dev/live_readonly.py [FEED]`
runs the read-only tools against that instance through a gateway that refuses any request that could change
Stroom. With a read/write key, the e2e suites run there with `E2E_TARGET=live` (every name carries `E2E_STAMP`), and
`dev/e2e_cleanup.py STAMP --apply` removes the run afterwards: filters, streams (marked deleted), documents and
folders.

The evaluation set in [dev/eval](dev/eval/README.md) has 10 cases across CSV, JSON, XML, syslog and key=value, each
with a reference solution: `--reference` runs those through the local stack without a model, and `--request` prints
the request to give an agent, whatever runs it. The Phase 0 spike (`spike/phase0.py`) proved the risky Stroom APIs;
what has been tested, locally and live, is in [spike/FINDINGS.md](spike/FINDINGS.md).
