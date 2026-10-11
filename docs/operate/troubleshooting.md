# Troubleshooting

## A tool call never returns, and the server logs nothing

The call never left the client. In VS Code, restart the MCP server from the MCP servers view, or reload the window,
and ask the agent to carry on. This has been seen after the server was redeployed during a chat: VS Code kept
using a session the new server process didn't know, and waited instead of starting a new one. Running the server
stateless avoids it (see [Upgrading](../admin/upgrading.md)).

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
