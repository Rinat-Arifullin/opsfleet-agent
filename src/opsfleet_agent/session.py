"""Profiles, session start and the local part of the startup check.

The brand scope of a session comes ONLY from the selected profile in config/profiles.yaml.
Nothing here reads scope from user text, arguments or the environment.
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from opsfleet_agent.config import ConfigError

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_BRAND_LEN = 100
ALL_PRODUCTS_LABEL = "All products"


@dataclass(frozen=True)
class Profile:
    user_id: str
    display_name: str
    brands: tuple[str, ...] = ()
    all_products: bool = False

    @property
    def scope_label(self) -> str:
        if self.all_products:
            return ALL_PRODUCTS_LABEL
        return "Brands: " + ", ".join(self.brands)


@dataclass(frozen=True)
class Session:
    session_id: str
    profile: Profile

    @property
    def scope_brands(self) -> tuple[str, ...] | None:
        """None means all products (explicit CEO flag); otherwise the exact brand tuple."""
        return None if self.profile.all_products else self.profile.brands


def default_profiles_path() -> Path:
    override = os.environ.get("OPSFLEET_PROFILES_YAML")  # a file location, never a scope
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "config" / "profiles.yaml"


def default_data_dir() -> Path:
    return Path(os.environ.get("OPSFLEET_DATA_DIR") or "data")


def _parse_profile(entry: Any, name: str) -> Profile:
    if not isinstance(entry, dict):
        raise ConfigError(f"{name} is invalid: each profile must be a mapping.")
    uid = entry.get("id")
    if not isinstance(uid, str) or not _ID_RE.match(uid):
        raise ConfigError(
            f"{name} is invalid: profile id {uid!r} must match [A-Za-z0-9_.-]{{1,64}}."
        )
    unknown = set(entry) - {"id", "display_name", "brands", "all_products"}
    if unknown:
        raise ConfigError(
            f"{name}: profile '{uid}' has unknown keys: {', '.join(sorted(map(str, unknown)))}."
        )
    display = entry.get("display_name")
    if not isinstance(display, str) or not display.strip() or _CTRL_RE.search(display):
        raise ConfigError(f"{name}: profile '{uid}' needs a non-empty display_name.")
    all_flag = entry.get("all_products", False)
    if not isinstance(all_flag, bool):
        raise ConfigError(f"{name}: profile '{uid}' all_products must be true or false.")
    brands_raw = entry.get("brands")
    if all_flag:
        if brands_raw:
            raise ConfigError(f"{name}: profile '{uid}' sets both all_products and brands.")
        return Profile(uid, display.strip(), (), True)
    if not isinstance(brands_raw, list) or not brands_raw:
        raise ConfigError(
            f"{name}: profile '{uid}' has an empty scope. "
            "List brands, or set all_products: true explicitly."
        )
    seen: set[str] = set()
    for b in brands_raw:
        if (
            not isinstance(b, str)
            or not b
            or b != b.strip()
            or len(b) > MAX_BRAND_LEN
            or _CTRL_RE.search(b)
        ):
            raise ConfigError(f"{name}: profile '{uid}' has a malformed brand value {b!r}.")
        if b in seen:
            raise ConfigError(f"{name}: profile '{uid}' lists brand {b!r} twice.")
        seen.add(b)
    return Profile(uid, display.strip(), tuple(brands_raw), False)


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    path = path or default_profiles_path()
    name = path.name
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise ConfigError(
            f"{name} cannot be read or is not valid YAML. Fix it or restore it from git."
        ) from None
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("profiles"), list)
        or not raw["profiles"]
    ):
        raise ConfigError(f"{name} is invalid: expected a non-empty 'profiles' list.")
    out: dict[str, Profile] = {}
    for entry in raw["profiles"]:
        p = _parse_profile(entry, name)
        if p.user_id in out:
            raise ConfigError(f"{name} is invalid: duplicate user '{p.user_id}'.")
        out[p.user_id] = p
    return out


def select_profile(profiles: dict[str, Profile], user_id: str) -> Profile:
    try:
        return profiles[user_id]
    except KeyError:
        raise ConfigError(
            f"Unknown user '{user_id}'. Valid users: {', '.join(sorted(profiles))}."
        ) from None


def start_session(profile: Profile) -> Session:
    return Session(session_id=uuid.uuid4().hex, profile=profile)


def banner(session: Session) -> str:
    p = session.profile
    return (
        f"User: {p.display_name} ({p.user_id})\n"
        f"Scope: {p.scope_label}\n"
        f"Session: {session.session_id}"
    )


def validate_scope_against_catalog(
    profile: Profile, known_brands: Iterable[str], product_count: int | None = None
) -> None:
    """AC-20.3: exact, case-sensitive brand check; the caller supplies the catalog."""
    if profile.all_products:
        return
    known = set(known_brands)
    for b in profile.brands:
        if b not in known:
            raise ConfigError(f"Profile '{profile.user_id}': brand {b!r} is not in products.brand.")
    if product_count == 0:
        raise ConfigError(f"Profile '{profile.user_id}': scope matches 0 products.")


def _check_dir_writable(d: Path, what: str) -> None:
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=d, prefix=".write-check-"):
            pass
    except OSError:
        raise ConfigError(
            f"{what} {d} is not writable. Fix permissions or set OPSFLEET_DATA_DIR."
        ) from None


def check_stores_writable(data_dir: Path | None = None) -> None:
    """TR-18: app DB, checkpoint DB and trace dir must be writable. Creates no DB files."""
    base = data_dir or default_data_dir()
    for fname, what in (
        ("app.db", "App DB directory"),
        ("checkpoints.db", "Checkpoint DB directory"),
    ):
        _check_dir_writable(base, what)
        f = base / fname
        if f.exists() and not os.access(f, os.W_OK):
            raise ConfigError(f"{what[:-10]} file {f} is not writable. Fix its permissions.")
    _check_dir_writable(base / "traces", "Trace directory")


def local_startup_check(
    profiles_path: Path | None = None, data_dir: Path | None = None
) -> dict[str, Profile]:
    """Network-free part of the startup check: profiles file valid, stores writable."""
    profiles = load_profiles(profiles_path)
    check_stores_writable(data_dir)
    return profiles
