# Stroom FastMCP server

MCP server that lets an agent take a raw data sample, or a feed that already holds data, and build working
Stroom content for it: a feed, an event-logging translation pipeline, and an indexing (Lucene or Elasticsearch)
pipeline, stepped and verified before anything is promoted. It works with any MCP client that can sign the user
in; it includes no agent or model of its own. See [docs/DESIGN.md](docs/DESIGN.md).

Status: 0.16.38, released as a container image and a Helm chart ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).
The server has 52 tools:

| Group | Tools |
| --- | --- |
| Explorer and pipelines | `find_documents` (by name, type or content), `describe_document` (content plus how Stroom runs a pipeline, what an XSLT does, a survey of what an index holds, or what an event-logging element takes) |
| Templates | `find_pipeline_templates`, `describe_template` (its children, its contract, and the shared XSLTs they import: each named template called, where, and what it writes and reads) |
| Samples and feeds | `profile_sample`, `create_feed`*, `upload_sample`, `record_source_notes` (the user's vendor documentation kept in Stroom, and notes from it the draft and checks follow; a catalogue the schema can't take is refused) |
| Translation | `save_text_converter`, `save_xslt` (written by hand, or an indexing XSLT from its plan), `save_dictionary`; `uuid=` replaces an existing one |
| Pipelines | `create_pipeline`*, `copy_pipeline`*, `update_pipeline` |
| Streams and errors | `find_streams`, `describe_stream` (children and attributes), `read_stream`, `summarise_streams` (errors triaged, or events by type and path) |
| Stepping | `step_pipeline`, `step_sample`, `step_records` (chosen records in place), `compare_outputs` (with unsaved draft code) |
| Processing | `processing_status`, `create_processor_filter`**, `set_processor_filter_enabled`**, `reprocess_streams`**, `wait_for_processing` |
| Standing instructions | `get_instructions` (AGENTS Documentation docs in Stroom, by folder) |
| Sampling | `survey_feed` (kinds of event in an existing feed, stream after stream, or in given sample streams), `set_shape_handling`* (kinds the user leaves untranslated) |
| Diagnosis | `locate_event` (stream and event back to raw part and record), `summarise_fix` (prove a fix, diff, manual steps) |
| Validation | `check_xslt`, `check_events` (schema and quality rules, of given XML or of Events streams) |
| Generation | `draft_translation_mapping` (a starting mapping from the sample), `build_translation_xslt` (event-logging XSLT from a field mapping, checked against the schema and the sample), `build_data_splitter` (a Data Splitter from a spec, run on the sample), `build_reference_xslt` (reference-data maps from a mapping) |
| Reference data | `find_reference_data` (maps, feeds and loaders the environment has), `update_pipeline` (set_properties, references) |
| Indexing | `get_field_conventions`, `draft_index_mapping` (a field plan from the user's example index template, an existing index in Stroom or a convention, or a discovery plan), `create_index_doc`* (with the plan's fields), `create_indexing_pipeline`*, `verify_index`* (a dashboard of the columns the user confirms, opening on the time field from the sample through today, with a stepping text pane; searches through Stroom, each hit traced back to its record) |
| Elasticsearch | `find_elastic_clusters`, `propose_index_template`* (built from the user's example, shown in the chat, then agreed by the user), `check_index_template`* (the user's correction, agreed when it fits), `create_index_doc` |
| Plan | `start_onboarding` (profile every file, create the build, return the plan), `build_status` (each step's state from the build; every write tool's result carries `next`) |
| Builds | `start_build`, `build_status`, `write_documentation` (a pipeline's, with a generated Errors section and errors the user accepts as benign*; or an existing index's*, from a survey), `promote_build`** |

\* needs the user's confirmation, \*\* needs approval. The user answers these in a form the client shows, so the
model never holds the answer; a client without forms gets an id to pass back once the user has agreed.

