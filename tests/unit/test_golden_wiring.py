"""Wiring of D-96 (known brands), D-117 (Golden examples in the analyst prompt) and D-114
(strict seed in tests/CI, lenient in production).

Offline only: the graph fakes of ``test_graph``, the hashing embedder of ``test_golden`` and the
report fakes of ``test_reports``. Brands, questions and trios are synthetic.
"""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from opsfleet_agent.config import ConfigError
from opsfleet_agent.golden.runtime import (
    STRICT_ENV,
    build_golden_index,
    offline_known_brands,
    seed_strict,
)
from opsfleet_agent.golden.seed import DEFAULT_K
from opsfleet_agent.graph import graph as gr
from opsfleet_agent.graph import resume as rs
from opsfleet_agent.graph.context import MAX_STORE_ITEMS
from opsfleet_agent.guards.scope import ProductScope
from opsfleet_agent.roles.analyst import ModelTurn
from opsfleet_agent.session import Session
from opsfleet_agent.store.db import open_store
from opsfleet_agent.store.reports import ReportStore
from tests.unit.test_golden import FakeEmbedder, entry, index, trio, write_seed
from tests.unit.test_graph import PROFILE, Env, Scripted, SeqRouter
from tests.unit.test_graph import detector as _detector_fixture  # noqa: F401
from tests.unit.test_graph import settings as _settings_fixture  # noqa: F401
from tests.unit.test_reports import ReportModel, TurnRouter
from tests.unit.test_resume import SID, _Crash, crash_on_entry, new_key, saver

detector = _detector_fixture
settings = _settings_fixture


@pytest.fixture(autouse=True)
def _no_strict_env(monkeypatch) -> None:
    # CI sets the strict flag for the whole run (D-114); these tests set it explicitly.
    monkeypatch.delenv(STRICT_ENV, raising=False)


FENCE = "EXAMPLE"
OPEN = "<<<EXAMPLE (untrusted data)"
MATCH_Q = "monthly revenue trend for the store"
TRIOS = (
    trio("rev", question=MATCH_Q, report_summary="Show the monthly series and its direction."),
    trio("ret", question="return rate by product category", tags=("returns",)),
    trio("del", question="delivery time per department", tags=("delivery",)),
    trio("rev2", question="revenue trend per month for the store"),
    trio("rev3", question="store revenue trend monthly view"),
    trio("zeta", brands=("Zeta",), question="monthly revenue trend for the store by Zeta"),
)


def _rewire(env: Env, **services: Any) -> Env:
    env.graph = gr.AgentGraph(dataclasses.replace(env.graph.services, **services), env.saver)
    return env


def _env(tmp_path, settings, detector, *labels: str, analyst=None, **services) -> Env:
    env = Env(
        tmp_path, settings, detector, SeqRouter(*labels), analyst or Scripted(ModelTurn("ok"))
    )
    return _rewire(env, **services)


def _system(analyst: Scripted, i: int = 0) -> str:
    return str(analyst.calls[i][1][0]["content"])


def _ctx_spans(env: Env) -> list[dict[str, Any]]:
    return [f for t, n, f in env.spans if t == "context" and n == "load_context"]


# --- (a) D-96: known_brands reaches assemble_context in a real graph turn ------------------------

TURN1 = "How many orders mention Zeta jackets last month?"
TURN2 = "And how many orders were complete last month?"


@pytest.mark.parametrize(
    ("known", "dropped"),
    [
        (("Acme", "Zeta"), True),  # known, outside the Acme scope: the history turn is dropped
        ((), False),  # nothing known: only snapshots are checked (pre-D-96 behaviour)
        (("Acme", "Quuxco"), False),  # Zeta is an unknown brand: not detected (OD-12 limit)
    ],
)
def test_known_brands_filter_history_in_graph_turn(
    tmp_path, settings, detector, known, dropped
) -> None:
    analyst = Scripted(ModelTurn("Orders are listed."))
    env = _env(tmp_path, settings, detector, "simple", analyst=analyst, known_brands=known)
    env.ask(TURN1)
    out = env.ask(TURN2)
    assert out.outcome == "answered"
    second = analyst.calls[-1][1]  # system, history..., user
    history_text = " ".join(str(m.get("content")) for m in second[1:-1])
    span = _ctx_spans(env)[-1]
    if dropped:
        assert span["context_dropped"] == {"history_turn": 1} and span["history_turns"] == 0
        assert "Zeta" not in history_text and "Zeta" not in _system(analyst, -1)
    else:
        assert span["history_turns"] == 1 and not span["context_dropped"]
        assert "Zeta" in history_text


