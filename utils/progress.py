"""What a long tool call is doing, told to the client as it goes."""
import logging
import time
from typing import Any

from fastmcp.server.dependencies import get_context

logger = logging.getLogger(__name__)

EVERY = 5.0     # seconds between a loop's notifications (Progress.now_and_then)


class Progress:
    """MCP progress notifications for one tool call: `await say("Searching ...")`. A client that asked for progress
    (a progressToken on its call) shows each message while the call runs; for any other, nothing is sent. Asked for by
    the user: describe_document on an index ran for minutes with nothing to show what it was doing.

    Never fails the call: a notification that can't be sent is logged and dropped."""

    def __init__(self, ctx: Any):
        self._ctx = ctx
        self._step = 0
        self._said = float('-inf')      # when the last notification went

    async def __call__(self, message: str) -> None:
        report = getattr(self._ctx, 'report_progress', None)
        if report is None:      # no MCP context (a tool run directly, a test)
            return
        self._step += 1         # progress must increase with every notification
        self._said = time.monotonic()
        try:
            await report(self._step, None, message)
        except Exception as e:
            logger.debug("Progress not sent (%s): %s", message, e)

    async def now_and_then(self, message: str, every: float = EVERY) -> None:
        """Say message only when nothing has been said for `every` seconds: for each turn of a loop (a record
        stepped), so a quick loop says nothing and a slow one says where it is now and then, not at every turn."""
        if time.monotonic() - self._said >= every:
            await self(message)


async def progress_from(ctx: Any) -> Progress:
    """The call's Progress, one for the whole call wherever it is asked for: a client drops a notification whose
    progress isn't more than the last one's."""
    try:
        found = await ctx.get_state('progress')
        if isinstance(found, Progress):
            return found
        made = Progress(ctx)
        await ctx.set_state('progress', made, serializable=False)     # for this request only
        return made
    except Exception:           # no request state (a tool run directly, a test): one of its own
        return Progress(ctx)


async def current_progress() -> Progress:
    """progress_from the tool call this runs in, for code handed the gateway rather than the call's context
    (step_spread); outside a call, one that says nothing."""
    try:
        ctx = get_context()
    except RuntimeError:
        return Progress(None)
    return await progress_from(ctx)
