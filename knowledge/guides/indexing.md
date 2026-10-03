# Indexing: Lucene and Elasticsearch

Stage 2 is the same on both backends: field plan, index doc, indexing pipeline from a template,
step the Events, process, then verify with a dashboard and test searches. Which backend a build uses
comes from the chosen indexing template (`find_pipeline_templates stage=indexing` reports it).

| | Lucene (Stroom `Index`) | Elasticsearch |
| --- | --- | --- |
| Template | `Indexing`: `IndexingFilter` with property `index` | e.g. `Events to Elasticsearch`: `ElasticIndexingFilter` with `cluster` and `indexName` |
| XSLT output | `records:2` | `xpath-functions` JSON XML |
| Fields | On the index doc (`create_index_doc` with the plan) | An Elasticsearch index template, built by `propose_index_template` from the user's example (an index template or an index's mapping, with its component templates) and applied to the cluster by its admin |

For Elasticsearch, ask the user for an example first: the index template a sibling source's index uses (or an
index's mapping) and the component templates it is composed of. Give it to `draft_index_mapping` as well as to
`propose_index_template`: the plan's field names then follow it (the example's `User.Id` for the user and `TypeId`
for the event type, not the convention profile's `user.name`), fields it has no name for are named in its style
(PascalCase, camelCase or ECS-style, dotted or run together as it is), and sample paths it maps are added. The
template then follows its field types (`ignore_above`, sub-fields, date formats), its objects (`"type": "object"`,
object-level `dynamic`) and its settings. The example's own fields this source doesn't write are listed, not copied.
The user confirms the template, or corrects it (`check_index_template`); once they say the cluster admin has
committed it, `create_processor_filter` starts indexing.

A discovery index is different: raw JSON indexed as it is into Elasticsearch, for exploration, with no
translation. Don't survey the data first; Elasticsearch maps its fields dynamically. Give `draft_index_mapping`
`discovery` with what the user confirmed (the timestamp field, any stream meta, fields to drop): only `StreamId`,
`EventId` and `@timestamp` are mapped explicitly, a JSON object held in a string is also indexed parsed as
`<field>_json`, and the template is permissive (strings as keywords, a field limit, malformed values ignored).

Events the index must not hold (heartbeats, a monitoring account) are left out at this step: give
`draft_index_mapping` `drop_when` XPath tests on an Event, e.g. `"EventDetail/TypeId = 'Heartbeat'"`; the drafted
XSLT applies templates only to the events none of them match.

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

Verify through Stroom, not by querying the backend: `verify_index` makes the dashboard once and runs the test
searches: the sample stream ids, an exact match on each key field, and a time range.
Shard or document counts are not a reliable signal until the index is flushed.
