# Getting started

## What you need

- **Stroom 7.13** that trusts an OpenID Connect provider (Keycloak, Entra ID, Okta, Auth0 and others).
- **The server**, deployed with its Helm chart or as a container: see [Deployment](admin/deployment.md).
- **A client set up to sign in**: an MCP client that supports OAuth, such as VS Code with Copilot Chat. The provider
  needs a client for it whose tokens carry both the server's audience and Stroom's: see
  [Sign-in and VS Code](admin/vscode.md).
- **Folder permissions in Stroom**: users need rights on the workspace folder (`MCP Workspace` by default), and, to
  promote, on the folders the content goes to.

## Connect

Add the server to VS Code's `mcp.json`:

```json
{
  "servers": {
    "stroom": {
      "type": "http",
      "url": "https://stroom-mcp.example.com/mcp",
      "oauth": {"clientId": "stroom-mcp-vscode"}
    }
  }
}
```

On first use, VS Code opens a browser to sign in. The server's workflows then appear in the chat as slash commands,
for example `/mcp.stroom.onboard_data_source`.

## A first onboarding

Ask the agent to onboard a source, naming the sample file, for example *"Onboard the VPN logs in
samples/vpn.log to Stroom"*. A typical run:

1. **Profile and plan.** The server reads the sample's format, fields and timestamps. It creates a build, a folder
   under `MCP Workspace`, and gives the agent the plan.
2. **Feed and upload.** The agent proposes a feed name and you confirm or correct it in a form. The sample is
   uploaded to the feed. A large file goes from your disk to Stroom directly, through a short `curl` command you
   approve, and never passes through the model.
3. **Translation.** The agent drafts a field mapping, and the server generates the XSLT (and a Data Splitter for
   text). Every sample record is stepped in Stroom until the output is valid event-logging XML. You confirm the
   template pipeline it builds on.
4. **Processing.** You approve a processor filter that processes only the sample streams, one task at a time.
5. **Indexing.** You confirm the backend, the field naming and the index name. For Elasticsearch, the index
   template is built from an example you give it, and your cluster admin applies it. The server checks the index
   by searching it through Stroom, as people will.
6. **Documentation.** The server writes Documentation docs for the pipelines, generated from the mapping and what
   processing produced.
7. **Promotion.** `build_status` lists anything still missing. You approve promotion, and the content moves into
   place. Each promoted pipeline gets a processor filter for new data, created disabled for you to review and
   enable.

Every confirmation and approval is a form you answer. The model only sees the outcome. To find out what each step
writes to Stroom, see [What it changes in Stroom](operate/what-it-changes.md).

## Other things to ask

The server has a workflow for each of these; see [Workflows](operate/workflows.md):

- build a pipeline for a feed that already holds data;
- fix an event that came out wrong, or add fields to an existing pipeline;
- index an existing Events feed, or make the next version of an index;
- index raw JSON, CSV or XML as it is, to explore it;
- evaluate and document a pipeline or an index someone else built;
- find kinds of event the sample missed;
- send Events to ArcSight as CEF.
