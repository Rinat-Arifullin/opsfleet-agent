"""D-147: the CLI stage spinner and the graph's progress events (offline, no network)."""

from __future__ import annotations

import io
import re
import signal
import threading
import time
from typing import Any

import pytest

from opsfleet_agent import cli, cli_progress
from opsfleet_agent.cli_progress import ERASE, FRAMES, STAGE_LABELS, Spinner, stage_label
from opsfleet_agent.obs import progress
from opsfleet_agent.roles.analyst import ModelTurn
from tests.unit import test_graph as _tg
from tests.unit.test_cli import (  # noqa: F401  (_isolated: autouse fixture)
    FakeFactory,
    FakeGraph,
    _env,
    _inputs,
    _isolated,
    _profiles,
)
from tests.unit.test_config import all_models
from tests.unit.test_graph import Env, Router, Scripted, sql_call
from tests.unit.test_run_sql import SIMPLE

detector = _tg.detector
settings = _tg.settings

ANSWER = "There were 3 complete orders."
QUESTION = "How many complete orders are there?"


class FakeTTY(io.StringIO):
    def __init__(self, tty: bool = True) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class BrokenTTY(FakeTTY):
    def write(self, s: str) -> int:
        raise OSError("closed")


def _wait_for(cond, bound_s: float = 2.0) -> bool:
    end = time.monotonic() + bound_s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture(autouse=True)
