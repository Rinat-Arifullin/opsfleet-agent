"""Live seeding of a case's ``session:`` fields for the live SUT (iteration live-1).

Each case runs under its own namespace: the profile is copied with the user id
``<id>.ev<tag>`` (a fresh ``tag`` per case), so saved reports, pending drafts, quota and
history from one case or run never reach another, and none of it touches a real user's rows.
The Langfuse trace keeps the base profile id (``Seeded.base_user_id``).

Seeds go through the production APIs, never raw SQL:

* ``saved_reports`` are inserted with ``ReportStore.save`` (owner check, required sections,
  secret scrub). ``owner`` absent, ``self`` or equal to the case profile means the running
  user; any other owner maps to that owner's namespaced id, so it is never the running user.
  The fixture id (``R-DEMO-0001``) is kept in the title, because the store assigns its own
  ids (OD-2 in docs/process/iter-live1-seed-ods.md).
* ``persona: <preset>`` is applied with ``commands.persona.apply_persona`` (validate, audit
  first, switch) to a per-case active file; the graph's persona source is swapped for the
  case and restored afterwards.
* ``setup_turns`` are played as real turns before the case's own turns; their results and
  spans are not scored.

* ``preferences`` (``format``/``depth``/``charts``) are saved for the namespaced user with
  ``commands.preferences.apply_nl_preference`` on ``SQLitePreferenceStore``, the same
  validation and store as ``/prefs set``, and reset after the case (D-239).

Unknown keys raise ``CaseError``.
All seed text is synthetic.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from evals.run import Case, CaseError

SUPPORTED_SESSION_KEYS: Final = frozenset(
    {"profile", "saved_reports", "persona", "setup_turns", "preferences"}
)
PREFERENCE_KEYS: Final = ("format", "depth", "charts", "rows")
REPORT_KEYS: Final = frozenset({"id", "title", "owner", "created", "tags", "sections"})
MAX_SEED_REPORTS: Final = 20
MAX_SETUP_TURNS: Final = 4
SELF_OWNER: Final = "self"
_UID_MAX: Final = 64  # session._ID_RE

# Synthetic persona presets for `session.persona`. Each must pass persona.parse_persona.
PERSONA_PRESETS: Final[Mapping[str, str]] = {
    "formal": (
        "version: eval-formal\n"
        "edited_by: eval-harness\n\n"
        "## Tone\n"
        "Speak in a formal, courteous register, as a senior analyst briefing a board.\n"
        "Be precise and measured. Lead with the conclusion, then the supporting figures.\n\n"
        "## Style\n"
        "Use complete sentences. Avoid contractions, slang and exclamation marks.\n"
        "State the unit and the time window for every figure.\n"
    ),
    "casual": (
        "version: eval-casual\n"
        "edited_by: eval-harness\n\n"
        "## Tone\n"
        "Speak like a relaxed, upbeat teammate chatting over coffee.\n"
        "Keep it warm and light while staying accurate.\n\n"
        "## Style\n"
        "Use short, friendly sentences and everyday words.\n"
        "State the unit and the time window for every figure.\n"
    ),
}

_SEED_TEXT: Final[Mapping[str, str]] = {
    "Definitions": "Synthetic eval fixture. No figure in this report comes from real data.",
    "Summary": "A placeholder summary written by the eval harness.",
    "Key metrics": "- Placeholder metric: n/a (synthetic)",
    "Insights": "1. First placeholder insight.\n2. Second placeholder insight.",
    "Action items": "- Review the placeholder insights.",
    "Limitations & hypotheses": "Synthetic content only; nothing here was measured.",
    "Data used": "No query ran: the eval harness seeded this report.",
}


def new_tag() -> str:
    return uuid.uuid4().hex[:8]


def run_user_id(base: str, tag: str) -> str:
    """The namespaced user id for ``base`` in the run ``tag`` (valid as a profile id)."""
    suffix = f".ev{tag}"
    return base[: _UID_MAX - len(suffix)] + suffix


def check_session(case: Case) -> None:
    extra = sorted(set(case.session) - SUPPORTED_SESSION_KEYS)
    if extra:
        raise CaseError(f"session seeds not supported by the live SUT: {extra}")


def setup_turns(case: Case) -> list[str]:
    raw = case.session.get("setup_turns") or []
    if not isinstance(raw, list) or not all(isinstance(t, str) and t.strip() for t in raw):
        raise CaseError("session.setup_turns must be a list of non-empty strings")
    if len(raw) > MAX_SETUP_TURNS:
        raise CaseError(f"{len(raw)} setup turns > cap {MAX_SETUP_TURNS}")
    return list(raw)


@dataclass(frozen=True)
class Seeded:
    """The namespaced profile a case runs as, plus what was seeded for it."""

    profile: Any  # session.Profile with the namespaced user id
    base_user_id: str
    tag: str
    reports: tuple[Any, ...] = ()  # SavedReport records
    persona_version: str | None = None
    preferences: Mapping[str, Any] | None = None  # what the store holds after seeding


def _owner_id(owner: Any, base_user_id: str, tag: str) -> str:
    if owner is None or owner == SELF_OWNER or owner == base_user_id:
        return run_user_id(base_user_id, tag)
    if not isinstance(owner, str) or not owner.strip():
        raise CaseError("session.saved_reports owner must be a profile id or 'self'")
    return run_user_id(owner.strip(), tag)


def _scope_label(profile: Any) -> str:
    return str(getattr(profile, "scope_label", "") or "synthetic")


def render_body(title: str, profile: Any, sections: Any = None) -> str:
    """A synthetic body with every required section (``reports.schema.missing_sections``)."""
    lines = [f"# {title}", "", "Data window: synthetic eval seed"]
    lines.append(f"Scope: {_scope_label(profile)}")
    for name, text in _SEED_TEXT.items():
        lines += ["", f"## {name}", text]
    if sections:
        lines += ["", "Seeded section list: " + ", ".join(str(s) for s in sections)]
    return "\n".join(lines) + "\n"


def _pass_guard(body: str) -> tuple[bool, str]:
    # Seeds are fixed synthetic text from this module and the case YAML; the store still runs
    # its secret scrub and section check (OD-3).
    return True, body


def seed_reports(store: Any, case: Case, run_profile: Any, base_user_id: str, tag: str,
                 session_id: str) -> tuple[Any, ...]:  # fmt: skip
    from opsfleet_agent.graph.context import snapshot_of
    from opsfleet_agent.guards.scope import ProductScope

    raw = case.session.get("saved_reports") or []
    if not raw:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_SEED_REPORTS:
        raise CaseError(f"session.saved_reports must be a list of at most {MAX_SEED_REPORTS}")
    if store is None:
        raise CaseError("the live runtime has no report store to seed")
    snapshot = snapshot_of(ProductScope.from_profile(run_profile))
    out = []
    for i, rep in enumerate(raw):
        if not isinstance(rep, dict) or not rep.get("title"):
            raise CaseError(f"session.saved_reports[{i}] needs a title")
        unknown = set(rep) - REPORT_KEYS
        if unknown:
            raise CaseError(f"session.saved_reports[{i}]: unknown keys {sorted(unknown)}")
        fixture_id = str(rep.get("id") or "").strip()
        title = f"{fixture_id} {rep['title']}".strip()
        key = f"eval-seed-{tag}-{i}"
        record, _ = store.save(
            owner_user_id=_owner_id(rep.get("owner"), base_user_id, tag),
            session_id=session_id, turn_id=f"seed{i}", title=title,
            body_markdown=render_body(title, run_profile, rep.get("sections")),
            sections={}, sql_used=[], scope_snapshot=snapshot,
            data_window="synthetic eval seed", draft_hash=key, idempotency_key=key,
            guard=_pass_guard, tags=[str(t) for t in rep.get("tags") or []],
            model_used="eval-seed",
        )  # fmt: skip
        out.append(record)
    return tuple(out)


def _preference_store(runtime: Any) -> Any:
    store = getattr(runtime, "preference_store", None)
    if store is None:
        from opsfleet_agent.graph.degraded import unwrap

        services = getattr(unwrap(getattr(runtime, "graph", None)), "services", None)
        store = getattr(services, "preferences", None)
    return store


def _raw_preference(key: str, value: Any) -> str:
    if key == "charts" and isinstance(value, bool):  # YAML `on`/`off` load as booleans
        return "on" if value else "off"
    return str(value).strip().lower()


def seed_preferences(store: Any, case: Case, user_id: str) -> Mapping[str, Any] | None:
    """Save ``session.preferences`` for ``user_id`` through the ``/prefs`` path (D-239)."""
    from opsfleet_agent.commands.preferences import (
        allowed_values,
        apply_nl_preference,
        canonical_value,
    )
    from opsfleet_agent.graph.nl_preferences import NLPreference

    raw = case.session.get("preferences")
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise CaseError(f"session.preferences must be a mapping of {list(PREFERENCE_KEYS)}")
    unknown = sorted(set(raw) - set(PREFERENCE_KEYS))
    if unknown:
        raise CaseError(f"session.preferences: unknown keys {unknown} "
                        f"(known: {list(PREFERENCE_KEYS)})")  # fmt: skip
    if store is None:
        raise CaseError("the live runtime has no preference store to seed")
    settings = []
    for key in (k for k in PREFERENCE_KEYS if k in raw):
        canonical = canonical_value(key, _raw_preference(key, raw[key]))
        if canonical is None:
            raise CaseError(f"session.preferences.{key} must be one of: {allowed_values(key)}")
        settings.append((key, canonical[0]))  # the canonical stored value
    detected = NLPreference(settings=tuple(settings))
    text, saved, rejected = apply_nl_preference(detected, store=store, user_id=user_id)
    if not saved or rejected:
        store.reset(user_id)
        raise CaseError(f"session.preferences not applied: {text}")
    return dict(store.load(user_id).preferences)


def _structural_smoke(persona: Any) -> bool:
    # The real smoke subset is a live eval run of its own (commands/persona.py); a seed only
    # needs the parsed, validated persona (OD-6).
    return bool(getattr(persona, "text", "").strip())


def apply_persona_preset(preset: Any, *, run_dir: Path, audit: Any, actor: str,
                         session_id: str) -> tuple[Callable[[], Any], str]:  # fmt: skip
    """Apply ``preset`` through the real persona API to a per-case active file. Returns the
    persona source to install in the graph and the applied version."""
    from opsfleet_agent.commands.persona import apply_persona
    from opsfleet_agent.persona import PersonaStore

    text = PERSONA_PRESETS.get(preset) if isinstance(preset, str) else None
    if text is None:
        raise CaseError(f"session.persona: unknown preset {preset!r} "
                        f"(known: {sorted(PERSONA_PRESETS)})")  # fmt: skip
    if audit is None:
        raise CaseError("the live runtime has no audit log; a persona change needs one")
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate = run_dir / "persona.candidate.md"
    candidate.write_text(text, encoding="utf-8")
    active = run_dir / "persona.md"
    change = apply_persona(
        candidate, active_path=active, history_dir=run_dir / "persona_history", audit=audit,
        actor=actor, smoke=_structural_smoke, session_id=session_id,
    )  # fmt: skip
    if not change.ok:
        raise CaseError(f"session.persona: {change.message}")
    store = PersonaStore(active)
    if store.current.version != change.to_version:
        raise CaseError("session.persona: the applied persona did not load")
    return store.refresh, str(change.to_version)


def _services(runtime: Any) -> Any:
    from opsfleet_agent.graph.degraded import unwrap

    services = getattr(unwrap(getattr(runtime, "graph", None)), "services", None)
    if services is None or not hasattr(services, "persona"):
        raise CaseError("the live graph exposes no persona source to seed")
    return services


@contextlib.contextmanager
def seeded(runtime: Any, case: Case, profile: Any, *, session_id: str, data_dir: Path,
           tag: str | None = None) -> Iterator[Seeded]:  # fmt: skip
    """Seed ``case.session`` for one case and undo the runtime-wide parts afterwards."""
    check_session(case)
    tag = tag or new_tag()
    base = profile.user_id
    run_profile = dataclasses.replace(profile, user_id=run_user_id(base, tag))
    reports = seed_reports(getattr(runtime, "report_store", None), case, run_profile, base, tag,
                           session_id)  # fmt: skip
    store = _preference_store(runtime) if "preferences" in case.session else None
    prefs = seed_preferences(store, case, run_profile.user_id)
    try:
        with _persona_seeded(runtime, case, run_profile, data_dir=data_dir, tag=tag) as version:
            yield Seeded(run_profile, base, tag, reports, version, prefs)
    finally:
        if prefs is not None:
            store.reset(run_profile.user_id)


@contextlib.contextmanager
def _persona_seeded(runtime: Any, case: Case, run_profile: Any, *, data_dir: Path,
                    tag: str) -> Iterator[str | None]:  # fmt: skip
    preset = case.session.get("persona")
    if preset is None:
        yield None
        return
    services = _services(runtime)
    source, version = apply_persona_preset(
        preset, run_dir=Path(data_dir) / "seed" / tag, audit=getattr(runtime, "audit_log", None),
        # the audit store takes uuid-hex session ids only; the eval id is not one (OD-12)
        actor=run_profile.user_id, session_id=uuid.uuid4().hex,
    )  # fmt: skip
    previous = services.persona
    services.persona = source
    try:
        yield version
    finally:
        services.persona = previous
