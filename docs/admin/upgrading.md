# Upgrading

Each release is a container image and a Helm chart of the same version. `/healthz` answers `ok` and the version
the server runs, for probes and for checking an upgrade took.

## Before you upgrade

- **Chats in progress.** A client connected to the old server process may not reconnect by itself. In VS Code, a
  tool call can then wait with nothing reaching the server, until the user restarts the MCP server in VS Code (see
  [Troubleshooting](../operate/troubleshooting.md)). Upgrade when no one is mid-build, or tell users to restart
  the server in VS Code afterwards.
- **Upload tickets.** With `UPLOAD_TICKETS=short` (the default), a ticket is a code the server process keeps, so a
  restart forgets tickets not yet used, and the user asks the agent for a new upload. `sealed` tickets carry
  everything and work on any replica, and survive a restart when `REQUEST_STATE_KEYS` is set, but they are long
  for the agent to copy.
- **Builds carry over.** A build's state is in Stroom, in its folder and `Build record`, not in the server. Work in
  progress carries on after the upgrade.

## Stateless HTTP: keep it off for VS Code

`statelessHttp` (`FASTMCP_STATELESS_HTTP`) is `false` by default, and should stay so for VS Code. Tried on a live
deployment, VS Code then refused the server's tools as "disabled" one after another, though they were ticked and
the server was never called, and confirmations and approvals came back as ids instead of forms, which the model
passed back without asking the user. Switching it off put both right.

With more than one replica, keep sessions and pin each client to one replica (`service.sessionAffinity:
ClientIP`), and set `requestState.existingSecret` so every replica can read the forms another sent (see
[Deployment](deployment.md)).

## The event-logging version is chosen, not configured

`EVENT_LOGGING_VERSION` (`build.eventLoggingVersion`) is gone. It defaulted to 3.5.2 whether or not Stroom held that
schema. A new translation now writes the version the user chooses from the event-logging schemas the Stroom
instance holds, the newest recommended; an existing translation keeps the version it was saved for. Remove the
setting from your values; a leftover one is ignored.

## Earlier versions

Versions before 0.17.13 tagged documents with per-build and per-check tags (`mcp-build-…`, `mcp-copy-of-…`,
`mcp-stepped-…`, `mcp-verified-…`, `mcp-validated-…`, `mcp-sample-files`). A build's tags are folded into its
`Build record` the first time it is read, and then removed. Tags on documents that are no longer in any build can
be deleted from Stroom.
