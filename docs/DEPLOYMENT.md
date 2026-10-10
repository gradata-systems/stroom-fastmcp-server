# Deployment

The server is one container that terminates TLS itself, signs users in with an OpenID Connect provider
(Keycloak, Entra ID, Okta, Auth0, ...) and calls Stroom as each
user with their own token. It holds no credentials for Stroom. VS Code (or any MCP client) connects to
`<publicBaseUrl>/mcp`; setup for users is in [VSCODE.md](VSCODE.md).

## Prerequisites

- **An OpenID Connect provider**: the one Stroom already trusts, with a public client for VS Code whose access
  tokens are JWTs carrying this server's audience and Stroom's (or one audience both accept), plus `sub`.
  Details: [VSCODE.md, section 1](VSCODE.md#1-identity-provider).
- **Stroom** trusts that provider (`identityProviderType: EXTERNAL_IDP`, audience `stroom`), and receipt accepts
  tokens if samples are uploaded through the server ([VSCODE.md, section 2](VSCODE.md#2-stroom)).
- **A certificate** for the server's DNS name: a Secret, or cert-manager. Plain HTTP is only allowed when a
  proxy in front terminates TLS.
- **Stroom folder permissions**: users need rights on the workspace folder (`MCP Workspace` by default), where
  builds, survey docs and backups are made; promotion needs rights on the destination folders.

## Kubernetes (Helm)

The chart is `charts/stroom-mcp`, published to `oci://ghcr.io/gradata-systems/charts/stroom-mcp` on each release
tag; the image is `ghcr.io/gradata-systems/stroom-fastmcp-server`.

**Air-gapped clusters.** The image holds every dependency (Saxon, which checks a mapping's XPaths, among them) and
fetches nothing at run time; FastMCP's check for a newer version of itself is off (`FASTMCP_CHECK_FOR_UPDATES=off`).
Copy the image and the chart into your own registry and set `image.repository` to it. The server then reaches only
Stroom, the OpenID Connect provider (its discovery document and signing keys) and the users' clients.

```yaml
# values.yaml
publicBaseUrl: https://stroom-mcp.example.com
stroom:
  url: https://stroom.example.com
  ca: {secretName: internal-ca}            # when Stroom uses a private CA
oidc:
  issuerUrl: https://keycloak.example.com/realms/stroom
  ca: {secretName: internal-ca}            # when the provider uses a private CA
tls:
  certManager: {enabled: true, issuerRef: {name: internal-ca}}   # or: existingSecret: stroom-mcp-tls
```

```
helm install stroom-mcp oci://ghcr.io/gradata-systems/charts/stroom-mcp --version 0.16.46 -f values.yaml
```

The chart refuses to render without `publicBaseUrl` (https), `stroom.url`, `oidc.issuerUrl`, and, with TLS on,
a certificate source. Everything else has a default; see `charts/stroom-mcp/values.yaml`.

- **TLS**: on by default. `tls.existingSecret` (a `kubernetes.io/tls` Secret) or `tls.certManager` (the
  certificate is issued for the host of `publicBaseUrl` unless `dnsNames` are given). The server reads the
  certificate at startup, so restart the pods after renewal, or annotate the workload for a reloader. `tls.enabled:
  false` only when an ingress or mesh terminates TLS; the server then sets `STROOM_MCP_TLS_TERMINATED_UPSTREAM`.
- **Service**: ClusterIP by default; a LoadBalancer can pass TLS straight through (`service.type`,
  `loadBalancerIP`, `loadBalancerSourceRanges`).
- **Replicas**: MCP sessions live in the replica that started them, so with more than one either set
  `statelessHttp: true` or `service.sessionAffinity: ClientIP`. Forms (confirmations and approvals) carry sealed
  state between rounds; several replicas must share the sealing keys: `requestState.existingSecret` (keys
  comma-separated, each at least 32 characters). The chart refuses `replicaCount` > 1 without them.
- **Elasticsearch**: nothing to configure. The server never connects to Elasticsearch: indexing, index
  doc fields, connection tests and test searches all go through Stroom, which uses its own Elastic Cluster docs.
  The server drafts index templates and checks the user's version against the pipeline; the user commits them.
- **Environment files**: `accessPolicy` (where template pipelines are looked for), `errorRules` (error
  triage) and `conventions` (field convention profiles) replace the image's copies when set.
- **Security**: runs as uid 10001 with a read-only root file system, no capabilities, and no service account
  token. `/healthz` is unauthenticated and doesn't depend on Stroom or the identity provider; it answers `ok` and
  the running version, e.g. `ok 0.16.46`, which MCP clients also see in the server's `serverInfo`.
- **Audit**: JSON lines on stdout for the cluster's log shipping: every tool call, resource read, Stroom request,
  confirmation, approval and refusal, with the user behind it. Events and fields: [AUDIT.md](AUDIT.md). To write
  it to a file instead, set `audit.file.enabled`. The file is on an `emptyDir` and lost with the pod, unless you
  also set `audit.file.persistence.enabled`. The chart then deploys a StatefulSet rather than a Deployment, giving
  each replica (`stroom-mcp-0`, `stroom-mcp-1`, ...) its own PersistentVolumeClaim, so each keeps its file across
  restarts and rollouts:

  ```yaml
  audit:
    file:
      enabled: true
      persistence:
        enabled: true
        size: 5Gi
        storageClassName: standard
  ```

  Turning persistence on or off for an existing release replaces the workload, restarting every pod. The claims
  outlive the release; delete them yourself when the audit files are no longer needed.

## Container

```
docker run -p 8443:8000 -v ./tls:/etc/stroom-mcp/tls:ro \
  -e STROOM_MCP_STROOM_URL=https://stroom.example.com \
  -e STROOM_MCP_OIDC_ISSUER_URL=https://keycloak.example.com/realms/stroom \
  -e STROOM_MCP_OIDC_AUDIENCE=stroom-mcp \
  -e STROOM_MCP_PUBLIC_BASE_URL=https://stroom-mcp.example.com \
  -e STROOM_MCP_TLS_CERTFILE=/etc/stroom-mcp/tls/tls.crt -e STROOM_MCP_TLS_KEYFILE=/etc/stroom-mcp/tls/tls.key \
  ghcr.io/gradata-systems/stroom-fastmcp-server:0.16.46
```

## Settings

Environment variables with the prefix `STROOM_MCP_` (or a `.env` file); the chart value that sets each is in
brackets.

| Setting | Default | Meaning |
| --- | --- | --- |
| `STROOM_URL` | required | Stroom base URL [`stroom.url`]. With several Stroom nodes, a URL that keeps a client on one node: a stepping session lives on the node that started it, and a slow step's follow-up must reach it. Through an ingress with cookie affinity (`affinity: cookie`) the follow-up carries the cookie its first response set; the in-cluster Service needs `sessionAffinity: ClientIP` |
| `STROOM_AUDIENCE` | `stroom` | Audience the forwarded token must carry for Stroom [`stroom.audience`] |
| `STROOM_UI_URL` | `STROOM_URL` | Base of Stroom links shown to users [`stroom.uiUrl`] |
| `STROOM_CA_CERTS` | | CA for Stroom's certificate, added to the system CAs [`stroom.ca`] |
| `STROOM_REQUEST_TIMEOUT` | `60` | Seconds per Stroom call [`stroom.requestTimeout`] |
| `STROOM_STEPPING_WAIT_MS` | `55000` | How long Stroom works on a step before answering 'not yet', kept under the request timeout so a slow step finishes in its first request [`extraEnv`] |
| `DATAFEED_PATH` | `/stroom/datafeed` | Receipt path for sample uploads |
| `OIDC_ISSUER_URL` | required | The provider's issuer, exactly as in the tokens' `iss` (keep a trailing `/` if it has one) [`oidc.issuerUrl`] |
| `OIDC_AUDIENCE` | required | This server's audience [`oidc.audience`] |
| `OIDC_JWKS_URI` | discovered | The provider's signing keys; by default `jwks_uri` from `<issuer>/.well-known/openid-configuration`, fetched on the first request [`oidc.jwksUri`] |
| `OIDC_TOKEN_ALGORITHM` | `RS256` | Token signing algorithm [`oidc.tokenAlgorithm`] |
| `OIDC_REQUIRED_SCOPES` | `openid` | Scopes every token must carry (`scope` or `scp`), comma- or space-separated; may be empty [`oidc.requiredScopes`] |
| `OIDC_CA_CERTS` | | CA for the provider's certificate, added to the system CAs [`oidc.ca`] |
| `PUBLIC_BASE_URL` | required | https URL clients use, in OAuth metadata [`publicBaseUrl`] |
| `REQUEST_STATE_KEYS` | per process | Shared keys sealing form state and the confirmation and approval ids given to clients without forms, comma-separated; set them when running more than one replica [`requestState`] |
| `UPLOAD_TICKET_MINUTES` | `15` | How long a sample-upload ticket lasts (never past the sign-in it carries): `upload_sample` with `files` gives the agent one, and a `curl` command per file that sends the file from the user's terminal to `/upload` [`extraEnv`] |
| `UPLOAD_TICKETS` | `short` | `short`: an upload ticket is a 12-character code this replica keeps until it expires (one replica, or routing that keeps a client on one; a restart forgets pending codes). `sealed`: the ticket carries everything, sealed with `REQUEST_STATE_KEYS`, so any replica opens it, but it runs to 2,500 characters for the agent to copy [`extraEnv`] |
| `MAX_UPLOAD_MB` | `100` | The largest sample file `/upload` takes. A proxy or ingress in front must let request bodies that large through to `/upload` (ingress-nginx refuses over 1 MB by default: `nginx.ingress.kubernetes.io/proxy-body-size: "100m"`) [`extraEnv`] |
| `TLS_CERTFILE`, `TLS_KEYFILE` | | Server certificate and key [`tls`] |
| `TLS_TERMINATED_UPSTREAM` | `false` | Serve plain HTTP behind a TLS proxy [`tls.enabled: false`]. Without it or a certificate, the server only starts when listening on localhost |
| `HOST`, `PORT` | `0.0.0.0`, `8000` | Listener [`containerPort`] |
| `WORKSPACE_FOLDER` | `MCP Workspace` | Explorer folder for builds [`build.workspaceFolder`] |
| `EVENT_LOGGING_VERSION` | `3.5.2` | Event-logging schema version for new translations [`build.eventLoggingVersion`] |
| `INSTRUCTIONS_DOC_NAME` | `AGENTS` | Name of standing-instruction Documentation docs [`build.instructionsDocName`] |
| `DEFAULT_CONVENTION` | | Field convention used when none is named [`build.defaultConvention`] |
| `CONVENTIONS_DIR` | `conventions` | Convention profiles [`conventions`] |
| `ACCESS_POLICY_FILE` | `access_policy.yaml` | Template pipeline sources [`accessPolicy`] |
| `ERROR_RULES_FILE` | `error_rules.yaml` | Error triage rules [`errorRules`] |
| `MAX_REPROCESS_STREAMS` | `10` | Streams per development reprocess [`processing.maxReprocessStreams`] |
| `REPROCESS_MAX_TASKS` | `1` | Task limit on reprocess filters [`processing.reprocessMaxTasks`] |
| `SAMPLE_MAX_TASKS` | `1` | Task limit on sample filters [`processing.sampleMaxTasks`] |
| `MAX_FEED_FILTER_TASKS` | `2` | Task limit on feed filters made at promotion [`processing.maxFeedFilterTasks`] |
| `MAX_RESPONSE_CHARS` | `100000` | Cap on a tool's reply [`limits.maxResponseChars`] |
| `MAX_STREAM_CHARS` | `20000` | Cap on stream text returned [`limits.maxStreamChars`] |
| `MAX_SAMPLE_RECORDS` | `500` | Records per `step_sample` call [`limits.maxSampleRecords`] |
| `AUDIT_LOG_FILE` | stdout | Audit JSON lines; rotate with logrotate, not `copytruncate` ([AUDIT.md](AUDIT.md#rotation)) [`audit.file.enabled`, `audit.file.path`]. The chart mounts a volume at the file's directory, since the root file system is read-only: an `emptyDir`, or with `audit.file.persistence.enabled` a claim per replica in a StatefulSet |
| `USE_ELICITATION` | `true` | Ask through forms when the client supports them |
| `DEV_NO_AUTH`, `STROOM_API_KEY` | | Local development only: no sign-in, Stroom called with an API key; refused unless listening on localhost, and the key is refused when sign-in is on |

`FASTMCP_STATELESS_HTTP` [`statelessHttp`] runs each request on its own.

## CI

`.github/workflows/ci.yml`: unit tests; `helm lint --strict` and a render of each `charts/stroom-mcp/ci/*-values.yaml`,
and checks the chart refuses to render without its required settings or a certificate; builds the image and
checks it refuses to start without TLS, then, run read-only with no capabilities and a self-signed
certificate, answers `/healthz` with `ok` and the version from `pyproject.toml` and an unauthenticated `POST /mcp` with 401 naming the issuer. Pushes
to master and tags publish the image; a `v<version>` tag matching `Chart.yaml` publishes the chart.