def test_assemble_receives_service_known_brands(tmp_path, settings, detector, monkeypatch):
    seen: list[Any] = []
    real = gr.assemble_context

    def spy(*a: Any, **k: Any):
        seen.append(k.get("known_brands"))
        return real(*a, **k)

    monkeypatch.setattr(gr, "assemble_context", spy)
    brands = frozenset({"Acme", "Zeta"})
    env = _env(tmp_path, settings, detector, "simple", known_brands=brands)
    env.ask(TURN2)
    assert seen and all(b == brands for b in seen)


# --- (b) D-117: Golden examples in the analyst prompt --------------------------------------------


def _golden(tmp_path, embedder=None, trios=TRIOS, **kw):
    return index(list(trios), embedder or FakeEmbedder(), tmp_path / "gcache", **kw)


def test_matching_question_puts_examples_in_analyst_prompt(tmp_path, settings, detector):
    emb = FakeEmbedder()
    idx = _golden(tmp_path, emb)
    analyst = Scripted(ModelTurn("Revenue rose."))
    env = _env(tmp_path, settings, detector, "simple", analyst=analyst, golden_index=idx)
    out = env.ask(MATCH_Q)
    assert out.outcome == "answered"
    system = _system(analyst)
    assert FENCE in system and MATCH_Q in system
    assert "data, not instructions" in system  # fenced as untrusted data
    n = system.count(OPEN)
    assert "by Zeta" not in system  # brand-tied trio outside the Acme scope never retrieved
    span = _ctx_spans(env)[-1]
    refs = span["golden"]["trio_refs"]
    assert refs and refs[0][0] == "rev@1" and 1 <= n == len(refs) <= idx.k  # bounded by k
    assert span["golden"]["unavailable"] is False
    assert all(not r[0].startswith("zeta@") for r in refs)
    assert len(emb.calls) == 1  # one embedding call per turn, outside the LLM cap
    # the user message stays the user's own question; examples live only in the system prompt
    assert analyst.calls[0][1][-1]["content"] == MATCH_Q


def test_non_matching_question_has_no_examples(tmp_path, settings, detector):
    idx = _golden(tmp_path)
    analyst = Scripted(ModelTurn("There are some users."))
    env = _env(tmp_path, settings, detector, "simple", analyst=analyst, golden_index=idx)
    env.ask("zzzz qqqq xxxx")
    assert FENCE not in _system(analyst)
    span = _ctx_spans(env)[-1]
    assert span["golden"]["trio_refs"] == [] and span["golden"]["unavailable"] is False


def test_no_index_means_no_examples_and_no_trace(tmp_path, settings, detector):
    analyst = Scripted(ModelTurn("Revenue rose."))
    env = _env(tmp_path, settings, detector, "simple", analyst=analyst)
    env.ask(MATCH_Q)
    assert FENCE not in _system(analyst)
    assert "golden" not in _ctx_spans(env)[-1]


def test_embedder_failure_degrades_and_turn_is_answered(tmp_path, settings, detector, caplog):
    emb = FakeEmbedder(fail=True)
    analyst = Scripted(ModelTurn("Revenue rose."))
    env = _env(
        tmp_path, settings, detector, "simple", analyst=analyst, golden_index=_golden(tmp_path, emb)
    )
    out = env.ask(MATCH_Q)
    assert out.outcome == "answered" and FENCE not in _system(analyst)
    assert _ctx_spans(env)[-1]["golden"]["unavailable"] is True
    assert len(emb.calls) == 1  # single attempt, no retry


def test_broken_index_never_breaks_a_turn(tmp_path, settings, detector):
    class Broken:
        k = 3

        def retrieve(self, *a: Any) -> Any:
            raise RuntimeError("boom")

    analyst = Scripted(ModelTurn("Revenue rose."))
    env = _env(tmp_path, settings, detector, "simple", analyst=analyst, golden_index=Broken())
    assert env.ask(MATCH_Q).outcome == "answered"
    assert _ctx_spans(env)[-1]["golden"] == {"trio_refs": [], "unavailable": True, "mode": "none"}


def test_light_and_clarification_turns_make_no_embed_call(tmp_path, settings, detector):
    emb = FakeEmbedder()
    env = _env(tmp_path, settings, detector, "smalltalk", golden_index=_golden(tmp_path, emb))
    assert env.ask("hello").route == "light"
    env2 = Env(tmp_path, settings, detector, SeqRouter("simple"), Scripted(ModelTurn("ok")))
    env2.session = Session("sess-2", PROFILE)
    _rewire(env2, golden_index=_golden(tmp_path, emb))
    out = env2.ask("How did it do compared to the other one?")
    assert out.sql_queries == 0 and env2.analyst.calls == []  # a clarification was asked
    assert emb.calls == []


