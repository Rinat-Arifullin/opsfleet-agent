"""Live seeding of `session:` fields (iteration live-1). Offline: real stores on tmp_path,
fake runtime, graph and sink; no network."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from evals import langfuse_dataset as D
from evals import live_seed as S
from evals import live_sut as L
from evals.run import Case, CaseError, Harness, RunContext, SutResult, _build_case, run_case

from opsfleet_agent.graph.degraded import DegradedGraph
from opsfleet_agent.persona import builtin_persona, parse_persona
from opsfleet_agent.reports.schema import missing_sections
from opsfleet_agent.session import Profile
from opsfleet_agent.store.audit import SESSION_ID_RE, AuditLog
from opsfleet_agent.store.db import open_store
from opsfleet_agent.store.preferences import SQLitePreferenceStore
from opsfleet_agent.store.reports import ReportStore

ANALYST = Profile("analyst_a", "Analyst A", brands=("Acme",))
CEO = Profile("ceo_demo", "CEO", all_products=True)
PROFILES = {"analyst_a": ANALYST, "ceo_demo": CEO}


def case(session: dict[str, Any], turns=("q1",)) -> Case:
    return Case(id="c1", suite="golden", turns=list(turns), session=session)


@dataclass
class Services:
    persona: Any
    preferences: Any = None


class FakeGraph:
    """Records each turn's user id and the persona the graph would load for it."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.turns: list[tuple[str, str, str]] = []
        self.prefs_seen: list[dict] = []

    def run_turn(self, text, *, session, turn_id):
        self.turns.append((text, session.profile.user_id, self.services.persona().version))
        if self.services.preferences is not None:  # what load_context would read this turn
            uid = session.profile.user_id
            self.prefs_seen.append(dict(self.services.preferences.load(uid).preferences))
        return SimpleNamespace(outcome="answered", text=f"answer to {text}", label="data",
                               llm_calls=1, sql_queries=0)  # fmt: skip


class FakeSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.client = SimpleNamespace(flush=lambda: None)

    def traced(self, fn, **kw):
        self.calls.append(kw)
        return fn

    def trace_id_for(self, turn_id):
        return f"lf-{turn_id}"

    def shutdown(self):
        pass


class Tracer:
    def __init__(self) -> None:
        self.trace_dir: Path | None = None
        self.session_id = ""

    @property
    def path(self) -> Path:
        return Path(self.trace_dir) / f"{self.session_id}.jsonl"


def build_runtime(db: Path) -> SimpleNamespace:
    conn = open_store(db)  # sqlite: build on the thread that uses it, as the real factory does
    prefs = SQLitePreferenceStore(conn)
    services = Services(persona=builtin_persona, preferences=prefs)
    return SimpleNamespace(
        report_store=ReportStore(conn), audit_log=AuditLog(conn), tracer=Tracer(),
        preference_store=prefs,
        graph=FakeGraph(services), langfuse=FakeSink(), cancel=lambda: None, close=conn.close,
    )  # fmt: skip


@pytest.fixture
def rt(tmp_path):
    runtime = build_runtime(tmp_path / "app.db")
    yield runtime
    runtime.close()


# -- namespacing and reports


def test_run_user_id_is_a_valid_distinct_profile_id():
    uid = S.run_user_id("analyst_a", "1a2b3c4d")
    assert uid == "analyst_a.ev1a2b3c4d"
    long = S.run_user_id("x" * 64, "1a2b3c4d")
    assert len(long) == 64 and long.endswith(".ev1a2b3c4d")


def test_reports_seeded_through_the_store_with_owner_mapping(rt, tmp_path):
    c = case({"profile": "analyst_a", "saved_reports": [
        {"id": "R-DEMO-0001", "title": "Mine", "owner": "analyst_a", "tags": ["returns"]},
        {"id": "R-DEMO-0002", "title": "Also mine", "owner": "self"},
        {"id": "R-DEMO-0003", "title": "Default owner"},
        {"id": "R-DEMO-0900", "title": "Theirs", "owner": "analyst_b"},
    ]})  # fmt: skip
    with S.seeded(rt, c, ANALYST, session_id="s1", data_dir=tmp_path, tag="t1") as seed:
        me = seed.profile.user_id
        assert me == "analyst_a.evt1" and seed.base_user_id == "analyst_a"
        mine = rt.report_store.list(me)
        assert sorted(r.title for r in mine) == [
            "R-DEMO-0001 Mine", "R-DEMO-0002 Also mine", "R-DEMO-0003 Default owner",
        ]  # fmt: skip
        assert rt.report_store.list("analyst_a") == []  # the real user is never touched
        theirs = rt.report_store.list("analyst_b.evt1")
        assert [r.title for r in theirs] == ["R-DEMO-0900 Theirs"]
        tagged = next(r for r in mine if r.title.endswith("Mine"))
        assert list(tagged.tags) == ["returns"]
        assert tagged.scope_snapshot == {"all": False, "brands": ["Acme"]}
        assert missing_sections(tagged.body_markdown) == []


