import dataclasses
from pathlib import Path

import pytest
import yaml

from opsfleet_agent import session as S
from opsfleet_agent.config import ConfigError


def write(tmp_path, profiles) -> Path:
    p = tmp_path / "profiles.yaml"
    p.write_text(yaml.safe_dump({"profiles": profiles}))
    return p


def test_shipped_profiles_are_valid():
    profiles = S.load_profiles()
    assert {"analyst_a", "analyst_b", "ceo_demo"} <= set(profiles)
    assert profiles["ceo_demo"].all_products and not profiles["analyst_a"].all_products


def test_scope_from_profile_only(monkeypatch):
    monkeypatch.setenv("OPSFLEET_BRANDS", "Everything")
    monkeypatch.setenv("OPSFLEET_SCOPE", "all")
    profiles = S.load_profiles()
    s = S.start_session(S.select_profile(profiles, "analyst_a"))
    assert s.scope_brands == ("Calvin Klein",)
    # the API accepts no scope input: only a profile; a "switch user" text has no entry point
    assert list(S.start_session.__annotations__) == ["profile", "return"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.profile.brands = ("X",)  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.profile = profiles["ceo_demo"]  # type: ignore[misc]
    assert S.start_session(profiles["ceo_demo"]).scope_brands is None
    assert S.start_session(profiles["ceo_demo"]).session_id != s.session_id


def test_profile_scope_validation(tmp_path):
    ok = {"id": "u", "display_name": "U", "brands": ["A B", "C's"]}
    assert S.load_profiles(write(tmp_path, [ok]))["u"].brands == ("A B", "C's")
    bad = [
        [{"id": "u", "display_name": "U", "brands": []}],
        [{"id": "u", "display_name": "U"}],  # no implicit all-products
        [{"id": "u", "display_name": "U", "brands": [" A"]}],
        [{"id": "u", "display_name": "U", "brands": [""]}],
        [{"id": "u", "display_name": "U", "brands": ["A", "A"]}],
        [{"id": "u", "display_name": "U", "brands": [1]}],
        [{"id": "u", "display_name": "U", "brands": ["A"], "all_products": True}],
        [{"id": "u", "display_name": "U", "all_products": "yes"}],
        [{"id": "u", "display_name": "U", "brands": ["A"], "extra": 1}],
        [{"id": "bad id", "display_name": "U", "brands": ["A"]}],
        [{"id": "u", "display_name": "", "brands": ["A"]}],
        [ok, dict(ok)],  # duplicate user
        [],
    ]
    for profiles in bad:
        with pytest.raises(ConfigError):
            S.load_profiles(write(tmp_path, profiles))
    with pytest.raises(ConfigError, match="u"):
        S.load_profiles(write(tmp_path, [{"id": "u", "display_name": "U", "brands": []}]))
    # catalog check: exact, case-sensitive; 0 products
    p = S.Profile("u", "U", ("Levi's",))
    S.validate_scope_against_catalog(p, ["Levi's"], 5)
    with pytest.raises(ConfigError, match="levis|Levi"):
        S.validate_scope_against_catalog(p, ["levi's"])
    with pytest.raises(ConfigError, match="0 products"):
        S.validate_scope_against_catalog(p, ["Levi's"], 0)


def test_startup_config_check(tmp_path, monkeypatch):
    prof = write(tmp_path, [{"id": "u", "display_name": "U", "brands": ["A"]}])
    data = tmp_path / "data"
    assert "u" in S.local_startup_check(prof, data)
    assert (data / "traces").is_dir()
    # invalid profiles file
    (tmp_path / "bad.yaml").write_text("profiles: [")
    with pytest.raises(ConfigError, match="bad.yaml"):
        S.local_startup_check(tmp_path / "bad.yaml", data)
    # unwritable store dir
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(ConfigError, match="not writable"):
        S.local_startup_check(prof, blocker / "sub")
    # unwritable DB file
    (data / "app.db").write_text("")
    (data / "app.db").chmod(0o400)
    try:
        import os

        if os.geteuid() != 0:
            with pytest.raises(ConfigError, match="not writable"):
                S.local_startup_check(prof, data)
    finally:
        (data / "app.db").chmod(0o600)
    # unwritable trace dir
    (data / "app.db").unlink()
    (data / "traces").chmod(0o500)
    try:
        import os

        if os.geteuid() != 0:
            with pytest.raises(ConfigError, match="Trace directory"):
                S.local_startup_check(prof, data)
    finally:
        (data / "traces").chmod(0o700)
