"""Iteration 24: fallback messages, degraded mode, per-user quotas (AC-15.2, AC-21.14, AC-22.8).

Offline fakes only; all data is synthetic. The wired graph is the iteration 17 env of
``test_reports`` whose invokes are swapped for gated ones, as `cli.build_runtime` does.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from opsfleet_agent import commands
from opsfleet_agent.graph import graph as gr
from opsfleet_agent.graph.degraded import (
    AI_UNAVAILABLE_TEXT,
    DEGRADED_NOTICE,
    QUOTA_CUT_SHORT_NOTICE,
    QUOTA_DRAFT_KEPT_NOTICE,
    QUOTA_NOTICE,
    QUOTA_TEXT,
    DegradedGraph,
    LLMHealth,
    QuotaExceeded,
)
from opsfleet_agent.graph.llm import TransientLLMError
from opsfleet_agent.graph.resume import ResumeKind, close_interrupted_turn, resume_turn
from opsfleet_agent.session import Session
from opsfleet_agent.store.audit import AuditLog
from opsfleet_agent.store.quota import QuotaLimits, QuotaStore
from tests.unit.test_graph import PROFILE
from tests.unit.test_library import ACME, _add
from tests.unit.test_reports import (
    ReportModel,
    _content,
    _pending,
)
from tests.unit.test_reports import detector as _detector_fixture  # noqa: F401 (fixture)
from tests.unit.test_reports import make_env as _make_env_fixture  # noqa: F401 (fixture)
from tests.unit.test_reports import settings as _settings_fixture  # noqa: F401 (fixture)
from tests.unit.test_reports import store as _store_fixture  # noqa: F401 (fixture)

detector = _detector_fixture
make_env = _make_env_fixture
settings = _settings_fixture
store = _store_fixture

QUESTION = "How many complete orders are there?"
DRAFT_ASK = "Write a report on complete orders"


class DownModel:
    """Every provider call fails transiently: primary, retries and fallback alike."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise TransientLLMError("503 synthetic outage")


@pytest.fixture
def conn(tmp_path):
    from opsfleet_agent.store.db import open_store

    c = open_store(tmp_path / "q.db")
    yield c
    c.close()


def _gate(env, router, analyst, quota, health) -> DegradedGraph:
    """Swap the env's invokes for gated ones (what `build_runtime` wires) and wrap the graph."""
    env.graph.services = dataclasses.replace(
        env.graph.services,
        router_invoke=health.wrap(router),
        analyst_invoke=health.wrap(analyst),
    )
    return DegradedGraph(env.graph, quota, health)


def _health(quota: QuotaStore) -> LLMHealth:
    return LLMHealth(quota, PROFILE.user_id)


def test_all_models_down_graceful(make_env, conn) -> None:
    env = make_env(label="simple")
    quota = QuotaStore(conn)
    down_r, down_a = DownModel(), DownModel()
    graph = _gate(env, down_r, down_a, quota, _health(quota))

    res = graph.run_turn(QUESTION, session=env.session)

    assert res.text == AI_UNAVAILABLE_TEXT  # AC-15.2: the specified message, no crash
    assert res.outcome == "error"
    assert res.notice == DEGRADED_NOTICE
    assert down_r.calls > 1  # retries and the fallback model were attempted first


def test_outage_then_recovery_keeps_session_m4(make_env, store, conn) -> None:
    model = ReportModel()
    env = make_env(model, label="report")
    quota = QuotaStore(conn)
    health = _health(quota)
    good_r, good_a = env.graph.services.router_invoke, env.graph.services.analyst_invoke
    graph = _gate(env, good_r, good_a, quota, health)

    assert graph.run_turn(DRAFT_ASK, session=env.session).outcome == "report_pending"
    owner = env.graph.open_resume(env.session).values["owner"]
    history = list(env.graph.open_resume(env.session).values.get("messages") or [])
    assert _pending(env) and owner == PROFILE.user_id

    down = DownModel()  # the outage: the same graph, saver and session
    graph = _gate(env, down, down, quota, health)
    out = graph.run_turn("hello", session=env.session)
    assert out.text.endswith(AI_UNAVAILABLE_TEXT) and out.notice == DEGRADED_NOTICE
    after = env.graph.open_resume(env.session).values
    assert after["owner"] == owner and len(after.get("messages") or []) >= len(history)
    assert store.count() == 0

    graph = _gate(env, good_r, good_a, quota, health)  # recovery, same env
    env.router.label = "simple"
    assert graph.run_turn(QUESTION, session=env.session).outcome == "answered"
    assert env.graph.open_resume(env.session).values["owner"] == owner


