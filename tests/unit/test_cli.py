"""CLI: startup refusals, REPL commands, Ctrl-C and narrow ``--resume`` (iteration 19).

Offline only: the model lister is a fake, the runtime is a fake (or a graph over fakes from
test_graph), ``.env`` is never read and every key and profile is synthetic.
"""

from __future__ import annotations

import re
import signal
from dataclasses import dataclass, field
from typing import Any

import pytest

from opsfleet_agent import cli, commands
from opsfleet_agent.commands.audit import UNAVAILABLE as AUDIT_UNAVAILABLE
from opsfleet_agent.graph import resume as rs
from opsfleet_agent.graph.graph import TurnResult
from tests.unit import test_graph as _tg
from tests.unit.test_graph import Env, Router
from tests.unit.test_resume import (
    ANSWER,
    SID,
    _Crash,
    crash_on_entry,
    escalating_analyst,
    new_key,
    saver,
)

from .test_config import SENTINEL, all_models

# re-export test_graph's module fixtures (the same objects, so pytest registers them here)
detector = _tg.detector
settings = _tg.settings

QUESTION = "How many complete orders are there?"


@dataclass
class FakeGraph:
    answer: str = "There were 3 orders."
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)
    on_turn: Any = None

    def run_turn(self, text: str, *, session, turn_id=None) -> TurnResult:
        self.calls.append((text, session.session_id, turn_id))
        if self.on_turn is not None:
            self.on_turn()
        return TurnResult(self.answer, outcome="answered")


@dataclass
class FakeFactory:
    graph: FakeGraph = field(default_factory=FakeGraph)
    calls: list[Any] = field(default_factory=list)
    cancels: list[str] = field(default_factory=list)
    closed: list[bool] = field(default_factory=list)
    audit_log: Any = None

    def __call__(self, settings, checkpointer, session, data_dir) -> cli.Runtime:
        self.calls.append(session)
        return cli.Runtime(
            graph=self.graph,
            cancel=lambda: self.cancels.append("cancel"),
            clear_cancel=lambda: self.cancels.append("clear"),
            audit_log=self.audit_log,
            persona_version=lambda: "builtin-0000abcd",
            close=lambda: self.closed.append(True),
        )


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("OPSFLEET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPSFLEET_PROFILES_YAML", raising=False)
    monkeypatch.setattr("opsfleet_agent.config.load_dotenv", lambda *a, **k: None)
    # main() installs a process-wide PII detector; keep it from leaking into other tests.
    monkeypatch.setattr("opsfleet_agent.guards.pii._default", None)


@pytest.fixture
def factory(monkeypatch) -> FakeFactory:
    f = FakeFactory()
    monkeypatch.setattr(cli, "build_runtime", f)
    return f


def _env(monkeypatch, key: str | None = None) -> str:
    key = key or new_key()
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", SENTINEL)
    monkeypatch.setenv("LANGGRAPH_AES_KEY", key)
    return key


def _inputs(monkeypatch, *items) -> None:
    it = iter(items)

    def fake_input(_prompt: str = "") -> str:
        v = next(it)
        if isinstance(v, BaseException) or (isinstance(v, type) and issubclass(v, BaseException)):
            raise v
        return v

    monkeypatch.setattr("builtins.input", fake_input)


def _profiles(monkeypatch, tmp_path, a_brands: str, b_brands: str = "[Acme]") -> None:
    p = tmp_path / "profiles.yaml"
    p.write_text(
        "profiles:\n"
        f"  - {{id: analyst_a, display_name: Analyst A, brands: {a_brands}}}\n"
        f"  - {{id: analyst_b, display_name: Analyst B, brands: {b_brands}}}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPSFLEET_PROFILES_YAML", str(p))


