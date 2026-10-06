"""Iteration 33: rename, Markdown export and retry report (AC-21.9, AC-21.12, AC-21.14, AC-21.15).

``/rename <id|n|title> <new title>`` and ``/export <id|n|title> [name.md]`` act on the caller's
OWN saved report in the CURRENT product scope only. Another user's report, a missing id and a
report saved under a scope the user no longer has all answer "not found" (no existence leak).
The ``rename_report`` and ``export_report`` tool functions (HLD §4.2 tool table) share the same
code path: :func:`do_rename` and :func:`do_export`.

Audit first (the HLD pattern of ``commands.access``): the ``report.renamed`` /
``report.exported`` row is written BEFORE the change; if that write fails nothing is changed.
If the change then fails, a best-effort ``failed`` row follows.

The new title is checked in code: at most :data:`MAX_TITLE_CHARS` characters, no control or
format characters, the secret scrub (``tracer.scrub_text``) and the output guard's PII scan.
An export never calls a model (it works with the LLM down): code builds the path under
``<data dir>/exports/<owner folder>/`` and refuses any other location (no separators, no ``..``,
no absolute path, the resolved file must sit directly in that folder). SQL is stripped (D-151a).
D-228: the owner folder name is a hash of the user id, neither folder may be a symlink, both
are set to 0700 on every export, and a file is created with ``O_EXCL``: an existing file is
never overwritten (a named export that exists is refused; a default name gets a ``-2`` ...
suffix).

``/retry`` is not a tool: it hands the fixed text "retry report" to the graph, which re-runs
only the report writer and verifier on this session's scrubbed ledger (no SQL, D-185).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import stat
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from opsfleet_agent.guards.output import check_output
from opsfleet_agent.guards.plain_language import strip_sql
from opsfleet_agent.obs import tracer as tr
from opsfleet_agent.reports.library import (
    AMBIGUOUS_HEAD,
    MAX_ID_CHARS,
    MAX_RESULTS,
    NO_ROW_TEXT,
    NOT_FOUND_TEXT,
    display_id,
    strip_display_prefix,
)
from opsfleet_agent.reports.matcher import MatchError, in_scope, owner_matches
from opsfleet_agent.session import default_data_dir
from opsfleet_agent.store.reports import ReportError, SavedReport

log = logging.getLogger(__name__)

__all__ = [
    "EXPORT_FAILED",
    "INVALID_ARGS",
    "MAX_TITLE_CHARS",
    "NOT_FOUND",
    "STORE_UNAVAILABLE",
    "do_export",
    "do_rename",
    "export_report",
    "exports_dir",
    "owner_export_dir",
    "owner_folder_name",
    "handle_export",
    "handle_rename",
    "rename_report",
    "validate_title",
]

MAX_TITLE_CHARS: Final = 120
EXPORTS_DIR_NAME: Final = "exports"
MAX_EXPORT_NAME_CHARS: Final = 100
#: How many ``-2`` ... suffixes a default export name may try before it gives up (D-228).
MAX_DEFAULT_NAME_TRIES: Final = 99
RENAMED: Final = "report.renamed"
EXPORTED: Final = "report.exported"

NOT_FOUND: Final = "NOT_FOUND"
INVALID_ARGS: Final = "INVALID_ARGS"
EXPORT_FAILED: Final = "EXPORT_FAILED"
STORE_UNAVAILABLE: Final = "STORE_UNAVAILABLE"

RENAME_USAGE: Final = 'Usage: /rename <report_id | row number | "title words"> <new title>'
EXPORT_USAGE: Final = "Usage: /export <report_id | row number | title words> [name.md]"
AUDIT_FAILED_TEXT: Final = "The audit record could not be written; nothing was changed."
UNAVAILABLE_TEXT: Final = "This command is unavailable right now (local store not open)."
_TEXT: Final = {
    NOT_FOUND: NOT_FOUND_TEXT,
    STORE_UNAVAILABLE: UNAVAILABLE_TEXT,
    EXPORT_FAILED: "The report could not be exported. Check the data directory.",
}

# A plain file name: letters, digits, "_", "-", "." inside, ending in ".md". No separators.
_EXPORT_NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,96}\.md", re.ASCII)


class ActionError(Exception):
    """A refused or failed action; ``code`` is the tool error code, ``text`` the user line."""

    def __init__(self, code: str, text: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.text = text or _TEXT.get(code, NOT_FOUND_TEXT)


@dataclass(frozen=True)
class Resolved:
    status: str  # "ok" | "not_found" | "ambiguous" | "no_row"
    report: SavedReport | None = None
    text: str = ""
    listing: tuple[str, ...] = ()


# --- shared checks -----------------------------------------------------------------------


def exports_dir(data_dir: Path | None = None) -> Path:
    """``<data dir>/exports``: the only directory an export may write to."""
    return Path(data_dir if data_dir is not None else default_data_dir()) / EXPORTS_DIR_NAME


def owner_folder_name(owner: str) -> str:
    """The per-owner folder under ``exports``: a hash of the user id, so the id is never a
    path component (D-228). ``commands.erase`` uses the same name to find a user's files."""
    return "u-" + hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:24]


