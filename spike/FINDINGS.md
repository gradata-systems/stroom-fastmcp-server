# Phase 0 spike findings

Run against a local `gchq/stroom:v7.13-beta.17` stack (`dev/stroom`), 2026-09-28, with
`spike/phase0.py`. Read-only checks were also made against the live instance.

| Area | Result | What the tools must do |
| --- | --- | --- |
| Auth for explorer calls | The insecure test credential authenticates as the processing user, which explorer endpoints reject ("Expecting a stroom user identity"). It could create an API key for `admin`, which works everywhere. | Call Stroom as a real user (API key or exchanged token), never the processing identity. |
| Explorer filter | `fetchExplorerNodes` and `find` return nothing below System unless `filter.requiredPermissions: ["VIEW"]`; `find` needs a name filter (`*`). Results include containing folders; filter by type client-side. The find index lags a move by a moment. | Done in `utils/stroom.explorer_filter` and `find_documents`. After a move, confirm with `explorer/v2/info`, not `find`. |
| Feed names | Default rule `^[A-Z0-9_-]{3,}$` rejected `SPIKE-AUTH-V1.0` (500, message names the pattern). Live allows `Keycloak-V1.2`, so the rule is per instance. | `create_feed` surfaces the pattern from the error and proposes a compliant name. |
| Datafeed | `POST /stroom/datafeed` with `Feed` and `Type` headers returns 200 and a receipt id; the Raw Events stream is findable at once via `meta/v1/find`. Locally receipt auth was off. | `upload_sample` returns the receipt id and stream id. Live receipt auth still to confirm. |
| Pipeline JSON | `fetchPipelineJson` returns the pipeline's own layer as PipelineData JSON (`add`/`remove` for elements, links, properties, references), identical to `fetchPipelineLayers`. A child is created by `PUT pipeline/v1/{uuid}` with `parentPipeline` and only its own property adds. | No need for `savePipelineJson`; write the doc with `pipelineData`. Risk closed. |
| Stepping | Each step is a fresh request (`FIRST`, then `FORWARD` with the last `stepLocation`) with no `sessionUuid`. The session id only polls an incomplete step; the server drops a session once its step completes, and idle sessions after 10 s. Sending a new or completed session id fails with "No stepping session found". | `step_pipeline`/`step_sample` loop as in `_step_all`; poll with `sessionUuid` only while `complete` is false. |
| Draft code | `code: {"translationFilter": "<xslt>"}` steps unsaved XSLT. A broken draft produced a `schemaFilter` error with line, column and message (`Invalid content ... '{NotAnElement}'. One of '{Generator}' is expected`). Clean code stepped all 3 records with no errors. | Validation loop needs no saves. |
| Processor filter | `POST processorFilter/v1` with `queryData.dataSource {type: StreamStore, uuid: "0"}` and an `Id` term per sample stream processed the sample: exactly one Events child, no Error child, valid events. | Sample-scoped filters work as designed. |
| Documentation doc | `explorer/v2/create` type `Documentation`, then `PUT documentation/v1/{uuid}`; Markdown is in the `documentation` field. Round trip exact. | `write_documentation` uses the `documentation` field. |
| Dashboard | A content-pack dashboard's `dashboardConfig` copied into a new dashboard and read back intact. Query component settings hold `dataSource`, `expression` and `automate`. | Build verification dashboards by copying a sibling's config and swapping the data source. |
| Move (promotion) | `PUT explorer/v2/move` moved the feed; UUID unchanged. | Promotion keeps references intact, as designed. |
| Schemas | This content pack ships event-logging v3.0.0 to v4.1.0; live has v3.5.2 and v4.0.2. | Read the XSD from the instance; the version stays configurable. |
| Lucene index (`spike/lucene.py`) | Index doc via `explorer/v2/create` type `Index`, then `PUT index/v2/{uuid}` (volume group, time field, partitioning); fields are separate, via `index/v2/addField` and `findFields`, not in the doc. Child of `Indexing` template sets `xsltFilter.xslt` and `indexingFilter.index`; XSLT emits `records:2` `<data name value>`. Stepping the Events showed each document; processing completed. | Stage 2 is the same flow on either backend; only the doc, field and XSLT output formats differ. |
| Lucene field types | Fields added with `fldType: KEYWORD` were accepted but indexed and stored nothing (null in results, exact match found 0). `TEXT` with `analyzerType: KEYWORD` works, as in the content pack's Example Index. `ID` and `DATE` work. | Map logical field types per backend: keyword is Lucene `TEXT`+`KEYWORD` analyzer, ES `keyword`. |
| Verification search | `dashboard/v1/search` against a dashboard built from scratch (query + table components, table columns `${Field}`), polled with `queryKey` until `complete`: stream ids 3/3, `UserId = 'bob'` 1/1, `EventTime` BETWEEN 2/2. `TableSettings` in `componentResultRequests` rejects `pageSize` (dashboard-only). | `run_test_searches` is backend-neutral; the dashboard only needs the index doc as its data source. |
| Other filters | Content-pack processor filters (Example Index, Example Dynamic Index) also indexed the spike's Events stream; shard doc counts are not a reliable signal until flushed. | Verify with searches, not shard counts; match tasks to a filter by id. |

