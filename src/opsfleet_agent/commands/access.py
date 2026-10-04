"""`access set <user> --brands ... | --all` (FR-08, AC-20.6, A-31). Plain function; wired in 19.

The shipped ``config/profiles.yaml`` is never rewritten. A change goes to an override file
under the data dir (``profile_overrides.json``). :func:`load_profiles_with_overrides` is what
session start calls: it merges the overrides over the YAML profiles, so a change takes effect
at the user's NEXT session. A live ``Session`` holds a frozen ``Profile`` (frozen dataclasses),
so the running session keeps its scope snapshot.

Order of work: validate everything (flags, user, brands against the catalog), write the
``scope.changed`` audit record, and only then persist. If the audit write fails nothing is
persisted. A corrupt or unreadable override file fails closed: loading refuses to start, and
``access_set`` refuses to write, rather than falling back to the YAML scope.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from opsfleet_agent.config import ConfigError
from opsfleet_agent.session import (
    Profile,
    _parse_profile,
    default_data_dir,
    load_profiles,
    validate_scope_against_catalog,
)
from opsfleet_agent.store.audit import SCOPE_INVALID, AuditLog

log = logging.getLogger(__name__)

OVERRIDES_FILE = "profile_overrides.json"
OVERRIDES_VERSION = 1
MAX_OVERRIDE_BYTES = 1_000_000
MAX_BRANDS = 200
SCOPE_CHANGED = "scope.changed"


class AccessError(ConfigError):
    """A refused access change or an unusable override file. Nothing was written."""


@dataclasses.dataclass(frozen=True)
class AccessChange:
    user_id: str
    old: Profile
    new: Profile


def overrides_path(data_dir: Path | None = None) -> Path:
    return (data_dir or default_data_dir()) / OVERRIDES_FILE


def _entry(profile: Profile) -> dict[str, Any]:
    if profile.all_products:
        return {"all_products": True}
    return {"brands": list(profile.brands)}


def _q(value: object, limit: int = 40) -> str:
    """A user id for a message: repr (escapes control characters), truncated."""
    text = str(value)
    return repr(text[:limit] + ("..." if len(text) > limit else ""))


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _read_bounded(path: Path) -> bytes | None:
    """Bytes of a regular file (None if missing). O_NONBLOCK so a FIFO cannot hang the open."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = f.read(MAX_OVERRIDE_BYTES + 1)
    if len(data) > MAX_OVERRIDE_BYTES:
        raise ValueError("too large")
    return data


def _read_overrides(path: Path) -> dict[str, dict[str, Any]]:
    """Raw override entries; a missing file is no overrides, anything else wrong is fatal."""
    try:
        data = _read_bounded(path)
        if data is None:
            return {}
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        if (
            not isinstance(raw, dict)
            or set(raw) != {"version", "overrides"}
            or type(raw["version"]) is not int
            or raw["version"] != OVERRIDES_VERSION
            or not isinstance(raw["overrides"], dict)
        ):
            raise ValueError("bad shape")
        return raw["overrides"]
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise AccessError(
            f"{path.name} cannot be read or is invalid. Fix or remove it "
            "(removing it restores the profiles.yaml scopes), then retry."
        ) from None


def _apply(base: Profile, entry: Any, name: str) -> Profile:
    if not isinstance(entry, dict) or set(entry) not in ({"brands"}, {"all_products"}):
        raise AccessError(f"{name} is invalid: bad entry for user {_q(base.user_id)}.")
    brands = entry.get("brands")
    if isinstance(brands, list) and len(brands) > MAX_BRANDS:
        raise AccessError(f"{name} is invalid: too many brands for user {_q(base.user_id)}.")
    merged = {"id": base.user_id, "display_name": base.display_name, **entry}
    try:
        return _parse_profile(merged, name)
    except ConfigError:
        raise AccessError(f"{name} is invalid: bad scope for user {_q(base.user_id)}.") from None


def load_profiles_with_overrides(
    profiles_path: Path | None = None, data_dir: Path | None = None
) -> dict[str, Profile]:
    """Profiles from YAML with the access overrides applied. Call at session start (16/19)."""
    profiles = load_profiles(profiles_path)
    path = overrides_path(data_dir)
    overrides = _read_overrides(path)
    if len(overrides) > len(profiles):
        raise AccessError(f"{path.name} is invalid: more entries than users.")
    out = dict(profiles)
    for uid, entry in overrides.items():
        if uid not in profiles:
            raise AccessError(f"{path.name} is invalid: unknown user {_q(uid)}.")
        out[uid] = _apply(profiles[uid], entry, path.name)
    return out


