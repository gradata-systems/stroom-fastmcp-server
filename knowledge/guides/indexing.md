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

A discovery index is different: raw data (JSON, delimited text, XML) indexed as it is into Elasticsearch, for
exploration, with no translation. Don't survey the data first; Elasticsearch maps its fields dynamically. Give
`draft_index_mapping` `discovery` with what the user confirmed (the input: `json`, `delimited`, which needs a Data
Splitter naming the columns, or `xml` with its `record` element; the timestamp field; any stream meta; fields to
drop): only `StreamId`,
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

Some problems only appear when documents reach Elasticsearch (a value of the wrong type for the mapping, a field
name it refuses, the field limit). In the workspace, `create_processor_filter` and `reprocess_streams` start
indexing with a batch size of 10, so each rejected document comes back whole in the Error stream; triage splits the
bulk response into one entry per document. `wait_for_processing` restores the template's default once indexing
completes without errors. Reprocessing does not remove the documents a stream already indexed: the approval gives the
`_delete_by_query` request for the cluster admin to run first, or the documents are indexed twice.

## Verifying

Verify through Stroom, not by querying the backend: `verify_index` makes the dashboard once and runs the test
searches: the sample stream ids, an exact match on each key field, and a time range.

The dashboard is for the people who will search the index. Suggest its columns (the time field and the key fields:
user, host, address, event type, outcome) and confirm them with the user; `verify_index` asks before making it.
StreamId and EventId are never shown: they are hidden columns, for the text pane and tracing hits. The query is the
time field from the sample's earliest event (rounded back to a 30-day boundary) through today, run on open; the table
is newest first; a text pane shows the selected row's record, with stepping and no extraction pipeline.
Shard or document counts are not a reliable signal until the index is flushed.

## Searching an Elasticsearch index through Stroom

Dashboard conditions behave as Elasticsearch would for: EQUALS (case-sensitive on keywords, with `*` wildcards:
`ca*`, `*aro*`), NOT_EQUALS, MATCHES_REGEX, IN (values separated by commas: `alice,bob`), ranges on numbers and
dates (GREATER_THAN, LESS_THAN, BETWEEN `from,to`), booleans (`true`), EQUALS `*` for any value, array values (a document whose
`tags` hold `retry` matches `tags = retry`), and fields inside objects (`user.name`, `message_json.error.code`).
Found against Stroom 7.13 and Elasticsearch 9, and not to be relied on: STARTS_WITH and CONTAINS find nothing (use
EQUALS with wildcards), IS_NULL and IS_NOT_NULL match every document (use EQUALS `*` and compare counts), and IN
with spaces
between the values finds nothing. `verify_index` refuses those searches.