## Phase 1 additions

| Area | Result | Consequence |
| --- | --- | --- |
| Error streams | MARKER fetch returns `summary` entries per severity and `storedError` entries (severity, element, message). Locations are `-1`: no record position. A bad date was a WARNING in the XSLT that caused two schema ERRORs. | Record positions come from stepping; triage keeps the cause (own XSLT warning, review) next to its effect (schema error, blocking). |
| Stepping to a record | `REFRESH` with a `stepLocation` jumps straight to a record; `LAST` works. | `step_pipeline` takes a record index. |
| Empty XSLTFilter | The template's unset `decorationFilter` output is identical to its input. | Unset XSLT steps after the first are optional (pass-through). |
| Segmented streams | `data/v1/fetch` returns one record per call from an Events stream, whatever `recordCount` asks for. | `read_stream` and `summarise_events` read record by record. |
| Receipt headers | Held in the `Meta Data` child part as `key:value` lines (`Feed`, `ReceivedTime`, `RemoteAddress`, `UploadUserId`, custom headers). | `get_stream_attributes` reads that part. |
| Schema selection | Stroom's XMLSchema docs carry a `systemId` equal to what Events name in `xsi:schemaLocation`; lxml loads Stroom's XSD directly. On live a Keycloak record validated against v3.5.2 picked this way. | `validate_events` needs no version configuration in the normal case. |
| Content search | Live `findInContent` answered "content is currently being indexed (17% complete)" on first use. | Pass the message through; the agent retries. |

## Phase 2 additions (`dev/e2e_phase2.py`)

