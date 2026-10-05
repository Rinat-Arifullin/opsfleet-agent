"""Profile matrix for eval cases (D-160): one run per (case, profile) plus scope invariants.

A case declares the profiles it runs under:

    profiles: all                       # every profile in config/profiles.yaml
    profiles: [analyst_b, ceo_demo]     # a subset; a golden case needs `profiles_reason`
    profiles_reason: the user names analyst_b's brands
    per_profile:                        # optional, shallow per-key overrides
      ceo_demo:
        expect: {must_contain: [all products]}
        fake: {text: ...}
    known_brands: [...]                 # optional extra brands for the answer check

A golden case without `profiles` runs under every profile (the D-160 default). Any other case
without `profiles` runs once, as before, under its `session.profile` (or none). Expanded runs
get the id ``<case id>@<profile>``, ``session.profile`` set to that profile, and the case's
``expect`` / ``fake`` / ``skip`` with that profile's overrides applied.

Every run that has a profile also gets scope-invariant checks, computed by code from the
system-under-test output (never by the LLM): see :func:`scope_checks`. Decisions:
docs/process/iter-d160-ods.md.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any, Final

PROFILES_ALL: Final = "all"
PROFILE_SEP: Final = "@"
OVERRIDE_KEYS: Final = frozenset({"expect", "fake", "skip"})
MAX_PROFILES: Final = 20  # bound on a case's profile list
MAX_SQL_CHECKED: Final = 50  # statements checked per run
MAX_KNOWN_BRANDS: Final = 200  # case-level known_brands entries
MATRIX_KEYS: Final = frozenset({"profiles", "profiles_reason", "per_profile", "known_brands"})


class MatrixError(ValueError):
    """A case's matrix fields are invalid; the message names the case, never data values."""


# --------------------------------------------------------------------------- schema


