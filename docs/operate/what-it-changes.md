# What it changes in Stroom

## Who it acts as

Every call to Stroom, sample uploads included, is made with the signed-in user's access token. The server holds no
Stroom credentials and no shared API key. So:

- Stroom's document and folder permissions apply as they do in the UI. A user who can't create a feed in a folder
  can't do it through the server either.
- Stroom's history and the documents' "created by" show the user, not a service account.
- The server's [audit log](audit.md) adds what Stroom can't see: which tool made the change, and what the user
  agreed to first.

## Where it builds

Everything the server makes is built in the **workspace folder**, `MCP Workspace` by default
(`STROOM_MCP_WORKSPACE_FOLDER`). Each piece of work is a **build**, a folder of its own:

```
System
└── MCP Workspace
    ├── onboard-acme-vpn               ← a build
    │   ├── ACME-VPN-V1.0              (Feed)
    │   ├── Build record               (Documentation: the server's record of the build)
    │   ├── Events                     (Records, for a pipeline writing Records)
    │   │   └── ACME-VPN-V1.0-Events   (Pipeline, XSLT, Text Converter, Documentation)
    │   ├── Indexing
    │   │   ├── ACME-VPN-V1.0 - Indexing  (Pipeline, XSLT, Index, Documentation)
    │   │   └── ACME-VPN-V1.0-VERIFY   (Dashboard)
    │   ├── CEF                        (CEF output pipelines, when there are any)
    │   └── Reference                  (reference-data pipelines, when there are any)
    └── backups                        ← production docs as they were before a working copy replaced them
```

Content moves out of the workspace only when the user approves [promotion](builds.md).

!!! warning "Leave build folders as they are"
    Don't move or rename anything in a build's folder, or the folder itself, until the build is promoted. A
    document's build is the top folder it is in under the workspace (its role subfolders, Events or Records, Indexing,
    CEF and Reference, are part of it), and the folder's `Build record` doc refers to the documents in it. A document
    moved out of the build drops out of it, and promotion leaves it behind. A renamed folder is a different build.

    Editing a document's content in Stroom is fine: the server notices hand edits and keeps them.

## What it can change

- **Its own documents in the workspace**: documents it created there and hasn't promoted. These carry the
  `mcp-managed` tag.
- **Production documents, through a working copy.** A change to an existing pipeline or XSLT is made on a copy in
  the build. On promotion, the production document is backed up to `MCP Workspace/backups`, the copy's content is
  written into it, and the copy is removed. The document keeps its UUID, so processor filters, pipeline references
  and dashboards keep working.
- **Processing**, only with the user's approval (see [Processing and indexing](processing.md)).

Anything else is refused by the server's write guard, whatever the model asks for.

## What it never does

- **Delete.** There are no delete tools. Abandoned builds stay in the workspace for a person to clear. Stroom itself
  marks a pipeline's earlier output for a stream deleted when that stream is reprocessed.
- **Create or change Elastic Cluster docs.** They hold credentials. The agent uses a cluster that existing indexes
  use, and asks when none fits.
- **Write to Elasticsearch directly.** It has no Elasticsearch connection. Documents reach Elasticsearch only
  through a Stroom indexing pipeline, and index templates are applied by your cluster admin.
- **Process production data with a pipeline under development.** A translation pipeline in a build only processes
  streams from the build's own feeds. Records in a production feed are stepped where they are, which writes nothing.
  A build that works from a feed already in Stroom creates no feed and uploads nothing unless you bring new samples;
  any test feed it makes stays in the workspace at promotion.
- **Search for you without asking.** Searches go through Stroom as you, on a dashboard that is never saved
  (`find_events`, an index's survey), and only to find what you describe.
- **Reprocess with production pipelines.** Promoted pipelines are refused by the write guard. Reprocessing
  production data is yours to do.
- **Manage users, permissions, nodes or volumes.**
- **Save XSLT that reaches outside the record.** XSLT runs on Stroom's nodes with Stroom's access, so code that
  reads the environment, files or URLs (`doc()`, `document()`, `unparsed-text()`, `environment-variable()` and
  the like), writes or loads documents by URI (`xsl:result-document`, `xsl:source-document`), evaluates an XPath
  made at run time (`xsl:evaluate`, `function-lookup()`), or imports by URL is refused, whether the agent wrote it
  or a mapping generated it. Functions of a shared XSLT the code imports (`geo:lookup-ip()`) are its own, whatever
  their names. A working copy keeps what its production XSLT already did: only new calls are refused.

## Tags

The server puts only these tags on documents, so they stay useful for filtering and don't grow Stroom's tag list:

| Tag | On | Meaning |
| --- | --- | --- |
| `mcp-generated` | Everything the server created, including promotion backups | Made by the server. Kept for good, so you can always find its content |
| `mcp-managed` | Documents still in a build | The server may still change it. Removed at promotion; later changes go through a working copy |
| `mcp-kept-mapping` | XSLTs it generated | The field mapping the XSLT was generated from is kept with it, in its Documentation tab |

A production document that a working copy was written back over is not tagged: the server didn't create it.

## The build record

Each build folder has a `Build record` Documentation doc. It holds what the server knows about the build:

- which documents are working copies, and of what;
- which code stepped clean, passed verification, or gave valid Events (by a hash of the code);
- which feeds hold samples from files;
- which feeds already in Stroom the build works from.

It replaces the per-build tags earlier versions used. Leave it in place: the checks before promotion read it.

## The documentation it writes

Each pipeline gets a Documentation doc named after it, and each XSLT has a version history table in its
Documentation tab. The documentation is generated from the mapping and from what processing produced: the field
mapping with sampled values, the kinds of event, and the errors processing found, with any the user accepted as
benign. It can't drift from the code without the server noticing. In the XSLT's Documentation tab, below a note
asking people to leave it, the server keeps the mapping the XSLT was generated from. That lets it regenerate the
XSLT, or explain it later.

## Names

Names follow your environment's conventions, learned from content already in Stroom and confirmed by the user. For
example, if feeds are named `Fortigate-FG60F-V1.2` and indexing pipelines `<Source> - Indexing`, new ones follow
suit. You can make your conventions explicit in a [standing instructions](standing-instructions.md) doc.