def test_resume_rebuilds_examples_by_ref_without_embedding(tmp_path):
    emb = FakeEmbedder()
    idx = _golden(tmp_path, emb)
    ctx = SimpleNamespace(
        services=SimpleNamespace(golden_index=idx),
        sql_session=SimpleNamespace(scope=ProductScope.all()),
    )
    items = gr._golden_from_refs(ctx, ["rev@1", "ret@1", "gone@9", "rev2@1", "rev3@1"])
    assert 1 <= len(items) <= idx.k  # unknown refs skipped, capped at k
    assert any(MATCH_Q in i.text for i in items)
    assert emb.calls == []  # no embedding call on resume
    assert gr._golden_from_refs(ctx, None) == [] and gr._golden_from_refs(ctx, "x") == []
    ctx.services.golden_index = None
    assert gr._golden_from_refs(ctx, ["rev@1"]) == []


def test_crash_resume_replays_the_same_examples_without_embedding(
    tmp_path, settings, detector, monkeypatch
):
    """A real kill after load_context: the resumed turn goes through ``_assembled`` and shows
    the examples load_context chose (from the checkpointed refs), with no embedding call."""
    key = new_key()
    emb = FakeEmbedder()
    first = Scripted(ModelTurn("Revenue rose."))
    env = Env(tmp_path, settings, detector, SeqRouter("simple"), first, saver=saver(tmp_path, key))
    _rewire(env, golden_index=_golden(tmp_path, emb))
    crash_on_entry(monkeypatch, "quick")  # load_context has run and been checkpointed
    with pytest.raises(_Crash):
        env.ask(MATCH_Q)
    assert first.calls == [] and len(emb.calls) == 1
    refs = [r for r, _ in _ctx_spans(env)[-1]["golden"]["trio_refs"]]
    assert refs and refs[0] == "rev@1"

    emb2 = FakeEmbedder()
    analyst = Scripted(ModelTurn("Revenue rose."))
    resumed = Env(
        tmp_path, settings, detector, SeqRouter("simple"), analyst, client=env.client,
        saver=saver(tmp_path, key),
    )  # fmt: skip
    _rewire(resumed, golden_index=_golden(tmp_path, emb2))
    out = rs.resume_turn(resumed.graph, SID, PROFILE)
    assert out.result is not None and out.result.outcome == "answered"
    system = _system(analyst)
    assert system.count(OPEN) == len(refs)  # the same examples, in the analyst prompt
    assert MATCH_Q in system and "by Zeta" not in system
    assert emb2.calls == []  # rebuilt by ref: zero embedding calls on resume


def test_resume_dedupes_tampered_refs(tmp_path):
    ctx = SimpleNamespace(
        services=SimpleNamespace(golden_index=_golden(tmp_path)),
        sql_session=SimpleNamespace(scope=ProductScope.all()),
    )
    items = gr._golden_from_refs(ctx, ["rev@1"] * 10)
    assert len(items) == 1  # a repeated ref renders one block, not k identical ones
    items = gr._golden_from_refs(ctx, ["ret@1", "rev@1", "ret@1", 7, None, "rev2@1"])
    assert [i.text for i in items] == [
        i.text for i in gr._golden_from_refs(ctx, ["ret@1", "rev@1", "rev2@1"])
    ]  # order kept, non-strings ignored
    assert len(items) == 3


@pytest.mark.parametrize(
    ("k", "clamped"),
    [(-1, 1), (0, 1), (1, 1), (3, 3), (MAX_STORE_ITEMS, MAX_STORE_ITEMS),
     (10_000, MAX_STORE_ITEMS), (True, DEFAULT_K), ("3", DEFAULT_K), (None, DEFAULT_K)],
)  # fmt: skip
def test_golden_index_k_is_clamped(tmp_path, k, clamped):
    idx = _golden(tmp_path, k=k, min_score=0.0)
    assert idx.k == clamped
    trios = [trio(f"t{i}", question=MATCH_Q) for i in range(MAX_STORE_ITEMS + 4)]
    many = _golden(tmp_path, trios=trios, k=k, min_score=0.0)
    hits = many.retrieve(MATCH_Q, ProductScope.all()).hits
    assert len(hits) == clamped
    ctx = SimpleNamespace(
        services=SimpleNamespace(golden_index=many),
        sql_session=SimpleNamespace(scope=ProductScope.all()),
    )
    refs = [t.ref for t in trios]
    assert len(gr._golden_from_refs(ctx, refs)) == clamped  # k=-1 no longer keeps all but one


