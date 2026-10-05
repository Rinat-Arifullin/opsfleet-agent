"""Command-line entry point: startup checks, narrow ``--resume`` and the REPL (iteration 19).

Startup order (every refusal is one line on stderr, exit code 2, no traceback):

1. profile (merged with admin overrides) and settings, before any network call;
2. local checks (profiles file, writable stores) and the encrypted checkpoint store; a
   missing or invalid ``LANGGRAPH_AES_KEY`` refuses to start (fail closed, D-78);
3. ``--resume``: the owner and scope pre-check reads the checkpoint locally. Another user's
   session and an unknown id get the same fixed line (no ownership oracle); a changed scope
   starts a new session instead of resuming (FR-76);
4. the network startup check (model listing) and the PII model, then the runtime.

The REPL is bounded (``MAX_TURNS``). Ctrl-C during a turn (or during the ``--resume``
turn) cancels the running BigQuery job, resets the runner's cancel flag, closes the turn in
the checkpoint so a later ``--resume`` never replays it, and returns to the prompt; Ctrl-C
at the prompt asks for a second one to quit. Ctrl-C at startup exits 130 and an unexpected
error exits 2 with one fixed line: never a traceback. Every answer and every command output
goes through :func:`terminal_safe`.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import sys
import threading
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from opsfleet_agent import commands
from opsfleet_agent.commands.access import load_profiles_with_overrides
from opsfleet_agent.config import (
    ConfigError,
    ModelLister,
    Settings,
    load_settings,
    safe_config_view,
    startup_check,
)
from opsfleet_agent.graph.graph import AES_KEY_ENV, build_checkpointer, scope_snapshot
from opsfleet_agent.graph.resume import (
    SCOPE_DRIFT_TEXT,
    STORE_REFUSED_TEXT,
    ResumeKind,
    close_interrupted_turn,
    resume_turn,
)
from opsfleet_agent.guards.pii import (
    PiiDetector,
    PiiDetectorError,
    build_allowlist,
    ensure_model_available,
    set_default_detector,
)
from opsfleet_agent.guards.scope import ProductScope
from opsfleet_agent.obs.tracer import install_log_filter, register_secret
from opsfleet_agent.session import (
    Profile,
    Session,
    banner,
    default_data_dir,
    local_startup_check,
    select_profile,
    start_session,
)

log = logging.getLogger(__name__)

MAX_TURNS: Final = 1000  # REPL bound: one process never loops forever
MAX_INPUT_CHARS: Final = 8000  # a pasted wall of text is cut before it reaches the graph
RESUME_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")  # used with fullmatch only
BAD_RESUME_ID_TEXT: Final = "Invalid --resume value: use the session id printed at /exit."
# One text for an unknown id, another user's session and a checkpoint the current key
# cannot decrypt: the reply never reveals that a session exists (no ownership oracle).
NO_SUCH_SESSION_TEXT: Final = (
    f"Cannot resume: no saved session with that id for this user under the current {AES_KEY_ENV}."
)
UNEXPECTED_TEXT: Final = (
    "Stopped on an unexpected error (details are in the log); check the setup in the README "
    "and try again."
)
CANCELLED_TEXT: Final = "Cancelled."
PROMPT_INTERRUPT_TEXT: Final = "(Press Ctrl-C again, or type /exit, to quit.)"
HINT_TEXT: Final = "Type /help for commands, /exit to quit."
EXIT_INTERRUPTED: Final = 130
CANCEL_BOUND_S: Final = 1.0  # the best-effort BigQuery cancel never holds the prompt longer
_MAC_FAILURE: Final = "MAC check failed"
_KEEP: Final = frozenset("\t\n")
# Cc: C0, DEL and C1 controls (ESC is in C0, so every ANSI/OSC sequence loses its
# introducer and prints as inert text). Cf: every format character (bidi controls incl.
# U+061C, zero-width characters, U+180E, U+FEFF, ...). Zl/Zp: the line and paragraph
# separators. All of these can drive the terminal, or reorder or hide text.
_UNSAFE_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})


def terminal_safe(text: str) -> str:
    """Strip terminal control and format characters before anything reaches stdout.

    Second layer behind the output guard: every agent answer and every command output goes
    through this, so no model or data text can drive the terminal. Tab and newline stay.
    """
    return "".join(
        c for c in text if c in _KEEP or unicodedata.category(c) not in _UNSAFE_CATEGORIES
    )


def _say(text: str) -> None:
    print(terminal_safe(text))


def _refuse(text: str) -> int:
    print(terminal_safe(text), file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="opsfleet-agent", description="Data-analysis chat agent over thelook_ecommerce."
    )
    p.add_argument("--user", required=True, help="profile id from config/profiles.yaml")
    p.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="finish the interrupted turn of one of YOUR sessions (id printed at /exit)",
    )
    return p


def _install_pii_detector(profiles: Iterable[Profile]) -> None:
    """Refuse to start without the spaCy model (8b); allowlist the configured brands.

    The full catalogue allowlist (brands, categories, departments from BigQuery) is an open
    decision (docs/process/iter19-ods.md).
    """
    ensure_model_available()
    brands = sorted({b for p in profiles for b in p.brands})
    set_default_detector(PiiDetector(build_allowlist(brands=brands)))


@dataclass
class Runtime:
    """What the REPL drives. Built by :func:`build_runtime` (or a test fake)."""

    graph: Any  # AgentGraph: run_turn(text, *, session, turn_id) -> TurnResult
    cancel: Callable[[], object]  # BigQueryRunner.cancel_inflight (normal flow, bounded)
    clear_cancel: Callable[[], object]  # BigQueryRunner.reset_cancel after a Ctrl-C (OD-6)
    tracer: Any = None
    audit_log: Any = None
    feedback_store: Any = None
    trace_dir: Path | None = None
    persona_version: Callable[[], str] | None = None
    close: Callable[[], object] | None = None
    report_store: Any = None  # iteration 17: ReportStore on app.db (/reports)


AgentFactory = Callable[[Settings, Any, Session, Path], Runtime]


def _make_router_invoke(settings: Settings) -> Any:  # pragma: no cover - needs the network
    """Adapter from the router protocol (system/user messages) to the Gemini chat model."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from opsfleet_agent.graph.llm import LLMResponse, build_chat_model

    cache: dict[str, Any] = {}

    def invoke(model: str, messages: Sequence[Any], timeout: float) -> LLMResponse:
        if model not in cache:
            cfg = next((r for r in settings.roles.values() if r.model == model), None)
            cache[model] = build_chat_model(
                model,
                settings.gemini_api_key,
                thinking_level=getattr(cfg, "thinking_level", None),
                thinking_budget=getattr(cfg, "thinking_budget", None),
            )
        lc = [
            SystemMessage(m.content) if m.role == "system" else HumanMessage(m.content)
            for m in messages
        ]
        msg = cache[model].invoke(lc, timeout=timeout)
        content = msg.content
        if isinstance(content, list):
            content = "".join(p if isinstance(p, str) else p.get("text", "") for p in content)
        usage = getattr(msg, "usage_metadata", None) or {}
        return LLMResponse(
            str(content or ""),
            int(usage.get("input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
        )

    return invoke


def build_runtime(
    settings: Settings, checkpointer: Any, session: Session, data_dir: Path
) -> Runtime:  # pragma: no cover - production wiring: BigQuery and Gemini clients
    """Wire the production graph: BigQuery runner, schema cache, guards, stores, tracer."""
    from opsfleet_agent.bq.client import BigQueryRunner, make_bigquery_client
    from opsfleet_agent.bq.schema import TableMetadataCache
    from opsfleet_agent.graph.graph import AgentGraph, GraphServices, build_run_sql_tool
    from opsfleet_agent.guards.differencing import DifferencingGuard
    from opsfleet_agent.guards.pii import default_detector
    from opsfleet_agent.obs.tracer import Tracer
    from opsfleet_agent.persona import PersonaStore
    from opsfleet_agent.roles.analyst import make_gemini_invoke
    from opsfleet_agent.store.audit import AuditLog, recorder
    from opsfleet_agent.store.db import open_store
    from opsfleet_agent.store.feedback import FeedbackStore
    from opsfleet_agent.store.fingerprints import FingerprintStore
    from opsfleet_agent.store.reports import ReportStore
    from opsfleet_agent.tools.run_sql import scoped_job_config_factory

    project = settings.google_cloud_project
    client = make_bigquery_client(project)
    runner = BigQueryRunner(client, project, job_config_factory=scoped_job_config_factory())
    cache = TableMetadataCache(client)
    conn = open_store(data_dir / "app.db")
    audit_log = AuditLog(conn)
    trace_dir = data_dir / "traces"
    tracer = Tracer(trace_dir, session.session_id)
    tool = build_run_sql_tool(
        settings,
        runner,
        DifferencingGuard(FingerprintStore(conn)),
        cache.refresh_date,
        tracer=tracer,
        audit=recorder(audit_log, user_id=session.profile.user_id),
    )
    personas = PersonaStore()
    reports = ReportStore(conn)  # iteration 17: the same app.db as audit and feedback
    services = GraphServices(
        settings=settings,
        persona=personas.refresh,
        detector=default_detector(),
        router_invoke=_make_router_invoke(settings),
        analyst_invoke=make_gemini_invoke(settings),
        run_sql=tool,
        cache=cache,
        tracer=tracer,
        reports=reports,
    )
    return Runtime(
        graph=AgentGraph(services, checkpointer),
        cancel=runner.cancel_inflight,
        clear_cancel=runner.reset_cancel,
        tracer=tracer,
        audit_log=audit_log,
        feedback_store=FeedbackStore(conn),
        trace_dir=trace_dir,
        persona_version=lambda: personas.current.version,
        close=conn.close,
        report_store=reports,
    )


def _resume_precheck(checkpointer: Any, session_id: str, profile: Profile) -> str:
    """Local, network-free owner and scope check before anything is built.

    Returns "ok", "drift", or a fixed refusal text. Another user's session, an unknown id
    and a checkpoint the current key cannot decrypt (AES MAC failure) all return the same
    text, so the reply never reveals that a session exists (OD-1).
    """
    try:
        stored = checkpointer.get_tuple({"configurable": {"thread_id": session_id}})
    except ValueError as exc:
        log.error("resume precheck: checkpoint read failed: %s", type(exc).__name__)
        return NO_SUCH_SESSION_TEXT if _MAC_FAILURE in str(exc) else STORE_REFUSED_TEXT
    except Exception as exc:
        log.error("resume precheck: checkpoint read failed: %s", type(exc).__name__)
        return STORE_REFUSED_TEXT
    values = (stored.checkpoint.get("channel_values") or {}) if stored is not None else {}
    if stored is None or values.get("owner") != profile.user_id:
        return NO_SUCH_SESSION_TEXT
    try:
        current = scope_snapshot(ProductScope.from_profile(profile))
    except Exception as exc:  # an invalid profile never resumes (fail closed)
        log.error("resume precheck: profile scope invalid: %s", type(exc).__name__)
        return "drift"
    return "ok" if values.get("scope_snapshot") == current else "drift"


@dataclass
class _Repl:
    runtime: Runtime
    session: Session
    last_turn_id: str | None = None

    def _ctx(self) -> commands.CommandContext:
        rt = self.runtime
        return commands.CommandContext(
            user_id=self.session.profile.user_id,
            session_id=self.session.session_id,
            last_turn_id=self.last_turn_id,
            trace_dir=rt.trace_dir,
            feedback_store=rt.feedback_store,
            audit_log=rt.audit_log,
            tracer=rt.tracer,
            persona_version=rt.persona_version,
            report_store=rt.report_store,
        )

    def new_session(self) -> None:
        self.session = start_session(self.session.profile)
        self.last_turn_id = None
        if self.runtime.tracer is not None:
            self.runtime.tracer.session_id = self.session.session_id

    def guarded(self, run: Callable[[], Any], turn_id: str | None) -> Any | None:
        """Run ``run`` with Ctrl-C mapped to a cancel; ``None`` when it was cancelled.

        The SIGINT handler only raises ``KeyboardInterrupt`` (no I/O in the handler). It is
        installed inside the ``try`` and the previous handler is restored before the cancel
        work runs, so a Ctrl-C at any point lands in exactly one place. The BigQuery cancel,
        the flag reset and the checkpoint close all run in normal flow (:meth:`_cancelled`).
        Used for a normal turn and for the ``--resume`` turn alike.
        """
        in_main = threading.current_thread() is threading.main_thread()
        previous = signal.getsignal(signal.SIGINT) if in_main else None
        try:
            try:
                if in_main:
                    signal.signal(signal.SIGINT, _raise_interrupt)
                return run()
            finally:
                if in_main and previous is not None:
                    signal.signal(signal.SIGINT, previous)
        except KeyboardInterrupt:
            self._cancelled(turn_id)
            return None

    def turn(self, text: str) -> None:
        """Run one question; Ctrl-C cancels the in-flight BigQuery job and returns."""
        turn_id = uuid.uuid4().hex[:12]
        result = self.guarded(
            lambda: self.runtime.graph.run_turn(text, session=self.session, turn_id=turn_id),
            turn_id,
        )
        if result is not None:
            self.show(result, turn_id)

    def show(self, result: Any, turn_id: str | None) -> None:
        """Print an answer and its notice; remember the turn for /feedback and /trace."""
        _say(result.text)
        if getattr(result, "notice", None):
            _say(result.notice)
        outcome = str(getattr(result, "outcome", None) or "")
        if turn_id and (outcome == "answered" or outcome.startswith("report_")):
            self.last_turn_id = turn_id  # m6: report turns too (draft, saved, cancelled)

    def _cancelled(self, turn_id: str | None) -> None:
        """After a Ctrl-C: cancel the job, reset the flag, close the turn, say so.

        A second Ctrl-C while this runs is swallowed (one quiet handler for the whole
        cleanup, plus a backstop catch), so it never surfaces as a traceback. Every step is
        bounded: the BigQuery cancel waits at most ``CANCEL_BOUND_S``.
        """
        rt = self.runtime
        in_main = threading.current_thread() is threading.main_thread()
        previous = signal.getsignal(signal.SIGINT) if in_main else None
        try:
            if in_main:
                signal.signal(signal.SIGINT, _ignore_interrupt)
            _bounded(rt.cancel, CANCEL_BOUND_S, "cancel")
            _step(rt.clear_cancel, "cancel reset")  # a stale flag never refuses the next turn
            # durable cancel: the cancelled turn is closed, so --resume never replays it
            _step(lambda: close_interrupted_turn(rt.graph, self.session, turn_id), "close")
            if rt.tracer is not None:
                _step(
                    lambda: rt.tracer.record(
                        "turn", name="turn_cancelled", turn_id=turn_id, outcome="cancelled"
                    ),
                    "trace of cancel",
                )
        except KeyboardInterrupt:  # backstop: a Ctrl-C before the quiet handler was set
            pass
        finally:
            if in_main and previous is not None:
                signal.signal(signal.SIGINT, previous)
        _say(CANCELLED_TEXT)

    def loop(self) -> int:
        interrupts = 0
        for _ in range(MAX_TURNS):
            try:
                line = input("> ")
            except EOFError:
                _say(f"Session: {self.session.session_id}")
                return 0
            except KeyboardInterrupt:
                interrupts += 1
                if interrupts >= 2:
                    _say(f"\nSession: {self.session.session_id}")
                    return EXIT_INTERRUPTED
                _say("\n" + PROMPT_INTERRUPT_TEXT)
                continue
            interrupts = 0
            line = line.strip()[:MAX_INPUT_CHARS]
            if not line:
                continue
            if commands.is_command(line):
                res = commands.dispatch(line, self._ctx())
                _say(res.text)
                if res.exit:
                    return 0
                continue
            try:
                self.turn(line)
            except KeyboardInterrupt:  # backstop: a Ctrl-C between two guarded steps
                _say(CANCELLED_TEXT)
        _say(f"Turn limit reached for one run. Session: {self.session.session_id}")
        return 0


def _raise_interrupt(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def _ignore_interrupt(_signum: int, _frame: Any) -> None:
    return None


def _step(fn: Callable[[], object], what: str) -> None:
    try:
        fn()
    except Exception as exc:
        log.error("%s failed: %s", what, type(exc).__name__)


def _bounded(fn: Callable[[], object], timeout_s: float, what: str) -> None:
    """Run a best-effort network call for at most ``timeout_s`` (a daemon thread)."""
    worker = threading.Thread(target=_step, args=(fn, what), daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        log.warning("%s still running after %.1fs; continuing", what, timeout_s)


def main(
    argv: Sequence[str] | None = None,
    *,
    lister: ModelLister | None = None,
    agent_factory: AgentFactory | None = None,
) -> int:
    """Run the CLI. Never shows a traceback: Ctrl-C exits 130, an unexpected error exits 2
    with one fixed line (only the exception type is logged)."""
    try:
        return _main(argv, lister=lister, agent_factory=agent_factory)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # startup model listing, PII model load, ADC, ...
        log.error("unexpected error: %s", type(exc).__name__)
        return _refuse(UNEXPECTED_TEXT)


def _main(
    argv: Sequence[str] | None,
    *,
    lister: ModelLister | None,
    agent_factory: AgentFactory | None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.resume is not None and not RESUME_ID_RE.fullmatch(args.resume):
        return _refuse(BAD_RESUME_ID_TEXT)  # the raw value is never echoed
    install_log_filter()
    data_dir = default_data_dir()
    checkpointer = None
    runtime: Runtime | None = None
    try:
        try:
            # Profile first: an unknown user fails before any LLM or BigQuery call (D-90:
            # the merged profiles, so an admin override applies to scope and allowlist).
            profiles = load_profiles_with_overrides(None, data_dir)
            profile = select_profile(profiles, args.user)
            settings = load_settings()  # loads .env, so the AES key may come from there
            register_secret(settings.gemini_api_key)
            register_secret(os.environ.get(AES_KEY_ENV))
            local_startup_check(data_dir=data_dir)
            checkpointer = build_checkpointer(data_dir)  # no valid AES key: refuse (D-78)
        except ConfigError as e:
            return _refuse(str(e))
        session = start_session(profile)
        resume_ok = False
        if args.resume is not None:
            verdict = _resume_precheck(checkpointer, args.resume, profile)
            if verdict == "ok":
                session = Session(args.resume, profile)
                resume_ok = True
            elif verdict == "drift":
                _say(SCOPE_DRIFT_TEXT)  # FR-76: a new session, never a resume
            else:
                return _refuse(verdict)
        try:
            settings = startup_check(lister=lister, dotenv=False)
            _install_pii_detector(profiles.values())
        except (ConfigError, PiiDetectorError) as e:
            return _refuse(str(e))
        log.info("config: %s", safe_config_view(settings))
        factory = agent_factory or build_runtime  # looked up per call (tests patch it)
        try:
            runtime = factory(settings, checkpointer, session, data_dir)
        except ConfigError as e:
            return _refuse(str(e))
        repl = _Repl(runtime, session)
        if resume_ok:
            graph = runtime.graph
            out = repl.guarded(lambda: resume_turn(graph, session.session_id, profile), None)
            if out is not None:
                if out.kind in (ResumeKind.KEY_REFUSED, ResumeKind.STORE_REFUSED):
                    return _refuse(out.text)
                if out.result is not None:
                    repl.show(out.result, out.turn_id)  # /feedback and /trace work at once
                else:
                    _say(out.text)
                if out.start_new_session:  # backstop: resume_turn re-checks owner and scope
                    repl.new_session()
        _say(banner(repl.session))
        _say(HINT_TEXT)
        return repl.loop()
    finally:
        if runtime is not None and runtime.close is not None:
            try:
                runtime.close()
            except Exception as exc:
                log.error("runtime close failed: %s", type(exc).__name__)
        conn = getattr(checkpointer, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:
                log.error("checkpoint close failed: %s", type(exc).__name__)