def validate_fields(raw: Mapping[str, Any], case_id: str, expect_keys: Iterable[str]) -> None:
    """Shape checks that need no profile list (run at load time)."""
    profiles = raw.get("profiles")
    if profiles is not None and profiles != PROFILES_ALL:
        if (
            not isinstance(profiles, list)
            or not profiles
            or len(profiles) > MAX_PROFILES
            or not all(isinstance(p, str) and p for p in profiles)
            or len(set(profiles)) != len(profiles)
        ):
            raise MatrixError(
                f"{case_id}: `profiles` must be `all` or a non-empty list of distinct profile ids"
            )
    reason = raw.get("profiles_reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise MatrixError(f"{case_id}: `profiles_reason` must be a non-empty string")
    per = raw.get("per_profile")
    if per is not None:
        if not isinstance(per, dict) or not all(isinstance(k, str) for k in per):
            raise MatrixError(f"{case_id}: `per_profile` must map profile ids to overrides")
        keys = set(expect_keys)
        for pid, over in per.items():
            if not isinstance(over, dict) or set(over) - OVERRIDE_KEYS:
                raise MatrixError(
                    f"{case_id}: per_profile.{pid} may only set {sorted(OVERRIDE_KEYS)}"
                )
            for part in ("expect", "fake"):
                if part in over and not isinstance(over[part], dict):
                    raise MatrixError(f"{case_id}: per_profile.{pid}.{part} must be a mapping")
            bad = set(over.get("expect") or {}) - keys
            if bad:
                raise MatrixError(f"{case_id}: per_profile.{pid} unknown expect keys {sorted(bad)}")
    known = raw.get("known_brands")
    if known is not None and (
        not isinstance(known, list)
        or len(known) > MAX_KNOWN_BRANDS
        or not all(isinstance(b, str) and b.strip() for b in known)
    ):
        raise MatrixError(f"{case_id}: `known_brands` must be a list of brand names")


def is_golden(case: Any) -> bool:
    return case.suite == "golden" or str(case.suite).startswith("golden/")


def profiles_for(
    case: Any, profile_ids: Sequence[str], all_golden: bool = False
) -> list[str] | None:
    """The profiles a case expands to, or None for a legacy single run (id unchanged).

    ``all_golden``: every case is a golden case (the cases dir is the golden dir itself, so
    the suite is ``.``; the Langfuse upload of ``evals/cases/golden``)."""
    golden = all_golden or is_golden(case)
    declared = case.profiles
    if declared is None:
        return list(profile_ids) if golden else None
    if declared == PROFILES_ALL:
        return list(profile_ids)
    unknown = [p for p in declared if p not in profile_ids]
    if unknown:
        raise MatrixError(f"{case.id}: unknown profiles {unknown}")
    subset = set(declared) != set(profile_ids)
    if subset and golden and not case.profiles_reason:
        raise MatrixError(
            f"{case.id}: runs under a subset of profiles; add `profiles_reason` to say why"
        )
    return list(declared)


def _merged(base: Mapping[str, Any] | None, over: Mapping[str, Any] | None) -> dict[str, Any]:
    out = dict(base or {})
    out.update(over or {})
    return out


def expand_profiles(
    cases: Sequence[Any],
    profile_ids: Sequence[str],
    only: Sequence[str] = (),
    all_golden: bool = False,
) -> list[Any]:
    """One run per (case, profile); `only` keeps the runs under those profiles."""
    wanted = set(only)
    unknown = sorted(wanted - set(profile_ids))
    if unknown:
        raise MatrixError(f"unknown --profile {unknown}; valid: {', '.join(profile_ids)}")
    out: list[Any] = []
    for case in cases:
        targets = profiles_for(case, profile_ids, all_golden)
        per = case.per_profile or {}
        stray = sorted(set(per) - set(targets or ()))
        if stray:
            raise MatrixError(
                f"{case.id}: per_profile names profiles it does not run under {stray}"
            )
        if targets is None:
            prof = case.session.get("profile") if isinstance(case.session, dict) else None
            if wanted and prof not in wanted:
                continue
            out.append(dataclasses.replace(case, base_id=case.id, profile=prof))
            continue
        for pid in targets:
            if wanted and pid not in wanted:
                continue
            over = per.get(pid) or {}
            fake = case.fake
            if case.fake is not None or "fake" in over:
                fake = _merged(case.fake, over.get("fake"))
            out.append(
                dataclasses.replace(
                    case,
                    id=f"{case.id}{PROFILE_SEP}{pid}",
                    base_id=case.id,
                    profile=pid,
                    session={**case.session, "profile": pid},
                    expect=_merged(case.expect, over.get("expect")),
                    fake=fake,
                    skip=over.get("skip", case.skip),
                )
            )
    return out


def profile_ids() -> list[str]:
    from opsfleet_agent.session import load_profiles

    return list(load_profiles())


# --------------------------------------------------------------------------- invariants


def _profiles_and_seed_brands() -> tuple[dict[str, Any], frozenset[str]]:
    return _load_profiles_and_brands(os.environ.get("OPSFLEET_PROFILES_YAML"))


@lru_cache(maxsize=4)
def _load_profiles_and_brands(_key: str | None) -> tuple[dict[str, Any], frozenset[str]]:
    """Profiles plus the brands the offline guards know (profiles and golden seed; no network).

    Cached per `OPSFLEET_PROFILES_YAML` value so a test that points it elsewhere is honoured."""
    from opsfleet_agent.golden.runtime import offline_known_brands
    from opsfleet_agent.session import load_profiles

    profiles = load_profiles()
    try:
        from opsfleet_agent.golden.seed import load_seed

        trios = load_seed().trios
    except Exception:  # noqa: BLE001 - the seed only widens the brand list
        trios = ()
    return profiles, offline_known_brands(profiles.values(), trios)


def clear_cache() -> None:
    _load_profiles_and_brands.cache_clear()


def known_brands_for(case: Any) -> frozenset[str]:
    _, seed = _profiles_and_seed_brands()
    return seed | frozenset(case.known_brands or ())


def _outside_brands(scope: Any, known: Iterable[str], text: str) -> list[str]:
    """The known brands outside ``scope`` the text names (the context guard's matcher)."""
    from opsfleet_agent.graph.context import _BrandMatcher

    if not _BrandMatcher(scope, known).names_outside(text):
        return []
    inside = set(scope.brands)
    named = [
        b
        for b in sorted(known)
        if b not in inside and _BrandMatcher(scope, [b]).names_outside(text)
    ]
    return named or ["(unnamed)"]


def _desanitized(sql: str) -> str | None:
    """Traced SQL with the tracer's `?` literal placeholders turned back into a literal.

    The tracer replaces every literal with ``?`` (``sanitize_sql``); the code CTE bodies have
    no literals (unit-tested), so this restores a statement ``verify_scoped`` can judge.
    Returns None when the text does not parse (a hash-only trace entry)."""
    import sqlglot
    from sqlglot import exp

    try:
        trees = [t for t in sqlglot.parse(sql, read="bigquery") if t is not None]
    except Exception:  # noqa: BLE001
        return None
    if len(trees) != 1:
        return None
    tree = trees[0].transform(
        lambda n: exp.Literal.number(0) if isinstance(n, exp.Placeholder) else n
    )
    return tree.sql(dialect="bigquery")


def check_sql(statements: Sequence[str], scope: Any) -> list[str]:
    """Static failure codes for executed SQL that does not carry the profile's scope."""
    from opsfleet_agent.guards.scope import ScopeInvariantError, verify_scoped
    from opsfleet_agent.guards.scope_ctes import SCOPE_PARAM

    problems: list[str] = []
    if len(statements) > MAX_SQL_CHECKED:
        problems.append(f"{len(statements)} statements > cap {MAX_SQL_CHECKED}")
    for i, raw in enumerate(list(statements)[:MAX_SQL_CHECKED], 1):
        sql = _desanitized(str(raw))
        if sql is None:
            problems.append(f"statement {i}: unparseable (trace kept no SQL text)")
            continue
        try:
            verify_scoped(sql, scope)
        except ScopeInvariantError as exc:
            problems.append(f"statement {i}: {exc}")
            continue
        if not scope.scoped and f"@{SCOPE_PARAM}" in sql:
            problems.append(f"statement {i}: brand filter on the all-products path")
    return problems


def scope_checks(case: Any, res: Any) -> list[tuple[str, bool, str]]:
    """Scope invariants for one (case, profile) run, from the SUT's text and executed SQL.

    * ``scope:profile``: the profile exists.
    * ``scope:answer_brands`` (brand-scoped profiles): the answer names no known brand outside
      the profile's brands.
    * ``scope:sql``: every executed statement passes ``verify_scoped`` for the profile's scope
      (code CTEs match the scoped or all-products template exactly; no base table or
      parameter outside them).
    * ``scope:all_products_path`` (all-products profiles): the scope resolves to all products
      and no executed statement carries the brand filter.
    """
    from opsfleet_agent.guards.scope import ProductScope

    profile_id = case.session.get("profile") if isinstance(case.session, dict) else None
    if not profile_id:
        return []
    profiles, _ = _profiles_and_seed_brands()
    profile = profiles.get(profile_id)
    if profile is None:
        return [("scope:profile", False, f"unknown profile {profile_id}")]
    try:
        scope = ProductScope.from_profile(profile)
    except Exception as exc:  # noqa: BLE001
        return [
            ("scope:profile", False, f"profile does not resolve to a scope ({type(exc).__name__})")
        ]
    out: list[tuple[str, bool, str]] = []
    if scope.scoped:
        named = _outside_brands(scope, known_brands_for(case), res.text or "")
        why = "answer names out-of-scope brand(s): " + ", ".join(named) if named else ""
        out.append(("scope:answer_brands", not named, why))
    sql = list(res.sql or [])
    problems = check_sql(sql, scope) if sql else []
    if sql:
        out.append(("scope:sql", not problems, "; ".join(problems[:5])))
    if not scope.scoped:
        bad = [p for p in problems if "brand filter" in p or "code_cte_template" in p]
        why = "; ".join(bad[:5]) or "profile is not all-products"
        out.append(("scope:all_products_path", scope.all_products and not bad, why))
    return [(n, ok, "" if ok else why) for n, ok, why in out]


# --------------------------------------------------------------------------- summary


def matrix(rows: Iterable[tuple[str, str | None, str]]) -> dict[str, dict[str, str]]:
    """{base case id: {profile: status}} for the runs that have a profile."""
    out: dict[str, dict[str, str]] = {}
    for base, profile, status in rows:
        if profile:
            out.setdefault(base, {})[profile] = status
    return out


def matrix_lines(table: Mapping[str, Mapping[str, str]], columns: Sequence[str] = ()) -> list[str]:
    """A plain-text case x profile table: PASS, FAIL, N/A, or - (not run under it)."""
    if not table:
        return []
    cols = list(dict.fromkeys([*columns, *(p for row in table.values() for p in row)]))
    cols = [c for c in cols if any(c in row for row in table.values())]
    width = max(4, *(len(b) for b in table)) + 2
    label = {"pass": "PASS", "fail": "FAIL", "na": "N/A"}
    lines = ["case".ljust(width) + "".join(c.ljust(max(len(c), 4) + 2) for c in cols)]
    for base in sorted(table):
        row = table[base]
        cells = "".join(label.get(row.get(c, ""), "-").ljust(max(len(c), 4) + 2) for c in cols)
        lines.append(base.ljust(width) + cells)
    return [line.rstrip() for line in lines]
