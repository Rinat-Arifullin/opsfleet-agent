"""``/delete`` (iteration 22a, ADR-007): starts the two-phase delete of the user's own reports.

The handler never deletes. It hands the argument text to the graph, which shows a preview
rendered by code and waits for the user's NEXT turn to confirm. The command is registered by
the CLI only when the delete service started; otherwise it stays unregistered.
"""

from __future__ import annotations

import logging
from typing import Final

from opsfleet_agent.commands import Command, CommandContext, CommandResult

log = logging.getLogger(__name__)

USAGE: Final = "/delete <words | report ids | this session>"
OFF_TEXT: Final = "Deleting reports is not available right now; nothing was deleted."
FAILED_TEXT: Final = "The delete could not be started; nothing was deleted."


def _delete(args: str, ctx: CommandContext) -> CommandResult:
    if ctx.delete_start is None:
        return CommandResult(OFF_TEXT)
    try:
        return CommandResult(ctx.delete_start(args))
    except Exception as exc:  # fail closed: nothing is deleted on any error
        log.error("/delete failed: %s", type(exc).__name__)
        return CommandResult(FAILED_TEXT)


DELETE_COMMAND: Final = Command(
    "/delete", USAGE, "Delete your saved reports (shows them and asks first).", _delete
)
