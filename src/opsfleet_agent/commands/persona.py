"""Persona change and rollback (FR-52, AC-27.4; HLD §6.8). Plain functions; wired in 19.

``apply_persona`` validates a candidate file with ``persona.parse_persona``, runs a smoke check
through an injected callable (the real smoke subset of the eval suite is live and runs in
``evals/``; unit tests inject a fake), writes a ``persona.changed`` audit row BEFORE touching
the active file, then switches. If the audit write fails the change is aborted and nothing
changes. A refused change (invalid file, failed smoke, no change) is audited best-effort as
``outcome=refused``; the refusal stands even if that refusal row cannot be written.

``rollback_persona`` restores the newest entry of a bounded history of previous versions, with
the same audit-first order. The history keeps at most ``MAX_HISTORY`` files; the oldest is
dropped. The smoke check is not re-run on rollback (a rollback must work when the smoke runner
itself is the problem); the row records ``smoke=skipped``.

Audit rows hold versions (``<file version>-<hash8>``), the actor id, the smoke result and a
reason code, never the persona body. Logs carry the same, never the body. The switch is an
atomic replace of the active file, which ``PersonaStore`` hot-reloads on the next turn.

``apply_persona`` / ``rollback_persona`` are the ONLY supported way to change the active persona.
Editing the active file directly goes live through ``PersonaStore`` without an audit row (OD-8,
HLD §6.8 deviation). When the active file is invalid, ``from_version`` and the archive source
fall back to the built-in default although ``PersonaStore`` still serves the last valid version
(OD-9). Calls are serialised across processes with a ``flock`` on ``history_dir/.lock``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

from opsfleet_agent.persona import (
    MAX_PERSONA_BYTES,
    Persona,
    PersonaInvalid,
    builtin_persona,
    parse_persona,
)
from opsfleet_agent.store.audit import AuditError, AuditLog

log = logging.getLogger(__name__)

MAX_HISTORY = 10
EVENT = "persona.changed"
SmokeCheck = Callable[[Persona], bool]

_ENTRY_RE = re.compile(r"(\d{8,})-[A-Za-z0-9._-]{1,48}\.md")

APPLIED = "Persona updated to {version}."
ROLLED_BACK = "Persona rolled back to {version}."
UNCHANGED = "Persona unchanged: the candidate is identical to the active version."
NO_HISTORY = "No previous persona version to roll back to."
AUDIT_FAILED = "Persona not changed: the audit log is unavailable."
SMOKE_FAILED = "Persona refused: the smoke check failed."


@dataclass(frozen=True)
class PersonaChange:
    ok: bool
    message: str
    from_version: str
    to_version: str | None = None
    reason: str | None = None  # short code; safe to log


def default_history_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "persona_history"


def _current(active_path: Path) -> tuple[Persona, bytes | None]:
    """The active persona and its raw bytes (None when it is the built-in default)."""
    try:
        if active_path.stat().st_size <= MAX_PERSONA_BYTES:
            raw = active_path.read_bytes()
            return parse_persona(raw), raw
    except (OSError, PersonaInvalid):
        pass
    return builtin_persona(), None


def _seq(path: Path) -> int:
    return int(path.name.split("-", 1)[0])


def _entries(history_dir: Path) -> list[Path]:
    """All history files, oldest first (numeric sequence order)."""
    try:
        names = [n for n in os.listdir(history_dir) if _ENTRY_RE.fullmatch(n)]
    except OSError:
        return []
    return sorted((history_dir / n for n in names), key=lambda p: (_seq(p), p.name))


def _next_seq(history_dir: Path) -> int:
    entries = _entries(history_dir)
    return _seq(entries[-1]) + 1 if entries else 1


def _read_entry(entry: Path) -> tuple[Persona, bytes] | None:
    """A valid history entry, or None for a symlink, non-regular, oversized or invalid one."""
    try:
        if not stat.S_ISREG(entry.lstat().st_mode):
            return None
        with entry.open("rb") as f:
            raw = f.read(MAX_PERSONA_BYTES + 1)
        return parse_persona(raw), raw
    except (OSError, PersonaInvalid):
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o644 & ~umask
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".persona-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _locked(history_dir: Path) -> Iterator[None]:
    """Serialise apply/rollback across processes (POSIX flock; a no-op elsewhere)."""
    if fcntl is None:
        yield
        return
    try:
        history_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(history_dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        log.warning("persona_lock_unavailable")
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _archive(history_dir: Path, current: Persona, raw: bytes) -> None:
    """Archive the current version; skip when the newest entry already holds it (retry)."""
    entries = _entries(history_dir)
    if entries:
        newest = _read_entry(entries[-1])
        if newest is not None and newest[0].content_hash == current.content_hash:
            return
    _atomic_write(history_dir / f"{_next_seq(history_dir):08d}-{current.version}.md", raw)
    entries = _entries(history_dir)
    for old in entries[: max(0, len(entries) - MAX_HISTORY)]:
        old.unlink(missing_ok=True)


def _audit(
    audit: AuditLog,
    *,
    actor: str,
    session_id: str | None,
    turn_id: str | None,
    outcome: str,
    from_version: str,
    to_version: str | None,
    smoke: str,
    reason: str | None = None,
) -> None:
    details = {"from_version": from_version, "to_version": to_version, "smoke": smoke}
    if reason:
        details["reason"] = reason
    event = audit.record(
        EVENT,
        actor_user_id=actor,
        session_id=session_id or uuid.uuid4().hex,
        turn_id=turn_id or uuid.uuid4().hex[:12],
        outcome=outcome,
        details=details,
    )
    if event is None:  # ignored as a duplicate: there is no row, so no change
        raise AuditError("audit row not written")


def _audit_write_failed(
    audit: AuditLog, from_version: str, to_version: str, smoke: str, **ctx: object
) -> None:
    """Best-effort second row so the audit trail does not say 'ok' for a change that failed."""
    try:
        _audit(
            audit, outcome="failed", from_version=from_version, to_version=to_version,
            smoke=smoke, reason="write_failed", **ctx,  # type: ignore[arg-type]
        )  # fmt: skip
    except AuditError:
        log.warning("persona_write_failure_not_audited")


def _refuse(
    audit: AuditLog, message: str, reason: str, current: Persona, to: str | None, **ctx: object
) -> PersonaChange:
    try:
        _audit(
            audit,
            outcome="refused",
            from_version=current.version,
            to_version=to,
            reason=reason,
            **ctx,  # type: ignore[arg-type]
        )
    except AuditError:
        log.warning("persona_refusal_not_audited reason=%s", reason)
    log.info("persona_change_refused reason=%s", reason)
    return PersonaChange(False, message, current.version, to, reason)


def _run_smoke(smoke: SmokeCheck, persona: Persona) -> bool:
    try:
        return smoke(persona) is True
    except Exception:  # noqa: BLE001 - a crashing smoke check is a failed one
        return False


def apply_persona(
    candidate_path: str | Path,
    *,
    active_path: str | Path,
    history_dir: str | Path,
    audit: AuditLog,
    actor: str,
    smoke: SmokeCheck,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> PersonaChange:
    """Validate, smoke-check, audit, then switch the active persona file to the candidate."""
    active, hist = Path(active_path), Path(history_dir)
    with _locked(hist):
        return _apply(candidate_path, active, hist, audit, actor, smoke, session_id, turn_id)


def _apply(
    candidate_path: str | Path,
    active: Path,
    hist: Path,
    audit: AuditLog,
    actor: str,
    smoke: SmokeCheck,
    session_id: str | None,
    turn_id: str | None,
) -> PersonaChange:
    ctx = {"actor": actor, "session_id": session_id, "turn_id": turn_id}
    current, current_raw = _current(active)
    try:
        with Path(candidate_path).open("rb") as f:
            raw = f.read(MAX_PERSONA_BYTES + 1)
        candidate = parse_persona(raw)
    except PersonaInvalid as exc:
        return _refuse(
            audit, f"Persona refused: invalid file ({exc.reason}).", exc.reason, current,
            None, smoke="skipped", **ctx,
        )  # fmt: skip
    except OSError:
        return _refuse(
            audit, "Persona refused: the file cannot be read.", "unreadable", current,
            None, smoke="skipped", **ctx,
        )  # fmt: skip
    if candidate.content_hash == current.content_hash:
        return _refuse(
            audit, UNCHANGED, "unchanged", current, candidate.version, smoke="skipped", **ctx
        )
    if not _run_smoke(smoke, candidate):
        return _refuse(
            audit, SMOKE_FAILED, "smoke_failed", current, candidate.version, smoke="fail", **ctx
        )
    try:  # audit first: no row, no change
        _audit(
            audit, outcome="ok", from_version=current.version, to_version=candidate.version,
            smoke="pass", **ctx,
        )  # fmt: skip
    except AuditError:
        log.warning("persona_change_aborted reason=audit_unavailable")
        return PersonaChange(
            False, AUDIT_FAILED, current.version, candidate.version, "audit_failed"
        )
    try:
        if current_raw is not None:
            _archive(hist, current, current_raw)
        _atomic_write(active, raw)
    except OSError:
        log.warning("persona_change_failed reason=write_failed")
        _audit_write_failed(audit, current.version, candidate.version, "pass", **ctx)
        return PersonaChange(
            False, "Persona not changed: the file could not be written.",
            current.version, candidate.version, "write_failed",
        )  # fmt: skip
    log.info("persona_changed from=%s to=%s", current.version, candidate.version)
    return PersonaChange(
        True, APPLIED.format(version=candidate.version), current.version, candidate.version
    )


def rollback_persona(
    *,
    active_path: str | Path,
    history_dir: str | Path,
    audit: AuditLog,
    actor: str,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> PersonaChange:
    """Restore the newest previous valid version (audit first). Clean refusal with none."""
    active, hist = Path(active_path), Path(history_dir)
    with _locked(hist):
        return _rollback(active, hist, audit, actor, session_id, turn_id)


def _rollback(
    active: Path,
    hist: Path,
    audit: AuditLog,
    actor: str,
    session_id: str | None,
    turn_id: str | None,
) -> PersonaChange:
    ctx = {"actor": actor, "session_id": session_id, "turn_id": turn_id}
    current, _ = _current(active)
    target: Persona | None = None
    raw = b""
    target_entry: Path | None = None
    skipped: list[Path] = []  # damaged or identical-to-active entries newer than the target
    entries = _entries(hist)
    for entry in reversed(entries[-4 * MAX_HISTORY :]):  # bounded scan
        got = _read_entry(entry)
        if got is None or got[0].content_hash == current.content_hash:
            skipped.append(entry)
            continue
        target, raw = got
        target_entry = entry
        break
    if target is None or target_entry is None:
        return _refuse(audit, NO_HISTORY, "no_history", current, None, smoke="skipped", **ctx)
    try:
        _audit(
            audit, outcome="ok", from_version=current.version, to_version=target.version,
            smoke="skipped", **ctx,
        )  # fmt: skip
    except AuditError:
        log.warning("persona_rollback_aborted reason=audit_unavailable")
        return PersonaChange(False, AUDIT_FAILED, current.version, target.version, "audit_failed")
    try:
        _atomic_write(active, raw)
    except OSError:
        log.warning("persona_rollback_failed reason=write_failed")
        _audit_write_failed(audit, current.version, target.version, "skipped", **ctx)
        return PersonaChange(
            False, "Persona not rolled back: the file could not be written.",
            current.version, target.version, "write_failed",
        )  # fmt: skip
    for entry in [*skipped, target_entry]:  # only now that the audit row and switch succeeded
        with contextlib.suppress(OSError):
            entry.unlink(missing_ok=True)
    log.info("persona_rolled_back from=%s to=%s", current.version, target.version)
    return PersonaChange(
        True, ROLLED_BACK.format(version=target.version), current.version, target.version
    )