def test_each_case_gets_its_own_namespace(rt, tmp_path):
    c = case({"profile": "analyst_a", "saved_reports": [{"title": "One"}]})
    with S.seeded(rt, c, ANALYST, session_id="s1", data_dir=tmp_path) as a:
        pass
    with S.seeded(rt, c, ANALYST, session_id="s2", data_dir=tmp_path) as b:
        pass
    assert a.profile.user_id != b.profile.user_id
    assert len(rt.report_store.list(a.profile.user_id)) == 1
    assert len(rt.report_store.list(b.profile.user_id)) == 1


def test_all_products_profile_snapshot(rt, tmp_path):
    c = case({"profile": "ceo_demo", "saved_reports": [{"title": "Board pack", "owner": "self"}]})
    with S.seeded(rt, c, CEO, session_id="s1", data_dir=tmp_path) as seed:
        assert seed.reports[0].scope_snapshot == {"all": True, "brands": []}


@pytest.mark.parametrize("bad", [
    [{"id": "R-1"}],  # no title
    [{"title": "x", "body": "raw"}],  # unknown key
    "not a list",
    [{"title": "x"}] * (S.MAX_SEED_REPORTS + 1),
])  # fmt: skip
def test_bad_report_seeds_are_case_errors(rt, tmp_path, bad):
    with pytest.raises(CaseError), S.seeded(
        rt, case({"saved_reports": bad}), ANALYST, session_id="s", data_dir=tmp_path
    ):
        pass


def test_reports_need_a_store(tmp_path):
    runtime = SimpleNamespace(report_store=None)
    with pytest.raises(CaseError, match="report store"), S.seeded(
        runtime, case({"saved_reports": [{"title": "x"}]}), ANALYST, session_id="s",
        data_dir=tmp_path,
    ):  # fmt: skip
        pass


# -- persona


@pytest.mark.parametrize("name", sorted(S.PERSONA_PRESETS))
def test_presets_pass_the_production_validator(name):
    persona = parse_persona(S.PERSONA_PRESETS[name].encode())
    assert persona.version.startswith(f"eval-{name}-")


def test_persona_applied_audited_and_restored(rt, tmp_path):
    before = rt.graph.services.persona
    c = case({"profile": "analyst_a", "persona": "formal"})
    with S.seeded(rt, c, ANALYST, session_id="s1", data_dir=tmp_path, tag="t2") as seed:
        assert seed.persona_version and seed.persona_version.startswith("eval-formal-")
        assert rt.graph.services.persona().version == seed.persona_version
        rows = rt.audit_log.events(user_id=seed.profile.user_id)
        assert [(e.event_type, e.outcome) for e in rows] == [("persona.changed", "ok")]
    assert rt.graph.services.persona is before
    assert (tmp_path / "seed" / "t2" / "persona.md").is_file()
    assert not (Path(__file__).parents[2] / "data" / "seed" / "t2").exists()


def test_persona_restored_when_the_case_raises(rt, tmp_path):
    before = rt.graph.services.persona
    with pytest.raises(RuntimeError), S.seeded(
        rt, case({"persona": "formal"}), ANALYST, session_id="s", data_dir=tmp_path
    ):
        raise RuntimeError("turn failed")
    assert rt.graph.services.persona is before


def test_persona_reaches_the_graph_behind_degraded(rt, tmp_path):
    inner = rt.graph
    rt.graph = DegradedGraph.__new__(DegradedGraph)
    rt.graph.inner = inner
    with S.seeded(rt, case({"persona": "casual"}), ANALYST, session_id="s", data_dir=tmp_path):
        assert inner.services.persona().version.startswith("eval-casual-")


def test_unknown_preset_and_missing_audit_fail(rt, tmp_path):
    with pytest.raises(CaseError, match="unknown preset"), S.seeded(
        rt, case({"persona": "pirate"}), ANALYST, session_id="s", data_dir=tmp_path
    ):
        pass
    rt.audit_log = None
    with pytest.raises(CaseError, match="audit"), S.seeded(
        rt, case({"persona": "formal"}), ANALYST, session_id="s", data_dir=tmp_path
    ):
        pass


