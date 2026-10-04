"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from opsfleet_agent.config import ConfigError, ModelLister, safe_config_view, startup_check
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


def main(argv: Sequence[str] | None = None, *, lister: ModelLister | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Profile first: an unknown user must fail before any LLM or BigQuery call.
        profile = select_profile(load_profiles(), args.user)
        settings = startup_check(lister=lister)
        local_startup_check()
    except ConfigError as e:
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
