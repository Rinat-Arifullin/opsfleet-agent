import json
import os
from pathlib import Path

import pytest
import yaml

from opsfleet_agent import session as S
from opsfleet_agent.commands import access as AC
from opsfleet_agent.config import ConfigError
from opsfleet_agent.store import audit as A
from opsfleet_agent.store.db import open_store

ADMIN = "admin_x"
CATALOG = ["Calvin Klein", "Carhartt", "Levi's", "Acme"]


@pytest.fixture
def env(tmp_path: Path):
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(
        yaml.safe_dump(
            {
                "profiles": [
                    {"id": "analyst_a", "display_name": "Analyst A", "brands": ["Calvin Klein"]},
                    {"id": "ceo_demo", "display_name": "CEO Demo", "all_products": True},
                ]
            }
        )
    )
    data = tmp_path / "data"
    conn = open_store(tmp_path / "app.db")
    yield {"profiles": profiles, "data": data, "audit": A.AuditLog(conn), "conn": conn}
    conn.close()


def run(env, user, **kw):
    kw.setdefault("catalog", lambda: CATALOG)
    return AC.access_set(
        user,
        actor=ADMIN,
        audit=env["audit"],
        profiles_path=env["profiles"],
        data_dir=env["data"],
        **kw,
    )


def load(env):
    return AC.load_profiles_with_overrides(env["profiles"], env["data"])


def audit_rows(env):
    return (
        env["conn"]
        .execute(
            "SELECT actor_user_id, details FROM audit_event WHERE event_type = 'scope.changed'"
        )
        .fetchall()
    )


def nothing_written(env):
    assert not AC.overrides_path(env["data"]).exists()
    assert audit_rows(env) == []


@pytest.mark.parametrize(
    ("kw", "new_brands", "new_scope"),
    [
        ({"brands": ["Carhartt", "Levi's"]}, ("Carhartt", "Levi's"), ["Carhartt", "Levi's"]),
        ({"all": True}, None, "all"),
    ],
    ids=["brand-list", "all"],
)
def test_access_set_audited_and_effective_next_session(env, kw, new_brands, new_scope):
    live = S.start_session(load(env)["analyst_a"])
    change = run(env, "analyst_a", **kw)
    assert change.old.brands == ("Calvin Klein",)
    assert change.new.all_products == ("all" in kw)
    # the live session keeps its snapshot (frozen) ...
    assert live.scope_brands == ("Calvin Klein",)
    # ... the next session gets the new scope
    nxt = S.start_session(S.select_profile(load(env), "analyst_a"))
    assert nxt.scope_brands == new_brands
    assert nxt.session_id != live.session_id
    # audit: actor, target, old and new scope; written for exactly this change
    ((actor, details),) = audit_rows(env)
    assert actor == ADMIN
    d = json.loads(details)
    assert d == {
        "target_user": "analyst_a",
        "old_scope": ["Calvin Klein"],
        "new_scope": new_scope,
    }
    # profiles.yaml is untouched, and other users are unaffected
    assert S.load_profiles(env["profiles"])["analyst_a"].brands == ("Calvin Klein",)
    assert load(env)["ceo_demo"].all_products


def test_unknown_brand_refused_nothing_written(env):
    for bad in (["Nope"], ["carhartt"], [" Carhartt"], ["Carhartt", "Carhartt"], ["Carhartt", ""]):
        with pytest.raises(AC.AccessError):
            run(env, "analyst_a", brands=bad)
    nothing_written(env)


def test_catalog_failure_refused_nothing_written(env):
    def boom():
        raise RuntimeError("bq down")

    with pytest.raises(AC.AccessError, match="catalog is unavailable"):
        run(env, "analyst_a", brands=["Carhartt"], catalog=boom)
    nothing_written(env)


def test_audit_failure_aborts(env):
    env["conn"].close()  # audit store unavailable
    with pytest.raises(AC.AccessError, match="audit record"):
        run(env, "analyst_a", brands=["Carhartt"])
    assert not AC.overrides_path(env["data"]).exists()
    assert load(env)["analyst_a"].brands == ("Calvin Klein",)


