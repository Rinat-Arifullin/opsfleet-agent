"""Live system under test for the eval runner (iteration 40b).

    uv run python evals/run.py --sut evals.live_sut:live_harness --cases-dir evals/cases/golden

Builds the real runtime the same way ``opsfleet_agent.cli`` does (same startup steps, the
same :func:`~opsfleet_agent.cli.build_runtime`), then runs each case's turns in a fresh
session. When ``LANGFUSE_*`` are set, every turn is one Langfuse trace, exactly as in the CLI.

Bounds: at most :data:`MAX_TURNS_PER_CASE` turns per case and a per-case wall-clock timeout
(``OPSFLEET_EVAL_CASE_TIMEOUT_S``, default :data:`DEFAULT_CASE_TIMEOUT_S`). A timed-out case
cancels its BigQuery job and its runtime is never reused.

State lives in a dedicated data dir (``OPSFLEET_EVAL_DATA_DIR``, default
``<OPSFLEET_DATA_DIR or data>/eval-live``), so eval sessions, reports and quota never mix
with the user's own store (OD-1 in docs/process/iter40b-ods.md). Inside it each case runs
as a namespaced user (``<id>.ev<tag>``) with its ``session:`` seeds (saved reports, persona,
setup turns) applied through the real APIs: see :mod:`evals.live_seed`.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from evals import live_seed
from evals.run import Case, CaseError, Harness, RunContext, SutResult

log = logging.getLogger(__name__)

MAX_TURNS_PER_CASE: Final = 8
MAX_FULL_SQL: Final = 50  # executed statements kept in memory per case
MAX_FULL_SQL_CHARS: Final = 20_000
DEFAULT_CASE_TIMEOUT_S: Final = 600.0  # a local model is slow; Gemini needs far less
MAX_CASE_TIMEOUT_S: Final = 3600.0
FLUSH_BOUND_S: Final = 5.0
ENV_DATA_DIR: Final = "OPSFLEET_EVAL_DATA_DIR"
ENV_CASE_TIMEOUT: Final = "OPSFLEET_EVAL_CASE_TIMEOUT_S"
UNATTRIBUTED: Final = "unattributed"  # LLM calls the trace does not name a model for
# Seeds applied live (evals/live_seed.py); `preferences` is not (iteration 39 has no store).
SUPPORTED_SESSION_KEYS: Final = live_seed.SUPPORTED_SESSION_KEYS


def eval_data_dir(env: Mapping[str, str] | None = None) -> Path:
    from opsfleet_agent.session import default_data_dir

    env = os.environ if env is None else env
    raw = env.get(ENV_DATA_DIR)
    return Path(raw) if raw else default_data_dir() / "eval-live"


def case_timeout_s(env: Mapping[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    try:
        value = float(env.get(ENV_CASE_TIMEOUT) or DEFAULT_CASE_TIMEOUT_S)
    except ValueError:
        value = DEFAULT_CASE_TIMEOUT_S
    return min(max(value, 1.0), MAX_CASE_TIMEOUT_S)


@dataclass
class Bootstrap:
    """What the CLI prepares once per process before it builds a runtime."""

    profiles: Mapping[str, Any]
    settings: Any
    checkpointer: Any


def cli_bootstrap(data_dir: Path) -> Bootstrap:  # pragma: no cover - needs .env and models
    """The CLI's startup sequence (``cli._main``) without the REPL and ``--resume``."""
    from opsfleet_agent.cli import _install_pii_detector, register_runtime_secrets
    from opsfleet_agent.commands.access import load_profiles_with_overrides
    from opsfleet_agent.config import load_settings, startup_check
    from opsfleet_agent.graph.graph import build_checkpointer
    from opsfleet_agent.obs.tracer import install_log_filter
    from opsfleet_agent.session import local_startup_check

    install_log_filter()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    profiles = load_profiles_with_overrides(None, data_dir)
    settings = load_settings()  # loads .env (never read or printed here)
    register_runtime_secrets(settings)
    local_startup_check(data_dir=data_dir)
    checkpointer = build_checkpointer(data_dir)
    settings = startup_check(lister=None, dotenv=False)
    _install_pii_detector(profiles.values())
    return Bootstrap(profiles, settings, checkpointer)


