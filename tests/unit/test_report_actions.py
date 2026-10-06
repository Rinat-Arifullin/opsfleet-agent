"""Iteration 33: rename, Markdown export and "retry report" (AC-21.9, AC-21.12, AC-21.14,
AC-21.15). Offline: fake Gemini and BigQuery, synthetic data only."""

from __future__ import annotations

from typing import Any

import pytest

from opsfleet_agent import commands
from opsfleet_agent.commands import report_actions as ra
from opsfleet_agent.graph import graph as gr
from opsfleet_agent.graph.budget import TURN_CAPS, TurnKind
from opsfleet_agent.reports.library import NOT_FOUND_TEXT
from opsfleet_agent.store.audit import AuditLog
from opsfleet_agent.store.reports import ReportError
from tests.unit.test_library import ACME, WIDE, _add
from tests.unit.test_reports import PROFILE, ReportModel
from tests.unit.test_reports import detector as _detector_fixture  # noqa: F401 (pytest fixture)
from tests.unit.test_reports import make_env as _make_env_fixture  # noqa: F401 (pytest fixture)
from tests.unit.test_reports import settings as _settings_fixture  # noqa: F401 (pytest fixture)
from tests.unit.test_reports import store as _store_fixture  # noqa: F401 (pytest fixture)

detector = _detector_fixture
make_env = _make_env_fixture
settings = _settings_fixture
store = _store_fixture

SID = "0123456789abcdef0123456789abcdef"  # synthetic 32-hex session id
TID = "0123456789ab"


@pytest.fixture
def audit(store) -> AuditLog:
    return AuditLog(store.conn)


def _ctx(store, audit, tmp_path, detector, user="analyst_a", scope=ACME):
    return commands.CommandContext(
        user_id=user, session_id=SID, report_store=store, scope=scope, audit_log=audit,
        export_dir=tmp_path / "exports", detector=detector,
    )  # fmt: skip


def _own(tmp_path, user="analyst_a"):
    return tmp_path / "exports" / ra.owner_folder_name(user)


def _events(audit, user="analyst_a") -> list[tuple[str, str, tuple[str, ...]]]:
    return [
        (e.event_type, e.outcome, tuple(e.target_ids))
        for e in audit.events(user_id=user, newest_first=False)
    ]


# --- named: rename + export owner-only and audited, /retry is a fixed turn -----------------------


def test_rename_export_retry_owner_only_audited(store, audit, tmp_path, detector) -> None:
    rid = _add(store, "k1", title="Synthetic orders")
    theirs = _add(store, "k2", owner="analyst_b", title="Synthetic other")
    mine = _ctx(store, audit, tmp_path, detector)

    out = commands.dispatch(f"/rename {rid} Weekly synthetic orders", mine).text
    assert out == f"Renamed R-{rid} to: Weekly synthetic orders"
    assert store.get(rid, "analyst_a").title == "Weekly synthetic orders"

    out = commands.dispatch(f"/export R-{rid}", mine).text
    path = _own(tmp_path) / f"R-{rid}.md"
    assert out == f"Exported R-{rid} to {path.resolve()}" and path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Weekly synthetic orders\n") and f"R-{rid}" in text
    assert "SELECT" not in text  # D-151a: no SQL in an export
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)

    # another user's report: "not found", nothing written, nothing audited for it
    assert commands.dispatch(f"/rename {theirs} Mine now", mine).text == NOT_FOUND_TEXT
    assert commands.dispatch(f"/export {theirs}", mine).text == NOT_FOUND_TEXT
    assert store.get(theirs, "analyst_b").title == "Synthetic other"
    assert not (_own(tmp_path) / f"R-{theirs}.md").exists()
    assert _events(audit) == [
        (ra.RENAMED, "ok", (rid,)),
        (ra.EXPORTED, "ok", (rid,)),
    ]
    assert _events(audit, "analyst_b") == []

    # /retry is a code-owned fixed turn; the CLI runs it like a typed "retry report"
    res = commands.dispatch("/retry", mine)
    assert res.turn == gr.RETRY_MESSAGE == commands.RETRY_TURN_TEXT and res.text == ""
    assert commands.dispatch("/retry now", mine).turn is None