def test_invalid_actor_aborts(env):
    with pytest.raises(AC.AccessError):
        AC.access_set(
            "analyst_a",
            brands=["Carhartt"],
            actor="a@b.c",
            catalog=lambda: CATALOG,
            audit=env["audit"],
            profiles_path=env["profiles"],
            data_dir=env["data"],
        )
    nothing_written(env)


def test_flags_mutually_exclusive_and_required(env):
    with pytest.raises(AC.AccessError, match="not both"):
        run(env, "analyst_a", brands=["Carhartt"], all=True)
    with pytest.raises(AC.AccessError, match="not both"):
        run(env, "analyst_a", brands=[], all=True)
    for kw in ({}, {"brands": []}):
        with pytest.raises(AC.AccessError, match="Give a brand list"):
            run(env, "analyst_a", **kw)
    nothing_written(env)


def test_unknown_user_refused(env):
    with pytest.raises(AC.AccessError, match="Unknown user"):
        run(env, "nobody", brands=["Carhartt"])
    nothing_written(env)


def test_corrupt_override_fails_closed(env):
    run(env, "analyst_a", brands=["Carhartt"])
    path = AC.overrides_path(env["data"])
    for junk in (
        "{not json",
        "[]",
        json.dumps({"version": 2, "overrides": {}}),
        json.dumps({"version": 1, "overrides": {"ghost": {"brands": ["Acme"]}}}),
        json.dumps({"version": 1, "overrides": {"analyst_a": {"brands": []}}}),
        json.dumps(
            {"version": 1, "overrides": {"analyst_a": {"brands": ["A"], "all_products": True}}}
        ),
        json.dumps({"version": 1, "overrides": {"analyst_a": "all"}}),
    ):
        path.write_text(junk)
        with pytest.raises(ConfigError):
            load(env)  # never falls back to the YAML scope
        before = len(audit_rows(env))
        with pytest.raises(AC.AccessError):
            run(env, "analyst_a", brands=["Acme"])
        assert len(audit_rows(env)) == before and path.read_text() == junk
    path.unlink()  # operator removes the override: YAML scope is back
    assert load(env)["analyst_a"].brands == ("Calvin Klein",)


def test_narrowing_ceo_to_brands_and_widening_back(env):
    change = run(env, "ceo_demo", brands=["Acme"])
    assert change.old.all_products and not change.new.all_products
    assert load(env)["ceo_demo"].brands == ("Acme",)
    ((_, details),) = audit_rows(env)
    assert json.loads(details)["old_scope"] == "all"
    run(env, "ceo_demo", all=True)
    assert load(env)["ceo_demo"].all_products
    assert [json.loads(r[1])["new_scope"] for r in audit_rows(env)] == [["Acme"], "all"]
    # other overrides survive a later change
    run(env, "analyst_a", brands=["Levi's"])
    assert load(env)["ceo_demo"].all_products and load(env)["analyst_a"].brands == ("Levi's",)
    # no temp files left behind
    assert [p.name for p in env["data"].iterdir()] == [AC.OVERRIDES_FILE]


def test_type_confusion_refused(env):
    for kw in ({"all": "no"}, {"all": 1}, {"brands": "Acme"}, {"brands": ["Acme", 5]}):
        with pytest.raises(AC.AccessError):
            run(env, "analyst_a", **kw)
    nothing_written(env)


def test_user_id_echoed_escaped_and_truncated(env):
    with pytest.raises(AC.AccessError) as ei:
        run(env, "\x1b[2Jghost" + "x" * 200, brands=["Acme"])
    msg = str(ei.value)
    assert "\x1b" not in msg and "\\x1b" in msg and "x" * 50 not in msg
    path = AC.overrides_path(env["data"])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "overrides": {"\x1b[2Jghost": {"brands": ["Acme"]}}}))
    with pytest.raises(AC.AccessError) as ei:
        load(env)
    assert "\x1b" not in str(ei.value)