def owner_export_dir(base: Path, owner: str) -> Path:
    return base / owner_folder_name(owner)


_SYMLINK_TEXT: Final = "Exports are written only to the exports folder (a folder is a symlink)."
_EXISTS_TEXT: Final = "A file with that name is already in your exports folder; pick another name."


def _private_dir(path: Path) -> None:
    """Create ``path`` (0700) if missing; refuse a symlink or a non-folder; re-apply 0700."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ActionError(INVALID_ARGS, _SYMLINK_TEXT)
    os.chmod(path, 0o700)


def prepare_owner_dir(base: Path, owner: str) -> Path:
    """``base`` and the owner's folder, both checked and 0700 (D-228). Raises ActionError."""
    try:
        base.parent.mkdir(parents=True, exist_ok=True)
        _private_dir(base)
        folder = owner_export_dir(base, owner)
        _private_dir(folder)
    except ActionError:
        raise
    except OSError as exc:
        log.error("export folder check failed: %s", type(exc).__name__)
        raise ActionError(EXPORT_FAILED) from None
    return folder


def _own_in_scope(store: Any, owner: str, scope: Any, report_id: str) -> SavedReport:
    """The owner's report in the current scope; anything else is NOT_FOUND (AC-21.5)."""
    rid = strip_display_prefix(str(report_id or "").strip())
    if not rid or len(rid) > MAX_ID_CHARS:
        raise ActionError(NOT_FOUND)
    rec = store.get(rid, owner)
    if rec is None or not in_scope(rec, scope):
        raise ActionError(NOT_FOUND)
    return rec


def resolve_report(
    store: Any, owner: str, scope: Any, ref: str, listing: Sequence[str] = ()
) -> Resolved:
    """Like ``/open``: a row number of the last listing, an id, or title words. Every path
    ends in the owner and scope check; a report from another scope is "not found" here."""
    ref = strip_display_prefix(" ".join(str(ref).split()))
    if not ref:
        return Resolved("not_found", text=NOT_FOUND_TEXT)
    if re.fullmatch(r"\d{1,2}", ref, re.ASCII):
        n = int(ref)
        if not 1 <= n <= len(listing):
            return Resolved("no_row", text=NO_ROW_TEXT)
        ref = listing[n - 1]
    elif " " in ref or len(ref) > MAX_ID_CHARS or store.get(ref, owner) is None:
        try:
            found = owner_matches(store, owner, scope, ref)
        except MatchError:
            return Resolved("not_found", text=NOT_FOUND_TEXT)
        rows = [r for r in found.rows if in_scope(r, scope)]
        if not rows:
            return Resolved("not_found", text=NOT_FOUND_TEXT)
        if len(rows) > 1:
            shown = rows[:MAX_RESULTS]
            lines = [AMBIGUOUS_HEAD] + [
                f"  {n}. {display_id(r.report_id)}  {r.created_at[:10]}  "
                f"{tr.scrub_text(' '.join(r.title.split()), MAX_TITLE_CHARS)}"
                for n, r in enumerate(shown, 1)
            ]
            return Resolved("ambiguous", text="\n".join(lines),
                            listing=tuple(r.report_id for r in shown))  # fmt: skip
        ref = rows[0].report_id
    try:
        return Resolved("ok", _own_in_scope(store, owner, scope, ref))
    except ActionError:
        return Resolved("not_found", text=NOT_FOUND_TEXT)


