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

## Not yet tested

- Elastic indexing: the local stack uses Lucene instead; Elasticsearch is exercised against the live instance later. Dashboard search is proven on Lucene.
- Keycloak token exchange, and whether live `/stroom/datafeed` accepts OIDC tokens.
- Stepping a pipeline with an empty XSLTFilter (the question of what Stroom does with a template's unset `decorationFilter`).
