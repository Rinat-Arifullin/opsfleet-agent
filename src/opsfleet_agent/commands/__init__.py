"""REPL command table (HLD cli.py commands list, AC-20.5).

Commands are CLI-only: none of them is a tool, so no model role can reach them. In particular
``/audit`` lives only here (SEC-17). Every handler returns plain text; the CLI passes it
through ``terminal_safe`` before stdout.

Report commands (all owner-only and scope-checked in ``reports.library``): ``/reports [words]``
lists the user's own saved reports (titles only; the optional words use the delete matcher),
``/open <id | row n | title words>`` shows one, ``/search <words> [tag:x] [from:D] [to:D]``
substring-searches them
(iteration 18). ``/export`` belongs to 22a and stays a stub (OD-5 in iter19-ods.md).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

log = logging.getLogger(__name__)

MAX_LINE_CHARS: Final = 4000  # bound on the argument text handed to a handler
UNKNOWN_TEXT: Final = "Unknown command; type /help."
NOT_AVAILABLE_TEXT: Final = "{name} is not available yet in this version."
STORE_UNAVAILABLE_TEXT: Final = "This command is unavailable right now (local store not open)."
EXAMPLE_QUESTIONS: Final = (
    "How many orders were completed last month?",
    "What are the top 5 product categories by revenue this year?",
    "Show monthly revenue for 2024 compared with 2023.",
)
EXIT_ALIASES: Final = frozenset({"exit", "quit"})


@dataclass
class CommandContext:
    """What a command may see. Built by the CLI per command; holds no secrets."""

    user_id: str
    session_id: str
    last_turn_id: str | None = None
    trace_dir: Path | None = None
    feedback_store: Any = None
    audit_log: Any = None
    tracer: Any = None
    persona_version: Callable[[], str] | None = None
    report_store: Any = None  # iteration 17: store.reports.ReportStore (owner-scoped reads)
    scope: Any = None  # iteration 18: the CURRENT guards.scope.ProductScope; None fails closed
    # Ids of the last /reports, /search or ambiguous /open listing, for "/open <n>". The CLI
    # passes the SAME list object every turn. Only /open reads it; no delete path takes it.
    listing: list[str] = field(default_factory=list)


REPORTS_LIST_LIMIT: Final = 20
NO_REPORTS_TEXT: Final = "You have no saved reports yet."
NO_MATCH_TEXT: Final = "No saved reports match."


@dataclass(frozen=True)
class CommandResult:
    text: str
    exit: bool = False


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    help: str
    handler: Callable[[str, CommandContext], CommandResult]
    stub: bool = field(default=False)


def _help(_args: str, _ctx: CommandContext) -> CommandResult:
    lines = ["Commands:"]
    for cmd in COMMANDS.values():
        suffix = " (not available yet)" if cmd.stub else ""
        lines.append(f"  {cmd.usage:<34} {cmd.help}{suffix}")
    lines.append("Example questions:")
    lines += [f"  {q}" for q in EXAMPLE_QUESTIONS]
    lines.append("Ctrl-C cancels a running answer; Ctrl-C twice at the prompt quits.")
    return CommandResult("\n".join(lines))


def _exit(_args: str, ctx: CommandContext) -> CommandResult:
    return CommandResult(f"Session: {ctx.session_id}\nGoodbye.", exit=True)


def _feedback(args: str, ctx: CommandContext) -> CommandResult:
    if ctx.feedback_store is None:
        return CommandResult(STORE_UNAVAILABLE_TEXT)
    from opsfleet_agent.commands.feedback import handle_feedback

    try:
        text = handle_feedback(
            args,
            store=ctx.feedback_store,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            last_turn_id=ctx.last_turn_id,
            tracer=ctx.tracer,
        )
    except Exception as exc:  # a store failure never crashes the REPL
        log.error("feedback failed: %s", type(exc).__name__)
        return CommandResult("Could not save feedback right now.")
    return CommandResult(text)


def _trace(args: str, ctx: CommandContext) -> CommandResult:
    if ctx.trace_dir is None:
        return CommandResult(STORE_UNAVAILABLE_TEXT)
    from opsfleet_agent.commands.trace import render_trace

    turn = args.strip() or ctx.last_turn_id
    if not turn:
        return CommandResult("Usage: /trace [turn_id] (no answered turn yet).")
    try:
        return CommandResult(render_trace(ctx.trace_dir, turn, ctx.session_id))
    except Exception as exc:
        log.error("trace failed: %s", type(exc).__name__)
        return CommandResult("Could not read the trace right now.")


def _audit(args: str, ctx: CommandContext) -> CommandResult:
    from opsfleet_agent.commands.audit import UNAVAILABLE, render_audit

    if ctx.audit_log is None:
        return CommandResult(UNAVAILABLE)
    return CommandResult(
        render_audit(args, log=ctx.audit_log, user_id=ctx.user_id, session_id=ctx.session_id)
    )


def _persona(_args: str, ctx: CommandContext) -> CommandResult:
    if ctx.persona_version is None:
        return CommandResult(STORE_UNAVAILABLE_TEXT)
    try:
        version = ctx.persona_version()
    except Exception as exc:
        log.error("persona version failed: %s", type(exc).__name__)
        return CommandResult("Could not read the persona right now.")
    return CommandResult(f"Active persona: {version} (read-only here).")


def _reports(args: str, ctx: CommandContext) -> CommandResult:
    """The user's OWN saved reports, newest first. With words, the delete matcher filters them
    (AC-21.4). A report from a scope the user no longer has shows masked (AC-21.5)."""
    if ctx.report_store is None:
        return CommandResult(STORE_UNAVAILABLE_TEXT)
    from opsfleet_agent.reports import library
    from opsfleet_agent.reports.matcher import MatchError

    try:
        res = library.list_reports(
            ctx.report_store, ctx.user_id, ctx.scope, args.strip() or None, REPORTS_LIST_LIMIT
        )
    except MatchError as exc:
        return CommandResult(f"Usage: /reports [words]. {exc}")
    except Exception as exc:  # a store failure never crashes the REPL
        log.error("reports list failed: %s", type(exc).__name__)
        return CommandResult("Could not read your reports right now.")
    ctx.listing[:] = [e.report_id for e in res.entries]
    empty = NO_MATCH_TEXT if args.strip() else NO_REPORTS_TEXT
    header = f"Your saved reports (newest first, up to {REPORTS_LIST_LIMIT}):"
    return CommandResult(library.render_list(res, header=header, empty=empty))


def _open(args: str, ctx: CommandContext) -> CommandResult:
    """By id, by a row number of the last list, or by title words (one hit opens it)."""
    if ctx.report_store is None:
        return CommandResult(STORE_UNAVAILABLE_TEXT)
    if not args.strip():
        return CommandResult("Usage: /open <report_id | row number | title words>")
    from opsfleet_agent.reports import library

    try:
        res = library.open_report(ctx.report_store, ctx.user_id, ctx.scope, args, ctx.listing)
    except Exception as exc:
        log.error("report open failed: %s", type(exc).__name__)
        return CommandResult("Could not open that report right now.")
    if res.status == "ambiguous":
        ctx.listing[:] = list(res.listing)
    return CommandResult(res.text)


_SEARCH_USAGE: Final = "Usage: /search <words> [tag:<tag>] [from:YYYY-MM-DD] [to:YYYY-MM-DD]"


def _search(args: str, ctx: CommandContext) -> CommandResult:
    """Substring search over the user's own in-scope reports. The only state kept is the id
    listing for "/open <n>"; it is never a delete target (AC-21.11)."""
    if ctx.report_store is None:
        return CommandResult(STORE_UNAVAILABLE_TEXT)
    from opsfleet_agent.reports import library

    try:
        res = library.search_reports(
            ctx.report_store, ctx.user_id, ctx.scope, **library.parse_search_args(args)
        )
    except library.LibraryError as exc:
        return CommandResult(f"{_SEARCH_USAGE}\n{exc}.")
    except Exception as exc:
        log.error("report search failed: %s", type(exc).__name__)
        return CommandResult("Could not search your reports right now.")
    ctx.listing[:] = [e.report_id for e in res.entries]
    header = f"Matching reports (newest first, {library.count_text(res)} in total):"
    return CommandResult(library.render_list(res, header=header, empty=NO_MATCH_TEXT))


def _stub(name: str) -> Callable[[str, CommandContext], CommandResult]:
    def handler(_args: str, _ctx: CommandContext) -> CommandResult:
        return CommandResult(NOT_AVAILABLE_TEXT.format(name=name))

    return handler


def _table() -> dict[str, Command]:
    cmds = [
        Command("/help", "/help", "List commands and example questions.", _help),
        Command("/exit", "/exit", "Print the session id and quit.", _exit),
        Command(
            "/feedback",
            "/feedback up|down [reason] [comment]",
            "Rate the last answer.",
            _feedback,
        ),
        Command("/trace", "/trace [turn_id]", "Show the trace of a turn.", _trace),
        Command("/audit", "/audit [--session|--user]", "Show your audit events.", _audit),
        Command("/persona", "/persona", "Show the active persona version.", _persona),
        Command("/reports", "/reports [words]", "List your saved reports.", _reports),
        Command("/open", "/open <id|n|title>", "Open a saved report.", _open),
        Command("/search", "/search <words> [tag:x]", "Search saved reports.", _search),
    ]
    for name, usage, text in (
        ("/export", "/export <report_id>", "Export a saved report."),
    ):
        cmds.append(Command(name, usage, text, _stub(name), stub=True))
    return {c.name: c for c in cmds}


COMMANDS: Final[dict[str, Command]] = _table()


def is_command(line: str) -> bool:
    s = line.strip()
    return s.startswith("/") or s.lower() in EXIT_ALIASES


def dispatch(line: str, ctx: CommandContext) -> CommandResult:
    """Run one command line (``/name args``). Never raises."""
    s = line.strip()
    if s.lower() in EXIT_ALIASES:
        return _exit("", ctx)
    name, _, args = s.partition(" ")
    cmd = COMMANDS.get(name.lower())
    if cmd is None:
        return CommandResult(UNKNOWN_TEXT)  # the unknown name is never echoed
    try:
        return cmd.handler(args.strip()[:MAX_LINE_CHARS], ctx)
    except Exception as exc:  # defence in depth: handlers already catch their own failures
        log.error("command %s failed: %s", cmd.name, type(exc).__name__)
        return CommandResult("That command failed; please try again.")