def validate_title(raw: Any, *, detector: Any = None) -> str:
    """The new title, or ``ActionError(INVALID_ARGS)``. Enforced in code, not in a prompt."""
    if not isinstance(raw, str):
        raise ActionError(INVALID_ARGS, "The new title must be text.")
    title = raw.strip()
    if not title:
        raise ActionError(INVALID_ARGS, "The new title is empty.")
    if len(title) > MAX_TITLE_CHARS:
        raise ActionError(
            INVALID_ARGS, f"The new title is too long (at most {MAX_TITLE_CHARS} characters)."
        )
    if any(unicodedata.category(c).startswith("C") for c in title):
        raise ActionError(INVALID_ARGS, "The new title has control characters.")
    if tr.scrub_text(title, MAX_TITLE_CHARS) != title:
        raise ActionError(INVALID_ARGS, "The new title looks like it holds a secret or an email.")
    verdict = _title_guard(title, detector)
    if not verdict.allowed or verdict.text != title:
        raise ActionError(INVALID_ARGS, "The new title was refused by the output guard.")
    return title


def _title_guard(title: str, detector: Any):
    return check_output(
        title, role="library_agent", label="library", tool_calls=("rename_report",),
        detector=detector,
    )  # fmt: skip


def _audit(audit: Any, event: str, *, owner: str, session_id: str, turn_id: str,
           report_id: str, outcome: str, error_type: str | None = None) -> None:  # fmt: skip
    audit.record(
        event, actor_user_id=owner, session_id=session_id, turn_id=turn_id,
        target_ids=[report_id], count=1, outcome=outcome,
        details={"error_type": error_type} if error_type else None,
    )  # fmt: skip


def _audit_first(audit: Any, event: str, **kw: Any) -> None:
    if audit is None:
        raise ActionError(STORE_UNAVAILABLE, AUDIT_FAILED_TEXT)
    try:
        _audit(audit, event, outcome="ok", **kw)
    except Exception as exc:  # noqa: BLE001 - audit first: no row, no change
        log.error("%s audit failed: %s", event, type(exc).__name__)
        raise ActionError(STORE_UNAVAILABLE, AUDIT_FAILED_TEXT) from None


def _audit_failed(audit: Any, event: str, exc: BaseException, **kw: Any) -> None:
    try:  # best effort: the 'ok' row is written but the change did not happen
        _audit(audit, event, outcome="failed", error_type=type(exc).__name__, **kw)
    except Exception:  # noqa: BLE001
        log.warning("%s 'failed' follow-up row could not be written", event)


# --- rename ------------------------------------------------------------------------------


def do_rename(
    *, store: Any, audit: Any, owner: str, scope: Any, session_id: str, turn_id: str,
    report_id: str, title: Any, detector: Any = None,
) -> SavedReport:  # fmt: skip
    """Rename one owned, in-scope report. Raises :class:`ActionError`."""
    if store is None:
        raise ActionError(STORE_UNAVAILABLE)
    new_title = validate_title(title, detector=detector)
    try:
        rec = _own_in_scope(store, owner, scope, report_id)
    except ActionError:
        raise
    except Exception as exc:  # noqa: BLE001 - a store failure is reported, never raised
        log.error("rename lookup failed: %s", type(exc).__name__)
        raise ActionError(STORE_UNAVAILABLE) from None
    kw = {"owner": owner, "session_id": session_id, "turn_id": turn_id,
          "report_id": rec.report_id}  # fmt: skip
    _audit_first(audit, RENAMED, **kw)

    def guard(text: str) -> tuple[bool, str]:
        v = _title_guard(text, detector)
        return v.allowed, v.text

    try:
        out = store.rename(rec.report_id, owner, new_title, guard)
        if out is None:
            raise ReportError("report not found at write time")
    except Exception as exc:  # noqa: BLE001
        _audit_failed(audit, RENAMED, exc, **kw)
        log.error("rename failed: %s", type(exc).__name__)
        if isinstance(exc, ReportError):
            raise ActionError(NOT_FOUND) from None
        raise ActionError(STORE_UNAVAILABLE) from None
    return out


