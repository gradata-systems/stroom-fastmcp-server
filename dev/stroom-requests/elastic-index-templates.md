# Stroom change: read an Elastic Index doc's index template and component templates

For the Stroom MCP server (stroom-fastmcp-server). Asked for by the user on 20 October 2026: getting the index template
and component templates to use as an example is cumbersome (Kibana Dev Tools, one `GET` per template, pasted into the
chat, and agents corrupted the bodies when they re-sent them). The user should be able to point at an Elastic Index
doc instead, and have Stroom fetch them. Pasting stays, for an example no Elastic Index doc covers.

The MCP server never connects to Elasticsearch itself: Stroom already holds each cluster's connection and
credentials, and applies the user's permissions on the doc. So Stroom reads the templates and returns them.

## Endpoint

`POST /api/elasticIndex/v1/templates`, beside `testIndex`.

Request: the Elastic Index doc's reference, as other resources take one.

```json
{"indexDocRef": {"type": "ElasticIndex", "uuid": "8d1c...", "name": "ecs-keycloak-v1"}}
```

Permission: the caller needs READ on the doc (as for `fetchElasticIndex`). Stroom calls Elasticsearch with the doc's
cluster connection, as a search does.

## What Stroom does

1. The doc's `indexName` (it may be a pattern or alias, e.g. `ecs-keycloak-v1` or `ecs-keycloak-*`).
2. The index names it stands for: `GET _resolve/index/<indexName>` (indices, data streams, aliases). Pick the newest
   concrete index or data stream (by creation date, or name order if that's unavailable). Use `indexName` itself if
   nothing resolves: the template is still found by pattern.
3. The index template that applies, by Elasticsearch's own rule: of the templates whose `index_patterns` match that
   name, the one with the highest `priority`. All templates come from `GET _index_template`. Report the other
   matching ones too, as `overlapping` (lower priority, not applied), like `_simulate_index` does.
4. Each component template in the applying template's `composed_of`: `GET _component_template/<name>`, one call per
   name (the API takes a single name or a wildcard, not a list). A name with no such template (Elasticsearch's
   built-ins like `ecs@mappings` exist on 8.13+; others may have been deleted) is reported as missing, not an error.

Every call can fail on its own, most often for want of Elasticsearch privileges. Reading templates needs cluster
privileges the cluster's credentials may not have (`manage_index_templates`, say), and `_resolve/index` needs
`view_index_metadata` on the index. Each part reports its own outcome, and the rest is still returned. Never fail
the whole request because one part was refused.

## Response

Bodies are exactly as Elasticsearch's `GET` returns them (the MCP server parses those shapes already).

```json
{
  "indexName": "ecs-keycloak-v1",
  "resolvedName": "ecs-keycloak-v1-000003",
  "indexTemplate": {
    "status": "OK",
    "name": "ecs-keycloak-v1",
    "body": {"index_patterns": ["ecs-keycloak-v1*"], "priority": 300, "composed_of": ["ecs@mappings", "org-ilm"],
             "template": {"settings": {}, "mappings": {}}},
    "overlapping": [{"name": "ecs-default", "index_patterns": ["ecs-*"], "priority": 100}]
  },
  "componentTemplates": [
    {"name": "ecs@mappings", "status": "OK", "body": {"template": {"mappings": {}}}},
    {"name": "org-ilm", "status": "FORBIDDEN", "message": "action [cluster:monitor/...] is unauthorized for user [stroom]..."}
  ]
}
```

`status`: `OK`; `NOT_FOUND` (no template matches, or a component name doesn't exist); `FORBIDDEN` (Elasticsearch
refused for want of privileges: pass its message on whole, as it names the privilege); `ERROR` (anything else,
with its message). With `indexTemplate.status` other than `OK`, `componentTemplates` is empty. `resolvedName` is
null when nothing resolved, or when resolving was refused (then add `resolveMessage`).

HTTP errors only for the request itself: 403 without READ on the doc, 404 for no such doc, and 5xx when the cluster
can't be reached at all (with the reason, as `testIndex` gives it).

## How the MCP server uses it

- In `get_field_conventions`' choices, "Follow an existing index in Stroom" reads the doc's index template and
  component templates this way. They become the example, exactly as if the user had pasted them (settings,
  composition, keyword sub-fields, `ignore_above`), and the user confirms what was read in a form.
- When the template can't be read (`FORBIDDEN`, `NOT_FOUND`, or a Stroom without this endpoint, which answers 404),
  it falls back to what works today: the doc's field list and Elasticsearch types. It tells the user why, and that
  they can paste the template instead.
- A component template that can't be read is asked for alone (one `GET _component_template/<name>` to paste), or
  left out if the user can't get it.
- The server detects the endpoint by calling it: no configuration.

## Tests worth having in Stroom

- A doc whose index name is a concrete index, a pattern, an alias and a data stream: each resolves, and the template
  with the highest priority matching it is the one returned.
- Two matching templates: the higher priority one returned, the other in `overlapping`.
- A cluster user without template privileges: `indexTemplate.status` is `FORBIDDEN` with Elasticsearch's message,
  and the response is 200.
- A component template refused or missing while the others read: reported per component.
- No READ on the doc: 403.
