# Phase 4 evaluation

The exit test for the LangGraph agent: at least 8 of these 10 samples reach indexed events with at most one
human hint each.

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

Each case (`cases/*.yaml`) holds the sample, the request the agent is given, what the output must contain
(record count, event types, paths every event must have), an optional list of `hints`, and a reference solution
(a Data Splitter where the format needs one, and a `build_translation_xslt` mapping).

## Running

Both modes need the local Stroom stack (`dev/stroom`).

**Reference** (no model): proves the cases and the scoring by putting each reference solution through the real
path: generated XSLT, stepping, processing, event validation, a Lucene index and a verification search.

```
uv run python dev/eval/run_eval.py --reference
```

**Agent**: start the MCP server locally, then point the runner at a model.

```
STROOM_MCP_STROOM_URL=http://127.0.0.1:18080 STROOM_MCP_STROOM_API_KEY=<admin key from dev/stroom/.env> \
STROOM_MCP_DEV_NO_AUTH=true STROOM_MCP_HOST=127.0.0.1 STROOM_MCP_PORT=8765 \
STROOM_MCP_EVENT_LOGGING_VERSION=4.1.0 STROOM_MCP_DEFAULT_CONVENTION=stroom-flat uv run python main.py

# e.g. Gemma served by vLLM (OpenAI-compatible):
AGENT_MODEL=openai:google/gemma-4-31b-it AGENT_MODEL_BASE_URL=http://gpu-host:8000/v1 OPENAI_API_KEY=unused \
uv run --extra agent python dev/eval/run_eval.py --agent
```

A scripted user answers the agent's questions: yes to every confirmation and approval, accepts the proposed
template, enables the indexing filter, and answers a request for help with the case's next hint (each counts
against the one allowed) or "no hint". Pass case names to run a subset (`--agent 04 06`), and `--timeout` to
change the per-case limit (default 1800 s).

Results are written to `results/<time>-<mode>.json` (ignored by git) with a summary table.
