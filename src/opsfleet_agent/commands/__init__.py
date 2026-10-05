"""REPL command table (HLD cli.py commands list, AC-20.5).

Commands are CLI-only: none of them is a tool, so no model role can reach them. In particular
``/audit`` lives only here (SEC-17). Every handler returns plain text; the CLI passes it
through ``terminal_safe`` before stdout.

Report commands (all owner-only and scope-checked in ``reports.library``): ``/reports [words]``
lists the user's own saved reports (titles only; the optional words use the delete matcher),
``/open <id | row n | title words>`` shows one, ``/search <words> [tag:x] [from:D] [to:D]``
searches them
(iteration 18; ranked FTS5 bm25 since iteration 37, substring fallback).
``/rename``, ``/export`` and ``/retry`` (iteration 33, ``commands.report_actions``)
rename a report, write it as a Markdown file under ``<data dir>/exports/<owner>/`` and re-run the
report phase of this session's last failed report (no SQL).
``/delete`` (iteration 22a, ``commands.delete``) is registered at startup only when the delete
service is ready; otherwise it stays unregistered (feature-off rollback path).
``/erase`` (iteration 35, D-222) only explains the erasure process: erasure itself is the
maintainer CLI ``commands.erase``, so no chat session can erase a user.
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
    # Iteration 22a: starts a two-phase delete on the graph and returns the reply text (the
    # preview, never a deletion). None while the delete feature is off (fail closed).
    delete_start: Callable[[str], str] | None = None
    # Iteration 40: obs.langfuse_sink.LangfuseSink when Langfuse is configured, else None.
    langfuse: Any = None
    # Iteration 39: store.preferences.SQLitePreferenceStore; None = /prefs unavailable.
    preference_store: Any = None
    # Iteration 33: where /export writes (None = <data dir>/exports) and the PII detector the
    # title guard uses (None = the process default).
    export_dir: Path | None = None
    detector: Any = None


REPORTS_LIST_LIMIT: Final = 20
NO_REPORTS_TEXT: Final = "You have no saved reports yet."
NO_MATCH_TEXT: Final = "No saved reports match."


@dataclass(frozen=True)
class CommandResult:
    text: str
    exit: bool = False
    # Iteration 33: a fixed message the CLI runs as a chat turn (``/retry`` -> "retry report").
    turn: str | None = None


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


def _prefs(args: str, ctx: CommandContext) -> CommandResult:
    if ctx.preference_store is None:
        return CommandResult(STORE_UNAVAILABLE_TEXT)
    from opsfleet_agent.commands.preferences import handle_prefs

    try:
        text = handle_prefs(
            args,
            store=ctx.preference_store,
            user_id=ctx.user_id,
            scope=ctx.scope,
            tracer=ctx.tracer,
        )
    except Exception as exc:  # a store failure never crashes the REPL
        log.error("prefs failed: %s", type(exc).__name__)
        return CommandResult("Could not read or save preferences right now.")
    return CommandResult(text)


def _trace(args: str, ctx: CommandContext) -> CommandResult:
    if ctx.trace_dir is None:
        return CommandResult(STORE_UNAVAILABLE_TEXT)
    from opsfleet_agent.commands.trace import render_trace

    turn = args.strip() or ctx.last_turn_id
    if not turn:
        return CommandResult("Usage: /trace [turn_id] (no answered turn yet).")
    try:
        text = render_trace(ctx.trace_dir, turn, ctx.session_id)
    except Exception as exc:
        log.error("trace failed: %s", type(exc).__name__)
        return CommandResult("Could not read the trace right now.")
    return CommandResult(text + _langfuse_line(ctx, turn))


def _langfuse_line(ctx: CommandContext, turn: str) -> str:
    """Iteration 40: the Langfuse trace id (and UI link) of the turn, when tracing is on."""
    if ctx.langfuse is None:
        return ""
    try:
        trace_id = ctx.langfuse.trace_id_for(turn)
        if not trace_id:
            return ""
        url = ctx.langfuse.trace_url(trace_id)
    except Exception:  # noqa: BLE001 - optional line, never breaks /trace
        return ""
    return f"\nLangfuse trace: {trace_id}" + (f" ({url})" if url else "")


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
    """Hybrid search over the user's own in-scope reports: FTS bm25 ranks fused with semantic
    (embedding) ranks by RRF (iteration 38, AC-21.13/14). Degrades to the ranked full-text
    search, then to the word match, when the embedding or the index is unavailable; the path
    that ran is recorded in the trace (never the query text). The only state
    kept is the id listing for "/open <n>"; it is never a delete target (AC-21.11)."""
    if ctx.report_store is None:
        return CommandResult(STORE_UNAVAILABLE_TEXT)
    from opsfleet_agent.reports import library

    try:
        res = library.search_reports(
            ctx.report_store, ctx.user_id, ctx.scope, mode="semantic",
            **library.parse_search_args(args),
        )  # fmt: skip
    except library.LibraryError as exc:
        return CommandResult(f"{_SEARCH_USAGE}\n{exc}.")
    except Exception as exc:
        log.error("report search failed: %s", type(exc).__name__)
        return CommandResult("Could not search your reports right now.")
    _trace_search(ctx, res)
    ctx.listing[:] = [e.report_id for e in res.entries]
    order = "best match first" if res.path in library.RANKED_PATHS else "newest first"
    header = f"Matching reports ({order}, {library.count_text(res)} in total):"
    return CommandResult(library.render_list(res, header=header, empty=NO_MATCH_TEXT))


def _trace_search(ctx: CommandContext, res: Any) -> None:
    """Which search path ran (ranked, substring or the fallback); never the query text."""
    if ctx.tracer is None:
        return
    try:
        ctx.tracer.record(
            "tool", "search_reports", tool="search_reports", outcome="ok",
            search_path=res.path, rows=len(res.entries), truncated=res.truncated_scan,
            semantic_unavailable=bool(getattr(res, "semantic_unavailable", False)),
        )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - tracing never breaks the command
        log.error("search trace failed: %s", type(exc).__name__)


def _rename(args: str, ctx: CommandContext) -> CommandResult:
    if ctx.report_store is None:
        return CommandResult(STORE_UNAVAILABLE_TEXT)
    from opsfleet_agent.commands.report_actions import handle_rename

    try:
        return CommandResult(handle_rename(args, ctx, detector=ctx.detector))
    except Exception as exc:  # a store failure never crashes the REPL
        log.error("report rename failed: %s", type(exc).__name__)
        return CommandResult("Could not rename that report right now.")


def _export(args: str, ctx: CommandContext) -> CommandResult:
    if ctx.report_store is None:
        return CommandResult(STORE_UNAVAILABLE_TEXT)
    from opsfleet_agent.commands.report_actions import handle_export

    try:
        return CommandResult(handle_export(args, ctx, export_dir=ctx.export_dir))
    except Exception as exc:
        log.error("report export failed: %s", type(exc).__name__)
        return CommandResult("Could not export that report right now.")


RETRY_TURN_TEXT: Final = "retry report"
ERASE_INFO_TEXT: Final = (
    "Erasing your data is done by the support team, not from the chat: ask a maintainer to "
    "run the erase command for your user id. It removes your saved reports, preferences, "
    "feedback, quotas, sessions, local traces and export files, and keeps only a "
    "pseudonymous audit record. Nothing was deleted now."
)


def _retry(args: str, _ctx: CommandContext) -> CommandResult:
    """AC-21.15: one retry of the report phase, run by the graph as a fixed turn (no SQL)."""
    if args.strip():
        return CommandResult("Usage: /retry (re-runs the last failed report of this session)")
    return CommandResult("", turn=RETRY_TURN_TEXT)


def _erase(_args: str, _ctx: CommandContext) -> CommandResult:
    """D-222: information only. No chat path can erase a user (SEC-18)."""
    return CommandResult(ERASE_INFO_TEXT)


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
        Command(  # D-151a: developer/support tool (AC-16.2); may show sanitized SQL
            "/trace", "/trace [turn_id]", "Developer: show the debug trace of a turn.", _trace
        ),
        Command("/audit", "/audit [--session|--user]", "Show your audit events.", _audit),
        Command("/persona", "/persona", "Show the active persona version.", _persona),
        Command(
            "/prefs",
            "/prefs [set <key> <value>|note|reset]",
            "View or change your answer preferences.",
            _prefs,
        ),
        Command("/reports", "/reports [words]", "List your saved reports.", _reports),
        Command("/open", "/open <id|n|title>", "Open a saved report.", _open),
        Command(
            "/search",
            "/search <words> [tag:x]",
            "Search saved reports by words and meaning, best match first.",
            _search,
        ),
        Command(
            "/rename", '/rename <id|n|"title"> <new title>', "Rename a saved report.", _rename
        ),  # fmt: skip
        Command(
            "/export",
            "/export <id|n|title> [name.md]",
            "Write a saved report as Markdown to your data/exports folder (never overwrites).",
            _export,
        ),  # fmt: skip
        Command("/retry", "/retry", "Retry the last failed report (no new queries).", _retry),
        Command("/erase", "/erase", "How to have all your data erased (support only).", _erase),
    ]
    return {c.name: c for c in cmds}


COMMANDS: Final[dict[str, Command]] = _table()


def register_command(cmd: Command) -> None:
    """Add an optional command at startup (``/delete`` when the delete feature is on)."""
    COMMANDS[cmd.name] = cmd


def unregister_command(name: str) -> None:
    """Remove an optional command (feature off or failed closed). Missing names are fine."""
    COMMANDS.pop(name, None)


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
