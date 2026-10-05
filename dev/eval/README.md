# Evaluation set

Twenty-one samples to measure how well an agent builds with the server, whatever runs the agent. The bar: every
case reaches indexed events with no hints. It holds for the reference solutions, and for an agent on the default
model, where a case passes when most of its runs pass (`run_agent.py --repeat`). Lighter models are measured
against the same bar rather than held to it: their pass rate shows how far the server's own guidance carries a
weaker model. A case below the bar is a finding to chase (in the server's messages, prompts or guides, or in the
case), not a margin to spend.

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
| `17_csv_firewall_mixed` | CSV with a header: traffic, admin and system records in one file | Network, Authenticate, Update, Export, Alert | Rules on two fields (`event_type` and `action`), five event types from one file, sparse and space-only columns, a time with an offset, `Rule` and `Data` on Network, mapped Alert type and severity |
| `18_json_event_string_freetext` | JSON array; the event is JSON in a string field: timestamp, username, event_type, an optional resource and a free-form message | Authenticate (logon, logoff, password change), View, Delete | `json-to-xml()` through `xpath` for every field, the event's own time (millisecond, offset) rather than the shipper's, free text kept whole as the Description (quotes, colons, text that looks like key=value), `values` checks |
| `19_jsonl_message_layouts` | JSON lines; the message's layout depends on its first word, with quoted values, optional and extra keys, and free text | Authenticate, Alert, Unknown | Several `extract` regexes over one field, `any_of` for a quoted or bare value, rules per kind, a catch-all rule for unrecognised lines, `values` checks that quoted names are read whole |
| `20_csv_coded_with_docs` | CSV with a header, the vendor's own codes (`A17`, `R7`) | Authenticate (logon, failed logon, logoff) | The vendor's documentation (`source_docs`): only it says A18 is a failed logon and A40 a logoff; `values` checks Action and Outcome/Success. `run_agent.py --without-source-docs 20` runs it without the documentation, to measure what the documentation is worth |
| `21_csv_connections_odd_fields` | CSV with a header: connections made and ended, with the broker's own fields (zones, `destination_key`, a TLS fingerprint, policy, connection id) | Network (Connect, Close) | Fields with no element of their own carried as `Data` on the side they describe (`destination_key` under `Destination`, `source_zone` under `Source`) rather than invented or left Unknown: `forbidden_types: [Unknown]` and `data` checks |

Each case (`cases/*.yaml`) holds the sample (or `samples`, several files), the request to give the agent, what the output must contain (record
count, event types, paths every event must have (`*` for one element any of several may fill, such as
`Network/*/Source` for Open, Permit or Deny; `a|b` for alternatives, in types as in paths), and optionally `values` some event must hold exactly, such as a
free-text message carried whole or a time read from the right field), optionally `forbidden_types` no event may have
(Unknown, for a source whose every record has an action element) and `data`: a Data element of a given Name (and
value) that every event, or with `every: false` some event, must have directly under a given element. Also an optional list of `hints`, and a reference solution (a Data
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
(a hint fails the case). A case passes when its Events hold the expected record count, event types and paths, and
the verification search finds them in the index.

**Headless Claude Code** (on the `claude` CLI's sign-in: the Claude subscription, never an API key, as
`ANTHROPIC_API_KEY` is kept from it): `run_agent.py` does all of the above unattended, on Haiku by default to spare
the plan's usage.

```
uv run python dev/eval/run_agent.py                                 # every case, on Haiku
uv run python dev/eval/run_agent.py 06 json                         # some cases
uv run python dev/eval/run_agent.py --model default 01              # Claude Code's default model instead
uv run python dev/eval/run_agent.py --model default --model haiku   # both, one after the other
uv run python dev/eval/run_agent.py --repeat 3                      # each case three times, a pass rate per case
```

It starts this checkout's server (no sign-in, the local stack, confirmations as pending ids rather than forms)
and, per case, gives `claude -p` the server's `onboard_data_source` prompt rendered with the case's sample, then
the request and the build and feed names to use. The agent has only the server's tools and resources: no file,
shell or web tools, and none of your settings, hooks or other MCP servers. Each time it stops, a second model
(`--user-model`, Haiku by default) plays the user on its last message: it agrees, answers from the request, says
to carry on, says not to promote, or, when the agent asks for help, the case's next hint is given (counted). The
build is then scored from Stroom with the same checks as `--reference`.

Results are written to `results/<time>-agent-<model>.json` with the user turns, help requests, tool calls and
the API-equivalent cost Claude Code reports; each case's transcript (stream-json) and the server log are in
`results/<time>-agent/`. A model's run is one sample, so one run can pass or fail by chance: with `--repeat N`
each case runs N times, the summary shows its passed runs, and it passes when most do; a case passing some runs
but not most is flaky rather than broken. `--effort`, `--max-user-turns` and `--turn-timeout` tune a run.

## Workflows beyond onboarding

`dev/eval/workflows.py` holds agent cases for the other prompts. Each has a setup (the starting state, made on the
local stack through the server's tools), the prompt and the user's request, the facts the scripted user may answer
from, what finished means, and a scorer reading Stroom. A case passes when the scorer finds no problem, with no help
requests and nothing confirmed without asking the user.

```
uv run python dev/eval/run_agent.py --workflow document_index              # the agent, on Haiku
uv run python dev/eval/run_agent.py --workflow document_index --reference  # setup and scorer, no model
```

| Workflow | Starting state | Passes when |
| --- | --- | --- |
| `document_index` | A production Elasticsearch index another system loads (documents pointing at a stream in this Stroom), its Elastic Index doc in production | The doc is promoted beside the index doc, its field table has every field, it has the Data surveyed summary, and Purpose and data is the agent's own prose (150 characters or more) built on what the user said of the index's purpose: the agent has to ask, as the survey cannot say why the index exists |
