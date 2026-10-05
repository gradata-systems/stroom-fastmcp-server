# Audit log

Every tool call, every request it makes to Stroom, every confirmation and approval,
and every refusal is written as one JSON object per line. Events go to stdout by default, which suits
Kubernetes log shipping, or to the file named by `STROOM_MCP_AUDIT_LOG_FILE`. Either way they are kept
separate from application logs.

Stroom records the changes too, under the user's own name, because every call forwards the user's token.
This log adds what Stroom can't see: which tool made the change, what the user agreed to first, and what
the server refused. Ship it somewhere the people being investigated can't modify it.

## Rotation

The server doesn't rotate or expire the audit log itself; that is left to what runs it.

- **stdout** (the default): the container runtime rotates container logs (in Kubernetes, the kubelet's
  `containerLogMaxSize` and `containerLogMaxFiles`), so ship them to a log store before they rotate away.
- **A file**: in Kubernetes, the chart's `audit.file` settings write it to a volume, optionally a claim per
  replica ([DEPLOYMENT.md](DEPLOYMENT.md#kubernetes-helm)). Rotate it with `logrotate` or similar, by moving the file aside. The server notices the file
  has been moved or deleted and reopens it by name before the next event, so nothing is lost or written to
  the old file. Don't use `copytruncate`: events written during the copy are lost. For example:

  ```
  /var/log/stroom-mcp/audit.jsonl {
      daily
      rotate 90
      compress
      delaycompress
      missingok
      notifempty
  }
  ```

  Keep one server process per file. Reopening relies on the operating system reporting that the file was
  moved, which works on Linux and in containers but not on Windows.

## Events

| Event | When | Key fields |
|---|---|---|
| `tool_call` | every tool invocation, when it finishes | `tool`, `arguments`, `outcome` (`success` or `error`), `error`, `duration_ms`, and where that time went when any did: `stroom_requests` and `stroom_ms` (its Stroom requests and their total time), `slowest` and `slowest_ms` (the longest), `user_ms` (waiting on the user in a form). The rest is this server's own work and the network |
| `resource_read` | every resource read, such as a guide or convention profile, when it finishes | `uri`, `outcome`, `error`, `duration_ms` |
| `stroom_request` | every Stroom REST request, and every sample upload to the datafeed | `method`, `path`, `outcome` (`success` or `error`), `status`, `error`, `took_ms`; uploads add `feed` and `bytes` |
| `confirmation` | a change the user confirms first, e.g. `create_feed`, `create_pipeline`, `copy_pipeline` | `action`, `details`, `outcome`, `via` or `id` |
| `approval` | a change that needs the user's approval, e.g. `promote_build`, `create_processor_filter`, `reprocess_streams`, `set_processor_filter_enabled` | `action`, `details`, `outcome`, `via` or `id` |
| `access_denied` | a request refused by the server or by Stroom | `reason`, and the fields below |

`confirmation` and `approval` outcomes:

| Outcome | Meaning |
|---|---|
| `requested` | The server asked the user. `via: form` when the client shows a form; otherwise `id` is the consent id handed back to the client. |
| `granted` | The user agreed: in a form (`via: form` or `elicitation`), or by the client calling again with the consent id (`via: id`). |
| `declined` | The user refused in a form. The tool call ends with an error and nothing is changed. |

`details` is exactly what the user was shown, such as the build, feed name and encoding, or the pipelines
and processor filters a promotion will change. A request and its answer arrive in separate tool calls, so
they have different `call_id`s. Match them on `action`, `details` and the user, or on `id`.

`access_denied` reasons:

| Reason | Meaning | Other fields |
|---|---|---|
| `invalid_token` | An access token was refused at sign-in; the request never reached a tool. | `check`: the first check it failed (below) |
| `not_managed` | A tool tried to change a document this server didn't create; changes go through a working copy in a build. | `doc` (`type`, `uuid`, `name`) |
| `not_built` | A tool that works only on documents this server built was given another. | `doc` |
| `stroom_401`, `stroom_403` | Stroom refused the request: the token wasn't accepted, or the user lacks the permission. | `status`, `method`, `path`; uploads add `feed` and `bytes` |

`invalid_token` checks, in the order they are made:

| Check | Meaning |
|---|---|
| `malformed` | Not a JWT. |
| `algorithm` | Signed with an algorithm other than `STROOM_MCP_OIDC_TOKEN_ALGORITHM`. |
| `signing_key` | The provider's signing keys couldn't be fetched, or have no key for the token's `kid`. |
| `expired` | Past its `exp`. Expect these routinely: clients refresh a token once it is refused. |
| `issuer` | `iss` isn't `STROOM_MCP_OIDC_ISSUER_URL`. |
| `audience` | `aud` lacks `STROOM_MCP_OIDC_AUDIENCE`. |
| `scopes` | Lacks a scope in `STROOM_MCP_OIDC_REQUIRED_SCOPES`. |
| `signature` | The signature doesn't verify: the token was altered or forged. |
| `sub` | Valid, but has no `sub` claim. |

The check is worked out from the rejected token's unverified contents, so it records why the token was
refused, not who presented it. `invalid_token` events therefore carry no identity. Requests with no token
at all aren't recorded: a client makes one before every sign-in, to discover where to sign in. The reason
for each refusal is also logged as a warning in the application log, with the values that didn't match.

Every event also carries:

- `ts`: the time, in UTC
- `sub`, `username` and `client_id`: who called, from the access token. `username` is
  `preferred_username`, or else `upn` (Entra ID), or else `email`; `client_id` is `azp`, the client the
  user signed in with. All three are null with `dev_no_auth`.
- `call_id`: links a `tool_call` or `resource_read` to the events it caused (null for `invalid_token`)

## Example

A `create_feed` call, answered with a consent id. The first call asks:

```json
{"ts": "2026-09-29T06:14:02.118+00:00", "event": "confirmation", "call_id": "5f0c9e1d2b6a4c7e9f3a1b2c3d4e5f60", "sub": "8d2f...", "username": "john.smith1", "client_id": "stroom-mcp-vscode", "action": "create_feed", "details": {"build": "acme-vpn-v1.0", "feed name": "ACME-VPN-V1.0", "encoding": "UTF-8", "stream type": "Raw Events"}, "outcome": "requested", "id": "c41e..."}
{"ts": "2026-09-29T06:14:02.120+00:00", "event": "tool_call", "call_id": "5f0c9e1d2b6a4c7e9f3a1b2c3d4e5f60", "sub": "8d2f...", "username": "john.smith1", "client_id": "stroom-mcp-vscode", "tool": "create_feed", "arguments": {"build": "acme-vpn-v1.0", "name": "ACME-VPN-V1.0"}, "outcome": "success", "duration_ms": 3}
```

The user agrees, and the second call creates the feed (the lookups that find the build folder, the tagging
and the read of the new feed are left out):

```json
{"ts": "2026-09-29T06:14:19.502+00:00", "event": "confirmation", "call_id": "0b7d2a4e6c8f4a1b9d3e5f7a9c1b3d5e", "sub": "8d2f...", "username": "john.smith1", "client_id": "stroom-mcp-vscode", "action": "create_feed", "details": {"build": "acme-vpn-v1.0", "feed name": "ACME-VPN-V1.0", "encoding": "UTF-8", "stream type": "Raw Events"}, "outcome": "granted", "via": "id"}
{"ts": "2026-09-29T06:14:19.689+00:00", "event": "stroom_request", "call_id": "0b7d2a4e6c8f4a1b9d3e5f7a9c1b3d5e", "sub": "8d2f...", "username": "john.smith1", "client_id": "stroom-mcp-vscode", "outcome": "success", "status": 200, "took_ms": 41, "method": "POST", "path": "/explorer/v2/create"}
{"ts": "2026-09-29T06:14:19.790+00:00", "event": "stroom_request", "call_id": "0b7d2a4e6c8f4a1b9d3e5f7a9c1b3d5e", "sub": "8d2f...", "username": "john.smith1", "client_id": "stroom-mcp-vscode", "outcome": "success", "status": 200, "took_ms": 22, "method": "PUT", "path": "/feed/v1/2b9f..."}
{"ts": "2026-09-29T06:14:19.795+00:00", "event": "tool_call", "call_id": "0b7d2a4e6c8f4a1b9d3e5f7a9c1b3d5e", "sub": "8d2f...", "username": "john.smith1", "client_id": "stroom-mcp-vscode", "tool": "create_feed", "arguments": {"build": "acme-vpn-v1.0", "name": "ACME-VPN-V1.0", "confirmation_id": "c41e..."}, "outcome": "success", "duration_ms": 293}
```

Refusals:

```json
{"ts": "2026-09-29T06:20:44.503+00:00", "event": "access_denied", "call_id": "a1b2...", "sub": "8d2f...", "username": "john.smith1", "client_id": "stroom-mcp-vscode", "reason": "not_managed", "doc": {"type": "XSLT", "uuid": "77c0...", "name": "ACME-VPN-Events-V1.0"}}
{"ts": "2026-09-29T06:21:10.017+00:00", "event": "access_denied", "call_id": null, "sub": null, "reason": "invalid_token", "check": "audience"}
```

## Finding the changes in Stroom

Everything the server creates is tagged `mcp-generated` (and, until it is promoted, `mcp-managed` and
`mcp-build-<build>`), so it can be found in Stroom's explorer by tag. Stroom's own logs record each change
under the user who made it. The `stroom_request` events give the time, method and path to match them on.

## Useful queries on the audit log

Once the audit log is indexed, for example in Stroom or Elasticsearch, you can answer questions about the
server's use:

- Everything one person did: filter on `username` or `sub`.
- What was promoted, and who approved it: filter on `event: approval`, `action: promote_build` and
  `outcome: granted`; `details` lists what changed.
- What users declined: filter on `outcome: declined`.
- Attempts to change production content directly: filter on `reason: not_managed`.
- Sign-in problems: filter on `reason: invalid_token` and rank `check`. A rise in `audience`, `issuer` or
  `signing_key` usually means a configuration change at the identity provider; `signature` means altered tokens.
- Slow or failing tools: filter on `event: tool_call` and sort by `duration_ms`, or filter on
  `outcome: error`. A slow call says where its time went: Stroom (`stroom_ms`, with the slowest request), the user
  (`user_ms`), or neither, which leaves this server and the network.