def _crashed_session(tmp_path, settings, detector, monkeypatch, key):
    """A synthetic analyst_a (brand Acme) session ``sess-1`` killed before ``deep``."""
    analyst, router = escalating_analyst(), Router("simple")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    crash_on_entry(monkeypatch, "deep")
    with pytest.raises(_Crash):
        env.ask(QUESTION)
    env.saver.conn.close()
    return env


def _no_lister():
    raise AssertionError("model listing must not be called")


# --- startup ---


def test_cli_exits_2_with_one_line_on_missing_env(monkeypatch, capsys, factory):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", SENTINEL)
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in err
    assert SENTINEL not in err
    assert not factory.calls


@pytest.mark.parametrize("bad", [None, "too-short", "x" * 33])
def test_cli_refuses_without_valid_aes_key(monkeypatch, capsys, factory, bad):
    _env(monkeypatch)
    if bad is None:
        monkeypatch.delenv("LANGGRAPH_AES_KEY")
    else:
        monkeypatch.setenv("LANGGRAPH_AES_KEY", bad)
    assert cli.main(["--user", "analyst_a"], lister=_no_lister) == 2
    captured = capsys.readouterr()
    err = captured.err
    assert len(err.strip().splitlines()) == 1 and "LANGGRAPH_AES_KEY" in err
    assert "Traceback" not in err
    if bad:
        assert bad not in err and bad not in captured.out
    assert not factory.calls


def test_cli_requires_user():
    with pytest.raises(SystemExit) as ei:
        cli.main([])
    assert ei.value.code == 2


@pytest.mark.parametrize("bad", ["../etc/passwd", "a b", "x" * 65, "\x1b[2Jid", "", "abc\n"])
def test_cli_rejects_malformed_resume_id_without_echo(monkeypatch, capsys, factory, bad):
    _env(monkeypatch)
    assert cli.main(["--user", "analyst_a", "--resume", bad], lister=_no_lister) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == cli.BAD_RESUME_ID_TEXT
    if bad.strip():
        assert bad not in captured.err and bad not in captured.out
    assert not factory.calls


def test_cli_startup_ctrl_c_exits_130_without_traceback(monkeypatch, capsys, factory):
    _env(monkeypatch)

    def interrupted():
        raise KeyboardInterrupt

    assert cli.main(["--user", "analyst_a"], lister=interrupted) == 130
    err = capsys.readouterr().err
    assert "Traceback" not in err and "KeyboardInterrupt" not in err
    assert not factory.calls


def test_cli_unexpected_startup_error_exits_2_with_fixed_line(monkeypatch, capsys, caplog):
    _env(monkeypatch)

    def broken(settings, checkpointer, session, data_dir):
        raise RuntimeError(f"boom {SENTINEL} /home/someone/secret-path")

    monkeypatch.setattr(cli, "build_runtime", broken)
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == cli.UNEXPECTED_TEXT
    assert "Traceback" not in captured.err + captured.out
    assert SENTINEL not in captured.err + captured.out + caplog.text
    assert "secret-path" not in caplog.text and "RuntimeError" in caplog.text  # type only


def test_cli_registers_secrets_and_installs_redaction(monkeypatch, factory):
    import logging

    from opsfleet_agent.obs import tracer as tr

    aes = "SENTINEL-aes-for-cli-test-012345"  # synthetic, 32 characters
    _env(monkeypatch, aes)
    _inputs(monkeypatch, "exit")
    tr.clear_secrets()
    try:
        assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
        assert tr._installed_factory is not None
        rec = logging.getLogger("x.y").makeRecord(
            "x.y", logging.INFO, "f", 1, f"k={SENTINEL} {aes}", None, None
        )
        assert SENTINEL not in rec.getMessage()
        assert aes not in rec.getMessage()
    finally:
        tr.uninstall_log_filter()
        tr.clear_secrets()