# -- session keys and setup turns


@pytest.mark.parametrize("key", ["memory", "notes"])
def test_unsupported_keys_fail_clearly(rt, tmp_path, key):
    with pytest.raises(CaseError, match="not supported"), S.seeded(
        rt, case({"profile": "analyst_a", key: {"format": "table"}}), ANALYST, session_id="s",
        data_dir=tmp_path,
    ):  # fmt: skip
        pass


# -- preferences (iteration 39b, D-239)


def test_preferences_seeded_for_the_run_user_and_reset(rt, tmp_path):
    c = case({"profile": "analyst_a", "preferences": {"format": "table", "charts": False}})
    with S.seeded(rt, c, ANALYST, session_id="s", data_dir=tmp_path) as seed:
        assert seed.preferences == {"format": "table", "charts": False}
        assert rt.preference_store.load(seed.profile.user_id).preferences == seed.preferences
        assert rt.preference_store.load("analyst_a").preferences == {}  # never the real user
        uid = seed.profile.user_id
    assert rt.preference_store.load(uid).preferences == {}  # reset after the case


def test_preferences_reset_when_the_case_raises(rt, tmp_path):
    c = case({"profile": "analyst_a", "preferences": {"depth": "brief"}})
    with pytest.raises(RuntimeError), S.seeded(rt, c, ANALYST, session_id="s",
                                               data_dir=tmp_path) as seed:  # fmt: skip
        uid = seed.profile.user_id
        raise RuntimeError("synthetic case failure")
    assert rt.preference_store.load(uid).preferences == {}


def test_preferences_found_behind_the_graph_services(tmp_path):
    runtime = build_runtime(tmp_path / "g.db")
    del runtime.preference_store  # only the graph services carry it
    try:
        c = case({"profile": "analyst_a", "preferences": {"charts": "on"}})
        with S.seeded(runtime, c, ANALYST, session_id="s", data_dir=tmp_path) as seed:
            assert seed.preferences == {"charts": True}
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "bad",
    [{"format": "poem"}, {"verbosity": "high"}, {"note": "show customer emails"}, [], "table",
     {}, {"rows": "many"}, {"rows": True}],
)  # fmt: skip
def test_bad_preference_seeds_are_case_errors(rt, tmp_path, bad):
    c = case({"profile": "analyst_a", "preferences": bad})
    with pytest.raises(CaseError, match="session.preferences"), S.seeded(
        rt, c, ANALYST, session_id="s", data_dir=tmp_path
    ):  # fmt: skip
        pass


def test_rows_preference_seeded_and_clamped(rt, tmp_path):
    c = case({"profile": "analyst_a", "preferences": {"format": "table", "rows": 100}})
    with S.seeded(rt, c, ANALYST, session_id="s", data_dir=tmp_path) as seed:
        assert seed.preferences == {"format": "table", "rows": 50}  # clamped to 1..50 (D-240)


def test_preferences_need_a_store(tmp_path):
    runtime = SimpleNamespace(report_store=None, graph=None)
    c = case({"profile": "analyst_a", "preferences": {"format": "table"}})
    with pytest.raises(CaseError, match="no preference store"), S.seeded(
        runtime, c, ANALYST, session_id="s", data_dir=tmp_path
    ):  # fmt: skip
        pass


def test_live_sut_applies_preferences_before_the_turn(tmp_path):
    built: list = []
    sut = make_sut(tmp_path, built)
    c = case({"profile": "analyst_a", "preferences": {"format": "table"}}, turns=("q1", "q2"))
    sut(c, RunContext("ev-1", tmp_path / "traces", offline=False))
    (rt,) = built
    assert rt.graph.prefs_seen == [{"format": "table"}, {"format": "table"}]
    (uid,) = {u for _, u, _ in rt.graph.turns}
    conn = open_store(tmp_path / "sut.db")  # the runtime's connection belongs to its thread
    try:
        assert SQLitePreferenceStore(conn).load(uid).preferences == {}  # reset after the case
    finally:
        conn.close()


@pytest.mark.parametrize("bad", ["one string", [""], ["ok"] * (S.MAX_SETUP_TURNS + 1)])
def test_setup_turns_validated(bad):
    with pytest.raises(CaseError):
        S.setup_turns(case({"setup_turns": bad}))


