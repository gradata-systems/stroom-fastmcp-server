# Confirmations and approvals

The agent may create content freely inside its build, but key details and anything with consequences beyond the
build are decided by the user:

- A **confirmation** fixes a detail an action uses, such as a feed name, the template a pipeline builds on, or an
  index name. The agent proposes it, saying where it came from, and the user confirms or corrects it.
- An **approval** guards an action: processing a build's sample, reprocessing in a build, or promoting a build.
- **Starting other processing is yours.** Any other processor filter (a whole feed, a CEF output, reference data, a
  promoted pipeline) is created disabled, and you enable it in Stroom: the agent gives you the pipeline's link and
  what enabling it does. See [Processing and indexing](processing.md).

## How the user answers

The server asks the user directly, in a form the client shows, so the model never holds or invents the answer. In
VS Code, forms appear in the chat. A client that can't show forms gets a summary and an id instead. The id is
bound to the exact request and user, expires, and works once, so it can't be reused or answered for something else.

Every request, answer and refusal is in the [audit log](audit.md), with how it was answered (`form`,
`elicitation` or `id`).

## What is confirmed

| Detail | When | Proposed from |
| --- | --- | --- |
| Feed name and encoding | Creating the feed | Your naming convention, the sample's profile |
| Template pipeline for the translation | Creating the events pipeline | Template pipelines and how existing pipelines use them |
| How the index's fields are named: an example index template, an existing index to follow, or a convention | The start of indexing, in a form of its own | Where similar sources index, conventions, existing indexes |
| Index backend, index doc name, index name or volume group, Elastic cluster and time field | Creating the index doc, once the agent has proposed them in the chat (an index name you change goes to the build's indexing pipelines too) | The field plan, your naming convention, the clusters Stroom has |
| Another Elasticsearch index name | When you ask for one: the Elastic Index doc and the build's indexing pipelines change together, and the index template is proposed again | The doc's name now, the pipelines writing to it, and what the old name left on the cluster (its template, an index the build wrote to) for the admin to remove |
| The index template (Elasticsearch) | Once it fits what the pipeline writes | Built from an example template: the one you paste, or the one an existing index's indices take, which Stroom reads for you |
| A new version, or a change in place, with its names | Updating an events pipeline | Your versioning convention, e.g. V1.2 to V1.3 |
| Columns of the verification dashboard | Verifying an index | The time field and the key fields |
| Errors accepted as benign, with the reason | Writing documentation | Errors the agent couldn't fix |
| Kinds of event left untranslated | Surveying an existing feed | The survey |
| Surveying fewer streams a call than the default 10 | Surveying an existing feed with a smaller sample, once per feed and number: a smaller `max_streams`, or a few of its streams given by id | The number you or the agent gave |
| Records translated as `Unknown` events (what happened isn't known: no Authenticate, Create or other action) | Saving a translation that keeps any, once per rule: asked again only for a rule that is new, or whose conditions or reason changed. The detail is shown in the chat first; the form only asks you to agree to it | How many sample records the rule catches (for the rule for the rest, those no other rule matches), what they hold, the reason, and the action elements their values suggest |
| Which feed you meant | A feed you named isn't in this Stroom | The three closest names, and a fourth choice to correct the name (or the cluster) |
| Indexing Events from a pipeline the server didn't build | Creating the processor filter for the indexing pipeline, or reprocessing | The pipeline the user names |
| Destination folders, and whether to keep the build's role subfolders (Events, Indexing) there | Promotion, as a tree shown in the chat | Where sibling sources' content lives |

## What needs approval

| Action | What the approval says |
| --- | --- |
| Processing the sample streams, or a feed's streams, with an events or indexing pipeline in a build (its filter is created enabled once you approve) | Which streams. For Elasticsearch: that the agreed index template has been committed to the cluster (or, when you say one of your own is committed instead, that you go on without agreeing one, in a form first). For an index the pipeline already wrote to, what makes it start fresh, as for reprocessing |
| Reprocessing streams with an events or indexing pipeline in a build (its filter is created enabled once you approve) | Which streams, at most 10 a call. For Elasticsearch: that the agreed index template has been applied to the cluster, and the request for the cluster admin to drop the index first (`DELETE _data_stream/<index>` or `DELETE <index>`), as documents already indexed stay; the template makes it again, and all the Events streams it should hold are processed again. For Lucene: that the build's own index is emptied first (its shards deleted), or else to delete its shards first |
| Promoting a build | Every document and where it goes, with anything the checks found still missing, and the processor filters it creates (disabled, with no minimum creation time) |

The [tool reference](../reference/tools.md) marks each tool that asks.

## What no instruction can change

[Standing instructions](standing-instructions.md) can shape how pipelines are built. They can't lift a
confirmation, an approval or the write guard. Those are enforced by the server whatever a prompt or a doc says.