def _default_factory(settings: Any, checkpointer: Any, session: Any, data_dir: Path) -> Any:
    from opsfleet_agent.cli import build_runtime

    return build_runtime(settings, checkpointer, session, data_dir)  # pragma: no cover


@dataclass
class _Entry:
    runtime: Any
    detector: Any = None


@dataclass
class LiveSut:
    """Callable ``(case, ctx) -> SutResult``; one runtime per profile, one session per case."""

    data_dir: Path = field(default_factory=eval_data_dir)
    timeout_s: float = field(default_factory=case_timeout_s)
    bootstrap: Callable[[Path], Bootstrap] = cli_bootstrap
    factory: Callable[[Any, Any, Any, Path], Any] = _default_factory
    # Langfuse trace ids of the last case's turns, in order (empty without Langfuse).
    last_trace_ids: list[str] = field(default_factory=list)
    _boot: Bootstrap | None = field(default=None, repr=False)
    _runtimes: dict[str, _Entry] = field(default_factory=dict, repr=False)
    _retired: list[Any] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _executor: ThreadPoolExecutor | None = field(default=None, repr=False)

    # -- setup

    def _bootstrapped(self) -> Bootstrap:
        if self._boot is None:
            self._boot = self.bootstrap(self.data_dir)
        return self._boot

    def _entry(self, profile: Any) -> _Entry:
        from opsfleet_agent.guards.pii import default_detector, set_default_detector
        from opsfleet_agent.session import start_session

        entry = self._runtimes.get(profile.user_id)
        if entry is None:
            boot = self._bootstrapped()
            runtime = self.factory(boot.settings, boot.checkpointer, start_session(profile),
                                   self.data_dir)  # fmt: skip
            # build_runtime sets the process-wide detector with this profile's brands.
            entry = _Entry(runtime, _safe(default_detector))
            self._runtimes[profile.user_id] = entry
        elif entry.detector is not None:
            set_default_detector(entry.detector)  # another profile's runtime may have run
        return entry

    def _profile(self, case: Case) -> Any:
        from opsfleet_agent.config import ConfigError
        from opsfleet_agent.session import select_profile

        live_seed.check_session(case)
        try:
            return select_profile(self._bootstrapped().profiles, case.session.get("profile"))
        except ConfigError as exc:
            raise CaseError(f"profile: {exc}") from None

    # -- one case

    def _owner(self) -> ThreadPoolExecutor:
        # The stores use sqlite connections bound to the thread that opened them
        # (store.db: check_same_thread=True), so bootstrap, runtimes, turns and close all
        # run on this one thread.
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eval-live")
        return self._executor

    def __call__(self, case: Case, ctx: RunContext) -> SutResult:
        n_turns = len(case.turns) + len(live_seed.setup_turns(case))
        if n_turns > MAX_TURNS_PER_CASE:
            raise CaseError(f"{n_turns} turns > cap {MAX_TURNS_PER_CASE}")
        self.last_trace_ids = []
        box: dict[str, Any] = {}
        future = self._owner().submit(self._run_case, case, ctx, box)
        try:
            results, ids, trace_path = future.result(timeout=self.timeout_s)
        except FutureTimeout:
            self._abandon(box.get("runtime"))
            raise CaseError(f"timed out after {self.timeout_s:.0f}s") from None
        self.last_trace_ids = [t for t in ids if t]
        return to_sut_result(list(results), trace_path, self.last_trace_ids,
                             full_sql=box.get("full_sql"))  # fmt: skip

    def _run_case(self, case: Case, ctx: RunContext, box: dict[str, Any]) -> tuple:
        from opsfleet_agent.session import Session

        profile = self._profile(case)
        setup = live_seed.setup_turns(case)
        runtime = self._entry(profile).runtime  # one runtime per base profile
        box["runtime"] = runtime
        # A fresh id per run: the checkpointer never resumes an earlier run of this case.
        sid = f"{ctx.session_id}-{uuid.uuid4().hex[:6]}"
        tracer = getattr(runtime, "tracer", None)
        with live_seed.seeded(runtime, case, profile, session_id=sid,
                              data_dir=self.data_dir) as seed:  # fmt: skip
            session = Session(sid, seed.profile)  # namespaced user: isolated rows and quota
            if tracer is not None:
                tracer.trace_dir = Path(ctx.trace_dir)
                # setup turns trace to their own file, so their spans are not scored
                tracer.session_id = f"setup-{uuid.uuid4().hex[:12]}"
            for text in setup:
                self._turn(runtime, session, text, seed.base_user_id)
            if tracer is not None:
                tracer.session_id = session.session_id
            with _full_sql_capture(tracer) as full_sql:
                pairs = [self._turn(runtime, session, t, seed.base_user_id) for t in case.turns]
        box["full_sql"] = full_sql
        results, ids = zip(*pairs, strict=True)
        return results, ids, (tracer.path if tracer is not None else None)

    def _abandon(self, runtime: Any) -> None:
        """A turn is stuck on the owner thread: cancel its query and retire that thread with
        everything it owns (bootstrap, runtimes). The next case starts from scratch."""
        _step(getattr(runtime, "cancel", None), "bigquery cancel")
        with self._lock:
            self._retired.extend(e.runtime for e in self._runtimes.values())
            self._runtimes.clear()
            self._boot = None
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def _turn(self, runtime: Any, session: Any, text: str,
              user_id: str | None = None) -> tuple[Any, str | None]:  # fmt: skip
        turn_id = uuid.uuid4().hex[:12]

        def run() -> Any:
            return runtime.graph.run_turn(text, session=session, turn_id=turn_id)

        sink = getattr(runtime, "langfuse", None)
        if sink is None:
            return run(), None
        traced = sink.traced(
            run,
            session_id=session.session_id,
            user_id=user_id or session.profile.user_id,  # Langfuse: the base profile id
            turn_id=turn_id,
            question=text,
        )
        result = traced()
        return result, sink.trace_id_for(turn_id)

    # -- shutdown

    def sinks(self) -> list[Any]:
        seen: list[Any] = []
        for rt in [e.runtime for e in self._runtimes.values()] + self._retired:
            sink = getattr(rt, "langfuse", None)
            if sink is not None and all(sink is not s for s in seen):
                seen.append(sink)
        return seen

    def flush(self) -> None:
        """Send buffered Langfuse data now, each sink bounded by FLUSH_BOUND_S."""
        for sink in self.sinks():
            client = getattr(sink, "client", None)
            if client is not None:
                _bounded(client.flush, FLUSH_BOUND_S, "langfuse flush")

    def close(self) -> None:
        for sink in self.sinks():
            _bounded(sink.shutdown, FLUSH_BOUND_S, "langfuse shutdown")
        executor, self._executor = self._executor, None
        if executor is not None:  # sqlite handles close on the thread that opened them
            future = executor.submit(self._close_owned)
            try:
                future.result(timeout=FLUSH_BOUND_S)
            except FutureTimeout:
                log.warning("runtime close still running after %.1fs; continuing", FLUSH_BOUND_S)
            executor.shutdown(wait=False, cancel_futures=True)
        self._runtimes.clear()
        self._boot = None

    def _close_owned(self) -> None:
        for rt in [e.runtime for e in self._runtimes.values()]:
            _step(getattr(rt, "close", None), "runtime close")
        conn = getattr(self._boot.checkpointer, "conn", None) if self._boot else None
        _step(getattr(conn, "close", None), "checkpoint close")