def test_other_user_and_scope_drift_get_not_found(store, audit, tmp_path, detector) -> None:
    wide = _add(store, "k1", title="Synthetic wide", brands=("Acme", "Zenith"))
    theirs = _add(store, "k2", owner="analyst_b", title="Synthetic theirs")
    kw = dict(store=store, audit=audit, owner="analyst_a", scope=ACME, session_id=SID,
              turn_id=TID, export_dir=tmp_path / "exports")  # fmt: skip
    for rid in (wide, theirs, "f" * 32, "../" * 5):
        out = ra.export_report(report_id=rid, **kw)
        assert out == {"ok": False, "error": {"code": ra.NOT_FOUND, "message": NOT_FOUND_TEXT,
                                              "retryable": False}}  # fmt: skip
        kw2 = {k: v for k, v in kw.items() if k != "export_dir"}
        res = ra.rename_report(report_id=rid, title="New synthetic", detector=detector, **kw2)
        assert res["ok"] is False and res["error"]["code"] == ra.NOT_FOUND
    # the same id in the wider scope is found
    ok = ra.export_report(report_id=wide, **dict(kw, scope=WIDE))
    assert ok["ok"] is True and ok["report_id"] == f"R-{wide}"
    assert ok["path"].endswith(f"R-{wide}.md")
    # a title phrase matches only the caller's own reports
    ctx = _ctx(store, audit, tmp_path, detector, scope=WIDE)
    assert commands.dispatch('/rename "Synthetic theirs" Taken', ctx).text == NOT_FOUND_TEXT


