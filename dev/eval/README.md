# Evaluation set

Thirty-five samples to measure how well an agent builds with the server, whatever runs the agent. The bar: every
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
| `22_syslog_cef_gateway` | CEF after an RFC 3164 syslog header | Authenticate, Network, Alert | The server's inferred Data Splitter (syslog header, CEF header, extension with spaces in values), rules on the signature id, a time without a year |
| `23_pipe_quoted_helpdesk` | Pipe-delimited with a header; quoted values holding pipes and doubled quotes | Create, View, Update | Quoting inferred, `unescape_quotes`, free text carried exactly |
| `24_csv_noheader_quoted_print` | CSV without a header; quoted values with commas and doubled quotes | Print | Columns named as the user says them, quoted values read whole (a user `"o'brien, pat"`) |
| `25_syslog_kv_nil_proxy` | RFC 5424 syslog with a key=value body; `-` for none | Network | `nil_values`: `forbidden_values` fails an event holding `-` as a user |
| `26_xml_fragments_windows_ns` | Windows event XML fragments in their own namespace | Authenticate, Create | The fragment wrapper from `profile_sample`, the Windows namespace, seven-digit fractions, logon types, `Data[@Name]` paths |
| `27_endash_auth_messages` | Application log lines whose separators are en dashes on most lines and hyphens on some | Authenticate | The trap the test environment met: a regex written with `-` matches only some lines |
| `28_xml_records_badges` | The source's own XML: `<records><record>` in no namespace | Authorise or Authenticate | Not mistaken for a Data Splitter's `records:2`: translated to Events like any XML |
| `29_csv_large_files_proxy` | Two CSV files, 1.7 MB and 16,000 records each, on the user's disk (`files`, generated) | Network | `upload_sample files=`: a command per file, run by the user (the harness); every record of both files processed and indexed (`expected.records` counts the streams whole) |
| `30_jsonl_two_shapes_idp` | JSON lines in two shapes (a new and a legacy layout) in one stream | Authenticate (logon, logoff, account lock, password change) | Rules per shape, each with its own time format; none Unknown |
| `31_kv_awkward_times_fortigate` | key=value with three times: epoch nanoseconds, epoch seconds, and a local date and time with no zone | Authenticate | The time read right whichever way: nanoseconds to milliseconds, or the local time with the zone the user gives when asked (`user_knows`); `values` holds the instants |
| `32_xml_fragments_windows_many` | Windows event XML fragments: 22 event ids, one each | Authenticate, Authorise, Process, Install, Create, Update, Delete, View | More kinds than the draft's twelve rules: every one its proper type, none Unknown |
| `33_csv_cp1252_hr` | A CSV file in Windows-1252 on the user's disk | Create, Update, Delete | The feed's encoding: sent whole as UTF-8 the accented names are broken in Stroom though the user's excerpt shows them right; the user knows the encoding when asked |
| `34_jsonl_schema_traps_nac` | JSON lines with values the schema rejects as written | Authenticate | Upper-case IPv6 (lower-cased), MAC addresses in three styles (to upper-case pairs), a port written `443/tcp`, an empty user |
| `35_xml_fragments_secretserver_messages` | Windows event XML fragments (Delinea Secret Server), the event in a message string; 33 records, one per category and action, cut from a user's 1,000-record sample | Authenticate, Update, View, Create, Delete, Alert, Process | The kind of event extracted from the message (`extract`), details after it in three separator styles, the source IP in `EventSource/Client/IPAddress` (not Data), a TypeId per kind of event (not the category), none Unknown |
| `21_csv_connections_odd_fields` | CSV with a header: connections made and ended, with the broker's own fields (zones, `destination_key`, a TLS fingerprint, policy, connection id) | Network (Connect, Close) | Fields with no element of their own carried as `Data` on the side they describe (`destination_key` under `Destination`, `source_zone` under `Source`) rather than invented or left Unknown: `forbidden_types: [Unknown]` and `data` checks |

Each case (`cases/*.yaml`) holds the sample (or `samples`, several files), the request to give the agent, what the output must contain (record
count, event types, paths every event must have (`*` for one element any of several may fill, such as
`Network/*/Source` for Open, Permit or Deny; `a|b` for alternatives, in types as in paths; an element the schema
makes optional is required only when the source data holds its value), and optionally `values` some event must hold exactly, such as a
free-text message carried whole or a time read from the right field), optionally `forbidden_types` no event may have
(Unknown, for a source whose every record has an action element) and `data`: a Data element of a given Name (and
value) that every event, or with `every: false` some event, must have directly under a given element, and
`forbidden_values` no event may hold at a path (a source's `-` for none, as a user id). Also an optional list of
`hints`, `user_knows` (what the user knows of their source without saying it in the request, such as a time zone
or an encoding: the scripted user answers from it when asked), and a reference solution (a Data Splitter where the
format needs one, and a `build_translation_xslt` mapping).

A case whose samples are files on the user's disk has `files` instead of `sample`: each a `text` in its
`encoding`, or made by a generator in `generated.py` (`generate: {kind, ...}`) when it is too large to keep here.
What the agent is shown is their first lines, as the user would paste them; the files themselves are in the agent's
working folder, and the reference run sends their bytes, as an upload command would.

The reference solutions from case 22 on use the server's own Data Splitter (`splitter: infer`, or a spec, with no
`converter`: `build_data_splitter` saves it in the build, where `create_pipeline` finds it) and the server's own
fragment wrapper (`converter: profile`). `feed_encoding` sets the feed's encoding.

**Offline** (no Stroom, no model): `uv run python dev/eval/offline.py [case ...]` checks each reference as
`build_translation_xslt` does before it saves (the mapping generates, the sample is read as the server's Data
Splitter reads it, every field and time format fits, the extractions and xpaths select something) and counts the
records; `tests/test_eval.py` runs it on every case. Stepping and processing are the reference run's.

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

When the agent hands the user something to do outside the chat, the harness does it, as the user would, and
tells the agent what came of it: the upload commands `upload_sample files=` gives (each file in the agent's folder
sent to its ticket's URL, as the curl command would) and an index template `propose_index_template` gives (applied
to the local Elasticsearch, as the cluster admin would).

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
| `fix_errors` | A production events pipeline made directly in Stroom, outside any build, writing Error streams: records whose time has a fraction of a second, and records from IPv6 clients (upper case), fail. The user names them (`update_events_pipeline` with the issue) | Fixed in place (a working copy, promoted): the production pipeline steps every record of its feed clean, each an Event, the fractions kept and the IPv6 addresses in lower case |
| `change_event_type` | A working production pipeline that writes PWCHANGE records, and SCREENLOCK ones, as Unknown. The user asks for PWCHANGE as Authenticate ChangePassword | Changed in place: each PWCHANGE record an Authenticate event with that action and its user; every other record's event exactly as before (SCREENLOCK still Unknown) |
| `records_output` | An asset inventory (CSV) that isn't events, and a records template of the environment's own (a parser writing Records, named as no standard template is). The user asks for it kept as Records and indexed as it is (`create_discovery_index`) | Records streams in the feed, not Events; the Elasticsearch index holds every device, by hostname |
| `document_index` | A production Elasticsearch index another system loads (documents pointing at a stream in this Stroom), its Elastic Index doc in production | The doc is promoted beside the index doc, its field table has every field, it has the Data surveyed summary, and Purpose and data is the agent's own prose (150 characters or more) built on what the user said of the index's purpose: the agent has to ask, as the survey cannot say why the index exists |
