# Stroom MCP server

An MCP server that lets an agent take a raw data sample, or a feed that already holds data, and build working
Stroom content for it: a feed, an event-logging translation pipeline, and an indexing pipeline (Lucene or
Elasticsearch), stepped and verified before anything is promoted. It also fixes, updates, evaluates and documents
existing pipelines and indexes.

It includes no agent or model of its own: people use it from an MCP client that can sign them in, such as VS Code.
The rules are enforced by the server, so they hold whatever the client or model:

- **As the user.** Every Stroom call is made with the signed-in user's token. The server holds no Stroom credentials.
- **In a workspace.** Content is built in `MCP Workspace/<build>` and moved into place only when the user approves.
- **Asked, not assumed.** Names, templates and anything that processes data are confirmed or approved in a form.
- **Checked before it runs.** Every sample record is stepped, and promotion shows what a build still lacks.
- **Audited.** Every tool call, Stroom request, confirmation, approval and refusal is logged with the user behind it.

Status: 0.18.30, released as a container image and a Helm chart.

## Documentation

**https://gradata-systems.github.io/stroom-fastmcp-server/**

- [Getting started](docs/getting-started.md)
- [For Stroom operators](docs/operate/index.md): what it changes in Stroom, confirmations and approvals, review and
  promotion, processing, troubleshooting
- [Deployment](docs/admin/deployment.md) and [Sign-in and VS Code](docs/admin/vscode.md)
- [Tools, prompts and resources](https://gradata-systems.github.io/stroom-fastmcp-server/reference/tools/) and the
  [agent guides](knowledge/guides)
- [Development](docs/development/index.md): running locally, tests, releasing; and the [design](docs/development/design.md)

## Quick start (development)

```
cp .env.example .env   # fill in
uv run python main.py
uv run pytest
```

`dev/stroom` runs Stroom and MySQL in Docker for the end-to-end suites: see [Development](docs/development/index.md).
