import re

import pytest

from opsfleet_agent import cli

from .test_config import SENTINEL, all_models


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OPSFLEET_DATA_DIR", str(tmp_path / "data"))
    # main() installs a process-wide PII detector; keep it from leaking into other tests.
    monkeypatch.setattr("opsfleet_agent.guards.pii._default", None)


def _env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", SENTINEL)


def test_cli_exits_2_with_one_line_on_missing_env(monkeypatch, capsys):
    monkeypatch.setattr("opsfleet_agent.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", SENTINEL)
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in err
    assert SENTINEL not in err


def test_cli_echo_loop_and_exit(monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", SENTINEL)
    inputs = iter(["hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda _="": next(inputs))
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    assert "hello" in capsys.readouterr().out


def test_cli_requires_user():
    with pytest.raises(SystemExit) as ei:
        cli.main([])
    assert ei.value.code == 2


def test_cli_registers_secrets_and_installs_redaction(monkeypatch):
    import logging

    from opsfleet_agent.obs import tracer as tr

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", SENTINEL)
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "SENTINEL-aes-for-cli-test")
    monkeypatch.setattr("builtins.input", lambda _="": "exit")
    tr.clear_secrets()
    try:
        assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
        assert tr._installed_factory is not None
        rec = logging.getLogger("x.y").makeRecord(
            "x.y", logging.INFO, "f", 1, f"k={SENTINEL} {'SENTINEL-aes-for-cli-test'}", None, None
        )
        assert SENTINEL not in rec.getMessage()
        assert "SENTINEL-aes-for-cli-test" not in rec.getMessage()
    finally:
        tr.uninstall_log_filter()
        tr.clear_secrets()


def test_cli_banner_shows_user_scope_session(monkeypatch, capsys):
    _env(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _="": "exit")
    assert cli.main(["--user", "analyst_b"], lister=lambda: all_models()) == 0
    out = capsys.readouterr().out
    assert "Analyst B" in out
    assert "Brands: Carhartt, Levi's" in out
    m = re.search(r"Session: ([0-9a-f]{32})", out)
    assert m
    monkeypatch.setattr("builtins.input", lambda _="": "exit")
    cli.main(["--user", "ceo_demo"], lister=lambda: all_models())
    out2 = capsys.readouterr().out
    assert "All products" in out2
    assert m.group(1) not in out2  # a new session id per run


def test_cli_rejects_unknown_user(monkeypatch, capsys):
    _env(monkeypatch)

    def boom():
        raise AssertionError("model listing must not be called")

    assert cli.main(["--user", "nobody"], lister=boom) == 2
    err = capsys.readouterr().err
    assert "nobody" in err and "analyst_a" in err and "ceo_demo" in err
    assert "Traceback" not in err


def test_cli_refuses_to_start_without_pii_model(monkeypatch, capsys):
    from opsfleet_agent.guards import pii

    _env(monkeypatch)

    def missing() -> None:
        raise pii.PiiModelMissing("spaCy model 'en_core_web_sm' is not installed")

    monkeypatch.setattr(cli, "ensure_model_available", missing)
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 2
    assert "en_core_web_sm" in capsys.readouterr().err


def test_cli_installs_detector_with_profile_brands(monkeypatch):
    from opsfleet_agent.guards import pii

    _env(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _="": "exit")
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    detector = pii.default_detector()
    brands = {b for p in cli.load_profiles().values() for b in p.brands}
    assert brands and all(detector.allowlist.covers(b, 0, len(b)) for b in brands)
