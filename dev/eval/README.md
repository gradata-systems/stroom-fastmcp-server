# Evaluation set

Forty-seven samples to measure how well an agent builds with the server, whatever runs the agent. The bar: every
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
| `36_xml_fragments_secretserver_whole_export` | As case 35, at the size of the user's export: 1,000 records (744 KB) in a file on the user's disk, generated from case 35's records | As case 35 | The whole sample read, not its head: the agent sees only the first lines, the request says no more than the user's did, every record is the same Windows event but for its message, and the first 400 are logons and secret views only, so the other kinds start about 280,000 characters in (Stroom returns about 80,000 a fetch). The scorer reads a stream's events in blocks spread over it, so they are checked too |
| `37_cef_gateway_whole_day` | Syslog then CEF, a day's file: 5,000 lines (883 KB) on the user's disk | Authenticate, Network, Alert | Case 22 at size: the first 3,000 lines are logons and allowed requests only, the other kinds start about 536,000 characters in; kinds of event read from the CEF header (signature id, name), not masked text; the mapping checks read lines spread over the sample, not its first 200 |
| `38_json_array_one_line_alerts` | One JSON array on a single line: 2,500 alerts (387 KB) on the user's disk | Alert | Case 02 as a SIEM exports it: Stroom holds the stream as one record of one line, so reading, surveying and stepping it rely on the server splitting it, and the parser needs `addRootObject` false; the critical alerts start about 230,000 characters in |
| `39_w3c_bluecoat_proxy` | Blue Coat access log, W3C extended format: 3,000 requests on the user's disk | Network | Columns named by a `#Fields:` directive (no first-line header), the device only in `#Remark` (an `extract` on the directive's field), quoted values with spaces, and a restart at the 2,000th request with new directives and the columns reordered and added |
| `40_json_cloudtrail_records` | One pretty-printed JSON object whose `Records` array holds 800 events (AWS CloudTrail) | Authenticate, View, Delete, Create, Update | The object is the parser's one record and `for_each` makes an Event of each item; nested fields; the kind of event in `eventName`; a source address that is sometimes a service's name; the later kinds past the 500th event |
| `41_java_multiline_app` | Java application log (logback), 2,000 entries with stack traces over several lines | Authenticate, Create, Alert, Update | `record_start`: a record begins at a line starting with its time, the stack trace's lines are part of it; a regex over a message of several lines (`[\s\S]`, not `.`); the later kinds past the 1,200th entry |
| `42_csv_quoted_newlines_tickets` | CSV export whose quoted comments hold commas, doubled quotes and new lines | Create, Update | `quoted_newlines`: a record ends at a new line outside quotes; `unescape_quotes`; an empty last column |
| `43_syslog_csv_palo_alto_traffic` | Palo Alto TRAFFIC logs: an RFC 3164 header, then 47 positional CSV columns | Network | A delimited body after a syslog header, its columns named by position until named from the vendor's documentation; the kind of event in type and subtype |
| `44_custom_tilde_delimited` | In-house access log: fields separated by ` ~ `, no header | Authenticate | A separator of several characters, which the CSV sniffer doesn't try; named columns |
| `45_jsonl_gelf` | Graylog GELF messages (JSON lines) | Authenticate, Alert | Fractional epoch seconds (`epoch_s`); the kind of event in an `_`-prefixed additional field; fields that differ by kind |
| `46_jsonl_ecs_input` | Events already in ECS (JSON lines, nested) | Authenticate, Delete | An ECS source translated to Events like any other; the kind of event in `event.action`; arrays in `event.category` |
| `47_kv_fortios_native` | FortiGate native logs (key=value) | Network, Authenticate, Alert | The kind of event in two keys (type, subtype); the time from date, time and tz; keys that differ by kind |
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

## Capabilities

What the cases exercise, by capability rather than source format (a case's `capabilities:`): a capability with no case is a gap, and `tests/test_eval.py` fails until it has one or a reason it is pending (`dev/eval/capabilities.py`, which also prints this table).

| Capability | What | Cases |
| --- | --- | --- |
| Format recognition | JSON, XML, CSV, CEF, syslog and free text detected without being told | `01`, `02`, `05`, `06`, `07`, `08`, `12`, `22`, `26`, `28`, `40` |
| Schema inference | fields and their types inferred from the sample | `01`, `02`, `09`, `15`, `43` |
| Field variability | different fields present for different kinds of event | `11`, `19`, `30`, `35`, `45`, `47` |
| Delimiter detection | commas, tabs, pipes, semicolons, spaces and separators of several characters | `01`, `08`, `09`, `23`, `24`, `27`, `39`, `43`, `44` |
| Escaping | embedded and doubled quotes, escaped separators | `18`, `19`, `22`, `23`, `24`, `42` |
| Nested structures | JSON objects and arrays, XML elements and attributes | `02`, `03`, `04`, `05`, `10`, `13`, `16`, `26`, `40`, `46` |
| Timestamp recognition | ISO 8601, epoch (seconds, milliseconds, nanoseconds), syslog, split and custom formats | `01`, `06`, `07`, `17`, `31`, `39`, `45`, `47` |
| Type inference | addresses, integers, booleans, values the schema rejects as written | `34`, `40` |
| Multi-line records | stack traces, quoted values over several lines | `41`, `42` |
| Event boundaries | where one record ends: fragments, arrays, wrapped objects, several events in one | `12`, `16`, `28`, `38`, `40`, `41` |
| Optional fields | missing, empty and placeholder values | `06`, `17`, `21`, `25`, `42` |
| Field normalisation | a source's names mapped to the schema's (ECS, codes, odd fields) | `15`, `20`, `21`, `46` |
| Schema variation | many kinds of event from one source | `05`, `14`, `17`, `19`, `30`, `32`, `36` |
| Malformed records | truncation, invalid quoting, missing separators, reported not dropped | pending: no case yet: a sample with truncated lines, unbalanced quotes and missing separators, whose records the server reports rather than drops |
| Mixed formats | syslog wrapping CEF or CSV, JSON or text inside JSON, text inside XML | `04`, `11`, `13`, `18`, `22`, `25`, `35`, `37`, `43` |
| Encoding | non-UTF-8 files, Unicode punctuation, control characters | `27`, `33` |
| Field repetition | repeated keys and elements, arrays of values | `16` |
| Format evolution | new or reordered fields, a changed version, mid-file | `14`, `39` |
| Large samples | samples bigger than one read, the kinds of event deep in the file | `29`, `36`, `37`, `38` |

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
