"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from opsfleet_agent.config import ConfigError, ModelLister, safe_config_view, startup_check
from opsfleet_agent.guards.pii import (
    PiiDetector,
    PiiDetectorError,
    build_allowlist,
    ensure_model_available,
    set_default_detector,
)
from opsfleet_agent.obs.tracer import install_log_filter, register_secret
from opsfleet_agent.session import (
    banner,
    load_profiles,
    local_startup_check,
    select_profile,
    start_session,
)

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="opsfleet-agent", description="Data-analysis chat agent over thelook_ecommerce."
    )
    p.add_argument("--user", required=True, help="profile id from config/profiles.yaml")
    return p


def _install_pii_detector(profiles) -> None:
    """Refuse to start without the spaCy model (8b); allowlist the configured brands.

    The full catalogue allowlist (brands, categories, departments from BigQuery) replaces
    this one once the BigQuery client is wired at startup (19).
    """
    ensure_model_available()
    brands = sorted({b for p in profiles for b in p.brands})
    set_default_detector(PiiDetector(build_allowlist(brands=brands)))


def main(argv: Sequence[str] | None = None, *, lister: ModelLister | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Profile first: an unknown user must fail before any LLM or BigQuery call.
        profiles = load_profiles()
        profile = select_profile(profiles, args.user)
        settings = startup_check(lister=lister)
        local_startup_check()
        _install_pii_detector(profiles.values())
    except (ConfigError, PiiDetectorError) as e:
        print(str(e), file=sys.stderr)
        return 2
    session = start_session(profile)
    install_log_filter()
    register_secret(settings.gemini_api_key)
    register_secret(os.environ.get("LANGGRAPH_AES_KEY"))
    log.info("config: %s", safe_config_view(settings))
    print(banner(session))
    print("Type 'exit' to quit.")
    while True:
        try:
            line = input("> ")
        except EOFError:
            break
        if line.strip().lower() in ("exit", "quit"):
            break
        if line.strip():
            print(line)
    return 0
