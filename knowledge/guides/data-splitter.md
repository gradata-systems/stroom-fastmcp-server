# Data Splitter recipes

A Data Splitter text converter (type `DATA_SPLITTER`, used by `DSParser`) turns text into
`records:2` XML: one `<record>` per match, with `<data name value>` for each field.

## Generate it from a spec

`build_data_splitter` takes the sample's text, infers the spec from its format (delimited with or without a header,
key=value, syslog with a key=value body) and runs it locally: the records it produces, the lines that match
nothing, and the field names the mapping may use. Give it a `spec` only when the inferred one is wrong or the
text is free-form (a regex with a name per group). Give the same spec to `build_translation_xslt` as `splitter`
so the mapping is checked against those records. A sample is the file's text, never a path: the server cannot
read the client's files, and refuses a path with that instruction.

| Spec | Example |
| --- | --- |
| Delimited with a header line | `{"kind": "delimited", "delimiter": ",", "header": true, "quote": "\""}` |
| Delimited, named columns | `{"kind": "delimited", "delimiter": "\|", "header": ["time", "user", "action"]}` |
| Regex, a name per group | `{"kind": "regex", "pattern": "^(\\S+) (\\S+) (.*)$", "names": ["time", "user", "message"]}` |
| key=value pairs | `{"kind": "key_value", "delimiter": " ", "pair_separator": "=", "quote": "\""}` |
| Syslog, body parsed further | `{"kind": "syslog", "rfc": "rfc3164", "body": {"kind": "key_value"}}` |
| CEF, alone or after syslog | `{"kind": "cef"}` (a syslog header before it is inferred as `body`) |

