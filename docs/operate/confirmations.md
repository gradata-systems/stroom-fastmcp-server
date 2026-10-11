# Confirmations and approvals

The agent may create content freely inside its build, but key details and anything with consequences beyond the
build are decided by the user:

- A **confirmation** fixes a detail an action uses, such as a feed name, the template a pipeline builds on, or an
  index name. The agent proposes it, saying where it came from, and the user confirms or corrects it.
- An **approval** guards an action: starting processing, reprocessing, or promoting a build.

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
| Index backend, field naming, indexing template, Elastic cluster, index name and version | One form at the start of indexing, since they depend on each other | Where similar sources index, conventions, existing indexes |
| The index template (Elasticsearch) | Once it fits what the pipeline writes | Built from an example template the user gives |
| A new version, or a change in place, with its names | Updating an events pipeline | Your versioning convention, e.g. V1.2 to V1.3 |
| Columns of the verification dashboard | Verifying an index | The time field and the key fields |
| Errors accepted as benign, with the reason | Writing documentation | Errors the agent couldn't fix |
| Kinds of event left untranslated | Surveying an existing feed | The survey |
| Indexing Events from a pipeline the server didn't build | Creating the processor filter for the indexing pipeline | The pipeline the user names |
| Destination folders | Promotion | Where sibling sources' content lives |

## What needs approval

| Action | What the approval says |
| --- | --- |
| Creating or enabling a processor filter | Which streams or feed, the task limit, the time bound. For Elasticsearch: that the agreed index template has been applied to the cluster |
| Reprocessing streams | Which streams, at most 10 a call. For Elasticsearch: the `_delete_by_query` request to run first, as documents already indexed stay |
| Promoting a build | Every document and where it goes, with anything the checks found still missing |

The [tool reference](../reference/tools.md) marks each tool that asks.

## What no instruction can change

[Standing instructions](standing-instructions.md) can shape how pipelines are built. They can't lift a
confirmation, an approval or the write guard. Those are enforced by the server whatever a prompt or a doc says.
