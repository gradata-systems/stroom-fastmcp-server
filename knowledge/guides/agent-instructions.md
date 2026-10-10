# Standing instructions (AGENTS docs)

A Documentation doc named `AGENTS` holds standing instructions for building pipelines: the equivalent of an
AGENTS.md, kept in Stroom beside the content it is about.

## Where it applies

| Doc | Applies to |
| --- | --- |
| `System/AGENTS` | Everything |
| `System/Feeds/Events/AGENTS` | Every feed and pipeline under `System/Feeds/Events` |
| `System/Feeds/Events/Keycloak/AGENTS` | Keycloak only |

`get_instructions` returns every doc that applies to the folders, feeds or documents given, from the most general
to the most specific, so the more specific instruction reads last. The agent loads them at the start of every run
and again once it knows where the work lives. The user's own request takes precedence, and no instruction can lift
an approval or the write guard: the server enforces those whatever a doc says.

## What to write

Short, concrete rules the agent can apply and you can check:

```markdown
# Standing instructions

## Fields
- The acting account goes in EventSource/User/Id; the account acted on goes in the event detail's User.
- Host names are lower case and without the domain (ws01, not WS01.corp.example).
- Keep the vendor's event code in EventDetail/TypeId, and a short description in EventDetail/Description.

## Time
- Sources without a time zone are UTC.

## Naming
- Feeds and events pipelines: <Vendor>-<Product>-V<major>.<minor>, e.g. Fortigate-FG60F-V1.2.
- Elasticsearch indices: ecs-<source>-v<n>.

## XSLT style
- Variables and named templates in camelCase.
- Always declare value maps as xsl:map.
```

An XSLT style section is applied through the translation mapping's `style` (naming, and when to use variables and
xsl:maps; see the XSLT guide), and the same `style` of an index plan and of a CEF plan (`draft_cef_mapping style=`),
so every generated XSLT follows it without hand edits.

The name the server looks for is `AGENTS` unless `STROOM_MCP_INSTRUCTIONS_DOC_NAME` says otherwise. Anyone who can
edit a folder can edit its `AGENTS` doc, so set folder permissions accordingly.
