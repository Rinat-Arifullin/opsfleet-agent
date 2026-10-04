import fnmatch
import logging
import socket
from pathlib import Path

import pytest
import yaml

from opsfleet_agent import config
from opsfleet_agent.config import ConfigError, load_settings, safe_config_view, startup_check

ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "SENTINEL-SECRET-0123456789"


def all_models() -> list[str]:
    raw = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())
    ids = {r["model"] for r in raw["roles"].values()}
    ids |= {r["fallback"] for r in raw["roles"].values() if "fallback" in r}
    ids.add(raw["embedding"]["model"])
    return sorted(ids)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", SENTINEL)


def test_startup_check_reports_missing_env_without_values(monkeypatch, caplog):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", SENTINEL)
    with caplog.at_level(logging.DEBUG), pytest.raises(ConfigError) as ei:
        startup_check(lister=lambda: all_models(), dotenv=False)
    assert "GOOGLE_CLOUD_PROJECT is not set" in str(ei.value)
    assert SENTINEL not in str(ei.value)
    assert SENTINEL not in caplog.text
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(ConfigError, match="GEMINI_API_KEY is not set"):
        startup_check(lister=lambda: all_models(), dotenv=False)


def test_socket_block_fixture_active():
    with pytest.raises(RuntimeError, match="blocked"):
        socket.create_connection(("127.0.0.1", 9))
    with pytest.raises(RuntimeError, match="blocked"):
        socket.getaddrinfo("example.com", 80)


def test_gitignore_covers_secrets_and_stores():
    patterns = [
        ln.strip()
        for ln in (ROOT / ".gitignore").read_text().splitlines()
        if ln.strip() and not ln.startswith(("#", "!"))
    ]

    def ignored(path: str) -> bool:
        for p in patterns:
            if p.endswith("/"):
                if path.startswith(p) or f"/{p}" in f"/{path}":
                    return True
            elif fnmatch.fnmatch(path.rsplit("/", 1)[-1], p) or fnmatch.fnmatch(path, p):
                return True
        return False

    for path in (".env", "agent.db", "agent.db-wal", "traces/x.json", "evals/results/raw/a.json"):
        assert ignored(path), path


def test_unlisted_model_id_is_reported(env):
    with pytest.raises(ConfigError, match=r"Model gemini-3\.8-flash is not available"):
        startup_check(
            lister=lambda: ["gemini-3.1-flash-lite", "gemini-embedding-001"], dotenv=False
        )


def test_listed_models_pass_with_prefix(env):
    s = startup_check(lister=lambda: [f"models/{m}" for m in all_models()], dotenv=False)
    assert s.embedding_dimensionality == 768
    assert s.roles["deep_analyst"].model == "gemini-3.8-flash"


def test_lister_failure_is_one_line(env):
    def boom():
        raise OSError(SENTINEL)

    with pytest.raises(ConfigError) as ei:
        startup_check(lister=boom, dotenv=False)
    assert SENTINEL not in str(ei.value)


def test_both_thinking_options_rejected(env, tmp_path):
    raw = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())
    raw["roles"]["router"].update(thinking_level="low", thinking_budget=0)
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="both thinking_level and thinking_budget"):
        load_settings(p, dotenv=False)


def test_safe_config_view_excludes_secrets(env):
    s = load_settings(dotenv=False)
    view = safe_config_view(s)
    assert set(view) == set(config.SAFE_KEYS)
    assert SENTINEL not in repr(view)
    assert SENTINEL not in repr(s)


def test_tunables_defaults_from_repo_config(env):
    s = load_settings(dotenv=False)
    assert s.small_cell_k == 5
    assert s.bq_unavailable_retry_delay_s == 2.0


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("policy", "small_cell_k", 1),
        ("policy", "small_cell_k", 1001),
        ("policy", "small_cell_k", True),
        ("policy", "small_cell_k", "5"),
        ("bq", "unavailable_retry_delay_s", -1),
        ("bq", "unavailable_retry_delay_s", 31),
        ("bq", "unavailable_retry_delay_s", "2"),
    ],
)
def test_tunables_out_of_range_rejected(env, tmp_path, section, key, value):
    raw = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())
    raw[section][key] = value
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match=f"{section}.{key}"):
        load_settings(p, dotenv=False)


def test_tunables_optional(env, tmp_path):
    raw = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())
    del raw["policy"], raw["bq"]
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump(raw))
    s = load_settings(p, dotenv=False)
    assert (s.small_cell_k, s.bq_unavailable_retry_delay_s) == (5, 2.0)
