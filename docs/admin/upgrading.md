# Upgrading

Each release is a container image and a Helm chart of the same version. `/healthz` answers `ok` and the version
the server runs, for probes and for checking an upgrade took.

## Before you upgrade

- **Chats in progress.** A client connected to the old server process may not reconnect by itself. In VS Code, a
  tool call can then wait with nothing reaching the server, until the user restarts the MCP server in VS Code (see
  [Troubleshooting](../operate/troubleshooting.md)). To avoid this, run the server stateless (below), or upgrade
  when no one is mid-build.
- **Upload tickets.** With `UPLOAD_TICKETS=short` (the default), a ticket is a code the server process keeps, so a
  restart forgets tickets not yet used, and the user asks the agent for a new upload. `sealed` tickets carry
  everything and work on any replica, and survive a restart when `REQUEST_STATE_KEYS` is set, but they are long
  for the agent to copy.
- **Builds carry over.** A build's state is in Stroom, in its folder and `Build record`, not in the server. Work in
  progress carries on after the upgrade.

## Stateless HTTP

By default, an MCP session lives in the server process that started it. With `statelessHttp: true` in the chart
(`FASTMCP_STATELESS_HTTP=true`), each request stands on its own. A restart or redeploy then strands no client, and
several replicas need no session affinity. Confirmation forms work the same either way: the form's state is sealed
and travels with it.

Without a session, the server doesn't get the client's name from the connection handshake, so the "made by" line
the server writes into build records and XSLT version histories may lack the client's name and version. The
server's version and the model the agent names still appear.

With more than one replica, also set `requestState.existingSecret` so every replica can read the forms another
sent (see [Deployment](deployment.md)).

## Earlier versions

Versions before 0.17.13 tagged documents with per-build and per-check tags (`mcp-build-…`, `mcp-copy-of-…`,
`mcp-stepped-…`, `mcp-verified-…`, `mcp-validated-…`, `mcp-sample-files`). A build's tags are folded into its
`Build record` the first time it is read, and then removed. Tags on documents that are no longer in any build can
be deleted from Stroom.
