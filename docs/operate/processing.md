# Processing and indexing

## Processor filters

Every processor filter the server creates or enables needs the user's approval, and is limited:

| Filter | Selects | Task limit |
| --- | --- | --- |
| Sample processing in a build | The sample stream ids only (`Meta Id IN (...)`) | 1 (`STROOM_MCP_SAMPLE_MAX_TASKS`) |
| Reprocessing in a build | At most 10 streams a call (`STROOM_MCP_MAX_REPROCESS_STREAMS`) | 1 (`STROOM_MCP_REPROCESS_MAX_TASKS`) |
| A promoted pipeline's feed | New data on its feed, from promotion, **created disabled** | 2 (`STROOM_MCP_MAX_FEED_FILTER_TASKS`) |

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
  Events from the events pipeline built for the source, even on a shared feed. Indexing Events from a pipeline the
  server didn't build needs the user's confirmation of that pipeline.

## Elasticsearch

The server has no Elasticsearch connection or credentials. Everything goes through Stroom, as the user:

1. The agent asks for an **example**: the index template a similar source uses (or an index's mapping), and the
   component templates it is composed of.
2. The server builds the new **index template** from it, following its names, types and settings, and checks it
   against the documents the indexing pipeline writes. The user sees it in the chat and agrees it, or corrects it,
   in which case it is checked again.
3. Your **cluster admin applies** the agreed template (a Dev Tools request).
4. The approval that starts indexing asks the user to confirm the template has been applied, since documents
   arriving first would create the index without it.

While a pipeline is in the workspace, indexing runs with a batch size of 10, so a document Elasticsearch rejects
comes back whole in the Error stream. The template's default is restored once indexing completes without errors,
and promotion makes sure of it.

Reprocessing into Elasticsearch doesn't remove documents already indexed, so the approval gives the
`_delete_by_query` request for the cluster admin to run first.

## Lucene

Lucene indexes are made as Stroom Index docs, with the fields from the field plan. The user confirms the volume
group with the other indexing details.

## Verification

An index is verified by searching it through Stroom, as people will. All documents for the sample streams, then
exact, case, wildcard, range and full-text searches. Each search's first hit is traced back by stepping the
indexing pipeline at its stream and event, to check it is the right record.
