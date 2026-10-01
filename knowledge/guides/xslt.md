# Stroom XSLT conventions

Stroom runs XSLT 2.0/3.0 (Saxon). Declare `xmlns:stroom="stroom"` to use Stroom functions.

## Input depends on the parser before the XSLT

| Parser (template) | XSLT input | `xpath-default-namespace` |
| --- | --- | --- |
| `DSParser` (Event Data (Text)) | `<records><record><data name="field" value="..."/>` | `records:2` |
| `JSONParser` (Event Data (JSON)) | JSON as `map`/`array`/`string` elements, keys in `@key` | `http://www.w3.org/2013/XSL/json` |
| `XMLParser` (Event Data (XML)) | the source XML | the source namespace |
| Indexing pipelines | `<Events>` from the Events stream, with `@StreamId` and `@EventId` on each `Event` | `event-logging:3` |

Match the record element and build one `Event` per record; the split filter hands the XSLT one
record at a time.

## Functions used most

| Function | Use |
| --- | --- |
| `stroom:format-date(value, 'pattern')` | Parse a timestamp into `yyyy-MM-ddTHH:mm:ss.SSSZ`. A third argument gives the input time zone (e.g. `'+10:00'`); the five-argument form also sets the output pattern and zone. A value that does not match logs a WARNING and returns empty, which then fails the schema. |
| `stroom:lookup('map', key)` | Reference data lookup through a reference loader attached to the XSLT element. |
| `stroom:meta('Name')` | A receipt header of the source stream, e.g. `Feed`, `ReceivedTime`, `RemoteAddress`, or a custom one. See `get_stream_attributes`. |
| `stroom:feed-name()`, `stroom:stream-id()`, `stroom:record-no()` | Where the record came from. |
| `stroom:dictionary('Name')` | Text of a Dictionary doc, e.g. a field mapping table. |
| `json-to-xml(string)` | Parse JSON held in a string field into `map`/`array` elements. |
| `stroom:log('ERROR', 'message')` | Raise your own marker, e.g. for an unexpected event type. |

## Generating instead of writing

`build_translation_xslt` writes the translation from a mapping, so the XSLT itself need not be written by hand:

```json
{"input": "data_splitter",
 "common": [{"path": "EventTime/TimeCreated", "field": "time", "time_format": "yyyy-MM-dd'T'HH:mm:ss"},
            {"path": "EventSource/System/Name", "value": "Acme VPN"},
            {"path": "EventSource/System/Environment", "value": "Prod"},
            {"path": "EventSource/Generator", "value": "vpnd"},
            {"path": "EventSource/Device/HostName", "field": "host"},
            {"path": "EventSource/User/Id", "field": "user"}],
 "events": [{"name": "logon", "when": [{"field": "action", "equals": "login"}],
             "fields": [{"path": "EventDetail/TypeId", "value": "VPN-Login"},
                        {"path": "EventDetail/Authenticate/Action", "value": "Logon"},
                        {"path": "EventDetail/Authenticate/User/Id", "field": "user"},
                        {"path": "EventDetail/Authenticate/Outcome/Success", "field": "result",
                         "map": {"ok": "true", "fail": "false"}},
                        {"path": "EventDetail/Authenticate/Data", "data_name": "session", "field": "sid"}]}]}
```

Fields are Data Splitter names, JSON keys (`user.name` for nested keys) or XML paths relative to the record.
The tool puts elements in schema order, converts times with `stroom:format-date`, leaves elements out when
their input is empty (or writes `default`), and logs records no rule matches. Unknown paths, constants the
schema does not allow, alternatives used together and missing required elements come back as problems to fix
in the mapping. Use `xpath` for a computed value, and write XSLT by hand only for what a mapping cannot
express, such as unpacking embedded JSON or reference lookups.

## Style

The mapping's `style` decides how the XSLT reads. Take it from an XSLT style section in the standing instructions
(AGENTS docs) when there is one; otherwise leave the defaults:

| Setting | Default | Effect |
| --- | --- | --- |
| `naming` | `snake_case` | Variables and named templates: `client_ip`, `event_source`, `action_to_success`. Also `camelCase`, `PascalCase`, `kebab-case`. |
| `variable_min_reads` | 3 | A field read this often in a template (its guard and value are two reads) goes in a variable, declared in the one rule that uses it, or at the top when several do. Fewer reads stay inline. 1: always variables. |
| `inline_map_max_keys` | 3 | A value map used by one element with at most this many keys is written inline as an `if`; longer or shared ones become one `xsl:map`. 0: always `xsl:map`. |

Elements that come out the same in several rules (EventTime, EventSource, ...) are written once as named
templates, whatever the style.

## Reuse

Existing pipelines `xsl:import` shared XSLTs by document name (e.g. `IP Lookup`) and keep field
mappings in Dictionary docs. Find them with `find_similar_translations` and `list_template_children`
before writing new code, and check imports resolve with `check_xslt`.

## Checking a draft

1. `check_xslt` for well-formedness, namespaces, function names and imports.
2. `step_pipeline` with `draft_code` for one record, then `step_sample` with `draft_code` for all of
   them. Nothing is saved; errors come back per record and element.