def _atomic_write(path: Path, overrides: dict[str, dict[str, Any]]) -> None:
    payload = json.dumps({"version": OVERRIDES_VERSION, "overrides": overrides}, sort_keys=True)
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".overrides-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        with contextlib.suppress(OSError):  # best effort: not every filesystem syncs a directory
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except OSError:
        raise AccessError(f"{path.name} could not be written. Check the data dir.") from None
    finally:
        if tmp_name:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def all_str(items: list[Any]) -> bool:
    return all(isinstance(b, str) for b in items)


def _old_scope_value(old: Profile, known: list[str]) -> str | list[str]:
    """The old scope for the audit row, or the ``invalid`` marker if it is stale or oversized."""
    if len(old.brands) > MAX_BRANDS:
        return SCOPE_INVALID
    try:
        validate_scope_against_catalog(old, known)
    except ConfigError:
        return SCOPE_INVALID
    return _scope_value(old)


def _scope_value(profile: Profile) -> str | list[str]:
    return "all" if profile.all_products else list(profile.brands)


def access_set(
    user: str,
    *,
    brands: Iterable[str] | None = None,
    all: bool = False,  # noqa: A002 - the CEO flag, named as in the CLI
    actor: str,
    catalog: Callable[[], Iterable[str]],
    audit: AuditLog,
    profiles_path: Path | None = None,
    data_dir: Path | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> AccessChange:
    """Set `user`'s scope for their next session. Raises AccessError; nothing is written then.

    ``catalog`` returns the known ``products.brand`` values (injected; BigQuery in production).
    Brands are matched exactly and case-sensitively, like profiles.yaml (iteration 16).
    """
    if not isinstance(all, bool):
        raise AccessError("all must be true or false.")
    if isinstance(brands, str | bytes):
        raise AccessError("brands must be a list of brand names, not a string.")
    brand_list = None if brands is None else list(brands)
    if brand_list is not None and not all_str(brand_list):
        raise AccessError("Every brand must be a string.")
    if all and brand_list is not None:
        raise AccessError("Give either a brand list or --all, not both.")
    if not all and not brand_list:
        raise AccessError("Give a brand list or --all.")
    if brand_list is not None and len(brand_list) > MAX_BRANDS:
        raise AccessError(f"Too many brands (max {MAX_BRANDS}).")

    current = load_profiles_with_overrides(profiles_path, data_dir)
    old = current.get(user)
    if old is None:
        raise AccessError(f"Unknown user {_q(user)}. Valid users: {', '.join(sorted(current))}.")

    entry = {"all_products": True} if all else {"brands": brand_list}
    try:
        new = _parse_profile({"id": user, "display_name": old.display_name, **entry}, "access")
    except ConfigError as exc:
        raise AccessError(str(exc)) from None
    try:
        known = list(catalog())
        validate_scope_against_catalog(new, known)
    except ConfigError as exc:
        raise AccessError(str(exc)) from None
    except Exception:  # noqa: BLE001 - catalog unavailable: refuse, never guess
        raise AccessError("The brand catalog is unavailable; nothing was changed.") from None

    path = overrides_path(data_dir)
    overrides = _read_overrides(path)  # fail closed on a corrupt file before auditing
    overrides[user] = _entry(new)

    sid = session_id or uuid.uuid4().hex
    tid = turn_id or uuid.uuid4().hex[:12]
    details = {
        "target_user": user,
        "old_scope": _old_scope_value(old, known),
        "new_scope": _scope_value(new),
    }

    def record(outcome: str) -> None:
        audit.record(
            SCOPE_CHANGED,
            actor_user_id=actor,
            session_id=sid,
            turn_id=tid,
            outcome=outcome,
            details=details,
        )

    try:  # audit first: if this fails nothing is persisted
        record("ok")
    except Exception:  # noqa: BLE001
        raise AccessError("The audit record could not be written; nothing was changed.") from None
    try:
        _atomic_write(path, overrides)
    except AccessError:
        try:  # best effort: the 'ok' row was written but the change was not applied
            record("failed")
        except Exception:  # noqa: BLE001
            log.warning("scope.changed 'failed' follow-up row could not be written")
        raise
    return AccessChange(user, old, new)
