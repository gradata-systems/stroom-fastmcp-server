# JSON input

`JSONParser` (Event Data (JSON) template) turns each JSON value into `xpath-functions` XML:

```json
{"ts": "2026-09-28T10:00:00Z", "user": {"name": "alice"}, "tags": ["a", "b"], "ok": true}
```

```xml
<map xmlns="http://www.w3.org/2005/xpath-functions">
  <string key="ts">2026-09-28T10:00:00Z</string>
  <map key="user"><string key="name">alice</string></map>
  <array key="tags"><string>a</string><string>b</string></array>
  <boolean key="ok">true</boolean>
</map>
```

With `xpath-default-namespace="http://www.w3.org/2005/xpath-functions"`, address fields as
`string[@key='ts']` and `map[@key='user']/string[@key='name']`.

`addRootObject` on the parser controls whether a wrapping element is added; children of existing
pipelines usually set it to `false`. Check `describe_pipeline` for the template's value.

## JSON inside a string field

Log shippers often wrap the real event as a string, sometimes after a text prefix:

```xml
<xsl:analyze-string select="string[@key='body']" regex="^.+?(\{{.+\}})$">
  <xsl:matching-substring>
    <xsl:apply-templates select="json-to-xml(regex-group(1))/*" mode="event"/>
  </xsl:matching-substring>
</xsl:analyze-string>
```

(Braces in a `regex` attribute are doubled because the attribute is an attribute value template.)
Some environments do this in a separate XSLT step before the translation (e.g. `innerJsonFilter`);
`list_template_children` shows whether sibling pipelines do.
