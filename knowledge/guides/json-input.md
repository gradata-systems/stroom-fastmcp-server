# JSON input

JSON is parsed by the `JSONParser` element of the Event Data (JSON) template. It needs no text converter: a
Data Splitter is for text, and a converter holding a `<jsonParser>` element parses nothing (`save_text_converter`
refuses one). The parser turns JSON into XML in the namespace `http://www.w3.org/2013/XSL/json` (not the
`xpath-functions` namespace that `json-to-xml()` uses):

```json
{"ts": "2026-09-28T10:00:00Z", "user": {"name": "alice"}, "tags": ["a", "b"], "ok": true}
```

```xml
<map xmlns="http://www.w3.org/2013/XSL/json">
  <string key="ts">2026-09-28T10:00:00Z</string>
  <map key="user"><string key="name">alice</string></map>
  <array key="tags"><string>a</string><string>b</string></array>
  <boolean key="ok">true</boolean>
</map>
```

## Layouts: JSON lines and arrays

The parser reads every top-level value in the stream. Its `addRootObject` property (true by default) wraps them
all in one `map`, which is what makes JSON lines (one object per line, or concatenated objects) well-formed XML:

| Source | `jsonParser.addRootObject` | XSLT root | Records | Mapping |
| --- | --- | --- | --- | --- |
| JSON lines | `true` (keep the default; false gives several root elements, which fails) | `/map` | `/map/map` | `input: json, json_layout: lines` |
| One JSON array | `false` (set it on `create_pipeline`, as sibling pipelines do) | `/array` | `/array/map` | `input: json, json_layout: array` |
| One JSON array, default left | `true` | `/map` | `/map/array/map` | the same; the generated XSLT matches both |

`profile_sample` reports which (`parser_properties`, `xslt_input`), and `build_translation_xslt` returns the
setting to apply as `pipeline_properties`. Check `describe_document` for the value a template's children use.

## Addressing fields

Declare `xmlns:json="http://www.w3.org/2013/XSL/json"` and match `json:map` and `json:string[@key='ts']`, or set
`xpath-default-namespace="http://www.w3.org/2013/XSL/json"`, as the generated XSLT does. With the wrong namespace
(or none) no template matches, the output is bare text, and processing writes nothing; `check_xslt` reports the
bare names, and stepping reports "Output contains no XML elements". In a mapping, fields are JSON keys:
`user.name` for nested keys.

## The event inside a string field

Log shippers often wrap the real event as a string. Two cases:

**Text** (a message such as `2026-10-01 10:00:00 alice LOGIN Successful login from 10.0.0.1`): parse it with the
mapping's `extract`, a regular expression whose groups become fields, not with `substring-before()` /
`substring-after()` chains:

```json
{"input": "json", "json_layout": "lines",
 "extract": [{"field": "message", "regex": "^(\\S+ \\S+) (\\S+) (\\S+) (.*)$", "names": ["ts", "user", "action", "desc"]},
             {"field": "desc", "regex": " from (\\S+)$", "names": ["client_ip"]}],
 "common": [{"path": "EventTime/TimeCreated", "field": "ts", "time_format": "yyyy-MM-dd HH:mm:ss", "timezone": "UTC"},
            {"path": "EventSource/User/Id", "field": "user"},
            {"path": "EventSource/Client/IPAddress", "field": "client_ip"},
            {"path": "EventDetail/Description", "field": "desc"}],
 "events": [{"name": "logon", "when": [{"field": "action", "equals": "LOGIN"}], "fields": ["..."]}]}
```

The XSLT it writes holds `analyze-string(message, regex)` in a variable and reads each group from it; a record the
pattern does not match gets no values, so its elements are left out and a rule can test `present: false`.

**JSON** (the field holds a JSON document, sometimes after a text prefix): read it with an `xpath` using
`json-to-xml()`, whose output is in the `http://www.w3.org/2005/xpath-functions` namespace:

```json
{"path": "EventSource/User/Id", "xpath": "json-to-xml(*[@key='message'])/*/*[@key='user']"}
```

There is no `stroom:json-parse()` or similar: `json-to-xml()` is the function, with no prefix. By hand, after a
prefix:

```xml
<xsl:analyze-string select="json:string[@key='body']" regex="^.+?(\{{.+\}})$">
  <xsl:matching-substring>
    <xsl:apply-templates select="json-to-xml(regex-group(1))/*" mode="event"/>
  </xsl:matching-substring>
</xsl:analyze-string>
```

(Braces in a `regex` attribute are doubled because the attribute is an attribute value template.)
Some environments do this in a separate XSLT step before the translation (e.g. `innerJsonFilter`);
`describe_template` shows whether sibling pipelines do.