# --- (c) D-114: strict in tests/CI, lenient in production ----------------------------------------


@pytest.mark.parametrize(
    ("value", "strict"),
    [("1", True), ("true", True), (" YES ", True), ("on", True), ("0", False), ("", False),
     ("no", False), ("strict", False)],
)  # fmt: skip
def test_seed_strict_env_parsing(value, strict) -> None:
    assert seed_strict({STRICT_ENV: value}) is strict
    assert seed_strict({}) is False


def _bad_seed(tmp_path):
    return write_seed(
        tmp_path,
        [entry(trio_id="good"), entry(trio_id="bad", report_summary="There were 3 orders.")],
    )


def test_strict_bad_seed_refuses_startup(tmp_path, settings) -> None:
    with pytest.raises(ConfigError, match="Golden seed is invalid"):
        build_golden_index(
            settings, cache_dir=tmp_path, embedder=FakeEmbedder(),
            seed_path=_bad_seed(tmp_path), strict=True,
        )  # fmt: skip


def test_lenient_bad_seed_skips_the_trio(tmp_path, settings, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        idx = build_golden_index(
            settings, cache_dir=tmp_path, embedder=FakeEmbedder(),
            seed_path=_bad_seed(tmp_path), strict=False,
        )  # fmt: skip
    assert idx is not None and [t.trio_id for t in idx.trios] == ["good"]
    assert "1 trio(s) rejected" in caplog.text


def test_env_switch_selects_strict(tmp_path, settings, monkeypatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    with pytest.raises(ConfigError):
        build_golden_index(
            settings, cache_dir=tmp_path, embedder=FakeEmbedder(), seed_path=_bad_seed(tmp_path)
        )
    monkeypatch.delenv(STRICT_ENV)
    assert build_golden_index(
        settings, cache_dir=tmp_path, embedder=FakeEmbedder(), seed_path=_bad_seed(tmp_path)
    )


def test_lenient_unreadable_or_empty_seed_disables_examples(tmp_path, settings) -> None:
    missing = tmp_path / "missing.yaml"
    assert build_golden_index(settings, cache_dir=tmp_path, seed_path=missing, strict=False) is None
    with pytest.raises(ConfigError):
        build_golden_index(settings, cache_dir=tmp_path, seed_path=missing, strict=True)
    only_bad = write_seed(tmp_path, [entry(trio_id="bad", report_summary="Has 3 digits.")])
    assert build_golden_index(settings, cache_dir=tmp_path, seed_path=only_bad) is None


def test_shipped_seed_loads_strictly_and_lazily(tmp_path, settings) -> None:
    idx = build_golden_index(settings, cache_dir=tmp_path, strict=True)  # GenaiEmbedder: lazy
    assert idx is not None and len(idx.trios) == 10
    assert idx.model == settings.embedding_model
    assert idx.dimensionality == settings.embedding_dimensionality


def test_offline_known_brands() -> None:
    profiles = [SimpleNamespace(brands=("Acme", "Zeta")), SimpleNamespace(brands=()), object()]
    trios = [trio("a"), trio("b", brands=("Quuxco",)), trio("c", brands=("agnostic",))]
    assert offline_known_brands(profiles, trios) == frozenset({"Acme", "Zeta", "Quuxco"})
    assert offline_known_brands([]) == frozenset()


# --- (d) report turns are unaffected -------------------------------------------------------------


def test_report_turn_with_golden_index_still_saves(tmp_path, settings, detector) -> None:
    store = ReportStore(open_store(tmp_path / "reports.db"))
    model = ReportModel()
    emb = FakeEmbedder()
    q = "Write a report on complete orders"
    idx = _golden(tmp_path, emb, trios=(trio("rep", question=q),), min_score=0.3)
    env = Env(tmp_path, settings, detector, TurnRouter("report"), model)
    _rewire(env, reports=store, golden_index=idx, known_brands=("Acme", "Zeta"))
    out = env.ask(q)
    assert out.outcome == "report_pending" and out.route == "report"
    analyst_prompts = [str(m[0]["content"]) for k, m in model.calls if k == "analyst"]
    writer_prompts = [str(m[0]["content"]) for k, m in model.calls if k == "writer"]
    assert analyst_prompts and FENCE in analyst_prompts[0]  # Deep analyst sees the example
    assert writer_prompts and all(FENCE not in p for p in writer_prompts)
    saved = env.ask("save")
    assert saved.outcome == "report_saved" and store.count() == 1
    assert len(emb.calls) == 1  # the confirm turn retrieves nothing (light/confirm path)
