"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from opsfleet_agent.config import ConfigError, ModelLister, safe_config_view, startup_check
from opsfleet_agent.obs.tracer import install_log_filter, register_secret

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="opsfleet-agent", description="Data-analysis chat agent over thelook_ecommerce."
    )
    p.add_argument("--user", required=True, help="user id (owner of saved reports)")
    return p


def main(argv: Sequence[str] | None = None, *, lister: ModelLister | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = startup_check(lister=lister)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    install_log_filter()
    register_secret(settings.gemini_api_key)
    register_secret(os.environ.get("LANGGRAPH_AES_KEY"))
    log.info("config: %s", safe_config_view(settings))
    print(f"Ready (user: {args.user}). Type 'exit' to quit.")
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
