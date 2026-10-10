# Indexing: Lucene and Elasticsearch

Stage 2 is the same on both backends: field plan, index doc, indexing pipeline from a template,
step the Events, process, then verify with a dashboard and test searches. Which backend a build uses
comes from the chosen indexing template (`find_pipeline_templates stage=indexing` reports it).

| | Lucene (Stroom `Index`) | Elasticsearch |
| --- | --- | --- |
| Template | `Indexing`: `IndexingFilter` with property `index` | e.g. `Events to Elasticsearch`: `ElasticIndexingFilter` with `cluster` and `indexName` |
| XSLT output | `records:2` | `xpath-functions` JSON XML |
| Fields | On the index doc (`create_index_doc` with the plan) | An Elasticsearch index template, built by `propose_index_template` from the user's example (an index template or an index's mapping, with its component templates) and applied to the cluster by its admin |

Following ECS (the `ecs` profile, no example): the server has the full schema. The plan's names are checked against
it (`ecs_check`: a name in an ECS field set that ECS doesn't define, or a known field of another type), the template
composes Elastic's `ecs@mappings`, and `get_field_conventions name=ecs ecs_fields=<set>` lists a field set's fields to
name one ECS's way. A field of the user's own goes outside ECS's field sets.

The plan is kept with the indexing XSLT, and later changes are made to the plan and saved again (`save_xslt
index_plan=... uuid=`). An edit the user made by hand in Stroom since (another field written, one taken out, a source
changed) is kept: a save that would undo it is refused, saying what it changed. Carry it into the plan (a field it
added as a field of the plan, with its name, type and source XPath) and save again; `discard_hand_edit=true` only when
the user says to drop it. `build_status` reports such an edit. Where your change is to a field they edited too, the
user is asked first whether to overwrite the XSLT with your change (every hand edit dropped) or decide field by
field, then, field by field, whether to keep their edit or use the proposed field: in forms, or as `needs_guidance`
with the questions to ask them exactly, answered by calling again with `hand_edit_choices={'*': 'overwrite'}` or
`{field: 'keep' or 'overwrite'}`. Never answer for them. If they keep it, leave your change to that field out and carry their edit.

For Elasticsearch, ask the user for an example first: the index template a sibling source's index uses (or an
index's mapping) and the component templates it is composed of. Give it to `draft_index_mapping` as well as to
`propose_index_template`: the plan's field names then follow it (the example's `User.Id` for the user and `TypeId`
for the event type, not the convention profile's `user.name`), fields it has no name for are named in its style
(PascalCase, camelCase or ECS-style, dotted or run together as it is), and sample paths it maps are added. The
template then follows its field types (`ignore_above`, sub-fields, date formats), its objects (`"type": "object"`,
object-level `dynamic`) and its settings. The example's own fields this source doesn't write are listed, not copied.
Before they confirm it, the user sees it whole: the template first comes back as `needs_review`, its Dev Tools
request to show in the chat as a code block; called again with `reviewed=true`, the form is a short summary (name,
pattern and priority, settings, fields and types). They confirm it, or correct it (`check_index_template`); once
they say the cluster admin has committed it, `create_processor_filter` starts indexing. An alias in the example
becomes a field when the pipeline writes that field (documents can't write to an alias), typed as its target.

The choice is the user's: `get_field_conventions backend=elasticsearch` asks them in a form, with the choices as a
picker (From an index template, Follow an existing index in Stroom, then a convention per profile), and its reply
(`status: chosen`) gives the next call; call it without asking first. Where the client has no forms, it returns the
choices (`needs_guidance`): offer exactly those, in order, recommending none. An example is pasted into the chat, as a choice form can't carry it, so ask for it there
and wait for it before drafting. Following an existing index in Stroom (`like_index`) reads its field names and
Elasticsearch types through Stroom, with nothing to paste, and the user confirms it in a form that says what was
read; its template's settings and component templates aren't readable that way, so they paste the template for
those. Going without an example (`without_example`) is confirmed in a form too. Without an example, `propose_index_template` builds nothing for the cluster. A template put on
the cluster without being agreed through it doesn't count, so indexing stays refused. Stepping clean is not
indexing: the build's `indexed` step is done only once `verify_index` passes.

A discovery index is different: raw data (JSON, delimited text, XML) indexed as it is into Elasticsearch, for
exploration, with no translation. Don't survey the data first; Elasticsearch maps its fields dynamically. Give
`draft_index_mapping` `discovery` with what the user confirmed (the input: `json`, `delimited`, which needs a Data
Splitter naming the columns, or `xml` with its `record` element; the timestamp field; any stream meta; fields to
drop): only `StreamId`,
`EventId` and `@timestamp` are mapped explicitly, a JSON object held in a string is also indexed parsed as
`<field>_json`, and the template is permissive (strings as keywords, a field limit, malformed values ignored).

A network event records its connection under its action (`Network/Permit`, `Network/Deny`, ...): index the
address, port and protocol once whichever action it is, with `*` for the action in the source
(`EventDetail/Network/*/Source/Device/IPAddress`), never a field per action (`SourceIp.Permit`). The plan does this,
and lists the paths it leaves out the same way. What happened (the action element's Type, Severity, Action,
Outcome) is planned too; with an example that nests names, fields sharing an element nest (`Alert.Type`,
`Source.Port`). The example the user pasted is kept in the build when the plan is drafted, so
`propose_index_template` follows it even when the conversation has lost it: never write a template of your own as
their example.

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

## Searching a Lucene index through Stroom

EQUALS with `*` wildcards works on every field (`ca*`, `*aro*`, `10.1.*` for an address range), as do IN, ranges on
numbers and dates, and CONTAINS on an analysed text field (`Description`) for a whole word. Found against Stroom 7.13,
without an error: STARTS_WITH and ENDS_WITH find nothing, CONTAINS finds nothing on a keyword field (user, host, address)
and only whole words on a text field, a range (BETWEEN, GREATER_THAN, ...) finds nothing on a text field, addresses
included, and IN finds nothing when a value holds a space (search it with EQUALS). `verify_index` refuses those searches, naming the wildcard to use.