| Area | Result | Consequence |
| --- | --- | --- |
| Exit test | CSV, JSON, XML and syslog samples each went from sample to valid Events with documentation; a field fix changed only its field (`compare_outputs`), was saved, reprocessed (one Events stream per raw stream), promoted, and an in-place fix was written back through a working copy. | Phase 2 tools work end to end on the local stack. |
| JSONParser namespace | Its output is in `http://www.w3.org/2013/XSL/json`, not `xpath-functions` (that is `json-to-xml()`'s). With the wrong namespace no template matched, stepping reported no error, and processing wrote no Events and no Error stream. | Guides corrected; stepping now flags output without XML elements as blocking; discovery XSLT maps namespaces. |
| Search index lag | Newly created docs are missing from `explorer/v2/find` for a moment: `upload_sample` could not find its new feed, and a promotion plan changed between the two approval calls. | Use `feed/v1/getDocRefForName` and read build folders from the explorer tree, never the search index, for anything just created. |
| Promotion | Moved docs kept the `mcp-managed` tag, so the guard still let the agent change production content. | Promotion removes the agent's tags; later changes need a working copy. |
| Consent | Approvals bound to the exact details caught the promotion plan changing between calls. | Build plans in a stable order. |

## Phase 3 additions (`dev/e2e_phase3.py`, `dev/check_es_xslt.py`)

| Area | Result | Consequence |
| --- | --- | --- |
| Lucene exit test | From a CSV build's Events: convention guidance (no default assumed), field plan, index doc and fields, drafted indexing XSLT, pipeline from `Indexing`, stepping, processing with no Error stream, dashboard searches (3 docs, exact matches, time range 2 of 3), documentation; a v2 copy adding one field differed from v1 only by that field, v2 found the failed logon by it, v1 untouched; 16 docs promoted. | Stage 2 works end to end on Lucene. |
| Elasticsearch XSLT | The drafted ES XSLT, run with Saxon over a real Events record, produced JSON XML that validates against Stroom's `xpath-functions.xsd` (the `JSON` schema group `Events to Elasticsearch` checks). Absent values are left out. | ES rendering proven without writing to Elasticsearch. |
| Live ES search | `run_test_searches` on the live Keycloak dashboard (read-only) returned Elasticsearch documents with ECS fields plus Stroom's `__stream_id__` and `__event_id__`. A real dashboard's table settings carry fields (`selectionHandlers`, `pageSize`) that the search API rejects as "Unable to process JSON". | Send only TableSettings fields; errors now include Stroom's detail. |
| Live clusters | `find_elastic_clusters` lists `ES_PROD` (six Elastic Index docs, e.g. Keycloak `ecs-keycloak-v1`) and `ES_DEV` (none), with credentials redacted. | Cluster proposal has real data. |

## Phase 4 additions (`dev/e2e_agent_transport.py`, `tests/test_agent.py`)

| Area | Result | Consequence |
| --- | --- | --- |
| MCP adapters | `langchain-mcp-adapters` fails to import against mcp 2 (`ImportError: RequestContext`); it pins `mcp<2`. | The agent wraps tools itself with `fastmcp.Client` (`agent/mcp_tools.py`). |
| Transport | All 52 tools load over streamable HTTP. A gated `create_feed` inside a LangGraph graph interrupts on `needs_confirmation`, then resumes and creates the feed with the id. | Consent works end to end through the agent. |
| Graph | Unit tests with a fake model: the gate goes both ways (a decline passes the user's note back to the model), facts are harvested from tool results, routing is bounded, and every node compiles. | Routing never depends on the model's prose. |

## Decisions and fix use case (`tests/test_processing.py`, `tests/test_diagnosis.py`)

| Area | Result | Consequence |
| --- | --- | --- |
| Multi-part streams | A two-entry zip upload made one Raw Events stream with 2 parts: raw data is non-segmented, so each part is one item (`totalItemCount` = parts). Processing it made one Events stream with a single part whose 4 events run on across both raw parts. Stepping reports `(partIndex, recordIndex)`, with record numbers restarting in each part. | An Event ID maps to a raw part and record by stepping and counting output events. Stepping keys records as `stream:part:record` past part 0, and `step_pipeline` takes `part`. |
| Locate (local) | `locate_event` on that stream: event 3 went to part 1 record 0, event 4 to part 1 record 1, and every stored event matched a fresh step. | Works across parts. |
| Locate (live, read-only) | On a `Keycloak-V1.2` Events stream from a 4-part raw stream, event 3 went to part 0 record 3: one earlier record produced no event, and counting handled it. The stored event matched a fresh step, and the pipeline's only code is its own XSLT. | Works on production data. For JSON sources, the record input shown is the XSLT's input (the parser's JSON XML). |
| summarise_fix (local) | A one-line Description change: ready, 4 of 4 records changed only `Event/EventDetail/Description`, stepping clean, a readable unified diff and manual steps. The same draft with the wrong `expected_paths` came back not ready, naming the unexpected field. | Proven before it is offered. |
| Two gates in one call | Consent ids are single use, so after the template confirmation the repeated call for approval found its confirmation id already used up. | An earlier gate's id now survives the call being repeated for a later gate, and is discarded once the action completes. The agent collects ids across repeated interrupts. |
| Mapping to XSLT (`dev/e2e_generator.py`) | The CSV, JSON, XML and syslog samples were translated from mappings only, with no hand-written XSLT: every record stepped clean and every event validated against the instance's v4.1.0 schema and passed the quality checks. `stroom:format-date` handled all four Java patterns. An XML record that matched no rule came back from `stroom:log('WARN', ...)` as a review group ("Log - No event mapping matched record 3") instead of being dropped. | A model only needs to produce the mapping; the schema checks turn most XSLT mistakes into mapping problems with suggestions. |
| Pipeline condition | The stream expression field `Pipeline` is a DocRef: `IS_DOC_REF` with the pipeline's DocRef selects only its outputs, and a different UUID matches nothing. `EQUALS` on a name is rejected (500). The Phase 3 indexing filters with this condition processed the Events, and the verification searches passed. | Every indexing filter over Events is tied to its source events pipeline. |
| Reprocessing in development | A second plain filter on a processed stream is refused and points to `reprocess_streams` (at most 10 streams, `maxProcessingTasks` 1). After the reprocess, Stroom itself marked the pipeline's earlier Events stream for that raw stream `DELETED`, leaving exactly one. | No delete tool is needed for superseded outputs. Phase 2 and 3 exit tests pass. |

## Not yet tested

- Writing to Elasticsearch (index templates, ES indexing pipelines processing into a live index): no ES credentials, and the live instance stays read-only. Covered by mocked tests and the XSLT/XSD check.
- Agent runs with a real model, and the evaluation set of 10 samples (the Phase 4 exit criterion).
- Forwarding a real user's Keycloak token (with `stroom` in `aud`) to Stroom, the agent's device sign-in, and whether live `/stroom/datafeed` accepts OIDC tokens.
- Stepping a pipeline with an empty XSLTFilter (the question of what Stroom does with a template's unset `decorationFilter`).