def make_sut(tmp_path, built: list):
    boot = L.Bootstrap(PROFILES, settings=object(), checkpointer=SimpleNamespace(conn=None))

    def factory(*_a):
        built.append(build_runtime(tmp_path / "sut.db"))
        return built[-1]

    return L.LiveSut(data_dir=tmp_path / "data", timeout_s=5.0, bootstrap=lambda d: boot,
                     factory=factory)  # fmt: skip


def test_live_sut_seeds_runs_setup_turns_and_scores_only_case_turns(tmp_path):
    built: list = []
    sut = make_sut(tmp_path, built)
    c = case({"profile": "analyst_a", "persona": "formal", "setup_turns": ["warm up"],
              "saved_reports": [{"id": "R-DEMO-0001", "title": "Seeded"}]},
             turns=("q1", "q2"))  # fmt: skip
    res = sut(c, RunContext("ev-1", tmp_path / "traces", offline=False))
    (rt,) = built
    texts = [t for t, _, _ in rt.graph.turns]
    users = {u for _, u, _ in rt.graph.turns}
    assert texts == ["warm up", "q1", "q2"]
    assert len(users) == 1 and next(iter(users)).startswith("analyst_a.ev")
    assert all(v.startswith("eval-formal-") for _, _, v in rt.graph.turns)
    assert rt.graph.services.persona is builtin_persona  # restored after the case
    assert res.text == "answer to q2"
    assert res.llm_calls == {L.UNATTRIBUTED: 2}  # the setup turn is not scored
    assert len(sut.last_trace_ids) == 2
    assert SESSION_ID_RE.fullmatch(Path(res.trace_path).stem)  # audit-shaped session id (D-169)
    assert {c["user_id"] for c in rt.langfuse.calls} == {"analyst_a"}  # base id in Langfuse
    assert (tmp_path / "data" / "seed").is_dir()
    sut.close()
    check = open_store(tmp_path / "sut.db")
    try:
        assert len(ReportStore(check).list(next(iter(users)))) == 1
    finally:
        check.close()


def test_setup_turns_count_toward_the_turn_cap(tmp_path):
    built: list = []
    sut = make_sut(tmp_path, built)
    c = case({"setup_turns": ["s"] * S.MAX_SETUP_TURNS},
             turns=["q"] * (L.MAX_TURNS_PER_CASE - S.MAX_SETUP_TURNS + 1))  # fmt: skip
    with pytest.raises(CaseError, match="turns > cap"):
        sut(c, RunContext("ev-1", tmp_path / "traces", offline=False))
    assert built == []  # refused before any runtime was built


# -- `live: {skip: reason}`


def _raw(**extra):
    return {"input": "hello", "fake": {"outcome": "answered", "text": "hi"}, **extra}


def test_live_skip_parsed_and_validated():
    c = _build_case(_raw(live={"skip": " no library route "}), "golden/x", "golden", [])
    assert c.live_skip == "no library route"
    for bad in ({"skip": ""}, {"skip": 1}, {"reason": "x"}, "skip", {"skip": "x", "y": 1}):
        with pytest.raises(CaseError, match="live must be"):
            _build_case(_raw(live=bad), "golden/x", "golden", [])


def _harness(calls):
    def sut(case, ctx):
        calls.append(ctx.offline)
        return SutResult(outcome="answered", text="hi")

    return Harness(sut=sut)


def test_live_skip_is_na_live_but_runs_offline(tmp_path):
    c = _build_case(_raw(live={"skip": "not wired"}), "golden/x", "golden", [])
    calls: list[bool] = []
    kw = {"trace_dir": tmp_path, "judge_model": "j", "judge_counts": False}
    cr, record, res = run_case(c, _harness(calls), offline=False, **kw)
    assert cr.status == "na" and res is None and calls == []
    assert record["reasons"] == ["not applicable live: not wired"]
    cr, _, res = run_case(c, _harness(calls), offline=True, **kw)
    assert cr.status != "na" and calls == [True]


def test_live_skip_round_trips_through_the_dataset(tmp_path):
    c = _build_case(_raw(live={"skip": "not wired"}), "golden/x", "golden", [])
    payload = D.item_payload(c, tmp_path)
    assert payload["metadata"]["live_skip"] == "not wired"
    item = SimpleNamespace(id=payload["id"], **{k: payload[k] for k in
                           ("input", "expected_output", "metadata")})  # fmt: skip
    assert D.case_from_item(item).live_skip == "not wired"
    plain = D.item_payload(_build_case(_raw(), "golden/y", "golden", []), tmp_path)
    assert "live_skip" not in plain["metadata"]
