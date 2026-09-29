# Indexing: Lucene and Elasticsearch

Stage 2 is the same on both backends: field plan, index doc, indexing pipeline from a template,
step the Events, process, then verify with a dashboard and test searches. Which backend a build uses
comes from the chosen indexing template (`find_pipeline_templates stage=indexing` reports it).

| | Lucene (Stroom `Index`) | Elasticsearch |
| --- | --- | --- |
| Template | `Indexing`: `IndexingFilter` with property `index` | e.g. `Events to Elasticsearch`: `ElasticIndexingFilter` with `cluster` and `indexName` |
| XSLT output | `records:2` | `xpath-functions` JSON XML |
| Fields | On the index doc (`set_index_fields`) | An index template, drafted by `propose_index_template` and committed to Elasticsearch by the user |

## Lucene XSLT output

```xml
<records xmlns="records:2" xsi:schemaLocation="records:2 file://records-v2.0.xsd" version="2.0">
  <record>
    <data name="StreamId" value="{@StreamId}"/>
    <data name="EventId" value="{@EventId}"/>
    <data name="EventTime" value="{EventTime/TimeCreated}"/>
    <data name="UserId" value="{EventSource/User/Id}"/>
  </record>
</records>
```

Field types: `ID` for `StreamId`/`EventId`, `DATE` for the time field (the index doc's time field),
`TEXT` with the `KEYWORD` analyzer for exact-match values, `TEXT` with `ALPHA_NUMERIC` for free
text. A field typed `KEYWORD` is accepted but indexes nothing.

## Elasticsearch XSLT output

```xml
<array xmlns="http://www.w3.org/2005/xpath-functions">
  <map>
    <number key="StreamId"><xsl:value-of select="@StreamId"/></number>
    <number key="EventId"><xsl:value-of select="@EventId"/></number>
    <string key="@timestamp"><xsl:value-of select="EventTime/TimeCreated"/></string>
    <map key="user"><string key="name"><xsl:value-of select="EventSource/User/Id"/></string></map>
  </map>
</array>
```

Arrays: `<array key="tags"><string>a</string></array>`. `indexName` may interpolate a value from the
document, e.g. `ecs-windows{_suffix}v1` with a `_suffix` string key; keys starting with `_` are not
indexed. Field names and types follow the environment's field convention.

## Verifying

Verify through Stroom, not by querying the backend: `create_verification_dashboard`, then
`run_test_searches` for the sample stream ids, an exact match on each key field, and a time range.
Shard or document counts are not a reliable signal until the index is flushed.
