"""Iteration 36: the maintainer feedback triage CLI (FR-47, AC-25.4, R4.2). No network."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

from opsfleet_agent.commands import triage as tg
from opsfleet_agent.store import db
from opsfleet_agent.store.audit import AuditError, AuditLog
from opsfleet_agent.store.feedback import FeedbackRecord, FeedbackStore

ROOT = Path(__file__).resolve().parents[2]
SID = "a" * 32
TID = "b" * 12
MAINT = frozenset({"maint_x"})
GOOD_SQL = (
    "SELECT p.category, SUM(oi.sale_price) AS revenue "
    "FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi "
    "JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id "
    "WHERE oi.status NOT IN ('Cancelled', 'Returned') "
    "GROUP BY p.category ORDER BY revenue DESC LIMIT 10"
)
QUESTION = "Which product categories bring in the most revenue?"
SUMMARY = "Revenue is concentrated in a few apparel categories."


@pytest.fixture
def env(tmp_path):
    conn = db.open_store(tmp_path / "app.db")
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(
        "profiles:\n"
        "  - id: synth_ceo\n    display_name: Synth CEO\n    all_products: true\n"
        "  - id: synth_brand\n    display_name: Synth Brand\n    brands: [Acme]\n",
        encoding="utf-8",
    )
    yield {"conn": conn, "tmp": tmp_path, "profiles": profiles}
    conn.close()


def _spans(tmp: Path, *spans: dict, session: str = SID, turn: str = TID) -> None:
    d = tmp / "traces"
    d.mkdir(exist_ok=True)
    with (d / f"{session}.jsonl").open("a", encoding="utf-8") as fh:
        for s in spans:
            fh.write(json.dumps({"session_id": session, "turn_id": turn, **s}) + "\n")


def _add(env, rating="up", reason=None, comment=None, user="synth_ceo", turn=TID) -> str:
    rec, _ = FeedbackStore(env["conn"]).add(
        user_id=user, session_id=SID, turn_id=turn, trace_id="tr-1",
        rating=rating, reason=reason, comment=comment,
    )  # fmt: skip
    return rec.feedback_id


def _run(env, *argv, actor="maint_x", **kw):
    out = io.StringIO()
    kw.setdefault("pii_scan", lambda text: [])
    kw.setdefault("dry_run", lambda sql, brands: None)
    kw.setdefault("eval_gate", lambda: True)
    code = tg.main(
        ["--as", actor, "--data-dir", str(env["tmp"]), *argv],
        conn=env["conn"], maintainers=MAINT, profiles_path=env["profiles"], out=out, **kw,
    )  # fmt: skip
    return code, out.getvalue()


def _state(env, fid):
    return FeedbackStore(env["conn"]).get(fid).triage_state


def _events(env, event_type=None):
    evs = AuditLog(env["conn"]).events(newest_first=False)
    return [e for e in evs if event_type is None or e.event_type == event_type]


def _rec(rating="down", reason=None) -> FeedbackRecord:
    return FeedbackRecord("f" * 32, "u", SID, TID, None, rating, None, reason, "new", "t")


# --- root cause rules ------------------------------------------------------------------------


def test_triage_root_cause_rules():
    rc = tg.root_cause
    assert rc(_rec(), []) == ("no_trace", [])
    blocked = [{"type": "guard", "name": "output", "verdict": "block"}]
    assert rc(_rec(), blocked)[0] == "guardrail_block"
    assert rc(_rec(), [{"type": "guard", "name": "router", "verdict": "refuse"}])[0] == (
        "guardrail_block"
    )
    sql_err = [{"type": "sql", "name": "run_sql", "status": "error", "error_class": "syntax"}]
    assert rc(_rec(), sql_err) == ("sql_error", ["sql:syntax"])
    assert rc(_rec(), [{"type": "tool", "tool": "run_sql", "error_code": "bytes_cap"}])[0] == (
        "sql_error"
    )
    assert rc(_rec(), [{"type": "role", "name": "analyst", "error_class": "give_up"}])[0] == (
        "sql_error"
    )
    assert rc(_rec(), [{"type": "llm", "name": "analyst", "fallback_used": True}])[0] == (
        "model_down"
    )
    assert rc(_rec(), [{"type": "guard", "name": "grounding", "grounding_flags": 2}])[0] == (
        "verifier_fail"
    )
    assert rc(_rec(), [{"type": "guard", "name": "report", "verdict": "no_draft"}])[0] == (
        "verifier_fail"
    )
    assert rc(_rec(), [{"type": "sql", "name": "run_sql", "rows": 0}])[0] == "empty_result"
    assert rc(_rec(), [{"type": "router", "route": "light"}])[0] == "misroute"
    assert rc(_rec(reason="misunderstood"), [{"type": "turn"}])[0] == "misroute"
    assert rc(_rec(), [{"type": "turn", "duration_ms": 45_000}])[0] == "slow"
    assert rc(_rec(reason="slow"), [{"type": "turn"}])[0] == "slow"
    ok = [{"type": "sql", "name": "run_sql", "rows": 5}, {"type": "turn", "duration_ms": 900}]
    assert rc(_rec(), ok)[0] == "intent_or_format"
    assert rc(_rec(rating="up"), ok) == ("clean", [])
    # Order: a guard block wins over a SQL error in the same turn.
    assert rc(_rec(), blocked + sql_err)[0] == "guardrail_block"
    # An up-rated light turn is not a misroute.
    assert rc(_rec(rating="up"), [{"type": "router", "route": "light"}])[0] == "clean"


# --- access ----------------------------------------------------------------------------------


def test_non_maintainer_refused_before_any_read(env):
    fid = _add(env, rating="down", comment="totals look off")
    for actor in ("synth_ceo", "nobody", "bad id!"):
        code, out = _run(env, "list", actor=actor)
        assert code == tg.EXIT_REFUSED and out.strip() == tg.REFUSED_ROLE
        assert fid not in out
    code, _ = _run(env, "classify", fid, actor="synth_ceo")
    assert code == tg.EXIT_REFUSED and _state(env, fid) == "new" and not _events(env)


def test_maintainers_file_fails_closed(tmp_path):
    assert tg.load_maintainers(tmp_path / "missing.yaml") == frozenset()
    bad = tmp_path / "m.yaml"
    bad.write_text("maintainers: [ok_id, 'bad id', 3]\n", encoding="utf-8")
    assert tg.load_maintainers(bad) == frozenset({"ok_id"})
    bad.write_text("maintainers: nope\n", encoding="utf-8")
    assert tg.load_maintainers(bad) == frozenset()
    assert "support_demo" in tg.load_maintainers(ROOT / "config" / "maintainers.yaml")


def test_production_maintainer_identity_is_documented_as_iam_not_as_flag():
    # D-234: `--as` is a local convenience; the HLD names the IAM principal as the identity.
    hld = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    note = next(line for line in hld.splitlines() if "Maintainer identity (D-234)" in line)
    assert "IAM principal" in note and "--as" in note and "not an authentication" in note
    cfg = (ROOT / "config" / "maintainers.yaml").read_text(encoding="utf-8")
    assert "not authentication" in cfg and "synthetic" in cfg


# --- list / show -----------------------------------------------------------------------------


def test_list_and_show_never_print_raw_pii(env):
    raw_mail = "pat.synthetic@example.org"
    fid = _add(env, rating="down", reason="wrong_numbers", comment=f"mail {raw_mail} please")
    _spans(env["tmp"], {"type": "sql", "name": "run_sql", "rows": 0})
    code, out = _run(env, "list", "--state", "new", "--class", "empty_result")
    assert code == 0 and fid in out and "empty_result" in out
    code, out = _run(env, "list", "--class", "sql_error")
    assert code == 0 and fid not in out
    code, out = _run(env, "show", fid[:10])
    assert code == 0 and "empty_result" in out and "example.org" not in out
    assert raw_mail not in out


def test_unknown_or_ambiguous_id(env):
    code, out = _run(env, "show", "0" * 12)
    assert code == tg.EXIT_REFUSED and "No such" in out
    code, out = _run(env, "show", "XYZ")
    assert code == tg.EXIT_REFUSED


# --- classify / dismiss ----------------------------------------------------------------------


def test_classify_and_dismiss_are_audited(env):
    fid = _add(env, rating="down", reason="slow")
    _spans(env["tmp"], {"type": "turn", "duration_ms": 31_000})
    code, out = _run(env, "classify", fid)
    assert code == 0 and "slow" in out and _state(env, fid) == "triaged"
    (ev,) = _events(env, "feedback.triaged")
    assert ev.outcome == "ok" and ev.actor_user_id == "maint_x"
    assert list(ev.target_ids) == [fid] and ev.session_id == SID and ev.turn_id == TID
    assert ev.details == {"root_cause": "slow", "triage_state": "triaged"}
    # A second classify is refused (and the refusal is audited); the state stays.
    code, _ = _run(env, "classify", fid)
    assert code == tg.EXIT_REFUSED and _state(env, fid) == "triaged"
    assert [e.outcome for e in _events(env, "feedback.triaged")] == ["ok", "refused"]

    code, _ = _run(env, "dismiss", fid, "--reason", "not_a_bug")
    assert code == 0 and _state(env, fid) == "dismissed"
    (ev,) = _events(env, "feedback.dismissed")
    assert ev.details["dismiss_reason"] == "not_a_bug"
    assert ev.details["triage_state"] == "dismissed"


def test_state_change_aborts_when_audit_fails(env, monkeypatch):
    fid = _add(env, rating="down")

    def boom(self, *a, **k):
        raise AuditError("disk full")

    monkeypatch.setattr(AuditLog, "record", boom)
    code, out = _run(env, "dismiss", fid, "--reason", "duplicate")
    assert code == tg.EXIT_REFUSED and "audit write failed" in out
    assert _state(env, fid) == "new"
    cases = env["tmp"] / "cases"
    code, _ = _run(env, "add-eval", fid, "--question", QUESTION, "--cases-dir", str(cases))
    assert code == tg.EXIT_REFUSED and _state(env, fid) == "new" and not cases.exists()


# --- add-eval --------------------------------------------------------------------------------


def test_add_eval_writes_case(env):
    fid = _add(env, rating="down", reason="wrong_numbers", user="synth_brand")
    _spans(env["tmp"], {"type": "guard", "name": "grounding", "grounding_flags": 1})
    cases = env["tmp"] / "cases"
    seen: list[str] = []
    code, out = _run(
        env, "add-eval", fid, "--question", "Revenue for Acme in 2024, mail ops@example.org",
        "--cases-dir", str(cases), pii_scan=lambda t: seen.append(t) or [],
    )  # fmt: skip
    assert code == 0, out
    path = cases / f"triage_{fid[:12]}.yaml"
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "example.org" not in case["input"] and "example.org" not in seen[0]
    assert case["tags"] == ["regression", "triage", "verifier_fail"]
    assert case["session"] == {"profile": "synth_brand"}
    assert case["expect"]["outcome"] == "answered"
    assert "@" in case["expect"]["must_not_contain"]
    assert case["skip"].startswith("triage draft")
    assert _state(env, fid) == "triaged"
    (ev,) = _events(env, "eval.case_added")
    assert ev.outcome == "ok" and ev.details["root_cause"] == "verifier_fail"
    # The eval runner accepts the drafted case.
    sys.path.insert(0, str(ROOT))
    from evals import run as eval_run

    (loaded,) = eval_run.load_cases(cases)
    assert loaded.turns == [case["input"]] and loaded.skip == case["skip"]
    # Never overwrites a reviewed case.
    code, out = _run(env, "add-eval", fid, "--question", QUESTION, "--cases-dir", str(cases))
    assert code == tg.EXIT_REFUSED and "could not be written" in out


def test_add_eval_refuses_remaining_pii(env):
    fid = _add(env, rating="down")
    cases = env["tmp"] / "cases"
    code, out = _run(
        env, "add-eval", fid, "--question", "Orders of Marlowe Finch", "--expect-refusal",
        "--cases-dir", str(cases), pii_scan=lambda t: ["PERSON"],
    )  # fmt: skip
    assert code == tg.EXIT_REFUSED and "PERSON" in out and "Marlowe" not in out
    assert not cases.exists() and _state(env, fid) == "new"
    (ev,) = _events(env, "eval.case_added")
    assert ev.outcome == "refused" and ev.details["gate"] == "pii"

    def broken(text):
        raise RuntimeError("model missing")

    code, out = _run(
        env, "add-eval", fid, "--question", QUESTION, "--cases-dir", str(cases), pii_scan=broken
    )
    assert code == tg.EXIT_REFUSED and "could not run" in out and not cases.exists()


# --- promote ---------------------------------------------------------------------------------


def _clean_turn(env, sql=GOOD_SQL):
    _spans(
        env["tmp"],
        {"type": "router", "route": "analyst"},
        {"type": "sql", "name": "run_sql", "rows": 10, "sql_text": sql},
        {"type": "turn", "duration_ms": 1200},
    )


def _promote(env, fid, sql_file, **kw):
    return _run(
        env, "promote", fid, "--question", QUESTION, "--summary", SUMMARY,
        "--sql-file", str(sql_file), **kw,
    )  # fmt: skip


def test_promote_runs_pii_scan_and_dry_run(env):
    fid = _add(env, rating="up")
    _clean_turn(env)
    sql_file = env["tmp"] / "q.sql"
    sql_file.write_text(GOOD_SQL, encoding="utf-8")
    calls: list[tuple] = []
    code, out = _promote(
        env, fid, sql_file,
        pii_scan=lambda t: calls.append(("pii", t)) or [],
        dry_run=lambda sql, brands: calls.append(("dry_run", tuple(brands))) or None,
        eval_gate=lambda: calls.append(("eval",)) or True,
    )  # fmt: skip
    assert code == 0, out
    assert [c[0] for c in calls] == ["pii", "pii", "dry_run", "eval"]
    assert calls[2] == ("dry_run", ("agnostic",))
    path = env["tmp"] / "golden_candidates" / f"triage-{fid[:12]}.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    (trio,) = doc["trios"]
    assert trio["question"] == QUESTION and trio["brands"] == ["agnostic"]
    assert _state(env, fid) == "promoted"
    (ev,) = _events(env, "golden.promoted")
    assert ev.outcome == "ok" and len(ev.details["sql_hash"]) == 64
    assert GOOD_SQL not in out and QUESTION not in out

    # The PII detector blocks; then a failing dry run blocks. Nothing changes either time.
    for kw, gate in (
        ({"pii_scan": lambda t: ["PERSON"]}, "pii"),
        ({"dry_run": lambda sql, brands: "invalid_query"}, "dry_run"),
        ({"dry_run": lambda sql, brands: 1 / 0}, "dry_run"),
    ):
        fid2 = _add(env, rating="up", turn="c" * 12)
        _spans(env["tmp"], {"type": "turn"}, turn="c" * 12)
        code, out = _promote(env, fid2, sql_file, **kw)
        assert code == tg.EXIT_REFUSED and _state(env, fid2) == "new", out
        assert _events(env, "golden.promoted")[-1].details["gate"] == gate
        assert len(list((env["tmp"] / "golden_candidates").iterdir())) == 1
        env["conn"].execute("DELETE FROM feedback WHERE feedback_id = ?", (fid2,))
        env["conn"].commit()


def test_promote_blocked_on_eval_regression(env):
    fid = _add(env, rating="up")
    _clean_turn(env)
    sql_file = env["tmp"] / "q.sql"
    sql_file.write_text(GOOD_SQL, encoding="utf-8")
    code, out = _promote(env, fid, sql_file, eval_gate=lambda: False)
    assert code == tg.EXIT_REFUSED and "not green" in out
    assert _state(env, fid) == "new"
    assert not (env["tmp"] / "golden_candidates").exists()
    (ev,) = _events(env, "golden.promoted")
    assert ev.outcome == "refused" and ev.details["gate"] == "eval"


def test_promote_eligibility_and_sql_source(env):
    sql_file = env["tmp"] / "q.sql"
    sql_file.write_text(GOOD_SQL, encoding="utf-8")
    down = _add(env, rating="down")
    _clean_turn(env)
    code, out = _promote(env, down, sql_file)
    assert code == tg.EXIT_REFUSED and "only an up-rated" in out
    env["conn"].execute("DELETE FROM feedback")
    env["conn"].commit()
    up = _add(env, rating="up", turn="d" * 12)
    sanitized = "SELECT status FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE id = ?"
    _spans(env["tmp"], {"type": "sql", "name": "run_sql", "rows": 3, "sql_text": sanitized},
           turn="d" * 12)  # fmt: skip
    # Trace SQL has literals replaced by `?`: refused, the reviewed SQL must come from a file.
    code, out = _run(env, "promote", up, "--question", QUESTION, "--summary", SUMMARY)
    assert code == tg.EXIT_REFUSED and "--sql-file" in out
    # A write the seed validator rejects (SQL policy) never reaches the dry run.
    bad = env["tmp"] / "bad.sql"
    bad.write_text("DELETE FROM `bigquery-public-data.thelook_ecommerce.users`", encoding="utf-8")
    dry: list[str] = []
    code, out = _promote(env, up, bad, dry_run=lambda s, b: dry.append(s))
    assert code == tg.EXIT_REFUSED and not dry and _state(env, up) == "new"
    # Figures in the summary are refused by the validator too.
    code, out = _run(
        env, "promote", up, "--question", QUESTION, "--summary", "Revenue was 12345.",
        "--sql-file", str(sql_file),
    )  # fmt: skip
    assert code == tg.EXIT_REFUSED and "figures" in out


def test_promote_sql_file_read_is_bounded(env, monkeypatch):
    """D-231: --sql-file reads at most MAX_SQL_FILE_BYTES + 1 bytes, never the whole file."""
    fid = _add(env, rating="up")
    _clean_turn(env)
    big = env["tmp"] / "big.sql"
    big.write_bytes(b"-- " + b"x" * (tg.MAX_SQL_FILE_BYTES * 4))
    sizes: list[int] = []
    real_open = Path.open

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.fh.__exit__(*exc)

        def read(self, n=-1):
            sizes.append(n)
            return self.fh.read(n)

    def spy_open(self, *a, **kw):
        fh = real_open(self, *a, **kw)
        return Spy(fh) if self == big else fh

    def no_whole_read(self):
        raise AssertionError("the whole file was read")

    monkeypatch.setattr(Path, "open", spy_open)
    monkeypatch.setattr(Path, "read_bytes", no_whole_read)
    code, out = _promote(env, fid, big, eval_gate=lambda: True)
    monkeypatch.undo()
    assert code == tg.EXIT_REFUSED and "too large" in out
    assert sizes == [tg.MAX_SQL_FILE_BYTES + 1]
    assert _state(env, fid) == "new"
