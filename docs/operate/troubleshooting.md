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

## Every large result asks to run Get-Content

VS Code saves a tool result over 8 KB to a file (`content.json` under its `workspaceStorage`) and tells the model to
read it with `read_file`, which needs no approval for those files. Some models read it with a terminal command
instead (`Get-Content … content.json | ConvertFrom-Json`), and every one asks you to approve it. The server tells the
model to use `read_file`; if one still uses the terminal, tell it to in the chat. To keep large results in the chat
instead, raise `github.copilot.chat.agent.largeToolResultsToDisk.thresholdBytes` (8192 by default), at the cost of
more of the model's context. Don't approve terminal commands wholesale for this.

## A feed or UUID "isn't in" Stroom

The server refuses a feed or document it can't find, naming the Stroom it is connected to (its
`STROOM_MCP_CLUSTER_NAME` and URL). With a server for each cluster, the agent may have used the wrong one: check the
cluster, then ask again naming it. For a feed name that isn't found, the server offers the three closest names in a
form, with a fourth choice to correct the name. See [several Stroom clusters](../admin/vscode.md#several-stroom-clusters).

## The server stops at start: "Can't write the audit log"

The audit log file's directory isn't writable by the server's user (uid 10001 in the image). On Kubernetes the pod's
`fsGroup` normally gives it, but some storage ignores `fsGroup` (NFS, some CSI drivers) and leaves the volume owned by
root. Set `audit.file.persistence.fixPermissions=true` in the Helm chart (an init container gives the directory to
the server's user; it runs as root, which a namespace enforcing the restricted Pod Security Standard refuses), have
the volume's ownership set by its provisioner, or leave the audit log on stdout.

## Documenting an index is refused: "the index has no field"

A pipeline that feeds the index writes a field Stroom doesn't list for it. Either the index's data was written
before the pipeline wrote that field (reindex it: for Elasticsearch, have the index template map the field first),
or the field was only just indexed and Stroom hasn't listed it yet (check again in a minute or two). The agent asks
you which. If no event carries a value for the field yet and you want the documentation now, the agent can write it
with the field marked as not in the index. `verify_index` fails the same way for a pipeline in a build.

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

That is by design: the promoted pipeline's filter is created disabled. Review the pipeline and enable the filter. It
has no minimum creation time, so it processes the feed's existing data as well: if an earlier version still processes
the same feed, disable it first, or set a minimum creation time on the new filter in Stroom.

## Stroom is busy with old processor filters

Each processor filter left enabled is checked on every task-creation cycle. Development filters only select their
sample streams, so once those are processed they can be disabled or deleted. Find a build's pipelines by the
`mcp-managed` tag and the build folder.