# --- export ------------------------------------------------------------------------------


def export_path(base: Path, name: str | None, report_id: str) -> Path:
    """The target file inside ``base``; ``ActionError(INVALID_ARGS)`` for anything else."""
    fname = name if name is not None else f"{display_id(report_id)}.md"
    if (
        not isinstance(fname, str)
        or len(fname) > MAX_EXPORT_NAME_CHARS
        or ".." in fname
        or not _EXPORT_NAME_RE.fullmatch(fname)
    ):
        raise ActionError(
            INVALID_ARGS,
            "The export name must be a plain file name ending in .md (no folders); "
            "files are written only to the exports folder.",
        )
    root = base.resolve()
    target = (root / fname).resolve()
    if target.parent != root:
        raise ActionError(INVALID_ARGS, "Exports are written only to the exports folder.")
    if name is None:  # a default name never overwrites: R-<id>-2.md, R-<id>-3.md, ...
        n = 1
        while os.path.lexists(target):
            n += 1
            if n > MAX_DEFAULT_NAME_TRIES:
                raise ActionError(INVALID_ARGS, _EXISTS_TEXT)
            target = root / f"{display_id(report_id)}-{n}.md"
    elif os.path.lexists(root / fname):
        raise ActionError(INVALID_ARGS, _EXISTS_TEXT)
    return target


def render_export(rec: SavedReport) -> str:
    """Markdown for the file: current title, date and data window, then the body without
    SQL (D-151a). The body's own "# " title line gives way to the current title."""
    body = strip_sql(rec.body_markdown).strip("\n")
    lines = body.split("\n")
    if lines and lines[0].startswith("# "):
        body = "\n".join(lines[1:]).lstrip("\n")
    head = (
        f"# {rec.title}\n\n"
        f"Report {display_id(rec.report_id)}, created {rec.created_at[:10]}, "
        f"data window {' '.join(rec.data_window.split())}.\n"
    )
    return f"{head}\n{body}\n"


