# Using the Stroom MCP server from VS Code

The server works with any MCP client that can sign the user in; this guide sets up VS Code's chat (agent
mode), and sections 1 and 2 apply to any client. The model works through the server's tools, the workflows are
prompts, and every rule that must hold (workspace and promotion, approvals, processing limits, the Elasticsearch
hand-over) is enforced by the server. Each call to Stroom is made as you, with your own access token.

## 1. Identity provider

Any OpenID Connect provider Stroom trusts (Keycloak, Entra ID, Okta, Auth0, ...) will do. The server needs:

- **A public client for VS Code**: authorization code flow with PKCE (S256), no client secret, redirect URIs
  `http://127.0.0.1:33418`, `http://127.0.0.1:33418/*` and `https://vscode.dev/redirect` (and
  `https://insiders.vscode.dev/redirect` for Insiders). VS Code is given its client id, so the provider need not
  support dynamic client registration.
- **JWT access tokens** signed with `STROOM_MCP_OIDC_TOKEN_ALGORITHM` (RS256 by default), whose `iss` is
  `STROOM_MCP_OIDC_ISSUER_URL`, with a `sub` claim and the scopes in `STROOM_MCP_OIDC_REQUIRED_SCOPES`
  (`openid` by default).
- **Audiences**: the server forwards the token to Stroom, so `aud` must include both `STROOM_MCP_OIDC_AUDIENCE`
  (e.g. `stroom-mcp`) and `STROOM_MCP_STROOM_AUDIENCE` (e.g. `stroom`). A provider that issues one audience per
  token (Entra ID, Okta) can't do that: set `STROOM_MCP_STROOM_AUDIENCE` to the server's audience instead and add
  that audience to Stroom's `allowedAudiences` (section 2).

### Keycloak

One client for VS Code, in the realm Stroom trusts:

| Setting | Value |
| --- | --- |
| Client id | `stroom-mcp-vscode` (any name; it goes in `mcp.json`) |
| Client authentication | Off (a public client) |
| Flows | Standard flow (authorization code) only; no direct access grants |
| PKCE | S256 (`pkce.code.challenge.method`) |
| Valid redirect URIs | As above |
| Access token audiences | Both `stroom-mcp` (the MCP server, `STROOM_MCP_OIDC_AUDIENCE`) and `stroom` (Stroom, `STROOM_MCP_STROOM_AUDIENCE`): add an Audience mapper for each, e.g. in a default client scope |
| Claims | `sub` (the `basic` scope) and `preferred_username` (the `profile` scope); the token's scope must include `openid` |

The issuer is the realm URL, `https://keycloak.example.com/realms/<realm>`. `dev/keycloak/realm-stroom.json` is
a working example (it also enables direct access grants, for scripted tests only).

### Entra ID

- Register an API app for the server, expose a scope (e.g. `api://stroom-mcp/access`) and set
  `accessTokenAcceptedVersion` to 2 in its manifest, so tokens have the v2 issuer. Register VS Code as a public
  client (mobile and desktop redirect URIs as above) with permission to that scope.
- `STROOM_MCP_OIDC_ISSUER_URL=https://login.microsoftonline.com/<tenant id>/v2.0`,
  `STROOM_MCP_OIDC_AUDIENCE` = the API app's client id (the `aud` of v2 tokens),
  `STROOM_MCP_STROOM_AUDIENCE` = the same, and `STROOM_MCP_OIDC_REQUIRED_SCOPES=access`: Entra puts only API
  scopes in the `scp` claim, never `openid`.
- `sub` differs per application in Entra; for Stroom to see the same user whether they sign in through its UI or
  through the server, match users on `oid` (`uniqueIdentityClaim: oid`).

## 2. Stroom

Stroom must trust the same provider, so it accepts the tokens the server forwards:

```yaml
appConfig:
  security:
    authentication:
      openId:
        identityProviderType: EXTERNAL_IDP
        openIdConfigurationEndpoint: https://keycloak.example.com/realms/<realm>/.well-known/openid-configuration
        allowedAudiences: ["stroom"]   # plus the server's audience if tokens carry only that
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
STROOM_MCP_OIDC_ISSUER_URL=https://keycloak.example.com/realms/<realm>
STROOM_MCP_OIDC_AUDIENCE=stroom-mcp
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

On first use VS Code opens a browser to sign in to the identity provider. Then, in chat (agent mode):

- Workflows are slash commands, e.g. `/mcp.stroom.onboard_data_source`, `/mcp.stroom.onboard_existing_feed`,
  `/mcp.stroom.fix_pipeline_issue`.
- Guides (`stroom://guide/...`) can be attached as context.
- Confirmations and approvals appear as forms that you answer; the model never holds the answer.
- Standing instructions from `AGENTS` docs in Stroom apply whoever connects; a workspace `AGENTS.md` can add
  your own on top.
- 58 tools stay within VS Code's per-request limit, but with other servers enabled you may want to switch some
  groups off in the tools picker.

## What was checked locally (`dev/e2e_oauth.py`)

With the dev Keycloak and Stroom trusting it: the server answers an unauthenticated request with 401 and its
protected resource metadata, which names the realm; the realm offers the authorization code flow with S256; a
token through the VS Code client carries both audiences; tools, prompts and resources list; tools run as the
user (a feed created through MCP is recorded in Stroom as created by that user; a sample upload to
`/stroom/datafeed` with receipt authentication on is accepted with the user's token); confirmations arrive as
forms and declining one stops the call; a tampered token is refused. The browser step of VS Code's sign-in was
stood in for by a password grant.