def test_cli_banner_shows_user_scope_session(monkeypatch, capsys, factory):
    _env(monkeypatch)
    _inputs(monkeypatch, "exit")
    assert cli.main(["--user", "analyst_b"], lister=lambda: all_models()) == 0
    out = capsys.readouterr().out
    assert "Analyst B" in out
    assert "Brands: Carhartt, Levi's" in out
    assert cli.HINT_TEXT in out
    m = re.search(r"Session: ([0-9a-f]{32})", out)
    assert m
    _inputs(monkeypatch, "exit")
    cli.main(["--user", "ceo_demo"], lister=lambda: all_models())
    out2 = capsys.readouterr().out
    assert "All products" in out2
    assert m.group(1) not in out2  # a new session id per run


def test_cli_rejects_unknown_user(monkeypatch, capsys, factory):
    _env(monkeypatch)
    assert cli.main(["--user", "nobody"], lister=_no_lister) == 2
    err = capsys.readouterr().err
    assert "nobody" in err and "analyst_a" in err and "ceo_demo" in err
    assert "Traceback" not in err
    assert not factory.calls


def test_cli_refuses_to_start_without_pii_model(monkeypatch, capsys, factory):
    from opsfleet_agent.guards import pii

    _env(monkeypatch)

    def missing() -> None:
        raise pii.PiiModelMissing("spaCy model 'en_core_web_sm' is not installed")

    monkeypatch.setattr(cli, "ensure_model_available", missing)
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 2
    assert "en_core_web_sm" in capsys.readouterr().err
    assert not factory.calls


def test_cli_installs_detector_with_profile_brands(monkeypatch, factory):
    from opsfleet_agent.guards import pii

    _env(monkeypatch)
    _inputs(monkeypatch, "exit")
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    detector = pii.default_detector()
    brands = {b for p in cli.load_profiles_with_overrides().values() for b in p.brands}
    assert brands and all(detector.allowlist.covers(b, 0, len(b)) for b in brands)


# --- REPL ---


def test_cli_answers_through_terminal_safe(monkeypatch, capsys, factory):
    _env(monkeypatch)
    factory.graph.answer = "Revenue \x1b[2Jwas 42\x1b]52;c;ZXZpbA==\x07."
    _inputs(monkeypatch, "  what was revenue?  ", "", "quit")
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "was 42" in out
    ((text, sid, turn_id),) = factory.graph.calls
    assert text == "what was revenue?" and len(sid) == 32 and re.fullmatch(r"[0-9a-f]{12}", turn_id)
    assert factory.closed == [True]


def test_cli_commands(monkeypatch, capsys, factory):
    _env(monkeypatch)
    _inputs(
        monkeypatch,
        "/help",
        "/audit",
        "/persona",
        "/reports",
        "/bogus \x1b[2Jsecret-ish",
        "/feedback up",
        "/exit",
    )
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    out = capsys.readouterr().out
    for name in ("/help", "/exit", "/feedback", "/trace", "/audit", "/persona", "/reports"):
        assert name in out
    assert "Example questions:" in out
    assert AUDIT_UNAVAILABLE in out  # no audit log wired in this fake runtime
    assert "Active persona: builtin-0000abcd" in out
    assert commands.NOT_AVAILABLE_TEXT.format(name="/reports") in out
    assert commands.UNKNOWN_TEXT in out and "secret-ish" not in out
    assert commands.STORE_UNAVAILABLE_TEXT in out  # /feedback with no store
    sid = re.search(r"Session: ([0-9a-f]{32})", out).group(1)
    assert out.rstrip().endswith("Goodbye.") and out.count(f"Session: {sid}") == 2
    assert not factory.graph.calls  # commands never reach the graph


def test_commands_table_has_no_tool_surface():
    """Commands are CLI-only; /audit in particular is never registered as a model tool."""
    from opsfleet_agent.tools import registry

    assert "/audit" in commands.COMMANDS
    for role in ("router", "quick_analyst", "deep_analyst", "library_agent"):
        names = registry.tools_for(role)
        assert not any("audit" in n or n.lstrip("/") in {"exit", "persona"} for n in names)
    ctx = commands.CommandContext(user_id="analyst_a", session_id="s1")
    assert commands.dispatch("exit", ctx).exit
    assert commands.dispatch("/trace", ctx).text == commands.STORE_UNAVAILABLE_TEXT


