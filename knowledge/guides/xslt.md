# Stroom XSLT conventions

Stroom runs XSLT 2.0/3.0 (Saxon). Declare `xmlns:stroom="stroom"` to use Stroom functions.

## Input depends on the parser before the XSLT

| Parser (template) | XSLT input | `xpath-default-namespace` |
| --- | --- | --- |
| `DSParser` (Event Data (Text)) | `<records><record><data name="field" value="..."/>` | `records:2` |
| `JSONParser` (Event Data (JSON)), no text converter | JSON as `map`/`array`/`string` elements, keys in `@key`; root `/map` for JSON lines, `/array` for an array (see the JSON guide) | `http://www.w3.org/2013/XSL/json` |
| `XMLParser` (Event Data (XML)) | the source XML | the source namespace |
| `XMLFragmentParser` with an XML_FRAGMENT wrapper converter | the fragments (one `<Event>` per line) inside the wrapper's `records` root | the fragments' own namespace, else the wrapper's: `records:2` (see the Data Splitter guide) |
| Indexing pipelines | `<Events>` from the Events stream, with `@StreamId` and `@EventId` on each `Event` | `event-logging:3` |

Match the record element and build one `Event` per record; the split filter hands the XSLT one
record at a time. Without the right `xpath-default-namespace` (or a prefix bound to it) a bare `match="record"`
or `select="map"` selects nothing, the output is empty text, and processing writes no Events and no error;
`check_xslt` names such expressions, and stepping reports "Output contains no XML elements".

## Output

Every element written below `Event` must be one the event-logging schema has: `EventTime`, `EventSource`,
`EventDetail` (holding `TypeId`, `Description` and exactly one action element such as `Authenticate`, `Process`,
`View`, `Alert` or `Unknown`), in schema order. An element the schema lacks (`EventDetail/ServerEvent`,
`Outcome/Result`) fails validation on every record; `check_xslt` refuses it and names what is allowed there,
and `build_translation_xslt` only accepts schema paths. Source values with no home go in `Data` elements
(`EventDetail/<Action>/Data` with `Name` and `Value`).

## Functions used most

Only the functions Stroom registers compile; `check_xslt` refuses any other `stroom:` name with the nearest
real one. There is no `stroom:json-parse()`: JSON in a string is parsed with `json-to-xml()`, and raw JSON by
the pipeline's `JSONParser`.

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

Fields are Data Splitter names, JSON keys (`user.name` for nested keys), XML paths relative to the record, or
names from `extract`. The tool puts elements in schema order, converts times with `stroom:format-date`, leaves
elements out when their input is empty (or writes `default`), and logs records no rule matches. Unknown paths,
constants the schema does not allow, alternatives used together and missing required elements come back as
problems to fix in the mapping. Give it the `sample` too (and the `splitter` spec for text): it then reports
fields no sample record has, with the nearest names, and time formats the sample's values do not fit, before
anything is stepped.

Each value has one source and optional modifiers:

| Source | Meaning |
| --- | --- |
| `field` | An input field, or a name from `extract` |
| `any_of: [a, b]` | The first of these fields with a value, for sources whose variants name a thing differently |
| `value` | A constant |
| `lookup: {map, field, path?}` | What reference data holds for the key (see the reference-data guide) |
| `xpath` | A computed value (embedded JSON: `json-to-xml(...)/*/*[@key='x']`) |

| Modifier | Meaning |
| --- | --- |
| `transform` | `lower`, `upper`, `trim`, `strip_domain` (`DOMAIN\user`, `user@domain` → `user`), `domain`, `digits` |
| `dictionary: name` | The value a Dictionary of key=value lines gives the field |
| `map`, `default`, `time_format`, `timezone` | As before: a value map, a fallback, time parsing |

Conditions test a field with `equals`, `one_of`, `matches`, `present` or `in_dictionary` (a Dictionary of one
entry per line). Write XSLT by hand only for what none of this expresses.

### Text fields holding several values

A message string such as `2026-10-01 10:00:00 alice LOGIN Successful login from 10.0.0.1` is parsed with
`extract`: a regular expression whose capture groups become fields, usable in `common`, `events` and `when` like
any input field. An extraction can read a field an earlier one produced. Not `substring-before()` /
`substring-after()` chains, which break on the first value with a space or a missing part.

```json
{"input": "json", "json_layout": "lines",
 "extract": [{"field": "message", "regex": "^(\\S+ \\S+) (\\S+) (\\S+) (.*)$", "names": ["ts", "user", "action", "desc"]},
             {"field": "desc", "regex": "^(Successful|Failed)", "names": ["result"]}],
 "common": [{"path": "EventTime/TimeCreated", "field": "ts", "time_format": "yyyy-MM-dd HH:mm:ss", "timezone": "UTC"},
            {"path": "EventSource/User/Id", "field": "user"},
            {"path": "EventDetail/Description", "field": "desc"}],
 "events": [{"name": "logon", "when": [{"field": "action", "equals": "LOGIN"}],
             "fields": [{"path": "EventDetail/Authenticate/Action", "value": "Logon"},
                        {"path": "EventDetail/Authenticate/Outcome/Success", "field": "result",
                         "map": {"Successful": "true", "Failed": "false"}}]}]}
```

The generated XSLT holds `analyze-string(message, regex)` in a variable per template and reads each group from
it. XPath regular expressions have no lookaround and no named groups; use `(?:...)` for groups that are not
fields, and anchor the pattern. A record the pattern does not match gets no values from it, so its elements are
left out; a rule can test that with `{"field": "ts", "present": false}` (and `drop` it, or map it to `Unknown`).

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