@contextmanager
def _full_sql_capture(tracer: Any) -> Iterator[list[str]]:
    """Keep, in memory only, the literal-free text of each executed statement of the turns.

    The tracer bounds every string (``MAX_STR``), so a long scoped statement reaches the trace
    as ``...[truncated]`` and the ``scope:sql`` check cannot parse it. This wraps the tracer's
    ``record`` for the case: it sees the same text the tracer sanitizes (``sanitize_sql``,
    literals replaced with ``?``, secrets scrubbed), keeps it here and writes nothing new."""
    from opsfleet_agent.obs.tracer import sanitize_sql, scrub_text

    captured: list[str] = []
    original = getattr(tracer, "record", None)
    if original is None:
        yield captured
        return

    def record(span_type: str, name: str | None = None, **fields: Any) -> Any:
        text = fields.get("sql_text")
        if span_type == "sql" and fields.get("status") == "ok" and isinstance(text, str):
            if len(captured) < MAX_FULL_SQL:
                clean = sanitize_sql(text)[0]
                captured.append(scrub_text(clean, MAX_FULL_SQL_CHARS) if clean else "")
        return original(span_type, name, **fields)

    tracer.record = record
    try:
        yield captured
    finally:
        del tracer.record  # back to the class method


def to_sut_result(
    results: list[Any],
    trace_path: Path | None,
    trace_ids: list[str],
    *,
    full_sql: list[str] | None = None,
) -> SutResult:
    """Map the turn results plus the local JSONL trace to the scorers' SutResult.

    ``full_sql`` (from :func:`_full_sql_capture`) replaces the traced statement texts when it
    lines up with the trace's executed statements one for one."""
    last = results[-1]
    spans = list(read_spans(trace_path)) if trace_path is not None else []
    llm: dict[str, int] = {}
    for s in spans:
        if s.get("type") == "llm":
            model = str(s.get("model") or UNATTRIBUTED)
            llm[model] = llm.get(model, 0) + 1
    total = sum(int(getattr(r, "llm_calls", 0) or 0) for r in results)
    missing = total - sum(llm.values())
    if missing > 0:
        llm[UNATTRIBUTED] = llm.get(UNATTRIBUTED, 0) + missing
    ran = [s for s in spans if s.get("type") == "sql" and s.get("status") == "ok"]
    tools = [str(s.get("tool") or s.get("name")) for s in spans if s.get("type") == "tool"]
    return SutResult(
        outcome=getattr(last, "outcome", None),
        text=str(getattr(last, "text", "") or ""),
        label=getattr(last, "label", None),
        sql=_statements(ran, full_sql),
        tools=tools,
        llm_calls=llm,
        bq_queries=max(len(ran), sum(int(getattr(r, "sql_queries", 0) or 0) for r in results)),
        bq_bytes=sum(int(s.get("bytes_billed") or 0) for s in ran),
        trace_id=None,  # run.py keeps the session id; Langfuse ids are in LiveSut.last_trace_ids
        trace_path=str(trace_path) if trace_path is not None else None,
    )


