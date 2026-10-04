import pytest

from opsfleet_agent import cli

from .test_config import SENTINEL, all_models


def test_cli_exits_2_with_one_line_on_missing_env(monkeypatch, capsys):
    monkeypatch.setattr("opsfleet_agent.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", SENTINEL)
    assert cli.main(["--user", "u1"], lister=lambda: all_models()) == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in err
    assert SENTINEL not in err


def test_cli_echo_loop_and_exit(monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", SENTINEL)
    inputs = iter(["hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda _="": next(inputs))
    assert cli.main(["--user", "u1"], lister=lambda: all_models()) == 0
    assert "hello" in capsys.readouterr().out


def test_cli_requires_user():
    with pytest.raises(SystemExit) as ei:
        cli.main([])
    assert ei.value.code == 2
