"""User erasure for maintainers (iteration 35; SEC-18, AC-28.6, FR-59; D-222..D-226).

``python -m opsfleet_agent.commands.erase --as <maintainer> --user <id>`` previews what an
erasure of one user would remove (per-store counts, never content) and prints a confirmation
token. The token is an HMAC over (maintainer, user, plan digest, expiry) keyed by a random
secret kept in the app store's ``meta`` table; it expires after :data:`CONFIRM_TTL_S`.

``... --user <id> --confirm <token> --retype <id>`` executes it:

1. Refusals before any write: the actor must be on the maintainer allowlist (the same file as
   triage, D-216), ids must be well formed, the maintainer cannot erase themselves, the token
   must match the CURRENT plan and not be expired, ``--retype`` must repeat ``--user``, the
   checkpoint store (when it exists) must open with the configured key, and every bounded
   scan must stay under its bound.
2. ``store.audit.audited_erase``: ``erase.executed`` first (pseudonymous), then every app.db
   row of the user in ONE transaction, the user's audit rows pseudonymised; if the audit write
   fails nothing is deleted.
3. After COMMIT: checkpoint threads owned by the user, the user's local trace files, export
   files of the user's reports and golden candidates built from the user's feedback. Each
   failing store appends ``erase.failed`` and the command exits 1.

Things that cannot be erased from here are LISTED, never silently skipped: regression-case
drafts in the repository, a profile override entry, ``config/profiles.yaml``, undecryptable
checkpoint threads and remote (Langfuse) traces.

There is no chat path: the REPL ``/erase`` only explains this process (D-222), so a normal
user session can never erase anyone.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TextIO

from opsfleet_agent.commands.triage import CANDIDATES_DIR, CASES_DIR, load_maintainers
from opsfleet_agent.session import default_data_dir
from opsfleet_agent.store.audit import (
    ACTOR_ID_RE,
    AuditError,
    AuditLog,
    EraseError,
    ErasePlan,
    audited_erase,
    erase_plan,
    record_erase_failure,
)

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2
CONFIRM_TTL_S: Final = 300
CONFIRM_KEY_META: Final = "erase_confirm_key"
#: Bounds on every scan outside app.db; over a bound the erase is refused before any write.
MAX_THREADS: Final = 10_000
MAX_FILES: Final = 50_000
HEADER_BYTES: Final = 8192
EXPORTS_DIR: Final = "exports"
TRACES_DIR: Final = "traces"

REFUSED_ROLE: Final = "Refused: erasure is for maintainers only. Pass --as <maintainer id>."
REFUSED_USER: Final = "Refused: --user must be a valid user id."
REFUSED_SELF: Final = "Refused: a maintainer cannot erase their own account."
REFUSED_TOKEN: Final = (
    "Refused: the confirmation token is invalid, expired or for another plan. "
    "Run the preview again."
)
REFUSED_RETYPE: Final = "Refused: --retype must repeat the --user id exactly."
NOT_COVERED: Final = (
    "Not erased from here: remote trace copies (Langfuse; delete them in Langfuse), "
    "backups of the data dir, and the user's entry in config/profiles.yaml."
)

_EXPORT_HEAD_RE: Final = re.compile(r"\nReport R-([A-Za-z0-9]{1,64}),")
_CANDIDATE_HEAD_RE: Final = re.compile(r"^# Golden candidate from feedback ([A-Za-z0-9]{1,64}) ")


class EraseRefused(Exception):
    """A refusal before any write (exit 2)."""


# --- confirmation token (D-222) ----------------------------------------------------------


def _confirm_key(conn: sqlite3.Connection) -> bytes:
    """The random HMAC key for confirmation tokens (created once, never printed)."""
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
        (CONFIRM_KEY_META, secrets.token_hex(32)),
    )
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (CONFIRM_KEY_META,)).fetchone()
    return bytes.fromhex(row[0])


def _mac(key: bytes, actor: str, user: str, digest: str, expires: int) -> str:
    msg = f"{actor}|{user}|{digest}|{expires}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def make_token(key: bytes, actor: str, user: str, digest: str, now: float) -> str:
    expires = int(now) + CONFIRM_TTL_S
    return f"{expires}.{_mac(key, actor, user, digest, expires)}"


def check_token(key: bytes, token: str, actor: str, user: str, digest: str, now: float) -> bool:
    m = re.fullmatch(r"(\d{1,12})\.([0-9a-f]{32})", token or "")
    if m is None:
        return False
    expires = int(m.group(1))
    if now > expires or expires - now > CONFIRM_TTL_S:
        return False
    return hmac.compare_digest(m.group(2), _mac(key, actor, user, digest, expires))


# --- stores outside app.db ---------------------------------------------------------------


@dataclass
class OutsidePlan:
    """What the erasure removes outside app.db, collected read-only before any write."""

    checkpoint_threads: list[str] = field(default_factory=list)
    undecryptable_threads: int = 0
    files: dict[str, list[Path]] = field(default_factory=dict)  # store -> paths
    listed: list[str] = field(default_factory=list)  # human action needed, never deleted

    def counts(self) -> dict[str, int]:
        out = {"checkpoints": len(self.checkpoint_threads)}
        out.update({k: len(v) for k, v in self.files.items()})
        return out


def _scan_dir(path: Path, pattern: str) -> list[Path]:
    if not path.is_dir():
        return []
    out: list[Path] = []
    for i, p in enumerate(path.glob(pattern)):
        if i >= MAX_FILES:
            raise EraseRefused(f"Refused: more than {MAX_FILES} files in {path.name}.")
        if p.is_file() and not p.is_symlink():
            out.append(p)
    return sorted(out)


def _head(path: Path) -> str:
    with path.open("rb") as fh:
        return fh.read(HEADER_BYTES).decode("utf-8", "replace")


def open_checkpoints(data_dir: Path, env: Mapping[str, str] | None) -> Any | None:
    """The checkpoint saver, or None when the store was never created (it is not created
    here). A missing or wrong key is a refusal: erasing without it would miss threads."""
    from opsfleet_agent.graph.graph import CHECKPOINT_FILE, build_checkpointer

    if not (data_dir / CHECKPOINT_FILE).exists():
        return None
    from opsfleet_agent.config import ConfigError

    try:
        return build_checkpointer(data_dir, env)
    except ConfigError as exc:
        raise EraseRefused(f"Refused: the checkpoint store cannot be read ({exc}).") from None


def _thread_owner(saver: Any, thread_id: str) -> tuple[bool, Any]:
    """(readable, owner) of a thread's latest checkpoint."""
    try:
        tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
    except Exception:  # noqa: BLE001 - written under another key or corrupt
        return False, None
    if tup is None:
        return True, None
    values = (tup.checkpoint or {}).get("channel_values") or {}
    return True, values.get("owner")