def test_pending_draft_survives_outage_and_saves_m4(make_env, store, conn) -> None:
    env = make_env(label="report")
    quota = QuotaStore(conn)
    health = _health(quota)
    good_r, good_a = env.graph.services.router_invoke, env.graph.services.analyst_invoke
    graph = _gate(env, good_r, good_a, quota, health)
    graph.run_turn(DRAFT_ASK, session=env.session)
    assert _pending(env)

    down = DownModel()
    graph = _gate(env, down, down, quota, health)
    assert graph.run_turn("save", session=env.session).outcome == "report_saved"
    # "save" is deterministic and needs no LLM: it works through the outage (m1)
    assert store.count() == 1 and down.calls == 0


def test_draft_status_prefix_kept_when_llm_down_m3(make_env, conn) -> None:
    env = make_env(label="report")
    quota = QuotaStore(conn)
    health = _health(quota)
    good_r, good_a = env.graph.services.router_invoke, env.graph.services.analyst_invoke
    graph = _gate(env, good_r, good_a, quota, health)
    graph.run_turn(DRAFT_ASK, session=env.session)

    down = DownModel()
    graph = _gate(env, down, down, quota, health)
    out = graph.run_turn("revise make the summary shorter", session=env.session)
    assert out.text.startswith(gr.REVISING_TEXT) and out.text.endswith(AI_UNAVAILABLE_TEXT)
    assert out.notice == DEGRADED_NOTICE

    other = graph.run_turn("tell me about the weather", session=env.session)
    assert other.text.endswith(AI_UNAVAILABLE_TEXT)
    if gr.NOT_SAVED_TEXT in other.text:  # the dropped draft is still announced
        assert other.text.startswith(gr.NOT_SAVED_TEXT)


def test_degraded_mode_lists_and_searches_reports_when_llm_down(
    make_env, store, conn, tmp_path
) -> None:
    rid = _add(store, "k1", title="Synthetic orders overview", extra="needle-token")
    env = make_env(label="simple")
    quota = QuotaStore(conn)
    down = DownModel()
    graph = _gate(env, down, down, quota, _health(quota))

    res = graph.run_turn(QUESTION, session=env.session)
    assert res.notice == DEGRADED_NOTICE and "/reports" in res.notice  # the AC's notice
    calls_before = down.calls

    # the same store the failing graph is wired to
    ctx = commands.CommandContext(
        user_id=PROFILE.user_id,
        session_id="0123456789abcdef0123456789abcdef",  # audit needs a real 32-hex session id
        report_store=env.graph.services.reports,
        scope=ACME,
        audit_log=AuditLog(conn),
        export_dir=tmp_path / "exports",
    )
    listed = commands.dispatch("/reports", ctx).text
    found = commands.dispatch("/search needle-token", ctx).text
    opened = commands.dispatch(f"/open {rid}", ctx).text

    assert "Synthetic orders overview" in listed
    assert "Synthetic orders overview" in found
    assert "Synthetic" in opened and "unavailable" not in opened.lower()
    assert down.calls == calls_before  # the library commands never touched an LLM
    # iteration 33: export works with the LLM down too (AC-21.14 export clause)
    exported = commands.dispatch(f"/export {rid}", ctx).text
    assert exported.startswith("Exported") and (tmp_path / "exports" / f"R-{rid}.md").is_file()
    assert down.calls == calls_before


def test_quota_blocks_after_limit(make_env, conn, store) -> None:
    now = [datetime(2026, 10, 5, 10, 0, tzinfo=UTC)]
    quota = QuotaStore(conn, QuotaLimits(llm_per_hour=4, llm_per_day=100), lambda: now[0])
    env = make_env(label="simple")
    good_r, good_a = env.graph.services.router_invoke, env.graph.services.analyst_invoke
    graph = _gate(env, good_r, good_a, quota, _health(quota))

    first = graph.run_turn(QUESTION, session=env.session)
    assert first.outcome == "answered" and first.llm_calls > 0
    for _ in range(6):  # run into the limit
        graph.run_turn(QUESTION, session=env.session)
    assert quota.usage(PROFILE.user_id)["llm_hour"] == 4  # never above the limit
    seen = env.router.calls

    blocked = graph.run_turn(QUESTION, session=env.session)
    assert blocked.outcome == "refused" and blocked.text == QUOTA_TEXT["llm_hour"]
    assert env.router.calls == seen  # refused before any provider call

    other = Session("sess-2", dataclasses.replace(PROFILE, user_id="analyst_b"))
    assert graph.run_turn(QUESTION, session=other).outcome == "answered"  # per user
    now[0] += timedelta(hours=1)  # the window rolls over
    assert graph.run_turn(QUESTION, session=env.session).outcome == "answered"

    # saved reports are still reachable at quota (AC-22.8)
    _add(store, "kq", title="Synthetic saved")
    ctx = commands.CommandContext(
        user_id=PROFILE.user_id, session_id="s", report_store=store, scope=ACME
    )
    assert "Synthetic saved" in commands.dispatch("/reports", ctx).text