def _write_export(path: Path, text: str) -> None:
    """A new file only (``O_EXCL``, no symlink follow), 0600, fsync. A partial file is
    removed on failure. An existing file is never replaced (D-228)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise


def do_export(
    *, store: Any, audit: Any, owner: str, scope: Any, session_id: str, turn_id: str,
    report_id: str, name: str | None = None, export_dir: Path | None = None,
) -> tuple[SavedReport, Path]:  # fmt: skip
    """Export one owned, in-scope report as Markdown. No model call. Raises ActionError."""
    if store is None:
        raise ActionError(STORE_UNAVAILABLE)
    try:
        rec = _own_in_scope(store, owner, scope, report_id)
    except ActionError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("export lookup failed: %s", type(exc).__name__)
        raise ActionError(STORE_UNAVAILABLE) from None
    base = export_dir if export_dir is not None else exports_dir()
    target = export_path(prepare_owner_dir(base, owner), name, rec.report_id)
    kw = {"owner": owner, "session_id": session_id, "turn_id": turn_id,
          "report_id": rec.report_id}  # fmt: skip
    _audit_first(audit, EXPORTED, **kw)
    try:
        _write_export(target, render_export(rec))
    except Exception as exc:  # noqa: BLE001
        _audit_failed(audit, EXPORTED, exc, **kw)
        log.error("export failed: %s", type(exc).__name__)
        raise ActionError(EXPORT_FAILED) from None
    return rec, target


# --- tool functions (HLD §4.2) -------------------------------------------------------------


def _tool_error(err: ActionError) -> dict[str, Any]:
    return {"ok": False, "error": {"code": err.code, "message": err.text, "retryable": False}}


def rename_report(
    *, store: Any, audit: Any, owner: str, scope: Any, session_id: str, turn_id: str,
    report_id: Any, title: Any, detector: Any = None,
) -> dict[str, Any]:  # fmt: skip
    """``rename_report`` -> ``{report_id, title}``; NOT_FOUND, INVALID_ARGS, STORE_UNAVAILABLE."""
    if not isinstance(report_id, str):
        return _tool_error(ActionError(INVALID_ARGS, "report_id must be text."))
    try:
        rec = do_rename(
            store=store, audit=audit, owner=owner, scope=scope, session_id=session_id,
            turn_id=turn_id, report_id=report_id, title=title, detector=detector,
        )  # fmt: skip
    except ActionError as err:
        return _tool_error(err)
    return {"ok": True, "report_id": display_id(rec.report_id), "title": rec.title}


def export_report(
    *, store: Any, audit: Any, owner: str, scope: Any, session_id: str, turn_id: str,
    report_id: Any, export_dir: Path | None = None,
) -> dict[str, Any]:  # fmt: skip
    """``export_report`` -> ``{report_id, path}``; NOT_FOUND, EXPORT_FAILED, STORE_UNAVAILABLE.
    The path is built by code from the id, never from model text."""
    if not isinstance(report_id, str):
        return _tool_error(ActionError(NOT_FOUND))
    try:
        rec, path = do_export(
            store=store, audit=audit, owner=owner, scope=scope, session_id=session_id,
            turn_id=turn_id, report_id=report_id, export_dir=export_dir,
        )  # fmt: skip
    except ActionError as err:
        if err.code == INVALID_ARGS:  # not in this tool's error list: the id is unusable
            err = ActionError(NOT_FOUND)
        return _tool_error(err)
    return {"ok": True, "report_id": display_id(rec.report_id), "path": str(path)}


# --- command handlers ----------------------------------------------------------------------


def split_ref(args: str) -> tuple[str, str]:
    """``"title words" rest`` or ``ref rest``: a quoted first part is the ref, else one token."""
    s = (args or "").strip()
    if s[:1] in {'"', "'"}:
        end = s.find(s[0], 1)
        if end > 0:
            return s[1:end].strip(), s[end + 1 :].strip()
    ref, _, rest = s.partition(" ")
    return ref, rest.strip()


def _turn_id() -> str:
    return uuid.uuid4().hex[:12]


def _resolve(args_ref: str, ctx: Any) -> Resolved:
    res = resolve_report(ctx.report_store, ctx.user_id, ctx.scope, args_ref, ctx.listing)
    if res.status == "ambiguous":
        ctx.listing[:] = list(res.listing)
    return res


def handle_rename(args: str, ctx: Any, *, detector: Any = None) -> str:
    ref, title = split_ref(args)
    if not ref or not title:
        return RENAME_USAGE
    try:
        res = _resolve(ref, ctx)
        if res.report is None:
            return res.text
        rec = do_rename(
            store=ctx.report_store, audit=ctx.audit_log, owner=ctx.user_id, scope=ctx.scope,
            session_id=ctx.session_id, turn_id=_turn_id(), report_id=res.report.report_id,
            title=title, detector=detector,
        )  # fmt: skip
    except ActionError as err:
        return err.text
    return f"Renamed {display_id(rec.report_id)} to: {rec.title}"


def split_export_args(args: str) -> tuple[str, str | None]:
    """``<ref> [name.md]``: a last token ending in ".md" is the file name."""
    s = (args or "").strip()
    head, _, last = s.rpartition(" ")
    if head.strip() and last.lower().endswith(".md"):
        ref, _ = split_ref(head) if head.strip()[:1] in {'"', "'"} else (head.strip(), "")
        return ref, last
    if s[:1] in {'"', "'"}:
        return split_ref(s)[0], None
    return s, None


def handle_export(args: str, ctx: Any, *, export_dir: Path | None = None) -> str:
    ref, name = split_export_args(args)
    if not ref:
        return EXPORT_USAGE
    try:
        res = _resolve(ref, ctx)
        if res.report is None:
            return res.text
        rec, path = do_export(
            store=ctx.report_store, audit=ctx.audit_log, owner=ctx.user_id, scope=ctx.scope,
            session_id=ctx.session_id, turn_id=_turn_id(), report_id=res.report.report_id,
            name=name, export_dir=export_dir,
        )  # fmt: skip
    except ActionError as err:
        return err.text
    return f"Exported {display_id(rec.report_id)} to {path}"