def _write_raw(env, text):
    path = AC.overrides_path(env["data"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.parametrize(
    "raw",
    [
        '{"version":1,"overrides":{"analyst_a":{"brands":["Acme"]},'
        '"analyst_a":{"all_products":true}}}',
        '{"version":true,"overrides":{}}',
        '{"version":1.0,"overrides":{}}',
        '{"version":1,"version":1,"overrides":{}}',
    ],
    ids=["dup-user", "version-bool", "version-float", "dup-version"],
)
def test_duplicate_keys_and_version_type_fail_closed(env, raw):
    _write_raw(env, raw)
    with pytest.raises(AC.AccessError):
        load(env)


def test_override_brand_cap_enforced_at_load(env):
    many = [f"B{i}" for i in range(AC.MAX_BRANDS + 1)]
    _write_raw(env, json.dumps({"version": 1, "overrides": {"analyst_a": {"brands": many}}}))
    with pytest.raises(AC.AccessError, match="too many brands"):
        load(env)


def test_oversized_yaml_scope_is_audited_as_invalid_and_can_be_narrowed(env):
    many = [f"B{i}" for i in range(AC.MAX_BRANDS + 50)]
    env["profiles"].write_text(
        yaml.safe_dump({"profiles": [{"id": "wide", "display_name": "W", "brands": many}]})
    )
    run(env, "wide", brands=["Acme"], catalog=lambda: [*many, "Acme"])
    ((_, details),) = audit_rows(env)
    assert json.loads(details)["old_scope"] == "invalid"
    assert load(env)["wide"].brands == ("Acme",)


def test_stale_old_scope_audited_as_invalid(env):
    run(env, "analyst_a", brands=["Carhartt"])
    # the catalog no longer has Carhartt
    run(env, "analyst_a", brands=["Acme"], catalog=lambda: ["Acme"])
    olds = [json.loads(r[1])["old_scope"] for r in audit_rows(env)]
    assert olds == [["Calvin Klein"], "invalid"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
def test_fifo_override_fails_closed_without_hanging(env):
    path = AC.overrides_path(env["data"])
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    with pytest.raises(AC.AccessError):
        load(env)
    with pytest.raises(AC.AccessError):
        run(env, "analyst_a", brands=["Acme"])
    assert audit_rows(env) == []


def test_oversized_override_file_rejected(env):
    _write_raw(env, " " * (AC.MAX_OVERRIDE_BYTES + 1))
    with pytest.raises(AC.AccessError):
        load(env)


def test_write_failure_after_audit_records_failed_row(env, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(AC.os, "replace", boom)
    with pytest.raises(AC.AccessError, match="could not be written"):
        run(env, "analyst_a", brands=["Carhartt"])
    rows = (
        env["conn"]
        .execute(
            "SELECT outcome FROM audit_event WHERE event_type = 'scope.changed' ORDER BY rowid"
        )
        .fetchall()
    )
    assert [r[0] for r in rows] == ["ok", "failed"]
    assert load(env)["analyst_a"].brands == ("Calvin Klein",)


def test_failed_followup_row_failure_is_logged_not_masking(env, monkeypatch, caplog):
    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(AC.os, "replace", boom)
    real = env["audit"].record
    calls = []

    def flaky(*a, **kw):
        calls.append(kw["outcome"])
        if kw["outcome"] == "failed":
            raise RuntimeError("audit down")
        return real(*a, **kw)

    monkeypatch.setattr(env["audit"], "record", flaky)
    with caplog.at_level("WARNING"), pytest.raises(AC.AccessError, match="could not be written"):
        run(env, "analyst_a", brands=["Carhartt"])
    assert calls == ["ok", "failed"]
    assert "follow-up" in caplog.text


def test_directory_is_fsynced_after_replace(env, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(AC.os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_mode), real(fd)))
    run(env, "analyst_a", brands=["Carhartt"])
    import stat as _st

    assert any(_st.S_ISDIR(m) for m in synced)