def read_spans(path: Path, max_lines: int = 100_000) -> Iterable[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                try:
                    span = json.loads(line)
                except ValueError:
                    continue
                if isinstance(span, dict):
                    yield span
    except OSError:
        return


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - optional: keep the detector the runtime already set
        return None


def _step(fn: Callable[[], object] | None, what: str) -> None:
    if fn is None:
        return
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - best effort cleanup
        log.error("%s failed: %s", what, type(exc).__name__)


def _bounded(fn: Callable[[], object], timeout_s: float, what: str) -> None:
    worker = threading.Thread(target=_step, args=(fn, what), daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        log.warning("%s still running after %.1fs; continuing", what, timeout_s)


def live_harness() -> Harness:
    """Factory for ``evals/run.py --sut evals.live_sut:live_harness``.

    No LLM judge: the configured judge is a hosted model and an uncalibrated judge counts
    for nothing anyway (OD-5)."""
    sut = LiveSut()
    atexit.register(sut.close)
    return Harness(sut=sut)


def _statements(ran: list[dict[str, Any]], full_sql: list[str] | None) -> list[str]:
    traced = [str(s.get("sql_text") or s.get("sql_hash") or "") for s in ran]
    if not full_sql or len(full_sql) != len(traced):
        return traced
    return [full or t for full, t in zip(full_sql, traced, strict=True)]