def test_cli_audit_command_shows_only_own_rows(monkeypatch, capsys, factory):
    """A log with rows for two users: /audit shows this session's own rows, /audit --user
    all of this user's sessions, and never another user's rows."""
    from opsfleet_agent.store.audit import AuditEvent

    def ev(n: int, user: str, session: str) -> AuditEvent:
        return AuditEvent(
            seq=n, event_id=f"e{n}", ts=f"2026-01-01T00:00:0{n}.000Z", actor_user_id=user,
            session_id=session, turn_id=f"turn-{user}-{n}", pending_action_id=None,
            event_type="report_deleted",
        )  # fmt: skip

    class Log:
        def __init__(self, rows):
            self.rows, self.queries = rows, []

        def events(self, *, user_id, session_id=None, newest_first=True, limit=50):
            self.queries.append((user_id, session_id))
            rows = [r for r in self.rows if r.actor_user_id == user_id]
            if session_id is not None:
                rows = [r for r in rows if r.session_id == session_id]
            return rows[:limit]

    state: dict[str, Any] = {}

    def make(settings, checkpointer, session, data_dir) -> cli.Runtime:
        state["sid"] = session.session_id
        factory.audit_log = Log(
            [ev(1, "analyst_a", session.session_id), ev(2, "analyst_a", "older-session"),
             ev(3, "analyst_b", session.session_id), ev(4, "analyst_b", "b-session")]
        )  # fmt: skip
        return FakeFactory.__call__(factory, settings, checkpointer, session, data_dir)

    monkeypatch.setattr(cli, "build_runtime", make)
    _env(monkeypatch)
    _inputs(monkeypatch, "/audit", "/audit --user", "/exit")
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    out = capsys.readouterr().out
    assert factory.audit_log.queries == [("analyst_a", state["sid"]), ("analyst_a", None)]
    assert out.count("turn=turn-analyst_a-1") == 2  # in both views
    assert out.count("turn=turn-analyst_a-2") == 1  # --user view only
    assert "analyst_b" not in out


def test_cli_ctrl_c_during_turn_cancels_job_and_returns_to_prompt(monkeypatch, capsys, factory):
    _env(monkeypatch)
    state = {"n": 0}

    def interrupt_first():
        state["n"] += 1
        if state["n"] == 1:
            signal.raise_signal(signal.SIGINT)  # as if the user pressed Ctrl-C mid-query

    factory.graph.on_turn = interrupt_first
    before = signal.getsignal(signal.SIGINT)
    _inputs(monkeypatch, "slow question", "next question", "/exit")
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    out = capsys.readouterr().out
    assert cli.CANCELLED_TEXT in out
    assert factory.cancels == ["cancel", "clear"]  # job cancelled, stale flag cleared
    assert len(factory.graph.calls) == 2 and factory.graph.answer in out  # loop continued
    assert signal.getsignal(signal.SIGINT) is before  # handler restored


def test_cli_ctrl_c_with_real_runner_resets_cancel_flag(monkeypatch, capsys):
    """The real BigQueryRunner: Ctrl-C sets its cancel flag, reset_cancel clears it, so the
    next turn's query is not refused by a stale flag."""
    from opsfleet_agent.bq.client import BigQueryRunner

    from .test_bq_client import FakeClient

    runner = BigQueryRunner(FakeClient(), "test-project")
    seen: list[bool] = []
    graph = FakeGraph()

    def on_turn():
        seen.append(runner._cancelled)
        if len(seen) == 1:
            signal.raise_signal(signal.SIGINT)

    graph.on_turn = on_turn

    after_cancel: list[bool] = []

    def cancel():
        runner.cancel_inflight()
        after_cancel.append(runner._cancelled)

    def make(settings, checkpointer, session, data_dir) -> cli.Runtime:
        return cli.Runtime(graph=graph, cancel=cancel, clear_cancel=runner.reset_cancel)

    monkeypatch.setattr(cli, "build_runtime", make)
    _env(monkeypatch)
    _inputs(monkeypatch, "slow question", "next question", "/exit")
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    assert cli.CANCELLED_TEXT in capsys.readouterr().out
    assert after_cancel == [True]  # the real cancel set the flag ...
    assert seen == [False, False]  # ... and reset_cancel cleared it before the next turn
    assert runner._cancelled is False


