"""Live eval SUT wiring (iteration 40b). Offline: fake bootstrap, runtime and sink."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from evals import live_sut as L
from evals.run import Case, CaseError, RunContext

from opsfleet_agent.session import Profile

PROFILES = {
    "analyst_a": Profile("analyst_a", "Analyst A", brands=("Acme",)),
    "ceo_demo": Profile("ceo_demo", "CEO", all_products=True),
}


class FakeTracer:
    def __init__(self) -> None:
        self.trace_dir: Path | None = None
        self.session_id = ""

    @property
    def path(self) -> Path:
        assert self.trace_dir is not None
        return self.trace_dir / f"{self.session_id}.jsonl"


class FakeSink:
    def __init__(self) -> None:
        self.traced_calls: list[dict[str, Any]] = []
        self.shut = False
        self.client = SimpleNamespace(flush=lambda: None)

    def traced(self, fn, **kw):
        self.traced_calls.append(kw)
        return fn

    def trace_id_for(self, turn_id: str) -> str:
        return f"lf-{turn_id}"

    def shutdown(self) -> None:
        self.shut = True


class FakeGraph:
    def __init__(self, tracer: FakeTracer, block: threading.Event | None = None) -> None:
        self.tracer, self.block, self.turns = tracer, block, []

    def run_turn(self, text, *, session, turn_id):
        if self.block is not None:
            self.block.wait(5)
        self.turns.append((text, session.session_id))
        self.tracer.trace_dir.mkdir(parents=True, exist_ok=True)
        with open(self.tracer.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "llm", "model": "local-model"}) + "\n")
            f.write(json.dumps({"type": "tool", "tool": "run_sql"}) + "\n")
            f.write(json.dumps({"type": "sql", "status": "ok", "sql_text": "SELECT 1",
                                "bytes_billed": 10}) + "\n")  # fmt: skip
            f.write(json.dumps({"type": "sql", "status": "refused", "sql_text": "DROP"}) + "\n")
        return SimpleNamespace(outcome="answered", text=f"answer to {text}", label="data",
                               llm_calls=2, sql_queries=1)  # fmt: skip


class FakeRuntime:
    def __init__(self, sink: FakeSink | None, block: threading.Event | None = None) -> None:
        self.tracer = FakeTracer()
        self.graph = FakeGraph(self.tracer, block)
        self.langfuse = sink
        self.cancelled = self.closed = False

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


def make_sut(tmp_path, *, sink=True, block=None, timeout_s=5.0):
    built: list[FakeRuntime] = []

    def factory(settings, checkpointer, session, data_dir):
        rt = FakeRuntime(FakeSink() if sink else None, block)
        built.append(rt)
        return rt

    boot = L.Bootstrap(PROFILES, settings=object(), checkpointer=SimpleNamespace(conn=None))
    sut = L.LiveSut(data_dir=tmp_path / "data", timeout_s=timeout_s,
                    bootstrap=lambda d: boot, factory=factory)  # fmt: skip
    return sut, built


def case(turns=("q1",), session=None, cid="c1") -> Case:
    return Case(id=cid, suite="golden", turns=list(turns),
                session=session if session is not None else {"profile": "analyst_a"})  # fmt: skip


def ctx(tmp_path, sid="ev-1") -> RunContext:
    return RunContext(sid, tmp_path / "traces", offline=False)


def test_runs_turns_in_fresh_session_and_maps_result(tmp_path):
    sut, built = make_sut(tmp_path)
    res = sut(case(("q1", "q2")), ctx(tmp_path))
    rt = built[0]
    sessions = {s for _, s in rt.graph.turns}
    assert [t for t, _ in rt.graph.turns] == ["q1", "q2"]
    assert len(sessions) == 1 and next(iter(sessions)).startswith("ev-1-")
    assert res.outcome == "answered" and res.text == "answer to q2"
    assert res.sql == ["SELECT 1", "SELECT 1"]  # only executed SQL
    assert res.tools == ["run_sql", "run_sql"]
    assert res.llm_calls == {"local-model": 2, L.UNATTRIBUTED: 2}  # 4 total, 2 named
    assert res.bq_queries == 2 and res.bq_bytes == 20
    assert res.trace_id is None and res.trace_path.startswith(str(tmp_path / "traces"))
    assert len(sut.last_trace_ids) == 2 and all(t.startswith("lf-") for t in sut.last_trace_ids)
    assert rt.langfuse.traced_calls[0]["user_id"] == "analyst_a"


def test_fresh_session_per_case_and_runtime_reused_per_profile(tmp_path):
    sut, built = make_sut(tmp_path)
    sut(case(cid="a"), ctx(tmp_path, "ev-a"))
    sut(case(cid="b"), ctx(tmp_path, "ev-a"))
    sut(case(cid="c", session={"profile": "ceo_demo"}), ctx(tmp_path, "ev-c"))
    assert len(built) == 2
    first = {s for _, s in built[0].graph.turns}
    assert len(first) == 2  # same ctx id, still two distinct sessions


def test_without_langfuse_no_trace_ids(tmp_path):
    sut, _ = make_sut(tmp_path, sink=False)
    sut(case(), ctx(tmp_path))
    assert sut.last_trace_ids == []


def test_turn_cap(tmp_path):
    sut, built = make_sut(tmp_path)
    with pytest.raises(CaseError, match="turns > cap"):
        sut(case(["q"] * (L.MAX_TURNS_PER_CASE + 1)), ctx(tmp_path))
    assert built == []


@pytest.mark.parametrize("seed", ["saved_reports", "persona", "preferences"])
def test_unsupported_session_seeds_fail_clearly(tmp_path, seed):
    sut, _ = make_sut(tmp_path)
    with pytest.raises(CaseError, match="not supported"):
        sut(case(session={"profile": "analyst_a", seed: []}), ctx(tmp_path))


def test_unknown_profile_is_case_error(tmp_path):
    sut, _ = make_sut(tmp_path)
    with pytest.raises(CaseError, match="profile"):
        sut(case(session={"profile": "nobody"}), ctx(tmp_path))


def test_timeout_cancels_and_retires_runtime(tmp_path):
    gate = threading.Event()
    sut, built = make_sut(tmp_path, block=gate, timeout_s=0.05)
    with pytest.raises(CaseError, match="timed out"):
        sut(case(), ctx(tmp_path))
    gate.set()
    assert built[0].cancelled
    assert sut.last_trace_ids == []
    sut.timeout_s = 5.0
    sut(case(), ctx(tmp_path))
    assert len(built) == 2  # the timed-out runtime is never reused


def test_close_shuts_sinks_and_runtimes(tmp_path):
    sut, built = make_sut(tmp_path)
    sut(case(), ctx(tmp_path))
    sut.flush()
    sut.close()
    assert built[0].langfuse.shut and built[0].closed


def test_store_is_used_on_the_thread_that_opened_it(tmp_path):
    """Regression: store.db connections are check_same_thread=True (smoke run, iter 40b)."""
    import sqlite3

    conns: list[sqlite3.Connection] = []
    closed: list[bool] = []

    def bootstrap(data_dir):
        conn = sqlite3.connect(":memory:")  # default check_same_thread=True
        conns.append(conn)

        def close():
            conn.close()  # ProgrammingError (swallowed, so no "closed") on another thread
            closed.append(True)

        cp = SimpleNamespace(conn=SimpleNamespace(close=close))
        return L.Bootstrap(PROFILES, settings=object(), checkpointer=cp)

    class StoreRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__(FakeSink())
            run = self.graph.run_turn

            def run_turn(text, **kw):
                conns[-1].execute("SELECT 1")  # raises ProgrammingError on another thread
                return run(text, **kw)

            self.graph.run_turn = run_turn

    sut = L.LiveSut(data_dir=tmp_path / "data", timeout_s=5.0, bootstrap=bootstrap,
                    factory=lambda *a: StoreRuntime())  # fmt: skip
    sut(case(("q1", "q2")), ctx(tmp_path))
    sut(case(cid="c2"), ctx(tmp_path))
    sut.close()
    assert closed == [True]  # closed on its own thread, not leaked


def test_case_timeout_env_is_clamped():
    assert L.case_timeout_s({}) == L.DEFAULT_CASE_TIMEOUT_S
    assert L.case_timeout_s({L.ENV_CASE_TIMEOUT: "0"}) == 1.0
    assert L.case_timeout_s({L.ENV_CASE_TIMEOUT: "99999"}) == L.MAX_CASE_TIMEOUT_S
    assert L.case_timeout_s({L.ENV_CASE_TIMEOUT: "x"}) == L.DEFAULT_CASE_TIMEOUT_S


def test_eval_data_dir_env(tmp_path):
    assert L.eval_data_dir({L.ENV_DATA_DIR: str(tmp_path)}) == tmp_path
    assert L.eval_data_dir({}).name == "eval-live"
