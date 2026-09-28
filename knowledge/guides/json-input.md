# JSON input

`JSONParser` (Event Data (JSON) template) turns JSON into XML in the namespace
`http://www.w3.org/2013/XSL/json` (not the `xpath-functions` namespace that `json-to-xml()` uses):

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

Declare `xmlns:json="http://www.w3.org/2013/XSL/json"` and match `/json:array`, `json:map`, and
`json:string[@key='ts']` (or set `xpath-default-namespace="http://www.w3.org/2013/XSL/json"`). With the
wrong namespace no template matches, the output is bare text, and processing writes nothing; stepping
reports this as "Output contains no XML elements".

`addRootObject` on the parser controls whether a wrapping element is added; children of existing
pipelines usually set it to `false`. Check `describe_pipeline` for the template's value.

## JSON inside a string field

Log shippers often wrap the real event as a string, sometimes after a text prefix. `json-to-xml()`
returns elements in the `http://www.w3.org/2005/xpath-functions` namespace, which is why existing XSLTs
often set that as `xpath-default-namespace` and use the `json:` prefix for the parser's own output:

```xml
<xsl:analyze-string select="json:string[@key='body']" regex="^.+?(\{{.+\}})$">
  <xsl:matching-substring>
    <xsl:apply-templates select="json-to-xml(regex-group(1))/*" mode="event"/>
  </xsl:matching-substring>
</xsl:analyze-string>
```

(Braces in a `regex` attribute are doubled because the attribute is an attribute value template.)
Some environments do this in a separate XSLT step before the translation (e.g. `innerJsonFilter`);
`list_template_children` shows whether sibling pipelines do.
