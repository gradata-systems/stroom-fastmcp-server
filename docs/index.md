# Stroom MCP server

An [MCP](https://modelcontextprotocol.io) server that lets an agent build working Stroom content from a sample of
raw data, or from a feed that already holds data:

- a **feed**, with the sample uploaded to it;
- an **events pipeline** that translates the data into [event-logging](https://github.com/gchq/event-logging-schema)
  XML, with its text converter and XSLT;
- an **indexing pipeline** into a Lucene index or Elasticsearch, with a dashboard to check it;
- **documentation** of each pipeline, generated from what it does.

Everything is stepped record by record and checked before it is processed, built in a workspace folder, and moved
into place only when a person approves it. The server also fixes, updates, evaluates and documents existing
pipelines and indexes, and answers questions about what a feed holds.

The server includes no agent or model. People use it from an MCP client they already have, such as VS Code's chat,
and the client's model drives the work. The rules are held by the server, not by prompts, so they hold for every
client and model:

- **As the user.** Every call to Stroom is made with the signed-in user's own token, so Stroom's permissions apply
  and Stroom records the change under their name. The server holds no Stroom credentials.
- **In a workspace.** New content is made in its own folder under `MCP Workspace`. Nothing outside it changes until
  the user approves promotion.
- **Asked, not assumed.** Names, templates, destinations and anything that processes data are confirmed or approved
  by the user in a form. The model never answers on their behalf.
- **Checked before it runs.** Mappings are checked against the schema and the sample, every sample record is
  stepped, and promotion shows what a build still lacks.
- **Audited.** Every tool call, Stroom request, confirmation, approval and refusal is logged with the user behind it.

## Where to start

| You want to | Read |
| --- | --- |
| Try it, or see what using it looks like | [Getting started](getting-started.md) |
| Know what it will do to your Stroom, and how to review it | [For Stroom operators](operate/index.md) |
| Install and run it | [Deployment](admin/deployment.md), then [Sign-in and VS Code](admin/vscode.md) |
| Look up a tool, prompt or guide | [Reference](reference/tools.md) |
| Work on the server itself | [Development](development/index.md) |