def plan_outside(
    plan: ErasePlan,
    data_dir: Path,
    *,
    saver: Any | None,
    trace_dir: Path,
    cases_dir: Path,
) -> OutsidePlan:
    out = OutsidePlan()
    user, sessions = plan.user_id, set(plan.session_ids)
    if saver is not None:
        rows = saver.conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints LIMIT ?", (MAX_THREADS + 1,)
        ).fetchall()
        if len(rows) > MAX_THREADS:
            raise EraseRefused(f"Refused: more than {MAX_THREADS} checkpoint threads.")
        for (tid,) in rows:
            readable, owner = _thread_owner(saver, str(tid))
            if not readable:
                out.undecryptable_threads += 1
            elif owner == user or (not owner and str(tid) in sessions):
                out.checkpoint_threads.append(str(tid))
    safe = {re.sub(r"[^A-Za-z0-9_-]", "_", s)[:64] + ".jsonl" for s in sessions}
    out.files["traces"] = [p for p in _scan_dir(trace_dir, "*.jsonl") if p.name in safe]
    reports = set(plan.report_ids)
    exports: list[Path] = []
    for p in _scan_dir(data_dir / EXPORTS_DIR, "*.md"):
        stem = p.name[2:-3] if p.name.startswith("R-") else None
        if stem in reports or any(m in reports for m in _EXPORT_HEAD_RE.findall(_head(p))):
            exports.append(p)
    out.files["exports"] = exports
    feedback = set(plan.feedback_ids)
    candidates: list[Path] = []
    for p in _scan_dir(data_dir / CANDIDATES_DIR, "*.yaml"):
        m = _CANDIDATE_HEAD_RE.match(_head(p))
        if m and m.group(1) in feedback:
            candidates.append(p)
    out.files["golden_candidates"] = candidates
    drafts = {f"triage_{fid[:12]}.yaml" for fid in feedback}
    for p in _scan_dir(cases_dir, "triage_*.yaml"):
        if p.name in drafts:
            out.listed.append(f"regression-case draft in the repository: {p.name} (remove by hand)")
    try:
        from opsfleet_agent.commands.access import _read_overrides, overrides_path

        if user in _read_overrides(overrides_path(data_dir)):
            out.listed.append(
                "profile override entry: restore the user's scope with the access command"
            )
    except Exception:  # noqa: BLE001 - unreadable override file: tell the maintainer
        out.listed.append("profile override file could not be read; check it by hand")
    if out.undecryptable_threads:
        out.listed.append(
            f"{out.undecryptable_threads} checkpoint thread(s) cannot be decrypted with the "
            "current key; their owner is unknown, so they are kept"
        )
    return out


