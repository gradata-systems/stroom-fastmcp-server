# Data Splitter recipes

A Data Splitter text converter (type `DATA_SPLITTER`, used by `DSParser`) turns text into
`records:2` XML: one `<record>` per match, with `<data name value>` for each field.

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
`<split delimiter="," containerStart="&quot;" containerEnd="&quot;">`.

## Without a header

Name the columns: `<data name="time" value="$1"/>` inside a `<split>` per column position, or use a
`<regex>` with groups.

## Syslog and other free text

```xml
<regex pattern="^&lt;(\d+)&gt;(\w{3} +\d+ \d{2}:\d{2}:\d{2}) (\S+) ([^:\[]+)(?:\[(\d+)\])?: (.*)$">
  <data name="pri" value="$1"/><data name="time" value="$2"/><data name="host" value="$3"/>
  <data name="tag" value="$4"/><data name="pid" value="$5"/><data name="message" value="$6"/>
</regex>
```

Split the message further with nested `<regex>` or `<split>`, e.g. key=value pairs with
`<split delimiter=" "><group value="$1"><split delimiter="=" maxMatch="1">...`.

Step the pipeline after each change: the `dsParser` element's output shows the records produced.

## XML fragments (XML_FRAGMENT converter)

Several root elements, e.g. one `<Event>` per line with no document root, are not XML the `XMLParser` can read.
The `XMLFragmentParser` reads them through a text converter of type `XML_FRAGMENT`: the wrapper document the
fragments are parsed inside, where the entity `fragment` is the stream (`profile_sample` returns it as
`text_converter.code`):

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

Two things follow from the wrapper:

- **Namespace.** A fragment that declares no `xmlns` of its own takes the wrapper's default namespace, so the
  XSLT reads it in `records:2` (mapping `xml_namespace: "records:2"`); a fragment with its own namespace (Windows
  event XML, say) keeps it.
- **Records.** The fragments sit under the wrapper's root: mapping `input: xml_fragments, record: Event` matches
  `/` and selects `*/Event`. The split filter still hands the XSLT one fragment at a time.

The pipeline needs an `XMLFragmentParser` element. Use a template whose chain has one (`find_pipeline_templates`
shows `parser`, and `child_must_supply` names its `textConverter`); when the environment has none, it says so, and
`create_pipeline` from the Event Data (XML) template with `replace_parser: XMLFragmentParser` puts one in place of
the `XMLParser`, with the converter on `xmlFragmentParser.textConverter`.
