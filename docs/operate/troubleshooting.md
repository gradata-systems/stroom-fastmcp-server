# Troubleshooting

## A tool call never returns, and the server logs nothing

The call never left the client. In VS Code, restart the MCP server from the MCP servers view, or reload the window,
and ask the agent to carry on. Seen after the server was redeployed during a chat: VS Code kept using a session the
new server process didn't know, and waited instead of starting a new one.

## Every Stroom call takes minutes, and VS Code's auth log loops

VS Code's authentication log (Output → Authentication) repeating "Token for session … is about to expire,
refreshing" for many sessions, with "Token scopes 'openid email profile' do not match requested scopes 'openid'",
means VS Code is refreshing sign-ins in a loop, and every Stroom call waits for a token behind it: calls of 3
minutes, the server answering each in milliseconds. Seen with 80 sessions stored for one server. To clear it:

1. Run **Authentication: Remove Dynamic Authentication Providers** from the command palette, restart VS Code and
   sign in again.
2. Give the client VS Code uses access tokens that last 15 minutes (in Keycloak, the client's Advanced tab, Access
   Token Lifespan), not Keycloak's default of 5.
3. Have the server ask for the scopes the provider grants, so VS Code's request and the token agree: for Keycloak,
   `STROOM_MCP_OIDC_REQUIRED_SCOPES=openid profile email`. Don't make `profile` optional instead: the server reads
   the user's name from it.

## Tools refused as "disabled" though they are ticked

The server was never called. Seen with `statelessHttp` on: switch it off (see
[Upgrading](../admin/upgrading.md#stateless-http-keep-it-off-for-vs-code)). With it off, check the tool picker, and
that VS Code's total tool count stays under its limit of 128 a request.

## Every call gets slower as the chat goes on

Check the server's own timings first: each `tool_call` in the [audit log](audit.md) has `duration_ms`, and
`before_ms`, the time it waited to be handled. VS Code has been seen taking several seconds per call, growing with
each call in the chat, while the server answered in milliseconds. A new chat starts fast again.

## The agent says a tool it needs is missing, or stops at a step

VS Code's Copilot Chat offers at most 128 tools per request. With its own tools and other servers, it hides many
behind `activate_*` group tools that the model has to call first. The server tells the model to do so, but a small
model can still stop. Switch off tools you don't need for this work in the tools picker, or raise
`github.copilot.chat.virtualTools.threshold`. See [Sign-in and VS Code](../admin/vscode.md#4-vs-code).

## Sign-in works, but Stroom calls fail with 401 or 403

- **401 from Stroom**: the user's token lacks Stroom's audience. Tokens must carry both the server's audience and
  `stroom` (or one audience both accept): see [Sign-in and VS Code](../admin/vscode.md#1-identity-provider).
- **403 from Stroom**: the user lacks permission in Stroom, for example on the workspace folder or a destination
  folder. The server acts as the user, so grant it in Stroom.

Both are in the audit log as `access_denied`.

## A large sample doesn't reach Stroom

Agents read files in pieces, and some clients cut long lines. For a large file, or one written on a single line,
the agent asks for an upload ticket and gives the user a `curl` command that sends the file from their disk to the
server's `/upload`. Check that a proxy in front lets bodies that large through to `/upload` (`MAX_UPLOAD_MB`;
ingress-nginx refuses over 1 MB by default). Without a terminal, upload the file in Stroom: the feed's Data tab,
Upload.

## Promotion warns about a step or the documentation

The pipeline's code changed after it was last stepped, or the documentation no longer matches the mapping. Ask the
agent to step the sample again or to write the documentation again, then promote. A hand edit of your own only
warns, and doesn't block promotion.

## A document dropped out of its build

It was moved out of the build folder, or the folder was renamed. Move it back under the same folder name. A
document's build is the folder it is in: see
[What it changes in Stroom](what-it-changes.md#where-it-builds).

## Processing didn't start after promotion

That is by design: the filter for new data is created disabled. Review the pipeline, disable any earlier version on
the same feed, and enable the new filter.

## Stroom is busy with old processor filters

Each processor filter left enabled is checked on every task-creation cycle. Development filters only select their
sample streams, so once those are processed they can be disabled or deleted. Find a build's pipelines by the
`mcp-managed` tag and the build folder.