def _delete_files(paths: Iterable[Path]) -> tuple[int, list[Path], str | None]:
    done, failed, err = 0, [], None
    for p in paths:
        try:
            p.unlink(missing_ok=True)
            done += 1
        except OSError as exc:
            failed.append(p)
            err = err or type(exc).__name__
    return done, failed, err


def _delete_threads(saver: Any, threads: Sequence[str]) -> tuple[int, str | None]:
    done = 0
    try:
        saver.conn.execute("PRAGMA secure_delete=ON")
        for tid in threads:
            saver.delete_thread(tid)
            done += 1
        saver.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:  # noqa: BLE001 - reported and audited by the caller
        return done, type(exc).__name__
    return done, None


# --- command -----------------------------------------------------------------------------


def _print_counts(out: TextIO, plan: ErasePlan, outside: OutsidePlan) -> None:
    print("app.db rows:", file=out)
    for table, n in sorted(plan.counts.items()):
        if table != "audit_event":
            print(f"  {table:<24} {n}", file=out)
    print(f"  {'audit_event':<24} {plan.counts.get('audit_event', 0)} (pseudonymised)", file=out)
    print("other stores:", file=out)
    for store, n in sorted(outside.counts().items()):
        print(f"  {store:<24} {n}", file=out)
    for line in outside.listed:
        print(f"  needs a person: {line}", file=out)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m opsfleet_agent.commands.erase",
        description="Erase all local data of one user (maintainers only; SEC-18).",
    )
    ap.add_argument("--as", dest="actor", required=True, help="maintainer id (maintainers.yaml)")
    ap.add_argument("--user", required=True, help="the user id to erase")
    ap.add_argument("--confirm", help="the token printed by the preview")
    ap.add_argument("--retype", help="the --user id again, to confirm")
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--trace-dir", type=Path, default=None)
    return ap