Resources: `stroom://guides`, `stroom://guide/{name}`, `stroom://conventions/{name}`. Prompts (the workflows, e.g. as
slash commands): `onboard_data_source`, `update_events_pipeline`, `update_indexing_pipeline`, `index_event_data`,
`create_discovery_index`, `evaluate_events_pipeline`, `onboard_existing_feed`, `fix_pipeline_issue`,
`document_index`. Each is diagrammed
in [docs/DESIGN.md](docs/DESIGN.md#workflows), with which one fits what the user has and wants.

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
- **Checked before promotion.** `build_status` and the promotion approval show what a build's pipelines still lack:
  a clean step of their current code (recorded as `mcp-stepped-*` tags on the pipeline), documentation whose Field
  mapping matches the current mapping and XSLT, and an XSLT that is still what its mapping generates.
- **Documented from the mapping.** The mapping an XSLT was generated from is kept with it; `write_documentation`
  generates the Field mapping section from it over the sample streams (sources and sampled values, exact per-rule
  counts), so the documentation cannot drift from the code. Its Errors section is generated from the Error streams
  processing produced; an error the user says is benign is recorded there with their reason (they confirm), and
  triage reports that kind of error as benign for the pipeline from then on, so it is not raised again.
- **Own errors first.** Errors stepping finds in the agent's own content (mapping, XSLT, text converter) are fixed by
  the agent before anything reaches the user; only what it cannot resolve (an inherited element, reference data, the
  source data) is raised.
- **Standing instructions.** A Documentation doc named `AGENTS` in a Stroom folder holds instructions for building
  pipelines there (and below), like an AGENTS.md; see `stroom://guide/agent-instructions`. `get_instructions` returns
  them, and `start_build` and `build_translation_xslt` hand them back too, so a model that skips the step still sees
  them.
- **Audited.** Every tool call, resource read, Stroom request, confirmation, approval and refusal is logged as a
  JSON line with the user behind it: [docs/AUDIT.md](docs/AUDIT.md).

Design decisions (see [docs/DESIGN.md](docs/DESIGN.md#open-questions-risks-and-delivery)):
- Reprocessing is allowed while developing a pipeline in the workspace: `reprocess_streams` takes at most 10 streams
  per call and runs one task at a time, and Stroom supersedes (deletes) the earlier output. Elasticsearch is the
  exception: documents a stream already indexed stay, so the approval gives the cluster admin the `_delete_by_query`
  request to run first. Promoted pipelines are
  refused by the write guard, so reprocessing production data is the user's, as is moving readers from one index
  version to the next.
- Elasticsearch indexing in the workspace starts with a batch size of 10, so a document Elasticsearch rejects comes
  back whole in the Error stream (a large bulk response is cut short there); `wait_for_processing` restores the
  template's default once indexing completes without errors, and promotion makes sure of it.
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
- Elasticsearch index templates follow the user's example: the agent asks for a sibling index's template (or an
  index's mapping) and any component templates it is composed of. Field names, types, objects and settings follow
  it, and documents are written with their structure (`"user": {"id", "name"}`). The template is checked against
  the documents the indexing pipeline writes; the user confirms it, or corrects it (checked again, listing the
  pipeline changes an incompatible one needs). The agreed template is kept with the pipeline.
- Elasticsearch indexing runs only through the Stroom indexing pipeline, and only once the user has confirmed, in
  the approval that starts it, that the cluster admin has committed the agreed template. The server has no
  Elasticsearch access of its own.
- Shared XSLTs (`xsl:import`) the environment's pipelines use are found through the template's children, read by
  name, and called in the same place in new XSLTs; an element they write is never also written by the generated
  XSLT.
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
(`STROOM_MCP_TLS_TERMINATED_UPSTREAM`), or it listens on localhost (development). `/healthz` answers `ok` and the version (e.g. `ok 0.16.38`) for probes.

For local development, `STROOM_MCP_DEV_NO_AUTH=true` runs the server without sign-in and calls Stroom with
`STROOM_MCP_STROOM_API_KEY`. The server refuses to start that way unless it is bound to localhost, and refuses the
API key when authentication is on.

Tests: `uv run pytest`. `tests/test_off_path.py` holds every state of the onboarding plan (in order, and out of it, as real clients went) with the step and call `next` names, and the wrong moves agents make, each of which must be stopped with the call that puts it right.

## Development and testing

`dev/stroom` runs Stroom v7.13 and MySQL in Docker, bound to localhost; destructive tests run there.

```
cd dev/stroom && ./init-env.sh && docker compose up -d
```

End-to-end suites, driving the real tools against that stack:

| Script | Covers |
| --- | --- |
| `dev/e2e_plan_walk.py` | The plan followed as an agent would: from `start_onboarding`, every call is the one `next` names, with only its placeholders filled, to promotion, on Lucene and on Elasticsearch (with the user's example template agreed and committed). Following `next` is never refused; wrong moves on the way are refused with the right call named, and leave `next` where it was |
| `dev/e2e_translation.py` | CSV, JSON, XML and syslog samples to valid Events; a field fix and reprocessing; promotion and a working copy written back |
| `dev/e2e_lucene_indexing.py` | Indexing on the Lucene backend: field plan, index doc, indexing pipeline, verification searches, a v2 copy |
| `dev/e2e_existing_feed.py` | Building a pipeline from a feed that already holds data: surveys, stepping in place, kinds left untranslated |
| `dev/e2e_generator.py` | Translations generated from field mappings, stepped and validated |
| `dev/e2e_instructions.py` | Standing instructions (AGENTS docs) by folder |
| `dev/e2e_elastic_handover.py` | The Elasticsearch index template: built from the user's example and component templates, agreed (and corrected), then indexing once committed. Without Elasticsearch, or with `--live` against Elasticsearch 9 (`docker compose --profile elastic up -d` in `dev/stroom`): templates applied, composition compared with `_simulate_index`, documents indexed with no dynamic fields, and searched through Stroom |
| `dev/e2e_discovery.py` | A discovery index on Elasticsearch 9 (`docker compose --profile elastic up -d` in `dev/stroom`): raw JSON, CSV (a Data Splitter, numbers recognised from text) and XML (nested objects, repeated elements as arrays) indexed as it is, with nested objects, arrays and a JSON message unpacked, mapped dynamically; the permissive template agreed and committed; Stroom's searches; the documentation. Then for an existing raw feed holding streams sent over time, with drifting data: no survey, the existing streams indexed by id and new ones by a feed filter, every record indexed. Then awkward shapes (`--shapes` alone): arrays of objects, nulls, empty values, keys Elasticsearch refuses or Stroom drops, a record Elasticsearch rejects (reported and explained, the rest indexed) |
| `dev/e2e_source_docs.py` | The user's vendor documentation kept in Stroom and used: notes drive the draft (fields where the dictionary puts them, a rule per catalogued event), a mapping that contradicts the catalogue is reported, the documentation lists the source fields' meanings, notes and documents promoted beside the feed; a long manual kept in parts and read a passage at a time; a catalogue guessed from a firewall sample refused, and a hand-written XSLT writing every record as Unknown blocked at stepping until saved from a mapping (Network/Permit, Network/Deny) |
| `dev/e2e_large_sample.py` | A 15 MB, 200,000-record file sent to Stroom directly (never through the model) and onboarded from its stream id: every record processed, and no tool reply over 64,000 characters |
| `dev/e2e_document_index.py` | An existing index documented: an Elasticsearch index nothing in Stroom feeds (and one whose documents have no StreamId, which Stroom never returns: documented from its mapping, saying why), one a production pipeline with a kept plan feeds, and a Lucene index; each located and confirmed, surveyed through Stroom (fields, the newest documents through an unsaved dashboard, the feeding pipelines), drafted with the generated field table and a link, and promoted beside the index doc or where the user chooses; documented again (the doc beside it written back, not duplicated); a 183-field index surveyed in groups of columns; a Lucene field that is not stored shown as such |
| `dev/e2e_errors.py` | Invalid data, and the response: an error in the agent's own mapping fixed before the user sees it; an inherited error the user accepts as benign (confirmed, recorded in the documentation's Errors section, then reported as benign in stepping and Error streams); a record Elasticsearch rejects, reported per document in batches of 10, fixed, the stream's earlier documents deleted first as the approval asks, the default batch size restored; XML that is not well-formed, caught before the pipeline is made and located by stepping |
| `dev/e2e_index_versions.py` | A new version of a production Elasticsearch indexing pipeline: v1 built, agreed, indexed and promoted; v2 copied with an added field (diff limited to it), its template from v1's agreed one, committed, started on new Events only; new data indexed by both (v1 unchanged, still running); searched both ways; v2 documented and promoted beside v1 |
| `dev/e2e_evaluate_and_fix.py` | A production pipeline the server did not build, with a schema failure and a mistranslated field: evaluated (errors first: the failure, and the record it loses; inputs never read; the events), a fix suggested and proven, the report promoted beside it; then a reported issue located, reproduced, proven (an unrelated error not in its way) and applied in place, with backups and its documentation updated |
| `dev/e2e_shared_xslt.py` | Shared XSLTs (`xsl:import`): found through sibling pipelines and read by name; a translation and an indexing XSLT call them in place (Event/Meta and EventSource/Device from `stroom:meta()`, a JSON object), mapping an element twice refused, Events valid with one of each |
| `dev/e2e_formats.py` | The formats sources send, each onboarded with the server's own Data Splitter and draft: quoted CSV (doubled quotes), TSV, pipe, headerless CSV (plain and quoted), syslog RFC 3164 and RFC 5424 with a key=value body, CEF alone and after syslog, quoted key=value, JSON lines and arrays, XML documents and XML fragments with their own namespace |
| `dev/e2e_fragments.py` | XML fragments end to end (the parser replaced, the wrapper set: the environment's own if it has one), and regexes: a '-' for the text's en dash refused at once with where it stops |
| `dev/e2e_records_source.py` | A source whose own XML is `<records><record>`: profiled, translated, validated and indexed as the source's XML, not a Data Splitter's `records:2` |
| `dev/e2e_stream_types.py` | Templates found by what they are, under names no standard template has; Raw Reference (the reference template and loader by structure, a lookup through a loader resolved with none named) and Records (a pipeline writing Records, indexed as records) |
| `dev/e2e_xslt_style.py` | How generated XSLT is written: each layout giving the same Events in Stroom, shared functions found in a sibling and called, the processing gate on replaced code, Unknown agreed with the user |
| `dev/e2e_oauth.py` | Sign-in as an MCP client does it, with the dev Keycloak in `dev/keycloak`, and Stroom trusting it |

Every Elasticsearch index the suites build is searched both ways (`dev/searching.py`): through Stroom's dashboards, as people will search, with each hit traced back to its record, and directly in Elasticsearch, as an independent check; both must return the expected count. Lucene (`dev/e2e_lucene_indexing.py`) is searched through Stroom only.

To call one tool directly (no MCP client or sign-in): `uv run python dev/try_tool.py find_pipeline_templates
stage=translation`, or `--live` for the instance in `.ai/secrets` (read-only tools only). `dev/live_readonly.py [FEED]`
runs the read-only tools against that instance through a gateway that refuses any request that could change
Stroom. With a read/write key, the e2e suites run there with `E2E_TARGET=live` (every name carries `E2E_STAMP`), and
`dev/e2e_cleanup.py STAMP --apply` removes the run afterwards: filters, streams (marked deleted), documents and
folders.

The evaluation set in [dev/eval](dev/eval/README.md) has 35 cases across CSV, TSV and pipe-delimited text, JSON,
XML and XML fragments, syslog, CEF and key=value (large files and a non-UTF-8 file among them), each with a
reference solution (`dev/eval/offline.py` checks them with no Stroom): `--reference` runs those through the local stack without a model, and `--request` prints
the request to give an agent, whatever runs it. `dev/eval/run_agent.py` runs them with headless Claude Code as the
agent and a scripted user, on Haiku by default (`--model default` for Claude Code's own), on the CLI's sign-in: the
Claude subscription, never an API key. `--workflow fix_errors` (or `change_event_type`, `records_output`,
`document_index`) runs a workflow beyond onboarding
(`dev/eval/workflows.py`: its setup, prompt, the user's facts and a scorer reading Stroom), and with `--reference`
checks that setup and scorer without a model. The Stroom API checks (`dev/api_checks`) prove the APIs the server relies on, which were built for the Stroom UI;
what has been found and tested, locally and live, is in [docs/FINDINGS.md](docs/FINDINGS.md).

## Releasing

```
uv run python dev/release.py --dry-run     # every check, nothing changed
uv run python dev/release.py --wait        # the next patch version, released; then CI's result
```

It releases from master with nothing uncommitted. When the code has changed since the last tag and the docs haven't,
it stops and lists the changes: write up what changed first (docs/FINDINGS.md for what was found and done, the tool
catalogue and e2e tables in docs/DESIGN.md, the guides in knowledge/ that agents read, the tools' own descriptions),
or give `--docs-unchanged "why"` when nothing a reader sees changed. The unit tests it runs check those tables, and
the evaluation README, list every tool, e2e suite, case and workflow the code has. It then bumps the version
everywhere it is held, commits, tags `vX.Y.Z` and pushes; CI builds the container image and the Helm chart.