def test_quota_gates_each_call_not_each_turn_m1(make_env, conn) -> None:
    model = ReportModel()
    env = make_env(model, label="report")  # a report turn needs router + analyst + writer...
    quota = QuotaStore(conn, QuotaLimits(llm_per_hour=1))
    health = _health(quota)
    graph = _gate(env, env.graph.services.router_invoke, model, quota, health)
    router = env.router

    res = graph.run_turn(DRAFT_ASK, session=env.session)

    assert router.calls + len(model.calls) <= 1  # at most one provider call in the whole turn
    assert quota.usage(PROFILE.user_id)["llm_hour"] == 1
    assert res.outcome == "refused" and res.text == QUOTA_TEXT["llm_hour"]
    with pytest.raises(QuotaExceeded):
        health.wrap(lambda: "never called")()


def test_save_cancel_and_delete_refusal_work_at_quota_m1(make_env, store, conn) -> None:
    env = make_env(label="report")
    quota = QuotaStore(conn, QuotaLimits(llm_per_hour=100))
    health = _health(quota)
    good_r, good_a = env.graph.services.router_invoke, env.graph.services.analyst_invoke
    graph = _gate(env, good_r, good_a, quota, health)
    graph.run_turn(DRAFT_ASK, session=env.session)
    assert _pending(env)
    quota.limits = QuotaLimits(llm_per_hour=0, llm_per_day=0)  # now at quota

    refused = graph.run_turn("revise and delete my reports", session=env.session)
    assert refused.text == gr.DELETE_WHILE_PENDING_TEXT and _pending(env)
    assert graph.run_turn("save", session=env.session).outcome == "report_saved"
    assert store.count() == 1

    graph.run_turn(DRAFT_ASK, session=env.session)  # blocked at quota: no new draft
    assert not _pending(env)
    quota.limits = QuotaLimits()
    graph.run_turn(DRAFT_ASK, session=env.session)
    quota.limits = QuotaLimits(llm_per_hour=0, llm_per_day=0)
    assert graph.run_turn("cancel", session=env.session).outcome == "report_cancelled"


def test_ctrl_c_mid_turn_keeps_accounting_m2(make_env, conn) -> None:
    class Interrupting(ReportModel):
        def __call__(self, model, messages, specs, timeout):
            if self._analyst >= 1:  # after the first query ran: the user hits Ctrl-C
                raise KeyboardInterrupt
            return super().__call__(model, messages, specs, timeout)

    model = Interrupting()
    env = make_env(model, label="simple")
    quota = QuotaStore(conn)
    graph = _gate(env, env.graph.services.router_invoke, model, quota, _health(quota))

    with pytest.raises(KeyboardInterrupt):
        graph.run_turn(QUESTION, session=env.session)

    usage = quota.usage(PROFILE.user_id)
    assert usage["llm_hour"] == env.router.calls + len(model.calls) + 1  # incl. the raising call
    scanned = env.graph._sql_session(env.session).bytes.used
    assert usage["bq_bytes_day"] == scanned


def test_bytes_cap_follows_remaining_daily_budget(make_env, conn) -> None:
    quota = QuotaStore(conn, QuotaLimits(bq_bytes_per_day=1000))
    quota.record_bytes(PROFILE.user_id, 400)
    env = make_env(label="simple")
    graph = _gate(env, env.graph.services.router_invoke, env.graph.services.analyst_invoke,
                  quota, _health(quota))  # fmt: skip
    graph.prepare(env.session)
    budget = env.graph._sql_session(env.session).bytes
    assert budget.cap == budget.used + 600
    quota.record_bytes(PROFILE.user_id, 600)
    graph.prepare(env.session)
    assert budget.cap == budget.used  # nothing left: every further query is refused


