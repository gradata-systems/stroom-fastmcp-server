# Stroom changes: Ask Stroom AI, from testing on 21 October 2026

For Stroom's developers. Found while the user onboarded a JSON-lines source through Ask Stroom AI with the Stroom MCP
server (stroom-fastmcp-server). Each is in Stroom, not the server: read from the user's branch `gh-5848`
(p-kimberley/stroom, `6a6388ea`). The server's own findings from the same testing are in `dev/FINDINGS.md`.

## Agent output hidden in "Working…"

What the agent wrote between tool calls (a discrepancy it flagged before carrying on, say) is folded away with the
tool calls, and the group collapses when the answer arrives, so the user missed it.

- `McpAgentChat` stores any text that comes with tool calls as `INTERIM_RESPONSE`.
- `AiActivityGroups.isActivity` puts `THINKING`, `INTERIM_RESPONSE`, `TOOL_CALL` and `TOOL_RESULT` in the activity
  group, titled "Working…"; `finish` collapses it.
- Only the final `AI_RESPONSE` (a reply with no tool calls) is shown outside it (`AskStroomAiPresenter`).
- `ContextAssembler.TOOL_GUIDANCE` tells the model as much: anything the user must read goes in the answer at the end
  of the turn, "and then wait for them". So an agent with findings ends its turn: in this testing it stopped at the
  end of each stage of a build.

Asked: show interim text outside the group (or keep the group open) when it says something for the user, or give
the agent a way to tell the user something mid-run that isn't folded. And prompt for `task_list` on multi-step work:
`TaskListReminder` only nudges once a list exists, so none was ever written.

## A dashboard Stroom's agent made could not be opened

A StroomQL dashboard from the built-in `propose_dashboard` failed in the browser (a frontend error) when opened.

- `DashboardBuilder` builds `EmbeddedQueryComponentSettings` without `automate` (and with `reference(false)`).
- `EmbeddedQueryPresenter.read()` calls `automate.isOpen()` on it: null. `QueryPresenter.read()` (the classic query)
  fills in a default `Automate` when it is null; the embedded one doesn't.
- `queryTablePreferences` is never set either, and `EmbeddedQueryPresenter` calls
  `getQueryTablePreferences().copy()` when the query runs.
- `TestDashboardBuilder` checks the model only: nothing loads a built dashboard in the UI.

Asked: `DashboardBuilder` sets `.automate(Automate.builder().build())` and empty `QueryTablePreferences`;
`EmbeddedQueryPresenter` guards against a null `automate`, as `QueryPresenter` does.

## No more than 20 changes to a dashboard at once

