# Using the Stroom MCP server from VS Code

VS Code's chat (agent mode) is the agent: its model works through the server's tools, the workflows are
prompts, and every rule that must hold (workspace and promotion, approvals, processing limits, the Elasticsearch
hand-over) is enforced by the server. Each call to Stroom is made as you, with your Keycloak token.

## 1. Keycloak

One client for VS Code, in the realm Stroom trusts:

| Setting | Value |
| --- | --- |
| Client id | `stroom-mcp-vscode` (any name; it goes in `mcp.json`) |
| Client authentication | Off (a public client) |
| Flows | Standard flow (authorization code) only; no direct access grants |
| PKCE | S256 (`pkce.code.challenge.method`) |
| Valid redirect URIs | `http://127.0.0.1:33418`, `http://127.0.0.1:33418/*`, `https://vscode.dev/redirect` (and `https://insiders.vscode.dev/redirect` for Insiders) |
| Access token audiences | Both `stroom-mcp` (the MCP server, `STROOM_MCP_KEYCLOAK_AUDIENCE`) and `stroom` (Stroom, `STROOM_MCP_STROOM_AUDIENCE`): add an Audience mapper for each, e.g. in a default client scope |
| Claims | `sub` (the `basic` scope) and `preferred_username` (the `profile` scope); the token's scope must include `openid` |

`dev/keycloak/realm-stroom.json` is a working example (it also enables direct access grants, for scripted
tests only).

## 2. Stroom

Stroom must trust the same realm, so it accepts the tokens the server forwards:

```yaml
appConfig:
  security:
    authentication:
      openId:
        identityProviderType: EXTERNAL_IDP
        openIdConfigurationEndpoint: https://keycloak.example.com/realms/<realm>/.well-known/openid-configuration
        allowedAudiences: ["stroom"]
        audienceClaimRequired: true
        uniqueIdentityClaim: sub
        userDisplayNameClaim: preferred_username
  receive:
    authenticationRequired: true   # uploads (upload_sample) carry the user's token too
```

Users are matched by `sub`; give them Stroom permissions as usual. `dev/stroom/docker-compose.oidc.yml` and
`config-oidc.yml` do this for the local stack.

## 3. The server

```
STROOM_MCP_STROOM_URL=https://stroom.example.com
STROOM_MCP_KEYCLOAK_REALM_URL=https://keycloak.example.com/realms/<realm>
STROOM_MCP_KEYCLOAK_AUDIENCE=stroom-mcp
STROOM_MCP_PUBLIC_BASE_URL=https://stroom-mcp.example.com
```

No Stroom API key: the server refuses one when sign-in is on. It serves TLS itself (or behind a TLS proxy);
deployment with the Helm chart and every setting: [DEPLOYMENT.md](DEPLOYMENT.md).

## 4. VS Code

Add the server to `.vscode/mcp.json` (workspace) or your user `mcp.json`; `docs/vscode/mcp.json` is an example:

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

On first use VS Code opens a browser to sign in to Keycloak. Then, in chat (agent mode):

- Workflows are slash commands, e.g. `/mcp.stroom.onboard_data_source`, `/mcp.stroom.onboard_existing_feed`,
  `/mcp.stroom.fix_pipeline_issue`.
- Guides (`stroom://guide/...`) can be attached as context.
- Confirmations and approvals appear as forms that you answer; the model never holds the answer.
- Standing instructions from `AGENTS` docs in Stroom apply whoever connects; a workspace `AGENTS.md` can add
  your own on top.
- 61 tools stay within VS Code's per-request limit, but with other servers enabled you may want to switch some
  groups off in the tools picker.

## What was checked locally (`dev/e2e_oauth.py`)

With the dev Keycloak and Stroom trusting it: the server answers an unauthenticated request with 401 and its
protected resource metadata, which names the realm; the realm offers the authorization code flow with S256; a
token through the VS Code client carries both audiences; tools, prompts and resources list; tools run as the
user (a feed created through MCP is recorded in Stroom as created by that user; a sample upload to
`/stroom/datafeed` with receipt authentication on is accepted with the user's token); confirmations arrive as
forms and declining one stops the call; a tampered token is refused. The browser step of VS Code's sign-in was
stood in for by a password grant.