def test_wrapper_never_raises_when_budget_hook_missing(make_env, conn) -> None:
    env = make_env(label="simple")
    quota = QuotaStore(conn)
    graph = _gate(env, env.graph.services.router_invoke, env.graph.services.analyst_invoke,
                  quota, _health(quota))  # fmt: skip

    def boom(session):
        raise RuntimeError("synthetic")

    # m3: a missing/private hook (as seen by the wrapper only) must not break the turn
    graph.inner = SimpleNamespace(_sql_session=boom, run_turn=env.graph.run_turn)
    graph.prepare(env.session)
    assert graph.run_turn(QUESTION, session=env.session).outcome == "answered"


def test_resume_works_through_the_wrapper_b1(make_env, store, conn) -> None:
    model = ReportModel()
    env = make_env(model, label="report")
    quota = QuotaStore(conn)
    graph = _gate(env, env.graph.services.router_invoke, model, quota, _health(quota))
    first = graph.run_turn(DRAFT_ASK, session=env.session)
    out = resume_turn(graph, env.session.session_id, PROFILE)  # the wrapped graph, as the CLI
    assert out.kind is ResumeKind.RESUMED and out.text == first.text
    assert graph.run_turn("save", session=env.session).outcome == "report_saved"


def test_close_interrupted_turn_works_through_the_wrapper_b1(make_env, conn) -> None:
    class Interrupting(ReportModel):
        def __call__(self, model, messages, specs, timeout):
            if "## Report writer rules" in _content(messages[0]) and "writer" in self.kinds():
                raise KeyboardInterrupt
            return super().__call__(model, messages, specs, timeout)

    model = Interrupting()
    env = make_env(model, label="report")
    quota = QuotaStore(conn)
    graph = _gate(env, env.graph.services.router_invoke, model, quota, _health(quota))
    graph.run_turn(DRAFT_ASK, session=env.session)
    with pytest.raises(KeyboardInterrupt):
        graph.run_turn("revise make it shorter", session=env.session, turn_id="t-rev")
    assert close_interrupted_turn(graph, env.session, "t-rev")  # False before the fix
    out = resume_turn(graph, env.session.session_id, PROFILE)
    assert out.kind is ResumeKind.NOTHING_PENDING


def test_quota_store_bytes_cap(conn) -> None:
    q = QuotaStore(conn, QuotaLimits(bq_bytes_per_day=1000))
    assert q.check("u").allowed and q.remaining_bytes("u") == 1000
    q.record_bytes("u", 1000)
    d = q.check("u")
    assert not d.allowed and d.reason == "bq_bytes_day" and q.remaining_bytes("u") == 0
    assert q.check("v").allowed


class ResumableInterrupt(ReportModel):
    """Ctrl-C at the analyst's first call (router done, no query yet); `boom=False` resumes,
    so every byte of the turn is scanned by the resumed part."""

    boom = True

    def __call__(self, model, messages, specs, timeout):
        if self.boom and specs:
            raise KeyboardInterrupt
        return super().__call__(model, messages, specs, timeout)


def _interrupted(make_env, quota):
    model = ResumableInterrupt()
    env = make_env(model, label="simple")
    health = _health(quota)
    graph = _gate(env, env.graph.services.router_invoke, model, quota, health)
    with pytest.raises(KeyboardInterrupt):
        graph.run_turn(QUESTION, session=env.session)
    model.boom = False
    return env, graph, health


def test_resume_charges_bytes_to_daily_quota_mn1(make_env, conn) -> None:
    quota = QuotaStore(conn)
    env, graph, _ = _interrupted(make_env, quota)
    before = env.graph._sql_session(env.session).bytes.used

    out = resume_turn(graph, env.session.session_id, PROFILE)

    assert out.kind is ResumeKind.RESUMED and out.result.outcome == "answered"
    used = env.graph._sql_session(env.session).bytes.used
    assert used > before  # the resumed turn scanned bytes of its own
    assert quota.usage(PROFILE.user_id)["bq_bytes_day"] == used  # all charged, incl. resume


def test_quota_hit_during_resume_shows_quota_text_mn2(make_env, conn) -> None:
    quota = QuotaStore(conn)
    env, graph, health = _interrupted(make_env, quota)
    quota.limits = QuotaLimits(llm_per_hour=quota.usage(PROFILE.user_id)["llm_hour"])

    out = resume_turn(graph, env.session.session_id, PROFILE)

    assert out.kind is ResumeKind.RESUMED
    assert out.result.text == QUOTA_TEXT["llm_hour"] and out.result.notice == QUOTA_NOTICE
    assert out.result.outcome == "refused"