Adding or removing many columns needs more: `DashboardChanges.MAX_CHANGES = 20` ("Give at most 20 changes at once.",
used by `ProposeDashboardChangeTool`; the same constant caps a format rule's conditions). The agent offered to split
them into batches, and then only the first batch could be applied (next section).

Asked: raise the limit, or let one change add or remove a list of columns.

## A second code change in stepping is refused at the diff dialog

Stepping with the agent: a change proposed and applied, a second asked for, and applying the second refused, "The
code has changed since this was proposed", as the first had changed the XSLT.

- `ProposeCodeChangeTool` takes the base code from `TabSnapshots.forLatestQuestion(chatId)`, the `STEP_CODE` part,
  which comes from the latest user message only.
- A message sent while a run is active (one waiting on an `ask_user` form or an approval, say) is a steer, and its
  tab snapshot isn't kept (`AskStroomAiPresenter`; `AskStroomAIService` stores one only when `steer()` is false). The
  second proposal is then made against the code from before the first change; a second proposal in the same turn as
  the first is too.
- `CodeChangeReviewPresenter` and `CodeChangeApplier` refuse when the editor's code differs from that base.

Asked: refresh the tab snapshot on steer messages, or have `propose_code_change` read the editor's current code; or,
at apply time, rebase hunks that don't overlap the change already applied.

## A change made while stepping doesn't show on Refresh

After a change on the first record, Refresh showed the output from before it; it showed once the user stepped to the
next record. Read from the code, not reproduced:

- `SteppingPresenter.step()` sends the edited code on Refresh and keeps the session.
- The server keys what it captured by fingerprints that include the code (`SteppingSession`), and a Refresh is exact
  and never moves to another record (`StoreStepResolver`, `SessionStepResolver`): the current record, not yet
  stepped with the new code, comes back as not found.
- On a not-found result at the same place with no indicators, `SteppingPresenter.getEffectiveStepData()` shows the
  last result found: the output from before the change.

Asked: a Refresh after a code change steps the current record again with the new code; the client doesn't fall back
to the last result when the code has changed since it.

## Documenting other documents

Asked by the user: the agent can't write a document's Documentation tab (its `description`) other than a
pipeline's or an XSLT's, which the MCP server writes itself, so it can't document a dashboard, say, when asked to.
Stroom's built-in `document_documentation` only reads it (`DocumentDocumentationTool`), and no `stroom-ai` tool writes
one; every document type has the field (`DashboardDoc.description`, say), so its own resource's update takes it.

Asked: a built-in tool that writes a document's Documentation tab when the user asks, approved by the user in the
chat as other changes are.

## A large tool argument must be written out by the model

Seen in testing (onboarding a Fortigate feed as a new version of its pipeline): the agent saved a 49 KB draft mapping
to its workspace (`/outputs/...`), read it with `jq` and `read_file`, and edited parts of it into files of its own; but
the save (`build_translation_xslt mapping=`) takes the mapping as an argument, which the model has to write out in full
in the call. At about 30 KB, with 60 regular expressions to copy exactly, it stopped twice to ask the user how to go
on, and a first smaller save was cut short. The workspace holds the text the call needs; nothing passes it on.

The MCP server now keeps a draft for the session, so a first save sends only its changes, and a key/value text's keys
are one entry; but any tool taking a large document (a mapping, an index template, an XSLT) meets the same limit.

Asked: a way for the agent to pass a workspace file, or a `jq` result of one, as a tool call's argument (expanded by
Stroom before the call is sent), so a large argument doesn't pass through the model.

Where it fits (`stroom-ai-impl`, at 6a6388ea):

- An MCP call runs in `McpAgentChat.runAndRecord()` (:513), which has the chat id and already saves a large *result*
  to the workspace (`saveOutput`, :1010). It calls `execute()` (:1203), and `McpToolset.callTool()` (:414) turns the
  model's JSON into the call's arguments with `McpToolAdapter.parseArguments()` (:93). Built-in tools reach the
  workspace through `ToolCallContext`; MCP calls don't, so the expansion belongs in `runAndRecord`, before
  `execute`, for MCP tools only (`!isBuiltIn(name)`).
- A reference: an argument value (at any depth) that is an object with one key, `{"$workspace": "/outputs/x.json"}`,
  optionally with `"jq": ".mapping"`, or, for a parameter typed as a string, the string `"$workspace:/outputs/x.json"`.
  A small class (`WorkspaceArguments.expand(chatId, argumentsJson)`) replaces each with the file's content: parsed
  JSON when the file (or the jq result) is JSON, so an object argument stays an object, else the text. Then a new
  `ToolExecutionRequest` with the same id and name carries the expanded JSON.
- Reading: `ChatWorkspace.fileId(chatId, path)` (:117) already refuses an expired file or one whose source document
  the user can no longer read (S6), and `localFile()` (:157) gives the whole content; `SafeJq` (as `JqTool` runs it)
  for the query. A bound on the expanded size (a new `AiWorkspaceConfig` limit, 1 MB say). Any failure (no such file,
  not available, bad jq, too large) is the call's result, `Error: ...`, and the server isn't called.
- Records: the audit log (`auditLog.toolCall`, :563) and the stored tool call keep the arguments as the model wrote
  them, the references, not the content; the expanded size can go beside them.
- Trust: the content is what the model could already have written into the call, so no new reach; the call is
  approved (or not) as now. Taint (`markTainted`) as for any read of the file.
- The model is told once, in `ContextAssembler.TOOL_GUIDANCE` (:76): to pass a workspace file as an MCP tool's
  argument, give `{"$workspace": "<path>"}` (with `"jq"` for a part of it) as the value; Stroom puts the content in.
- Tests beside `McpAgentChat`'s: an object and a string reference expanded, JSON kept as JSON, jq, a nested
  reference, each refusal, and the audit keeping the reference.

Nothing changes in the MCP server for it: its large arguments (`mapping`, `changes`, `example_template`) take an
object or a string already. Once Stroom has it, the server's instructions can say so for Stroom AI, as they name its
`task_list` and `ask_user`.