def test_cli_ctrl_c_at_prompt(monkeypatch, capsys, factory):
    _env(monkeypatch)
    _inputs(monkeypatch, KeyboardInterrupt, "exit")
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    assert cli.PROMPT_INTERRUPT_TEXT in capsys.readouterr().out
    _inputs(monkeypatch, KeyboardInterrupt, KeyboardInterrupt)
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 130
    assert re.search(r"Session: [0-9a-f]{32}", capsys.readouterr().out)
    _inputs(monkeypatch, KeyboardInterrupt, "hi", KeyboardInterrupt, EOFError)
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0  # counter reset


def test_cli_turn_limit_is_bounded(monkeypatch, capsys, factory):
    _env(monkeypatch)
    monkeypatch.setattr(cli, "MAX_TURNS", 3)
    monkeypatch.setattr("builtins.input", lambda _="": "again")
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    assert len(factory.graph.calls) == 3
    assert "Turn limit reached" in capsys.readouterr().out


# --- narrow --resume ---


def test_resume_rejects_other_users_session(
    tmp_path, settings, detector, monkeypatch, capsys, factory
):
    """Same scope, other user: refused before any network call, with the same fixed line as
    an unknown id (no ownership oracle)."""
    key = _env(monkeypatch)
    _profiles(monkeypatch, tmp_path, "[Acme]", "[Acme]")
    _crashed_session(tmp_path, settings, detector, monkeypatch, key)

    assert cli.main(["--user", "analyst_b", "--resume", SID], lister=_no_lister) == 2
    other = capsys.readouterr()
    assert other.err.strip() == cli.NO_SUCH_SESSION_TEXT and other.out == ""
    assert not factory.calls

    assert cli.main(["--user", "analyst_b", "--resume", "no-such-id"], lister=_no_lister) == 2
    unknown = capsys.readouterr()
    assert unknown.err == other.err and unknown.out == ""  # identical: nothing revealed


def test_resume_scope_drift_new_session(tmp_path, settings, detector, monkeypatch, capsys, factory):
    """analyst_a's brands changed since the crash: a new session, never a replay."""
    key = _env(monkeypatch)
    _profiles(monkeypatch, tmp_path, "[Other]")
    env = _crashed_session(tmp_path, settings, detector, monkeypatch, key)
    counts = (len(env.analyst.calls), len(env.router.calls), len(env.client.executed))
    _inputs(monkeypatch, "/exit")

    assert cli.main(["--user", "analyst_a", "--resume", SID], lister=lambda: all_models()) == 0
    out = capsys.readouterr().out
    assert rs.SCOPE_DRIFT_TEXT in out
    sid = re.search(r"Session: ([0-9a-f]{32})", out).group(1)
    assert sid != SID and f"Session: {SID}" not in out
    assert factory.calls[0].session_id == sid
    assert (len(env.analyst.calls), len(env.router.calls), len(env.client.executed)) == counts


