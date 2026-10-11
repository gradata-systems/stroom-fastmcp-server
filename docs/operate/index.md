# For Stroom operators

These pages are for people who run Stroom and need to know what the server does in it: what it creates and where,
what it never touches, what users are asked before anything runs, and how to review and take over what an agent
built.

In short:

- The server acts **as the signed-in user**, with their token. It can do no more in Stroom than they can, and
  Stroom's own history shows the change under their name.
- It **builds in a workspace folder** (`MCP Workspace/<build>`) and can only change documents it made there. A
  production document is changed through a working copy, with a backup kept.
- Anything that **processes data, or moves content out of the workspace, needs the user's approval** in a form.
  Development processing is limited to the sample streams, one task at a time.
- It **deletes nothing**. It has no delete tools, and abandoned builds stay in the workspace for you to clear.
- It has **no Elasticsearch access** of its own. Indexing goes through Stroom's indexing pipelines, and index
  templates are applied by your cluster admin.
- **Every action is audited**, with the user behind it and what they agreed to.

| Page | What it covers |
| --- | --- |
| [What it changes in Stroom](what-it-changes.md) | Folders, documents, tags, the build record, naming, and what is never touched |
| [Confirmations and approvals](confirmations.md) | What users are asked, when, and how the answer reaches the server |
| [Builds, review and promotion](builds.md) | Reviewing a build, the checks before promotion, what promotion does, and after |
| [Processing and indexing](processing.md) | Processor filters, limits, reprocessing, and the Elasticsearch hand-over |
| [Workflows](workflows.md) | What users can ask for, and which workflow does it |
| [Standing instructions](standing-instructions.md) | `AGENTS` docs: your rules for how pipelines are built |
| [Errors and triage](errors.md) | How errors are classed, fixed or accepted, and how to mark known-harmless ones |
| [Audit log](audit.md) | The JSON audit events and how to query them |
| [Troubleshooting](troubleshooting.md) | Common problems and what to do |
