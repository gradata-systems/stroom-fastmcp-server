# Development

## Running locally

```
cp .env.example .env   # fill in
uv run python main.py
```

TLS is required: the server refuses to start without a certificate unless a proxy in front terminates TLS
(`STROOM_MCP_TLS_TERMINATED_UPSTREAM`), or it listens on localhost. `/healthz` answers `ok` and the version.

For local development, `STROOM_MCP_DEV_NO_AUTH=true` runs the server without sign-in and calls Stroom with
`STROOM_MCP_STROOM_API_KEY`. The server refuses to start that way unless it is bound to localhost, and refuses the
API key when sign-in is on.

To call one tool directly, with no MCP client or sign-in: `uv run python dev/try_tool.py find_pipeline_templates
stage=translation`.

## Tests

`uv run pytest` runs the unit tests. `tests/test_off_path.py` holds every state of the onboarding plan, in order and
out of it as real clients went, with the step and call `next` names, and the wrong moves agents make, each of which
must be stopped with the call that puts it right.

`dev/stroom` runs Stroom 7.13 and MySQL in Docker, bound to localhost. Destructive tests run there:

```
cd dev/stroom && ./init-env.sh && docker compose up -d
```

The end-to-end suites drive the real tools against that stack. Run them one at a time:

| Script | Covers |
| --- | --- |
| `dev/e2e_plan_walk.py` | The plan followed as an agent would: from `start_onboarding`, every call is the one `next` names, with only its placeholders filled, to promotion, on Lucene and on Elasticsearch (with the user's example template agreed and committed). Following `next` is never refused; wrong moves on the way are refused with the right call named, and leave `next` where it was |
| `dev/e2e_translation.py` | CSV, JSON, XML and syslog samples to valid Events; a field fix and reprocessing; promotion and a working copy written back |
| `dev/e2e_lucene_indexing.py` | Indexing on the Lucene backend: field plan, index doc, indexing pipeline, verification searches, a v2 copy |
| `dev/e2e_existing_feed.py` | Building a pipeline from a feed that already holds data: surveys, stepping in place, kinds left untranslated |
| `dev/e2e_generator.py` | Translations generated from field mappings, stepped and validated |
| `dev/e2e_instructions.py` | Standing instructions (AGENTS docs) by folder |
| `dev/e2e_elastic_handover.py` | The Elasticsearch index template: built from the user's example and component templates, agreed (and corrected), then indexing once committed. Without Elasticsearch, or with `--live` against Elasticsearch 9 (`docker compose --profile elastic up -d` in `dev/stroom`): templates applied, composition compared with `_simulate_index`, documents indexed with no dynamic fields, and searched through Stroom |
| `dev/e2e_discovery.py` | A discovery index on Elasticsearch 9 (`docker compose --profile elastic up -d` in `dev/stroom`): raw JSON, CSV (a Data Splitter, numbers recognised from text) and XML (nested objects, repeated elements as arrays) indexed as it is, with nested objects, arrays and a JSON message unpacked, mapped dynamically; the permissive template agreed and committed; Stroom's searches; the documentation. Then for an existing raw feed holding streams sent over time, with drifting data: no survey, the existing streams indexed by id and new ones by a feed filter, every record indexed. Then, with no example template (`--data-stream` alone), a data stream: the user picks which of two time fields is `@timestamp`, Stroom indexes into the data stream and searches it, and reprocessing clears it first with the approval's `_delete_by_query`. Then awkward shapes (`--shapes` alone): arrays of objects, nulls, empty values, keys Elasticsearch refuses or Stroom drops, a record Elasticsearch rejects (reported and explained, the rest indexed) |
| `dev/e2e_source_docs.py` | The user's vendor documentation kept in Stroom and used: notes drive the draft (fields where the dictionary puts them, a rule per catalogued event), a mapping that contradicts the catalogue is reported, the documentation lists the source fields' meanings, notes and documents promoted beside the feed; a long manual kept in parts and read a passage at a time; a catalogue guessed from a firewall sample refused, and a hand-written XSLT writing every record as Unknown blocked at stepping until saved from a mapping (Network/Permit, Network/Deny) |
| `dev/e2e_large_sample.py` | A 15 MB, 200,000-record file sent to Stroom directly (never through the model) and onboarded from its stream id: every record processed, and no tool reply over 64,000 characters |
| `dev/e2e_document_index.py` | An existing index documented: an Elasticsearch index nothing in Stroom feeds (and one whose documents have no StreamId, which Stroom never returns: documented from its mapping, saying why), one a production pipeline with a kept plan feeds, and a Lucene index; each located and confirmed, surveyed through Stroom (fields, the newest documents through an unsaved dashboard, the feeding pipelines), drafted with the generated field table and a link, and promoted beside the index doc or where the user chooses; documented again (the doc beside it written back, not duplicated); a 183-field index surveyed in groups of columns; a Lucene field that is not stored shown as such |
| `dev/e2e_errors.py` | Invalid data, and the response: an error in the agent's own mapping fixed before the user sees it; an inherited error the user accepts as benign (confirmed, recorded in the documentation's Errors section, then reported as benign in stepping and Error streams); a record Elasticsearch rejects, reported per document in batches of 10, fixed, the stream's earlier documents deleted first as the approval asks, the default batch size restored; XML that is not well-formed, caught before the pipeline is made and located by stepping |
| `dev/e2e_index_versions.py` | A new version of a production Elasticsearch indexing pipeline: v1 built, agreed, indexed and promoted; v2 copied with an added field (diff limited to it), its template from v1's agreed one, committed, started on new Events only; new data indexed by both (v1 unchanged, still running); searched both ways; v2 documented and promoted beside v1 |
| `dev/e2e_evaluate_and_fix.py` | A production pipeline the server did not build, with a schema failure and a mistranslated field: evaluated (errors first: the failure, and the record it loses; inputs never read; the events), a fix suggested and proven, the report promoted beside it; then a reported issue located, reproduced, proven (an unrelated error not in its way) and applied in place, with backups and its documentation updated |
| `dev/e2e_shared_xslt.py` | Shared XSLTs (`xsl:import`): found through sibling pipelines and read by name; a translation and an indexing XSLT call them in place (Event/Meta and EventSource/Device from `stroom:meta()`, a JSON object), mapping an element twice refused, Events valid with one of each; the translation's mapping lost and rebuilt with its shared templates, read from the imported XSLT; a shared XSLT of functions called from the translation, and read back when the mapping is lost |
| `dev/e2e_formats.py` | The formats sources send, each onboarded with the server's own Data Splitter and draft (and times as sources write them: offsets, IANA zone names and epoch seconds with a fraction, each converted to UTC with its milliseconds, a named zone kept as TimeZoneName, and a value from stream meta, MyHostName, in EventSource/Device/HostName): quoted CSV (doubled quotes), TSV, pipe, headerless CSV (plain and quoted), syslog RFC 3164 and RFC 5424 with a key=value body, CEF alone and after syslog, quoted key=value, JSON lines and arrays, XML documents and XML fragments with their own namespace |
| `dev/e2e_fragments.py` | XML fragments end to end (the parser replaced, the wrapper set: the environment's own if it has one), and regexes: a '-' for the text's en dash refused at once with where it stops |
| `dev/e2e_records_source.py` | A source whose own XML is `<records><record>`: profiled, translated, validated and indexed as the source's XML, not a Data Splitter's `records:2` |
| `dev/e2e_stream_types.py` | Templates found by what they are, under names no standard template has; Raw Reference (the reference template and loader by structure, a lookup through a loader resolved with none named, and rebuilt by rebuild_mapping when its mapping is lost) and Records (a pipeline writing Records, indexed as records) |
| `dev/e2e_restyle.py` | An events pipeline in a build brought up to the generator's style: a FortiGate-like feed checked against every record, a generator upgrade told from a hand edit, the kept mapping summarised, regenerated with uuid alone, every Event unchanged; a hand edit (a Data element added, the Rule no longer written) named by build_status, carried into the mapping by rebuild_mapping, the same Events; the Documentation tab cleared, the loss named, the mapping rebuilt from the XSLT; an inexpressible edit refused until accepted |
| `dev/e2e_describe.py` | A feed onboarded, indexed and promoted, then asked about: its pipelines (one found through a filter naming the feed's doc) and generated docs, its index, a field traced from the index doc to its event-logging path and source, with how to search it |
| `dev/e2e_coverage.py` | Kinds the sample missed: found across the whole feed in the later stream alone, a rule added, only that stream reprocessed, the CEF pipeline that follows reviewed; pings kept as Unknown found in batches; an indexing follow-on found through a Dictionary |
| `dev/e2e_cef.py` | CEF for ArcSight through Kafka: the question about keys outside the CEF dictionary, a draft and an override, the XSLT saved with its plan, a pipeline of its own with no template, stepped, reviewed and documented; a hand-written CEF pipeline reviewed, an AGENTS doc deciding keys, topic, mappings and template, text output, a change promoted with its version history |
| `dev/e2e_xslt_style.py` | How generated XSLT is written: each layout giving the same Events in Stroom, shared functions found in a sibling and called, the processing gate on replaced code, Unknown agreed with the user |
| `dev/e2e_oauth.py` | Sign-in as an MCP client does it, with the dev Keycloak in `dev/keycloak`, and Stroom trusting it |

Every Elasticsearch index the suites build is searched two ways (`dev/searching.py`): through Stroom's dashboards,
as people will search, with each hit traced back to its record, and directly in Elasticsearch, as an independent
check. Both must return the expected count. Lucene (`dev/e2e_lucene_indexing.py`) is searched through Stroom only.
`dev/e2e_cleanup.py STAMP --apply` removes what a run made: filters, streams (marked deleted), documents and folders.

An evaluation set of samples measures how well an agent builds with the server, whatever runs it: see
[dev/eval](https://github.com/gradata-systems/stroom-fastmcp-server/tree/master/dev/eval).

## The docs site

This site is built with MkDocs Material from `docs/`, with the agent guides from `knowledge/guides` and the tool
reference generated from the tools themselves (`dev/docs_hooks.py`):

```
uv run --group docs mkdocs serve            # preview at http://127.0.0.1:8000
uv run --group docs mkdocs build --strict   # as CI and the release check it
```

A release tag publishes it to GitHub Pages.

## Releasing

```
uv run python dev/release.py --dry-run     # every check, nothing changed
uv run python dev/release.py --wait        # the next patch version, released; then CI's result
```

It releases from master with nothing uncommitted. When the code has changed since the last tag and the docs haven't,
it stops and lists the changes. Write up what changed first: this site's pages in `docs/`, the guides in
`knowledge/` that agents read, the tools' own descriptions, and `dev/FINDINGS.md` for what was found and done. Or
give `--docs-unchanged "why"` when nothing a reader sees changed. The unit tests it runs check that the design's
tool catalogue, the e2e table above and the evaluation README list every tool, suite, case and workflow the code
has, and it builds this site. It then bumps the version everywhere it is held, commits, tags `vX.Y.Z` and pushes. CI
builds the container image and the Helm chart, and publishes the site.
