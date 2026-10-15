# Clients

The server includes no agent. People use it from an MCP client: a chat client, an IDE's agent mode, or an agent
framework. Nothing a workflow depends on lives in the client. The server holds the rules (the write guard,
confirmations and approvals, processing limits, the Elasticsearch hand-over, the checks before promotion), the
prompts carry the workflows, and every tool's reply names the next step.

VS Code is set up and tested end to end: see [Sign-in and VS Code](vscode.md).

## What a client needs

| A client needs | Why | What the server provides |
| --- | --- | --- |
| Streamable HTTP | The transport | `<public url>/mcp` |
| OAuth sign-in as the user: authorization code with PKCE, or another flow that gives the user's own token | Every Stroom call acts as the user | Protected resource metadata (`/.well-known/oauth-protected-resource/mcp`) naming the provider. Tokens carry both audiences (the server's and `stroom`, or one both accept), `sub`, and the required scopes (`openid` by default) |
| Forms, or a way to show a summary and send back an id | The user, not the model, answers confirmations and approvals | Forms on clients that support them; otherwise a summary and an id the client passes back once the user agrees |
| Prompts (optional) | The workflows, as slash commands | Thirteen prompts; a client without prompts can send the same text |
| Resources (optional) | Guides as context | `stroom://guides`, `stroom://guide/{name}`, `stroom://conventions/{name}` |
| Patience with long calls | Processing takes time | `wait_for_processing` returns at its timeout with a hint to call again |
| Room for 57 tools | Some clients cap tools per request | Tool groups a client can switch off, such as Elasticsearch on a Lucene-only instance; each tool's `_meta.group` (with `groupSummary`) for clients that offer tools in groups |
| The server's instructions in the model's context | They hold the order of work and the rules the tools rely on | The `instructions` returned at initialize |
| Consent decided per tool (optional) | A client that asks before tool calls | Annotations on every tool (below), and `_meta.confirms` on those that ask in their own forms |
| Buttons for the user's steps (optional) | Steps only the user takes, such as enabling a processor filter | `user_actions` in a result (below); the same steps are in its text for every client |

## Tool annotations

Every tool carries MCP annotations, for a client that decides from them whether to ask the user first:

| Annotation | Meaning here |
| --- | --- |
| `readOnlyHint` | The tool changes nothing in Stroom. A tool that records its result in the build (`step_sample`, `check_events`, `survey_feed` with a build) is not read-only. |
| `destructiveHint` | It can start processing or change production: `create_processor_filter`, `reprocess_streams`, `promote_build`. A tool that writes only the build's own drafts and records is not: the write guard refuses it anything else. |
| `openWorldHint` | False for every tool: they reach this Stroom (and its Elasticsearch through it), nothing else. |
| `_meta.confirms` | The tool asks the user in its own form before its main change and before anything it starts processing, so a client needn't ask as well. Every destructive tool has it. |

## User actions

A result that hands a step to the user carries `user_actions`, for a client to show as buttons, where a click is the
user's consent (Stroom's Ask Stroom AI does). Four actions only:

| Action | Arguments | For |
| --- | --- | --- |
| `enable_processor_filter` | `filter_id`, `doc` (the pipeline) | A filter created disabled (`create_processor_filter`, `reprocess_streams`), waited on (`wait_for_processing`), or made by promotion |
| `disable_processor_filter` | `filter_id`, `doc`; `optional` | An earlier version's filter, once a promoted new version takes over |
| `open_document` | `doc` | A new feed (upload samples on its Data tab), documentation to review, a Lucene index whose shards are deleted first |
| `elasticsearch_request` | `request`, `by` | For the cluster admin to copy and run: the agreed index template, or dropping an index before it is indexed again. Only `PUT _index_template/<name>`, `DELETE _data_stream/<index>` or `DELETE <index>` |

Each has an `id`, a `link` (Stroom's own `?action=open-doc` URL for `doc`), a `summary` built from fixed wording, ids
and document names (never record values), and an optional `after`: what comes first, as other actions' ids or a
condition. The server never takes a click as done: it reads Stroom's state (`wait_for_processing`, `build_status`).
A client should build a button's text from Stroom rather than the summary, check the filter's state when clicked,
show `elasticsearch_request` only for copying, and never act on a user action without the user.

## Links

Every Stroom link the server returns is Stroom's own `?action=open-doc&docType=…&docUuid=…`, built from
`STROOM_MCP_STROOM_UI_URL` (else `STROOM_MCP_STROOM_URL`). Set the UI URL to Stroom's public address, so links work
in a browser and a client inside Stroom can open them in place.

## Clients for searching, not building

A chat client for the people who search the data, rather than build pipelines, needs only `describe_feed`. It
answers what a feed holds and what a field means, where it comes from and how to search it, from the documentation
the server generated and the indexes' own field types. It only reads, as the signed-in user. OpenWebUI is an
example. Stroom's own assistant (Ask Stroom AI) uses the full server, with the tool annotations, groups,
user actions and links above. On a Stroom with its own identity provider it forwards its user's token from that
provider ([Stroom's internal identity provider](deployment.md#stroom-s-internal-identity-provider)); no other client
can sign in to such a Stroom's server.

## Agent frameworks

Replies are made to be routed in code rather than by the model: gates come back as a `status` with an id, and
verdicts are fields (stepping's `verdict`, processing's `gate`, `build_status`'s `before_promotion`). A framework
can loop on them, with the model only writing the mapping and explaining results. Its state can live in the build:
the build folder and its docs carry what a later session needs to carry on.
