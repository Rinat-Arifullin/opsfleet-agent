"""Startup wiring for the Golden index and the offline brand list (D-96, D-114, D-117).

Kept out of ``cli.build_runtime`` (production wiring, not unit-tested) so the decisions are
covered by offline tests. Nothing here makes a network call: the embedder is lazy.

* **Seed strictness (D-114)**: tests and CI validate the shipped seed with ``strict=True``
  (``tests/unit/test_golden.py``); a CI job can also set ``OPSFLEET_GOLDEN_STRICT=1`` so the
  CLI fails at startup on any bad trio. In production the default is lenient: a bad trio is
  skipped with a logged warning and the count is logged, and an unreadable seed disables Golden
  examples instead of blocking the session.
* **Known brands (D-96)**: the union of every profile's brands and the brands named by the seed.
  The full ``products.brand`` catalogue needs a BigQuery query and stays deferred (OD-12), so
  a brand in neither list is not detected by the context name check.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from opsfleet_agent.config import ConfigError
from opsfleet_agent.golden.seed import (
    AGNOSTIC,
    Embedder,
    GenaiEmbedder,
    GoldenIndex,
    GoldenSeedError,
    GoldenTrio,
    load_seed,
)

log = logging.getLogger(__name__)

STRICT_ENV: Final = "OPSFLEET_GOLDEN_STRICT"
_TRUE: Final = frozenset({"1", "true", "yes", "on"})


def seed_strict(env: Mapping[str, str] | None = None) -> bool:
    """D-114: strict seed validation only when ``OPSFLEET_GOLDEN_STRICT`` is truthy."""
    environ = os.environ if env is None else env
    return str(environ.get(STRICT_ENV, "")).strip().casefold() in _TRUE


def build_golden_index(
    settings: Any,
    *,
    cache_dir: Path | None,
    embedder: Embedder | None = None,
    seed_path: Path | str | None = None,
    strict: bool | None = None,
) -> GoldenIndex | None:
    """The Golden index for the graph, or None when Golden examples are disabled.

    Strict: any bad trio or an unreadable seed raises :class:`ConfigError` (startup refuses;
    the message carries the trio id and reason code only).
    Lenient: bad trios are skipped (count logged); an unreadable seed or an empty result
    returns None. ``embedder`` defaults to the lazy :class:`GenaiEmbedder` from settings."""
    strict = seed_strict() if strict is None else strict
    try:
        loaded = load_seed(seed_path, strict=strict)
    except GoldenSeedError as exc:
        if strict:
            raise ConfigError(f"Golden seed is invalid: {exc}") from None
        log.warning("golden seed unavailable, examples disabled: %s", type(exc).__name__)
        return None
    if loaded.rejected:
        log.warning("golden seed: %d trio(s) rejected and skipped", len(loaded.rejected))
    if not loaded.trios:
        log.warning("golden seed has no valid trios; examples disabled")
        return None
    model = settings.embedding_model
    dim = settings.embedding_dimensionality
    if embedder is None:
        embedder = GenaiEmbedder(settings.gemini_api_key, model, dim)
    return GoldenIndex(loaded.trios, embedder, model, dim, cache_dir=cache_dir)


def offline_known_brands(
    profiles: Iterable[Any], trios: Iterable[GoldenTrio] = ()
) -> frozenset[str]:
    """D-96: brands from the profiles plus the seed (exact strings, as in ``products.brand``)."""
    out = {b for p in profiles for b in getattr(p, "brands", ()) if isinstance(b, str) and b}
    out |= {b for t in trios for b in t.brands if b and b != AGNOSTIC}
    return frozenset(out)