def test_resume_does_not_leak_stale_quota_reason_mn2(make_env, conn) -> None:
    quota = QuotaStore(conn)
    env, graph, health = _interrupted(make_env, quota)
    health.quota_reason = "llm_day"  # left over from an earlier blocked turn

    out = resume_turn(graph, env.session.session_id, PROFILE)

    assert out.result.outcome == "answered" and not out.result.notice
    assert health.quota_reason == ""


def test_resume_with_plain_agent_graph_still_works(make_env) -> None:
    env = make_env(label="report")
    first = env.graph.run_turn(DRAFT_ASK, session=env.session)
    out = resume_turn(env.graph, env.session.session_id, PROFILE)  # no quota wrapper
    assert out.kind is ResumeKind.RESUMED and out.text == first.text


def test_quota_keeps_real_answer_when_writer_blocked_mn3(make_env, conn) -> None:
    model = ReportModel()
    env = make_env(model, label="report")
    quota = QuotaStore(conn, QuotaLimits(llm_per_hour=3))  # router + analyst pass, writer blocked
    health = _health(quota)
    graph = _gate(env, env.graph.services.router_invoke, model, quota, health)

    res = graph.run_turn(DRAFT_ASK, session=env.session)

    assert health.quota_reason == "llm_hour" and not _pending(env)
    # iteration 33 (D-191): a blocked writer is report_failed, not answered; the answer stays
    assert res.outcome == "report_failed" and res.text != QUOTA_TEXT["llm_hour"]
    assert "3 complete orders" in res.text  # the analysis answer is shown, not thrown away
    assert gr.RETRY_HINT_TEXT in res.text  # retry report re-runs only the writer (iteration 33)
    assert QUOTA_TEXT["llm_hour"] in res.notice and res.notice.endswith(QUOTA_NOTICE)


def test_quota_notice_on_unverified_draft_mn4(make_env, store, conn) -> None:
    model = ReportModel()
    env = make_env(model, label="report")
    quota = QuotaStore(conn, QuotaLimits(llm_per_hour=4))  # only the verifier is blocked
    health = _health(quota)
    graph = _gate(env, env.graph.services.router_invoke, model, quota, health)

    res = graph.run_turn(DRAFT_ASK, session=env.session)

    assert health.quota_reason == "llm_hour"
    assert res.outcome == "report_pending" and _pending(env)
    assert QUOTA_CUT_SHORT_NOTICE in res.notice and res.notice.endswith(QUOTA_NOTICE)
    assert graph.run_turn("save", session=env.session).outcome == "report_saved"
    assert store.count() == 1


def _draft_then_limit(make_env, conn, extra_calls: int):
    model = ReportModel()
    env = make_env(model, label="report")
    quota = QuotaStore(conn, QuotaLimits(llm_per_hour=10_000))
    graph = _gate(env, env.graph.services.router_invoke, model, quota, _health(quota))
    assert graph.run_turn(DRAFT_ASK, session=env.session).outcome == "report_pending"
    used = quota.usage(PROFILE.user_id)["llm_hour"]
    quota.limits = QuotaLimits(llm_per_hour=used + extra_calls)
    return env, graph, model


def test_revise_at_quota_keeps_draft_pending_mn5(make_env, store, conn) -> None:
    env, graph, model = _draft_then_limit(make_env, conn, extra_calls=0)
    calls = env.router.calls + len(model.calls)

    res = graph.run_turn("revise make the summary shorter", session=env.session)

    assert res.outcome == "refused" and res.text == QUOTA_TEXT["llm_hour"]
    assert QUOTA_DRAFT_KEPT_NOTICE in res.notice and res.notice.endswith(QUOTA_NOTICE)
    assert env.router.calls + len(model.calls) == calls  # the graph never ran
    assert _pending(env)  # the draft is still pending and savable
    assert graph.run_turn("save", session=env.session).outcome == "report_saved"
    assert store.count() == 1


def test_revise_cut_short_mid_way_drops_draft_mn5_pinned(make_env, conn) -> None:
    # OD-7 (owner decision): a quota hit AFTER the revise started drops the old draft, as an
    # outage mid-revise does; the graph closes the draft before the revise's first call
    env, graph, _ = _draft_then_limit(make_env, conn, extra_calls=1)

    res = graph.run_turn("revise make the summary shorter", session=env.session)

    assert res.text.startswith(gr.REVISING_TEXT) and res.notice.endswith(QUOTA_NOTICE)
    assert not _pending(env)
