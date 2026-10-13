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
| Prompts (optional) | The workflows, as slash commands | Twelve prompts; a client without prompts can send the same text |
| Resources (optional) | Guides as context | `stroom://guides`, `stroom://guide/{name}`, `stroom://conventions/{name}` |
| Patience with long calls | Processing takes time | `wait_for_processing` returns at its timeout with a hint to call again |
| Room for 57 tools | Some clients cap tools per request | Tool groups a client can switch off, such as Elasticsearch on a Lucene-only instance |

## Clients for searching, not building

A chat client for the people who search the data, rather than build pipelines, needs only `describe_feed`. It
answers what a feed holds and what a field means, where it comes from and how to search it, from the documentation
the server generated and the indexes' own field types. It only reads, as the signed-in user. OpenWebUI is an
example, as is Stroom's own assistant in later Stroom builds.

## Agent frameworks

Replies are made to be routed in code rather than by the model: gates come back as a `status` with an id, and
verdicts are fields (stepping's `verdict`, processing's `gate`, `build_status`'s `before_promotion`). A framework
can loop on them, with the model only writing the mapping and explaining results. Its state can live in the build:
the build folder and its docs carry what a later session needs to carry on.