def test_export_refuses_traversal(store, audit, tmp_path, detector) -> None:
    rid = _add(store, "k1", title="Synthetic orders")
    ctx = _ctx(store, audit, tmp_path, detector)
    base = tmp_path / "exports"
    for name in ("../escape.md", "..md", "/tmp/x.md", "sub/x.md", ".hidden.md", "x.txt",
                 "a\\b.md", "~x.md"):  # fmt: skip
        with pytest.raises(ra.ActionError) as err:
            ra.export_path(base, name, rid)
        assert err.value.code == ra.INVALID_ARGS
    out = commands.dispatch(f"/export {rid} ../escape.md", ctx).text
    assert "exports folder" in out
    assert not (tmp_path / "escape.md").exists()
    # a symlink planted in the owner's export folder that points outside is refused too
    own = _own(tmp_path)
    own.mkdir(parents=True, exist_ok=True)
    (own / "link.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(ra.ActionError):
        ra.export_path(own, "link.md", rid)
    assert commands.dispatch(f"/export {rid} link.md", ctx).text.startswith("Exports are")
    assert not (tmp_path / "outside.md").exists()
    assert _events(audit) == []  # a refused export writes no audit row
    # a plain name is fine; D-228: a second export to the same name is refused, not overwritten
    assert commands.dispatch(f"/export {rid} weekly.md", ctx).text.startswith("Exported")
    assert commands.dispatch(f"/export {rid} weekly.md", ctx).text == ra._EXISTS_TEXT
    assert sorted(p.name for p in own.iterdir()) == ["link.md", "weekly.md"]


def test_rename_title_is_validated_in_code(store, audit, tmp_path, detector) -> None:
    rid = _add(store, "k1", title="Synthetic orders")
    kw = dict(store=store, audit=audit, owner="analyst_a", scope=ACME, session_id=SID,
              turn_id=TID, report_id=rid, detector=detector)  # fmt: skip
    for bad in ("", "   ", "x" * 121, "Tab\there", "Bell\x07", 42,
                "Mail jane.doe@example.com", "see api_key=synthetic123"):  # fmt: skip
        out = ra.rename_report(title=bad, **kw)
        assert out["ok"] is False and out["error"]["code"] == ra.INVALID_ARGS, bad
    assert store.get(rid, "analyst_a").title == "Synthetic orders"
    assert _events(audit) == []  # an invalid title is refused before the audit row
    ok = ra.rename_report(title="x" * 120, **kw)
    assert ok == {"ok": True, "report_id": f"R-{rid}", "title": "x" * 120}


def test_audit_first_failure_aborts(store, audit, tmp_path, detector, monkeypatch) -> None:
    rid = _add(store, "k1", title="Synthetic orders")
    ctx = _ctx(store, audit, tmp_path, detector)

    def boom(*a: Any, **k: Any):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(audit, "record", boom)
    assert commands.dispatch(f"/rename {rid} Changed", ctx).text == ra.AUDIT_FAILED_TEXT
    assert commands.dispatch(f"/export {rid}", ctx).text == ra.AUDIT_FAILED_TEXT
    assert store.get(rid, "analyst_a").title == "Synthetic orders"
    assert not (_own(tmp_path) / f"R-{rid}.md").exists()
    # no audit log at all: fail closed as well
    none = commands.CommandContext(user_id="analyst_a", session_id=SID, report_store=store,
                                   scope=ACME, export_dir=tmp_path / "exports")  # fmt: skip
    assert commands.dispatch(f"/export {rid}", none).text == ra.AUDIT_FAILED_TEXT


def test_failure_after_audit_records_failed(store, audit, tmp_path, detector, monkeypatch) -> None:
    rid = _add(store, "k1", title="Synthetic orders")
    ctx = _ctx(store, audit, tmp_path, detector)

    def broken_rename(*a: Any, **k: Any):
        raise ReportError("synthetic")

    def broken_write(*a: Any, **k: Any):
        raise OSError("synthetic")

    monkeypatch.setattr(store, "rename", broken_rename)
    monkeypatch.setattr(ra, "_write_export", broken_write)
    assert commands.dispatch(f"/rename {rid} Changed", ctx).text == NOT_FOUND_TEXT
    out = ra.export_report(store=store, audit=audit, owner="analyst_a", scope=ACME,
                           session_id=SID, turn_id=TID, report_id=rid,
                           export_dir=tmp_path / "exports")  # fmt: skip
    assert out["ok"] is False and out["error"]["code"] == ra.EXPORT_FAILED
    assert _events(audit) == [
        (ra.RENAMED, "ok", (rid,)), (ra.RENAMED, "failed", (rid,)),
        (ra.EXPORTED, "ok", (rid,)), (ra.EXPORTED, "failed", (rid,)),
    ]  # fmt: skip


def test_ambiguous_title_lists_rows(store, audit, tmp_path, detector) -> None:
    a = _add(store, "k1", title="Synthetic margin north")
    b = _add(store, "k2", title="Synthetic margin south")
    ctx = _ctx(store, audit, tmp_path, detector)
    out = commands.dispatch('/export "synthetic margin"', ctx).text
    assert "1." in out and "2." in out and not (tmp_path / "exports").exists()
    assert set(ctx.listing) == {a, b}
    assert commands.dispatch("/export 2", ctx).text.startswith(f"Exported R-{ctx.listing[1]}")


# --- named: retry report re-runs writer + verifier on the stored ledger, no SQL -------------------


def test_retry_report_reuses_ledger_no_sql(make_env, store, monkeypatch) -> None:
    model = ReportModel()
    env = make_env(model)

    def fail(**kw: Any):
        raise ReportError("synthetic failure")

    with monkeypatch.context() as m:
        m.setattr(store, "save", fail)
        env.ask("Write a report on complete orders")
        out = env.ask("save")
    assert out.outcome == "report_unsaved" and store.count() == 0
    sql_before, analyst_before = len(env.client.calls), model.kinds().count("analyst")
    calls_before = len(model.calls)

    turn = commands.dispatch("/retry", commands.CommandContext(user_id="u", session_id=SID)).turn
    out = env.ask(turn)
    assert out.outcome == "report_pending" and gr.REPORT_PROMPT in out.text
    new = model.kinds()[calls_before:]
    assert set(new) <= {"writer", "verifier"} and "writer" in new
    assert len(new) <= TURN_CAPS[TurnKind.RETRY_REPORT].llm_calls == 8
    assert TURN_CAPS[TurnKind.RETRY_REPORT].sql_queries == 0
    assert len(env.client.calls) == sql_before  # no SQL ran
    assert model.kinds().count("analyst") == analyst_before
    writer_msgs = [msgs for k, msgs in model.calls[calls_before:] if k == "writer"][0]
    assert "order_items" in " ".join(str(getattr(m, "content", m)) for m in writer_msgs)
    assert [s for s in env.spans if s[0] == "role" and s[1] == gr.RETRY_NODE]
    saved = env.ask("save")
    assert saved.outcome == "report_saved" and store.count() == 1
    # saved clears the marker: nothing left to retry
    assert env.ask("retry report").text == gr.NO_RETRY_TEXT


def test_retry_without_a_ledger_says_so(make_env) -> None:
    env = make_env(ReportModel())
    out = env.ask("retry report")
    assert out.text == gr.NO_RETRY_TEXT and len(env.client.calls) == 0


def test_retry_is_capped(make_env, store, monkeypatch) -> None:
    model = ReportModel()
    env = make_env(model)
    monkeypatch.setattr(gr, "_build_report", lambda ctx, **kw: None)  # the writer always fails
    out = env.ask("Write a report on complete orders")
    assert out.outcome == "report_failed" and gr.RETRY_HINT_TEXT in out.text
    sql_before = len(env.client.calls)
    for _ in range(gr.RETRY_LIMIT):
        again = env.ask("retry report")
        assert again.text == gr.RETRY_FAILED_TEXT
    assert env.ask("retry report").text == gr.NO_RETRY_TEXT  # out of attempts
    assert len(env.client.calls) == sql_before and store.count() == 0
    assert PROFILE.user_id  # the session owner ran every retry


# --- D-228: per-owner export folder, symlink refusal, 0700, never overwrite ----------------------


def test_exports_are_per_owner_and_never_overwrite(store, audit, tmp_path, detector) -> None:
    import os

    mine = _add(store, "k1", title="Synthetic orders")
    theirs = _add(store, "k2", owner="analyst_b", title="Synthetic other")
    a = _ctx(store, audit, tmp_path, detector)
    b = _ctx(store, audit, tmp_path, detector, user="analyst_b")
    # the same file name for two owners lands in two folders; the folder name hides the id
    assert commands.dispatch(f"/export {mine} weekly.md", a).text.startswith("Exported")
    assert commands.dispatch(f"/export {theirs} weekly.md", b).text.startswith("Exported")
    pa, pb = _own(tmp_path) / "weekly.md", _own(tmp_path, "analyst_b") / "weekly.md"
    assert f"R-{mine}" in pa.read_text() and f"R-{theirs}" in pb.read_text()
    assert "analyst" not in ra.owner_folder_name("analyst_a")
    # a default name never overwrites: the second export gets a -2 suffix
    assert commands.dispatch(f"/export {mine}", a).text.endswith(f"R-{mine}.md")
    assert commands.dispatch(f"/export {mine}", a).text.endswith(f"R-{mine}-2.md")
    # 0700 is re-applied on every use
    os.chmod(tmp_path / "exports", 0o755)
    os.chmod(_own(tmp_path), 0o755)
    assert commands.dispatch(f"/export {mine} again.md", a).text.startswith("Exported")
    assert oct((tmp_path / "exports").stat().st_mode & 0o777) == oct(0o700)
    assert oct(_own(tmp_path).stat().st_mode & 0o777) == oct(0o700)
    n_events = len(_events(audit))
    # a symlinked owner folder is refused before the audit row and nothing is written
    outside = tmp_path / "outside"
    outside.mkdir()
    c = _ctx(store, audit, tmp_path, detector, user="analyst_c")
    rc = _add(store, "k3", owner="analyst_c", title="Synthetic third")
    _own(tmp_path, "analyst_c").symlink_to(outside)
    assert commands.dispatch(f"/export {rc}", c).text == ra._SYMLINK_TEXT
    assert list(outside.iterdir()) == [] and len(_events(audit)) == n_events
    # a symlinked exports folder is refused too
    other = tmp_path / "other"
    (other / "exports").mkdir(parents=True)
    (other / "real").mkdir()
    (other / "exports").rmdir()
    (other / "exports").symlink_to(other / "real")
    d = commands.CommandContext(
        user_id="analyst_a", session_id=SID, report_store=store, scope=ACME, audit_log=audit,
        export_dir=other / "exports", detector=detector,
    )  # fmt: skip
    assert commands.dispatch(f"/export {mine}", d).text == ra._SYMLINK_TEXT
    assert list((other / "real").iterdir()) == []
