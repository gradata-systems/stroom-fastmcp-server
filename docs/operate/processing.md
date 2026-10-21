# Processing and indexing

## Processor filters

**Processing the sample streams, or a feed's streams, through an events (or records) or indexing pipeline in a
build**, or **reprocessing** one after a fix: you approve it in a form, and the server creates the filter enabled so it runs
straight away. For Elasticsearch, the approval also confirms the agreed index template is committed to the cluster.
If you committed an index template of your own instead of agreeing one with the agent, say so: you confirm that in a
form of its own (the server doesn't check your template against what the pipeline writes), and it is recorded with
the pipeline in place of an agreed one, so you aren't asked again.

Every other processor filter is created **disabled**: any other pipeline in a build (a CEF output, which sends to
Kafka, or reference data), and a promoted pipeline's filter. Enabling one, and so starting
processing, is yours, on the pipeline's Processors tab in Stroom. The agent gives you the pipeline's link and what
enabling it will do (which streams, where the output goes, and anything to do first, such as having the
Elasticsearch index template committed), then waits for you to say it's enabled before it waits for the processing.
A disabled filter changes nothing, so creating one asks for no approval. The agent can disable a filter, never enable
one.

| Filter | Selects | Created | Task limit |
| --- | --- | --- | --- |
| Sample processing in a build | The sample stream ids only (`Meta Id IN (...)`) | Enabled once you approve, for an events or indexing pipeline; otherwise disabled | 1 (`STROOM_MCP_SAMPLE_MAX_TASKS`) |
| Reprocessing in a build | At most 10 streams a call (`STROOM_MCP_MAX_REPROCESS_STREAMS`) | Enabled once you approve, for an events or indexing pipeline; otherwise disabled | 1 (`STROOM_MCP_REPROCESS_MAX_TASKS`) |
| A feed in a build | The feed's streams created after a time you approve | Enabled once you approve, for an events or indexing pipeline; otherwise disabled | 2 (`STROOM_MCP_MAX_FEED_FILTER_TASKS`) |
| A promoted pipeline's feed | Everything on its feed, existing data and the streams the pipeline processed in its build included | Disabled | 2 (`STROOM_MCP_MAX_FEED_FILTER_TASKS`) |

Other limits:

- **Only the server's own pipelines.** The processing tools act only on workspace pipelines the server built.
  Production pipelines are refused.
- **Only the build's own feeds.** A translation pipeline under development processes only streams from its build's
  feeds. Its Events land in the input's feed, so processing a production stream would put test Events into a
  production feed.
- **Processed once.** A plain filter is refused for a stream the pipeline has already processed. That needs
  reprocessing, which Stroom handles by marking the earlier output deleted, so each raw stream keeps one Events
  stream.
- **The right Events.** An indexing filter also carries `Pipeline IS_DOC_REF <events pipeline>`, so it indexes only
  Events from the events pipeline built for the source, even on a shared feed. After promotion it names that pipeline
  and the stream type, with no feed condition: the pipeline says which Events. Indexing Events from a pipeline the
  server didn't build needs the user's confirmation of that pipeline.
- **By reference, not by name.** Filters name feeds and pipelines with `IS_DOC_REF`, by their UUID. Stroom lists the
  filter as depending on them, and renaming a feed or pipeline doesn't break it.

## Elasticsearch

The server has no Elasticsearch connection or credentials. Everything goes through Stroom, as the user:

1. The agent asks for an **example**: the index template a similar source uses (or an index's mapping), and the
   component templates it is composed of.
2. The server builds the new **index template** from it, following its names, types and settings, and checks it
   against the documents the indexing pipeline writes. The user sees it in the chat and agrees it, or corrects it,
   in which case it is checked again.
3. Your **cluster admin applies** the agreed template (a Dev Tools request). Without an example, the template makes a **data stream**,
   named as the index, with `@timestamp` as its time; with one, the example decides (index or data stream).
4. **Indexing starts** only once the template has been applied, since documents arriving first would create the
   index without it. Approving the sample's indexing, or a reprocessing, confirms it; for a filter you enable
   yourself, such as a promoted one, the agent says so with the filter's link.

While a pipeline is in the workspace, indexing runs with a batch size of 10, so a document Elasticsearch rejects
comes back whole in the Error stream. The template's default is restored once indexing completes without errors,
and promotion makes sure of it.

Indexing again doesn't remove documents already indexed: when the Events translation is amended and its sample
processed again, the superseded Events' documents stay beside the new ones. So when an indexing pipeline that has
already indexed something gets a new sample filter, or reprocesses, the index starts fresh: the approval says the
cluster admin drops it first, `DELETE _data_stream/<index>` when the agreed template makes a data stream, else
`DELETE <index>`. The agreed template stays on the cluster and makes the index again as documents arrive. Every
document goes, so all the Events streams the index should hold are processed again. A promoted indexing pipeline's
filter says the same in `before_enabling`, to do before you enable it.

## Lucene

Lucene indexes are made as Stroom Index docs, with the fields from the field plan. The user confirms the volume
group with the other indexing details.

Indexing again into a Lucene index doesn't remove the documents already there either, and it starts fresh the same
way. When the index is the build's own (no other pipeline of the build writes to it), the server deletes its shards
once you approve, which the approval says. Otherwise the approval tells you to delete its shards (the index's Shards
tab) first. A promoted indexing pipeline's filter says so in `before_enabling`.

## Verification

An index is verified by searching it through Stroom, as people will. All documents for the sample streams, then
exact, case, wildcard, range and full-text searches. Each search's first hit is traced back by stepping the
indexing pipeline at its stream and event, to check it is the right record.