`body` parses one field further (syslog's `message`, or a regex group) and adds its fields; the recipes below
are what it writes.

## Delimited with a header row

```xml
<?xml version="1.1" encoding="UTF-8"?>
<dataSplitter xmlns="data-splitter:3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xsi:schemaLocation="data-splitter:3 file://data-splitter-v3.0.xsd" version="3.0">
  <split delimiter="\n" maxMatch="1">          <!-- first line: remember the column names -->
    <group>
      <split delimiter=",">
        <var id="heading" />
      </split>
    </group>
  </split>
  <split delimiter="\n">                        <!-- every other line: one record -->
    <group>
      <split delimiter=",">
        <data name="$heading$1" value="$1" />
      </split>
    </group>
  </split>
</dataSplitter>
```

Change `delimiter` for tab (`\t`), pipe or semicolon data. Quoted fields need
`<split delimiter="," containerStart="&quot;" containerEnd="&quot;">`. A doubled quote inside a quoted value
(`"Said ""bye"" and left"`) stays doubled: Data Splitter has no way to make it one (its `escape` attribute breaks
the quoting), so the mapping reads such a field with `transform: unescape_quotes`; the draft sets it where the
sample has them.

## Without a header

Name the columns in the spec: `{"kind": "delimited", "header": ["time", "user", "src_ip", "action"]}`. Give
the same spec to `draft_translation_mapping` and `build_translation_xslt` as `splitter`, so they use those names
rather than inferring `col1`, `col2`, ...

## Syslog and other free text

```xml
<regex pattern="^&lt;(\d+)&gt;(\w{3} +\d+ \d{2}:\d{2}:\d{2}) (\S+) ([^:\[]+)(?:\[(\d+)\])?: (.*)$">
  <data name="pri" value="$1"/><data name="time" value="$2"/><data name="host" value="$3"/>
  <data name="tag" value="$4"/><data name="pid" value="$5"/><data name="message" value="$6"/>
</regex>
```

Split the message further with a nested `<group value="$6">`. Key=value pairs are one `<regex>` matched
along the text, its value in one group whether quoted or not:

```xml
<regex pattern="\s*([^=\s]+)=&quot;?((?&lt;=&quot;)[^&quot;]*(?=&quot;)|(?&lt;!&quot;)[^\s&quot;]*)&quot;?">
  <data name="$1" value="$2" />
</regex>
```

Not `<split delimiter="=">` with `$2`: a split has only `$1`, and Stroom fails with "Group number 2 not found". Nor a
value made of two groups (`$2$3`) or two alternative regexes: a group that took no part in the match can't be
named, and a group's regexes aren't tried in turn at each place. `build_data_splitter` writes this for you.

Many syslog and key=value sources write `-` for none (RFC 5424's NILVALUE): the mapping's `nil_values: ["-"]` leaves
those elements out instead of writing `-` (the draft sets it when the sample has them).

## CEF

`CEF:Version|Device Vendor|Device Product|Device Version|Signature ID|Name|Severity|Extension`, alone or after a
syslog header. The header's fields are `cef_version`, `cef_vendor`, `cef_product`, `cef_device_version`,
`cef_signature_id`, `cef_name` and `cef_severity`; the extension's pairs are fields by their own keys (`suser`, `src`,
`act`, `msg`, `rt`...), a value running to the next ` key=`, spaces and all. A syslog header before `CEF:` gives
`pri`, `time` and `host`. `profile_sample` names the format and `build_data_splitter` infers the spec
(`{"kind": "cef"}`); the draft reads `act` for the kind of event, `suser` for the user, `src`/`dst` for addresses.

Step the pipeline after each change: the `dsParser` element's output shows the records produced.

## XML fragments (XML_FRAGMENT converter)

Several root elements, e.g. one `<Event>` per line with no document root, are not XML the `XMLParser` can read.
The `XMLFragmentParser` reads them through a text converter of type `XML_FRAGMENT`: the wrapper document the
fragments are parsed inside, where the entity `fragment` is the stream. Environments usually have wrappers of
their own: `profile_sample` returns the one to use as `text_converter.code` (the environment's own when there is one,
named in `text_converter.environment`), and `build_data_splitter` with `save_as` saves it in the build. With none,
the standard one is a `records` wrapper:

```xml
<?xml version="1.1" encoding="UTF-8"?>
<!DOCTYPE records [
<!ENTITY fragment SYSTEM "fragment">
]>
<records xmlns="records:2" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="records:2 file://records-v2.0.xsd" version="2.0">
&fragment;
</records>
```

An `<Events xmlns="event-logging:3">` wrapper is as common (fragments that are event-logging `<Event>`s already
always take one):

```xml
<?xml version="1.1" encoding="UTF-8"?>
<!DOCTYPE Events [
<!ENTITY fragment SYSTEM "fragment">
]>
<Events xmlns="event-logging:3" xmlns:stroom="stroom" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:schemaLocation="event-logging:3 file://event-logging-v3.5.2.xsd" Version="3.5.2">
&fragment;
</Events>
```

Two things follow from the wrapper:

- **Namespace.** A fragment that declares no `xmlns` of its own takes the wrapper root's default namespace, so the
  XSLT reads it in `records:2` inside a `<records>` wrapper and in `event-logging:3` inside an `<Events>` one: set
  the mapping's `xml_namespace` to it (`records:2` is the default for `xml_fragments`). A fragment with its own namespace
  (Windows event XML, say) keeps it, and the mapping gives it as `xml_namespace`. Read in the wrong namespace, every
  record comes out as an empty `Events`: `step_sample` blocks on that.
- **Records.** The fragments sit under the wrapper's root: mapping `input: xml_fragments, record: Event` matches
  `/` and selects `*/Event`. The split filter still hands the XSLT one fragment at a time.

The pipeline needs an `XMLFragmentParser` element. Use a template whose chain has one (`find_pipeline_templates`
shows `parser`, and `child_must_supply` names its `textConverter`); when the environment has none, it says so, and
`create_pipeline` from a translation template whose parser is the `XMLParser` (whatever it is called here) with
`replace_parser: XMLFragmentParser` puts one in place of the `XMLParser`, with the converter on
`xmlFragmentParser.textConverter`.
