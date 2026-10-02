# Evaluation set

Sixteen samples to measure how well an agent builds with the server, whatever runs the agent. The bar: at least
80% of them (13 of 16) reach indexed events with at most one human hint each.

| Case | Format | Events | What it tests |
| --- | --- | --- | --- |
| `01_csv_vpn` | CSV with a header | Authenticate (logon, logoff) | Rules per action, outcome map, time zone |
| `02_json_alert` | JSON array | Alert | Enumerated values (severity map), `Data` entries |
| `03_json_nested_process` | Nested JSON | Process | Nested keys (`host.name`), required Process elements |
| `04_json_embedded` | JSON with JSON in a string field | Authenticate | Unpacking with `json-to-xml()` through an `xpath` source |
| `05_xml_ns_files` | XML in a namespace | View, Delete | Input namespace, two event types |
| `06_syslog5424_ssh` | RFC 5424 syslog | Authenticate | Data Splitter regex, optional group, outcome map |
| `07_syslog3164_sudo` | RFC 3164 syslog | Process | A timestamp without a year, RunAs |
| `08_kv_firewall` | key=value | Network | Nested Data Splitter, protocol map, Permitted |
| `09_csv_noheader_copy` | CSV without a header | Copy | Named columns from a regex, Source and Destination |
| `10_xml_attrs_lock` | XML attributes | Authenticate (screen lock) | Attribute paths, enumerated actions |
| `11_jsonl_message_text` | JSON lines, event in a message string | Authenticate | No text converter (JSONParser, `json_layout: lines`), `extract` regexes for the time, user, action, outcome and client address |
| `12_xml_fragments_events` | XML fragments: one `<Event>` per line, no root, no namespace | Authenticate | `XML_FRAGMENT` wrapper converter, `create_pipeline` with `replace_parser: XMLFragmentParser` (no fragment template), `input: xml_fragments` with the wrapper's `records:2` namespace, attribute and `Data[@Name]` paths |
| `13_json_embedded_message` | JSON array; JSON in a string field; a key=value message string inside that | Authenticate | `json-to-xml()` through `xpath`, then `extract` on an `xpath` source |
| `14_csv_two_files_variants` | Two CSV exports of one source: renamed columns, an extra column, a new action | Authenticate | Several sample files (one stream each, all stepped), `any_of`, the mapping checked against every file first |
| `15_csv_lookup_reference_data` | CSV events plus a CSV user directory | Authenticate | Reference data end to end: Raw Reference feed, `build_reference_xslt`, Reference Data pipeline, `references` on the events pipeline, `lookup` with case-normalised keys, `transform` |
| `16_json_batches_items` | JSON array of batches, each holding an events array and role arrays | Authenticate | `for_each` (one record, several events) with `scope: record` fields, `repeat` (one Group per role), `drop_when` on records and items |

Each case (`cases/*.yaml`) holds the sample (or `samples`, several files), the request to give the agent, what the output must contain (record
count, event types, paths every event must have), an optional list of `hints`, and a reference solution (a Data
Splitter where the format needs one, and a `build_translation_xslt` mapping).

## Running

Both need the local Stroom stack (`dev/stroom`).

**Reference** (no model): proves the cases and the scoring by putting each reference solution through the real
path: generated XSLT, stepping, processing, event validation, a Lucene index and a verification search.

```
uv run python dev/eval/run_eval.py --reference          # all cases, or name some: --reference 04 06
```

Results are written to `results/<time>-reference.json` (ignored by git) with a summary table.

**An agent**: start the server locally, connect the agent to it, and give it each case's request.

```
STROOM_MCP_STROOM_URL=http://127.0.0.1:18080 STROOM_MCP_STROOM_API_KEY=<admin key from dev/stroom/.env> \
STROOM_MCP_DEV_NO_AUTH=true STROOM_MCP_HOST=127.0.0.1 STROOM_MCP_PORT=8765 \
STROOM_MCP_EVENT_LOGGING_VERSION=4.1.0 STROOM_MCP_DEFAULT_CONVENTION=stroom-flat uv run python main.py

uv run python dev/eval/run_eval.py --request 06         # the request (with its sample) to paste into the agent
```

Play the user: agree to confirmations and approvals, accept the proposed index template, enable the indexing
filter when it is handed over, and when the agent asks for help give the case's next hint, counting each one
(only one is allowed). A case passes when its Events hold the expected record count, event types and paths, and
the verification search finds them in the index.