def test_resume_own_session_finishes_interrupted_turn(
    tmp_path, settings, detector, monkeypatch, capsys
):
    key = _env(monkeypatch)
    _profiles(monkeypatch, tmp_path, "[Acme]")
    env = _crashed_session(tmp_path, settings, detector, monkeypatch, key)
    built: list[Any] = []

    def real_factory(s, checkpointer, session, data_dir) -> cli.Runtime:
        fresh = Env(
            tmp_path, settings, detector, env.router, env.analyst,
            client=env.client, saver=checkpointer,
        )  # fmt: skip
        built.append(session)
        return cli.Runtime(
            graph=fresh.graph, cancel=lambda: None, clear_cancel=lambda: None,
            trace_dir=tmp_path / "traces",
        )  # fmt: skip

    monkeypatch.setattr(cli, "build_runtime", real_factory)
    _inputs(monkeypatch, "/trace", "/exit")
    assert cli.main(["--user", "analyst_a", "--resume", SID], lister=lambda: all_models()) == 0
    out = capsys.readouterr().out
    assert ANSWER in out and f"Session: {SID}" in out  # same session continues
    assert "no answered turn yet" not in out  # the resumed turn is /trace's default turn
    assert built[0].session_id == SID
    assert len(env.client.executed) == 1  # the completed query was not re-run


def test_resume_wrong_key_refused(tmp_path, settings, detector, monkeypatch, capsys, factory):
    key = _env(monkeypatch)
    _profiles(monkeypatch, tmp_path, "[Acme]")
    _crashed_session(tmp_path, settings, detector, monkeypatch, key)
    other_key = new_key()
    monkeypatch.setenv("LANGGRAPH_AES_KEY", other_key)
    assert cli.main(["--user", "analyst_a", "--resume", SID], lister=_no_lister) == 2
    captured = capsys.readouterr()
    assert other_key not in captured.err + captured.out and key not in captured.err
    assert not factory.calls
    # the same line as an unknown id: a wrong key reveals nothing about which ids exist
    assert cli.main(["--user", "analyst_a", "--resume", "no-such-id"], lister=_no_lister) == 2
    unknown = capsys.readouterr()
    assert captured.err == unknown.err and captured.err.strip() == cli.NO_SUCH_SESSION_TEXT
    # another user under the wrong key: still the same line
    assert cli.main(["--user", "analyst_b", "--resume", SID], lister=_no_lister) == 2
    assert capsys.readouterr().err == unknown.err


def test_resume_ctrl_c_during_resume_cancels_and_continues(
    tmp_path, settings, detector, monkeypatch, capsys, factory
):
    key = _env(monkeypatch)
    _profiles(monkeypatch, tmp_path, "[Acme]")
    _crashed_session(tmp_path, settings, detector, monkeypatch, key)

    def interrupted_resume(*_a, **_k):
        signal.raise_signal(signal.SIGINT)  # as if Ctrl-C landed mid-resume

    monkeypatch.setattr(cli, "resume_turn", interrupted_resume)
    before = signal.getsignal(signal.SIGINT)
    _inputs(monkeypatch, "next question", "/exit")
    assert cli.main(["--user", "analyst_a", "--resume", SID], lister=lambda: all_models()) == 0
    captured = capsys.readouterr()
    assert cli.CANCELLED_TEXT in captured.out and "Traceback" not in captured.err
    assert factory.cancels == ["cancel", "clear"]
    assert factory.graph.calls[0][:2] == ("next question", SID)  # the prompt continued
    assert signal.getsignal(signal.SIGINT) is before


def _interrupt_on_entry(monkeypatch, target: str) -> None:
    """A Ctrl-C (KeyboardInterrupt) once, on entry of graph node ``target``."""
    from opsfleet_agent.graph import graph as gr

    real = gr._make_nodes
    fired = [False]

    def make(ctx):
        nodes = real(ctx)
        fn = nodes[target]

        def interrupted(state):
            if not fired[0]:
                fired[0] = True
                raise KeyboardInterrupt
            return fn(state)

        return {**nodes, target: interrupted}

    monkeypatch.setattr(gr, "_make_nodes", make)


