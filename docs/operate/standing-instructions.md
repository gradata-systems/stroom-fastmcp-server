# Standing instructions

A Documentation doc named `AGENTS` in a Stroom folder holds your rules for building pipelines there and below, like
an `AGENTS.md` in a code repository. Use them to set your environment's conventions once, rather than having each
user repeat them:

- field placement (which account goes in `EventSource/User`, how host names are written);
- time zones for sources that don't give one;
- naming for feeds, pipelines and indexes;
- XSLT style, applied by the generator, so generated code follows it without hand edits.

## Required imports

Many environments keep common steps in shared XSLTs that every pipeline XSLT imports. An `AGENTS` doc can require
them, per stage, and the server applies it rather than only handing it to the agent:

```markdown
## Required imports
- translation: Stream-Meta-V1
- indexing: Common-Index-V1, Lookups-V2
```

Stages are `translation` (Raw Events to Events), `records` (pipelines writing Records), `indexing`, `reference` and
`cef`. XSLTs the server generates get
the imports, and the agent is told the named templates each offers, to call where they write; an XSLT written by
hand without them is refused when it is saved. Like any standing instruction, the section applies in its doc's
folder and below.

## Component templates

An `AGENTS` doc can also say which Elasticsearch component templates new index templates compose:

```markdown
## Component templates
- include: logs-ilm-90d, org-base
- exclude: legacy-base
```

Included components are referenced in every index template the server proposes there, whatever fields they map,
and the fields they map are left to them. Excluded ones are left out, even when the user's example composes them,
unless the user asks for one themselves. A template the user brings is checked against the section too.

## Where they apply

| Doc | Applies to |
| --- | --- |
| `System/AGENTS` | Everything |
| `System/Feeds/Events/AGENTS` | Every feed and pipeline under `System/Feeds/Events` |
| `System/Feeds/Events/Keycloak/AGENTS` | Keycloak only |

Where several apply, they are read from the most general to the most specific. The server returns them from the
tools that start and shape a build, so they apply even if a model skips the step that loads them.

## Limits

- The user's own request takes precedence over a standing instruction.
- No instruction can lift a confirmation, an approval or the write guard.
- **Anyone who can edit a folder can edit its `AGENTS` doc**, so set folder permissions accordingly.
- The doc name is `AGENTS` unless `STROOM_MCP_INSTRUCTIONS_DOC_NAME` says otherwise.

What to write, with an example: the [standing instructions guide](../reference/guides/agent-instructions.md).