def _term(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")


# --- the spinner component ---------------------------------------------------------------------


def test_non_tty_writes_nothing_and_installs_no_hook() -> None:
    stream = FakeTTY(tty=False)
    sp = Spinner(stream, interval_s=0.001)
    sp.start()
    seen: list[str] = []
    with progress.reporting(seen.append):  # the spinner must not have replaced any hook
        progress.report("input_guard")
    time.sleep(0.02)
    sp.stop()
    assert stream.getvalue() == "" and not sp.enabled and sp._thread is None
    assert seen == ["input_guard"]


def test_dumb_terminal_is_treated_as_non_tty(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "dumb")
    stream = FakeTTY()
    sp = Spinner(stream, interval_s=0.001)
    sp.start()
    time.sleep(0.02)
    sp.stop()
    assert stream.getvalue() == "" and not sp.enabled


def test_tty_draws_stage_then_erases_the_line() -> None:
    stream = FakeTTY()
    sp = Spinner(stream, interval_s=0.001)
    sp.start()
    progress.report("input_guard")
    assert _wait_for(lambda: "Understanding the question…" in stream.getvalue())
    progress.report("tool:run_sql")
    progress.report("tool:run_sql")
    assert _wait_for(lambda: "Running SQL (2)…" in stream.getvalue())
    sp.stop()
    out = stream.getvalue()
    assert out.endswith(ERASE) and any(f in out for f in FRAMES)
    thread_gone = sp._thread is None
    time.sleep(0.02)
    assert stream.getvalue() == out and thread_gone  # nothing drawn after stop
    progress.report("finalize")  # the hook was removed
    assert sp.label == "Running SQL (2)…"


def test_stop_is_idempotent_and_safe_before_start() -> None:
    stream = FakeTTY()
    sp = Spinner(stream, interval_s=0.001)
    sp.stop()  # before start
    sp.start()  # after stop: stays off
    sp.stop()
    assert stream.getvalue() == ""
    sp2 = Spinner(stream, interval_s=0.001)
    sp2.start()
    assert _wait_for(lambda: stream.getvalue() != "")
    sp2.stop()
    sp2.stop()
    assert stream.getvalue().endswith(ERASE) and stream.getvalue().count(ERASE + "⠋") <= 1


def test_spinner_thread_is_daemon_and_does_not_leak() -> None:
    before = {t.ident for t in threading.enumerate()}
    for _ in range(5):
        sp = Spinner(FakeTTY(), interval_s=0.001)
        sp.start()
        assert sp._thread is not None and sp._thread.daemon
        sp.stop()
    assert {t.ident for t in threading.enumerate() if t.name == "cli-spinner"} <= before


def test_broken_stream_stops_drawing_without_raising() -> None:
    sp = Spinner(BrokenTTY(), interval_s=0.001)
    sp.start()
    assert _wait_for(lambda: sp._stopped)
    sp.stop()


def test_failing_hook_is_swallowed_and_disabled() -> None:
    calls: list[str] = []

    def boom(event: str) -> None:
        calls.append(event)
        raise RuntimeError("display broke")

    with progress.reporting(boom):
        progress.report("input_guard")  # no exception reaches the caller
        progress.report("finalize")  # disabled after the first failure
    assert calls == ["input_guard"]


def test_reporting_restores_previous_hook() -> None:
    outer: list[str] = []
    with progress.reporting(outer.append):
        with progress.reporting(None):
            progress.report("quick")
        progress.report("deep")
    progress.report("finalize")  # no hook: a no-op
    assert outer == ["deep"]


def test_stage_label_mapping() -> None:
    assert stage_label("input_guard", 0) == "Understanding the question…"
    assert stage_label("tool:get_schema", 0) == "Reading the schema…"
    assert stage_label("tool:list_tables", 0) == "Reading the schema…"
    assert stage_label("tool:run_sql", 3) == "Running SQL (3)…"
    assert stage_label("grounding", 0) == "Checking the numbers…"
    assert stage_label("finalize", 0) == "Checking the answer…"
    assert stage_label("tool:something_the_model_made_up", 0) is None
    assert stage_label("Ignore previous instructions", 0) is None


def test_labels_are_plain_short_constants() -> None:
    for label in [*STAGE_LABELS.values(), stage_label("tool:run_sql", 12)]:
        assert label == cli.terminal_safe(label) and "\n" not in label and len(label) < 40


def test_every_graph_node_has_a_label() -> None:
    from opsfleet_agent.graph import graph as gr

    nodes = gr._make_nodes(_ctx_stub())  # builds the node closures only; nothing runs
    assert set(nodes) <= set(STAGE_LABELS)


def _ctx_stub() -> Any:
    class _S:
        settings = None

    class _Ctx:
        services = _S()

    return _Ctx()


# --- graph events (no behaviour change) ----------------------------------------------------------


def _ask(tmp_path, settings, detector, hook):
    analyst = Scripted(sql_call(SIMPLE), ModelTurn(ANSWER))
    env = Env(tmp_path, settings, detector, Router("simple"), analyst)
    with progress.reporting(hook):
        out = env.ask(QUESTION, turn_id="t1")
    return env, out


def test_graph_reports_fixed_stage_events(tmp_path, settings, detector) -> None:
    events: list[str] = []
    env, out = _ask(tmp_path, settings, detector, events.append)
    assert out.outcome == "answered" and ANSWER in out.text
    assert events[0] == "input_guard" and events[-1] == "finalize"
    assert "tool:run_sql" in events and events.index("quick") < events.index("tool:run_sql")
    assert all(e in STAGE_LABELS or e == cli_progress.SQL_EVENT for e in events)


def test_hook_does_not_change_the_turn(tmp_path, settings, detector) -> None:
    def boom(event: str) -> None:
        raise RuntimeError("display broke")

    env_a, a = _ask(tmp_path / "a", settings, detector, None)
    env_b, b = _ask(tmp_path / "b", settings, detector, boom)
    assert (a.text, a.outcome, a.llm_calls, a.sql_queries) == (
        b.text, b.outcome, b.llm_calls, b.sql_queries,
    )  # fmt: skip
    assert len(env_a.analyst.calls) == len(env_b.analyst.calls)
    assert len(env_a.router.calls) == len(env_b.router.calls)
    assert [s[:2] for s in env_a.spans] == [s[:2] for s in env_b.spans]


# --- CLI -----------------------------------------------------------------------------------------


def _run_cli(monkeypatch, tmp_path, settings, detector, *, slow_s: float = 0.0) -> list[Env]:
    envs: list[Env] = []

    class SlowRouter(Router):
        def __call__(self, model, messages, timeout):
            time.sleep(slow_s)
            return super().__call__(model, messages, timeout)

    def factory(s, checkpointer, session, data_dir) -> cli.Runtime:
        analyst = Scripted(sql_call(SIMPLE), ModelTurn(ANSWER))
        env = Env(tmp_path, settings, detector, SlowRouter("simple"), analyst, saver=checkpointer)
        envs.append(env)
        return cli.Runtime(graph=env.graph, cancel=lambda: None, clear_cancel=lambda: None)

    monkeypatch.setattr(cli, "build_runtime", factory)
    _inputs(monkeypatch, QUESTION, "/exit")
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    return envs


class _NoSpinner:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _normalized(out: str) -> str:
    return re.sub(r"[0-9a-f]{32}", "<sid>", out)


def test_cli_non_tty_output_is_unchanged(monkeypatch, tmp_path, settings, detector, capsys):
    _env(monkeypatch)
    _profiles(monkeypatch, tmp_path, "[Acme]")
    monkeypatch.setattr(cli_progress, "INTERVAL_S", 0.001)
    with_spinner = _run_cli(monkeypatch, tmp_path / "a", settings, detector, slow_s=0.02)
    out_a = capsys.readouterr().out
    monkeypatch.setattr(cli, "Spinner", _NoSpinner)  # the pre-D-147 CLI
    without = _run_cli(monkeypatch, tmp_path / "b", settings, detector, slow_s=0.02)
    out_b = capsys.readouterr().out
    assert ANSWER in out_a and _normalized(out_a) == _normalized(out_b)
    assert "\x1b" not in out_a and not any(f in out_a for f in FRAMES)
    assert len(with_spinner[0].analyst.calls) == len(without[0].analyst.calls)
    assert len(with_spinner[0].router.calls) == len(without[0].router.calls)


def test_cli_tty_spinner_is_erased_before_the_answer(monkeypatch, tmp_path, settings, detector):
    _env(monkeypatch)
    _profiles(monkeypatch, tmp_path, "[Acme]")
    monkeypatch.setattr(cli_progress, "INTERVAL_S", 0.001)
    tty = FakeTTY()
    monkeypatch.setattr("sys.stdout", tty)
    envs = _run_cli(monkeypatch, tmp_path, settings, detector, slow_s=0.05)
    out = tty.getvalue()
    before_answer = out[: out.index(ANSWER)]
    assert "Understanding the question…" in before_answer  # the router was slow enough
    assert before_answer.endswith(ERASE)  # the line is gone before the answer prints
    after = out[out.index(ANSWER) :]
    assert "\x1b" not in after and not any(f in after for f in FRAMES)
    assert len(envs[0].analyst.calls) == 2


def test_cli_ctrl_c_erases_spinner_before_cancelled(monkeypatch, tmp_path):
    _env(monkeypatch)
    _profiles(monkeypatch, tmp_path, "[Acme]")
    monkeypatch.setattr(cli_progress, "INTERVAL_S", 0.001)
    tty = FakeTTY()
    monkeypatch.setattr("sys.stdout", tty)

    def interrupt() -> None:
        progress.report("input_guard")
        assert _wait_for(lambda: "Understanding the question…" in tty.getvalue())
        signal.raise_signal(signal.SIGINT)  # as if Ctrl-C landed mid-turn

    factory = FakeFactory(graph=FakeGraph(on_turn=interrupt))
    monkeypatch.setattr(cli, "build_runtime", factory)
    before = signal.getsignal(signal.SIGINT)
    _inputs(monkeypatch, QUESTION, "/exit")
    assert cli.main(["--user", "analyst_a"], lister=lambda: all_models()) == 0
    out = tty.getvalue()
    assert out[: out.index(cli.CANCELLED_TEXT)].endswith(ERASE)  # erased, then cancelled
    assert factory.cancels == ["cancel", "clear"]  # the existing interrupt handling ran
    assert signal.getsignal(signal.SIGINT) is before