def test_cancelled_turn_is_not_replayed_by_resume(
    tmp_path, settings, detector, monkeypatch, capsys
):
    """Durable cancel: Ctrl-C mid-turn closes the checkpointed turn, so a later --resume
    finds nothing pending and runs no query and no model call."""
    _env(monkeypatch)
    _profiles(monkeypatch, tmp_path, "[Acme]")
    analyst, router = escalating_analyst(), Router("simple")
    shared: dict[str, Any] = {}

    def real_factory(s, checkpointer, session, data_dir) -> cli.Runtime:
        env = Env(
            tmp_path, settings, detector, router, analyst,
            client=shared.get("client"), saver=checkpointer,
        )  # fmt: skip
        shared["client"] = env.client
        return cli.Runtime(graph=env.graph, cancel=lambda: None, clear_cancel=lambda: None)

    monkeypatch.setattr(cli, "build_runtime", real_factory)
    _interrupt_on_entry(monkeypatch, "deep")
    _inputs(monkeypatch, QUESTION, "/exit")
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    out = capsys.readouterr().out
    assert cli.CANCELLED_TEXT in out
    sid = re.search(r"Session: ([0-9a-f]{32})", out).group(1)
    counts = (len(analyst.calls), len(router.calls), len(shared["client"].executed))
    assert counts[2] == 1  # the quick query ran before the Ctrl-C

    _inputs(monkeypatch, "/exit")
    assert cli.main(["--user", "analyst_a", "--resume", sid], lister=lambda: all_models()) == 0
    out = capsys.readouterr().out
    assert rs.NOTHING_PENDING_TEXT in out and f"Session: {sid}" in out
    assert (len(analyst.calls), len(router.calls), len(shared["client"].executed)) == counts


def test_resume_nothing_pending_continues_same_session(
    tmp_path, settings, detector, monkeypatch, capsys
):
    key = _env(monkeypatch)
    _profiles(monkeypatch, tmp_path, "[Acme]")
    env = Env(
        tmp_path, settings, detector, Router("simple"), escalating_analyst(),
        saver=saver(tmp_path, key),
    )  # fmt: skip
    assert env.ask(QUESTION).outcome == "answered"
    env.saver.conn.close()

    def real_factory(s, checkpointer, session, data_dir) -> cli.Runtime:
        fresh = Env(tmp_path, settings, detector, env.router, env.analyst, saver=checkpointer)
        return cli.Runtime(graph=fresh.graph, cancel=lambda: None, clear_cancel=lambda: None)

    monkeypatch.setattr(cli, "build_runtime", real_factory)
    _inputs(monkeypatch, "/exit")
    assert cli.main(["--user", "analyst_a", "--resume", SID], lister=lambda: all_models()) == 0
    out = capsys.readouterr().out
    assert rs.NOTHING_PENDING_TEXT in out and f"Session: {SID}" in out


# --- terminal safety ---


def test_terminal_safe_strips_control_sequences() -> None:
    from opsfleet_agent.cli import terminal_safe

    hostile = (
        "\x1b]8;;https://evil.example\x1b\\click\x1b]8;;\x1b\\"
        "\x1b]52;c;ZXZpbA==\x07\x1b[2J\x9b31m42\x08\x0812\r"
    )
    out = terminal_safe(hostile)
    assert not any(ord(c) < 0x20 and c not in "\n\t" for c in out)
    assert not any(0x7F <= ord(c) <= 0x9F for c in out)
    assert terminal_safe("a\tb\nc — ü") == "a\tb\nc — ü"


def test_terminal_safe_strips_bidi_and_separators() -> None:
    from opsfleet_agent.cli import terminal_safe

    assert terminal_safe("a‮b⁦c​d e﻿") == "abcd e"


def test_terminal_safe_strips_every_format_character() -> None:
    from opsfleet_agent.cli import terminal_safe

    # U+061C arabic letter mark, U+180E mongolian vowel separator, ZWJ, soft hyphen
    assert terminal_safe("a؜b᠎c‍d­e") == "abcde"