def main(
    argv: Sequence[str] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    maintainers: frozenset[str] | None = None,
    env: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.time,
    cases_dir: Path | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    allowed = load_maintainers() if maintainers is None else maintainers
    if not ACTOR_ID_RE.fullmatch(args.actor or "") or args.actor not in allowed:
        print(REFUSED_ROLE, file=out)
        return EXIT_REFUSED
    if not ACTOR_ID_RE.fullmatch(args.user or ""):
        print(REFUSED_USER, file=out)
        return EXIT_REFUSED
    if args.user == args.actor:
        print(REFUSED_SELF, file=out)
        return EXIT_REFUSED
    data_dir = args.data_dir or default_data_dir()
    trace_dir = args.trace_dir or data_dir / TRACES_DIR
    own = conn is None
    saver = None
    try:
        if conn is None:
            from opsfleet_agent.store.db import open_store

            conn = open_store(data_dir / "app.db")
        log = AuditLog(conn, clock=clock)
        saver = open_checkpoints(data_dir, os.environ if env is None else env)
        plan = erase_plan(log, args.user)
        outside = plan_outside(
            plan, data_dir, saver=saver, trace_dir=trace_dir, cases_dir=cases_dir or CASES_DIR
        )
        key = _confirm_key(conn)
        if args.confirm is None:
            print(f"Erasure preview for user {args.user!r} (nothing deleted yet):", file=out)
            _print_counts(out, plan, outside)
            print(NOT_COVERED, file=out)
            token = make_token(key, args.actor, args.user, plan.digest(), clock())
            print(
                f"To erase, within {CONFIRM_TTL_S // 60} minutes run the same command with\n"
                f"  --confirm {token} --retype <the user id>",
                file=out,
            )
            return EXIT_OK
        if not check_token(key, args.confirm, args.actor, args.user, plan.digest(), clock()):
            raise EraseRefused(REFUSED_TOKEN)
        if args.retype != args.user:
            raise EraseRefused(REFUSED_RETYPE)
        return _execute(log, args.actor, plan, outside, saver, out)
    except EraseRefused as exc:
        print(str(exc), file=out)
        return EXIT_REFUSED
    except EraseError as exc:
        print(f"Refused: {exc}. Nothing was deleted.", file=out)
        return EXIT_REFUSED
    except AuditError as exc:
        print(f"Erase aborted: {exc}. Nothing was deleted.", file=out)
        return EXIT_FAIL
    finally:
        if saver is not None:
            saver.conn.close()
        if own and conn is not None:
            conn.close()


def _record(log: AuditLog, event: Any, store: str, err: str) -> None:
    try:  # the rows are already gone; a lost erase.failed must not hide the printed failure
        record_erase_failure(log, event, store=store, error_type=err)
    except AuditError:
        pass


def _execute(
    log: AuditLog,
    actor: str,
    plan: ErasePlan,
    outside: OutsidePlan,
    saver: Any | None,
    out: TextIO,
) -> int:
    done = audited_erase(log, actor_user_id=actor, user_id=plan.user_id,
                         expected_digest=plan.digest())  # fmt: skip
    print(f"Erased user data; audit record {done.event.event_id} ({done.pseudonym}).", file=out)
    for table, n in sorted(done.deleted.items()):
        print(f"  {table:<24} {n} deleted", file=out)
    print(f"  {'audit_event':<24} {done.audit_rows} pseudonymised", file=out)
    failed = False
    if saver is not None:
        n, err = _delete_threads(saver, outside.checkpoint_threads)
        print(f"  {'checkpoints':<24} {n} thread(s) deleted", file=out)
        if err:
            failed = True
            _record(log, done.event, "checkpoints", err)
            print(f"  checkpoints FAILED ({err}); delete the remaining threads by hand", file=out)
    for store, paths in sorted(outside.files.items()):
        n, left, err = _delete_files(paths)
        print(f"  {store:<24} {n} file(s) deleted", file=out)
        if left:
            failed = True
            _record(log, done.event, store, err or "OSError")
            print(f"  {store} FAILED ({err}); remove by hand:", file=out)
            for p in left:
                print(f"    {p.name}", file=out)
    for line in outside.listed:
        print(f"  needs a person: {line}", file=out)
    print(NOT_COVERED, file=out)
    return EXIT_FAIL if failed else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
