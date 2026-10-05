"""Optional Langfuse sink (iteration 40, ADR-002): one Langfuse trace per user turn.

Off unless ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY`` and ``LANGFUSE_HOST`` (or
``LANGFUSE_BASE_URL``) are all set. The agent behaves the same with it on or off.

How it works
* :meth:`LangfuseSink.traced` wraps one turn. While the turn runs, the sink buffers events:
  every span the :class:`~opsfleet_agent.obs.tracer.Tracer` records (through
  ``Tracer.extra_sink``, so the span is already allowlisted, SQL-sanitized and stripped of
  sensitive keys) and every LLM attempt (through :meth:`LangfuseSink.wrap_llm`, wrapped
  around the router and analyst invokes). When the turn ends the buffer becomes one trace.
* Nesting: a ``router`` or ``role`` span adopts the LLM attempts and tool/SQL spans recorded
  since the previous container, so each analyst step shows its own generations and queries.
  Guards, context, the turn summary and LLM calls outside a role (light reply, report writer
  and verifier, forced answer) are direct children of the turn.

What is sent (PII is enforced here, in code, not by Langfuse settings)
* question and answer: NER mask, regex scrub (``pii_regex``), secret scrub, length cap;
* LLM prompts and outputs: the same, per message; tool-result messages are replaced by
  ``[tool result omitted: N chars]`` (they hold query rows); ``run_sql`` call arguments are
  replaced by sanitized SQL (literals become ``?``);
* tool/SQL spans: metadata only (sanitized SQL, rows count, bytes, cache hit), never rows;
* guard spans: verdict and rule codes.
* A ``mask`` function built on the same scrubbers is also given to the Langfuse client, so
  anything that reaches the SDK is scrubbed a second time. ``pending_action``,
  ``__interrupt__``, proofs, keys and tokens are dropped by ``drop_sensitive``.

Fail-open: any Langfuse error is logged once at debug level (type name only) and turns the
sink off for the rest of the process; it never changes or breaks a turn. Flush and shutdown
are bounded by the caller (``cli._bounded``) and by the client ``timeout``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Final

from opsfleet_agent.guards import pii_regex
from opsfleet_agent.obs.tracer import drop_sensitive, sanitize_sql, scrub_text

__all__ = [
    "ENV_HOST",
    "ENV_PUBLIC_KEY",
    "ENV_SECRET_KEY",
    "LangfuseSink",
    "build_sink",
    "is_configured",
    "make_mask",
]

logger = logging.getLogger(__name__)

ENV_PUBLIC_KEY: Final = "LANGFUSE_PUBLIC_KEY"
ENV_SECRET_KEY: Final = "LANGFUSE_SECRET_KEY"
ENV_HOST: Final = "LANGFUSE_HOST"
ENV_BASE_URL: Final = "LANGFUSE_BASE_URL"

CLIENT_TIMEOUT_S: Final = 5  # per export request; the SDK does not retry without bound
FLUSH_INTERVAL_S: Final = 2.0
MAX_TEXT: Final = 4000  # one message, question or answer
MAX_EVENTS: Final = 500  # per turn buffer; extra events are counted, not kept
MAX_TRACE_IDS: Final = 64  # turn_id -> trace id, for /trace
REDACTED: Final = "[redacted]"
CONTAINER_TYPES: Final = frozenset({"router", "role"})
CHILD_TYPES: Final = frozenset({"llm", "tool", "sql"})
_AS_TYPE: Final = {
    "router": "agent",
    "role": "agent",
    "llm": "generation",
    "tool": "tool",
    "sql": "tool",
    "guard": "guardrail",
}
_BOOKKEEPING: Final = frozenset(
    {"trace_id", "span_id", "parent_id", "type", "name", "ts", "session_id", "turn_id"}
)

Factory = Callable[..., Any]
Detector = Any  # guards.pii.PiiDetector (duck-typed: .mask(text).text)


def _host(env: Mapping[str, str]) -> str:
    return (env.get(ENV_HOST) or env.get(ENV_BASE_URL) or "").strip()


def is_configured(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(
        (env.get(ENV_PUBLIC_KEY) or "").strip()
        and (env.get(ENV_SECRET_KEY) or "").strip()
        and _host(env)
    )


# --- scrubbing ------------------------------------------------------------------------------


def _scrub_str(text: str, max_len: int = MAX_TEXT) -> str:
    """Regex PII scrub, then secret/email scrub and the length cap. Fails closed."""
    try:
        text = pii_regex.scrub(text).text
    except Exception:  # noqa: BLE001 - never send what could not be scrubbed
        return REDACTED
    return scrub_text(text, max_len)


def _mask_value(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _scrub_str(value)
    if depth > 8:
        return "[depth]"
    if isinstance(value, Mapping):
        cleaned = drop_sensitive(dict(value), MAX_TEXT)
        return {k: _mask_value(v, depth + 1) for k, v in cleaned.items()}
    if isinstance(value, list | tuple):
        return [_mask_value(v, depth + 1) for v in value]
    return drop_sensitive(value, MAX_TEXT)


def make_mask() -> Callable[..., Any]:
    """The ``mask=`` function for the Langfuse client: a second scrub of everything the SDK
    sends (input, output, metadata), with the same rules as the first."""

    def mask(*, data: Any, **_: Any) -> Any:
        try:
            return _mask_value(data)
        except Exception:  # noqa: BLE001 - fail closed
            return REDACTED

    return mask


class _Cleaner:
    """First-layer scrub: NER (if a detector is given), then regex and secrets."""

    def __init__(self, detector: Detector | None) -> None:
        self.detector = detector

    def text(self, value: Any, max_len: int = MAX_TEXT) -> str:
        if not isinstance(value, str):
            value = "" if value is None else str(value)
        if not value:
            return ""
        if self.detector is not None:
            try:
                value = self.detector.mask(value).text
            except Exception:  # noqa: BLE001 - NER failed: fail closed
                return REDACTED
        return _scrub_str(value, max_len)

    def tool_call(self, name: Any, args: Any, call_id: Any = None) -> dict[str, Any]:
        out: dict[str, Any] = {"name": scrub_text(str(name), 100)}
        if call_id:
            out["id"] = scrub_text(str(call_id), 100)
        if str(name) == "run_sql" and isinstance(args, Mapping):
            sql = args.get("sql")
            text, digest = sanitize_sql(sql) if isinstance(sql, str) else (None, "")
            rest = {k: v for k, v in args.items() if k != "sql"}
            out["args"] = {**_mask_value(drop_sensitive(rest)), "sql": text, "sql_hash": digest}
        else:
            out["args"] = _mask_value(drop_sensitive(args)) if args is not None else None
        return out

    def message(self, msg: Any) -> dict[str, Any]:
        if isinstance(msg, Mapping):
            role, content = msg.get("role"), msg.get("content")
            calls = msg.get("tool_calls") or ()
        else:
            role, content = getattr(msg, "role", None), getattr(msg, "content", None)
            calls = getattr(msg, "tool_calls", None) or ()
        role = scrub_text(str(role or "unknown"), 40)
        out: dict[str, Any] = {"role": role}
        if role in ("tool", "function"):  # query results and schema payloads: never sent
            size = len(content) if isinstance(content, str) else len(str(content or ""))
            out["content"] = f"[tool result omitted: {size} chars]"
            return out
        out["content"] = self.text(content)
        if calls:
            out["tool_calls"] = [self._call(c) for c in list(calls)[:20]]
        return out

    def _call(self, call: Any) -> dict[str, Any]:
        if isinstance(call, Mapping):
            fn = call.get("function") if isinstance(call.get("function"), Mapping) else call
            return self.tool_call(
                fn.get("name"), fn.get("args", fn.get("arguments")), call.get("id")
            )
        return self.tool_call(getattr(call, "name", "?"), getattr(call, "args", None),
                              getattr(call, "id", None))  # fmt: skip

    def messages(self, msgs: Any) -> list[dict[str, Any]]:
        try:
            return [self.message(m) for m in list(msgs)[:60]]
        except Exception:  # noqa: BLE001
            return [{"role": "unknown", "content": REDACTED}]

    def output(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        text = getattr(value, "text", None)
        calls = getattr(value, "tool_calls", None)
        if text is None and calls is None:
            return f"<{type(value).__name__}>"
        return {
            "text": self.text(text or ""),
            "tool_calls": [self._call(c) for c in list(calls or ())[:20]],
        }


# --- the sink -------------------------------------------------------------------------------


class _Turn:
    def __init__(self, turn_id: str | None, session_id: str, user_id: str, question: str) -> None:
        self.turn_id = turn_id
        self.session_id = session_id
        self.user_id = user_id
        self.question = question
        self.start_ns = time.time_ns()
        self.events: list[dict[str, Any]] = []
        self.dropped = 0


class LangfuseSink:
    """Buffers one turn's events and exports them as one Langfuse trace. Thread-safe."""

    def __init__(
        self,
        client: Any,
        *,
        host: str = "",
        provider: str = "gemini",
        detector: Detector | None = None,
        propagate: Callable[..., AbstractContextManager[Any]] | None = None,
    ) -> None:
        self.client = client
        self.host = host.rstrip("/")
        self.provider = provider
        self.clean = _Cleaner(detector)
        self._propagate = propagate
        self._lock = threading.Lock()
        self._turn: _Turn | None = None
        self._trace_ids: OrderedDict[str, str] = OrderedDict()
        self.last_trace_id: str | None = None
        self.enabled = True
        self._warned = False

    # -- failure handling

    def _fail(self, exc: BaseException) -> None:
        self.enabled = False
        if not self._warned:
            self._warned = True
            logger.debug("langfuse sink disabled after %s", type(exc).__name__)

    # -- inputs

    def on_span(self, clean: dict[str, Any]) -> None:
        """``Tracer.extra_sink``: buffer an already cleaned span if a turn is running."""
        if not self.enabled:
            return
        end = time.time_ns()
        dur = clean.get("duration_ms")
        start = end - int(dur * 1e6) if isinstance(dur, int | float) and dur > 0 else end
        self._add({"kind": str(clean.get("type") or "span"), "start": start, "end": end,
                   "span": dict(clean)})  # fmt: skip

    def _add(self, event: dict[str, Any]) -> None:
        with self._lock:
            turn = self._turn
            if turn is None:
                return
            if len(turn.events) >= MAX_EVENTS:
                turn.dropped += 1
                return
            turn.events.append(event)

    def wrap_llm(self, name: str, invoke: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a router or analyst invoke: same signature, same result, same exceptions.
        Records each call (one attempt of the LLM wrapper) as a generation."""

        def traced_invoke(*args: Any, **kwargs: Any) -> Any:
            if not self.enabled or self._turn is None:
                return invoke(*args, **kwargs)
            start = time.time_ns()
            result: Any = None
            error: BaseException | None = None
            try:
                result = invoke(*args, **kwargs)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                try:
                    self._add(self._generation(name, args, kwargs, result, error, start))
                except Exception as exc:  # noqa: BLE001 - never break the call
                    self._fail(exc)

        return traced_invoke

    def _generation(
        self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any], result: Any,
        error: BaseException | None, start: int,
    ) -> dict[str, Any]:  # fmt: skip
        model = kwargs.get("model", args[0] if args else None)
        messages = kwargs.get("messages", args[1] if len(args) > 1 else ())
        tools = kwargs.get("tools", args[2] if len(args) > 2 and name == "llm.tools" else ())
        event: dict[str, Any] = {
            "kind": "llm",
            "name": name,
            "start": start,
            "end": time.time_ns(),
            "model": scrub_text(str(model or ""), 100),
            "input": self.clean.messages(messages),
            "tools": [scrub_text(str(getattr(t, "name", t)), 60) for t in list(tools or ())[:30]],
        }
        if error is not None:
            event["error_class"] = type(error).__name__
        else:
            event["output"] = self.clean.output(getattr(result, "value", None))
            event["usage"] = {
                "input": int(getattr(result, "tokens_in", 0) or 0),
                "output": int(getattr(result, "tokens_out", 0) or 0),
            }
        return event

    # -- the turn

    def traced(
        self,
        fn: Callable[[], Any],
        *,
        session_id: str,
        user_id: str,
        turn_id: str | None,
        question: str,
    ) -> Callable[[], Any]:
        """Return ``fn`` wrapped so that its run becomes one trace. Result and exceptions
        (including ``KeyboardInterrupt``) pass through unchanged."""

        def run() -> Any:
            if not self.enabled:
                return fn()
            with self._lock:
                self._turn = _Turn(turn_id, session_id, user_id, question)
            result: Any = None
            outcome = "error"
            try:
                result = fn()
                outcome = str(getattr(result, "outcome", "") or "answered")
                return result
            except KeyboardInterrupt:
                outcome = "cancelled"
                raise
            finally:
                with self._lock:
                    turn, self._turn = self._turn, None
                if turn is not None and self.enabled:
                    try:
                        self._emit(turn, result, outcome)
                    except Exception as exc:  # noqa: BLE001 - fail open
                        self._fail(exc)

        return run

    def trace_id_for(self, turn_id: str | None) -> str | None:
        if turn_id is None:
            return None
        with self._lock:
            return self._trace_ids.get(turn_id)

    def trace_url(self, trace_id: str) -> str:
        return f"{self.host}/trace/{trace_id}" if self.host else ""

    # -- export

    def _emit(self, turn: _Turn, result: Any, outcome: str) -> None:
        end = time.time_ns()
        answer = self.clean.text(getattr(result, "text", "") or "") if result is not None else ""
        label = str(getattr(result, "label", "") or "")
        route = str(getattr(result, "route", "") or "")
        metadata = {
            "turn_id": turn.turn_id or "",
            "outcome": outcome,
            "label": label,
            "route": route,
            "provider": self.provider,
        }
        propagate = self._propagate or _default_propagate()
        with propagate(
            session_id=turn.session_id,
            user_id=turn.user_id,
            trace_name="turn",
            metadata={k: scrub_text(v, 200) for k, v in metadata.items()},
        ):
            root = self._start(None, "turn", "agent", turn.start_ns,
                               input=self.clean.text(turn.question))  # fmt: skip
            for parent_event, children in _nest(turn.events):
                if parent_event is None:
                    for child in children:
                        self._child(root, child, None)
                    continue
                container = self._child(root, parent_event, children)
                for i, child in enumerate(children, 1):
                    self._child(container, child, i)
            root.update(
                output=answer,
                metadata={**metadata, "dropped_events": turn.dropped},
                **({"level": "ERROR", "status_message": outcome} if outcome == "error" else {}),
            )
            root.end(end_time=end)
        trace_id = getattr(root, "trace_id", None)
        if trace_id:
            with self._lock:
                self.last_trace_id = trace_id
                if turn.turn_id:
                    self._trace_ids[turn.turn_id] = trace_id
                    while len(self._trace_ids) > MAX_TRACE_IDS:
                        self._trace_ids.popitem(last=False)

    def _start(self, parent: Any, name: str, as_type: str, start_ns: int, **kw: Any) -> Any:
        """Create an observation with its real start time when the SDK allows it (private
        OTel path), else through the public API (start time = now)."""
        try:
            from opentelemetry import trace as otel_trace

            tracer = self.client._otel_tracer
            ctx = otel_trace.set_span_in_context(parent._otel_span) if parent is not None else None
            span = tracer.start_span(name=name, start_time=start_ns, context=ctx)
            return self.client._create_observation_from_otel_span(
                otel_span=span, as_type=as_type, **kw
            )
        except (AttributeError, TypeError, ImportError):
            owner = self.client if parent is None else parent
            return owner.start_observation(name=name, as_type=as_type, **kw)

    def _child(self, parent: Any, event: dict[str, Any], attempt: int | None) -> Any:
        kind = event["kind"]
        if kind == "llm":
            meta = {"provider": self.provider, "tools": event.get("tools") or [],
                    "latency_ms": (event["end"] - event["start"]) // 1_000_000}  # fmt: skip
            if attempt is not None:
                meta["attempt"] = attempt
            kw: dict[str, Any] = {"model": event.get("model") or None,
                                  "input": event.get("input"), "metadata": meta}  # fmt: skip
            if "error_class" in event:
                meta["status"] = "error"
                meta["error_class"] = event["error_class"]
                kw.update(level="ERROR", status_message=event["error_class"])
            else:
                meta["status"] = "ok"
                kw.update(output=event.get("output"), usage_details=event.get("usage"))
            obs = self._start(parent, event["name"], "generation", event["start"], **kw)
            obs.end(end_time=event["end"])
            return obs
        span = event["span"]
        fields = {k: v for k, v in span.items() if k not in _BOOKKEEPING and v is not None}
        name = f"{kind}:{span.get('name') or kind}" if kind != "turn" else "turn_summary"
        kw = {"metadata": fields}
        if kind == "guard":
            kw["output"] = {k: fields[k] for k in
                            ("verdict", "rule", "rule_hits", "grounding_flags", "redaction_count")
                            if k in fields}  # fmt: skip
        if span.get("status") == "error" or kind == "error":
            kw.update(level="ERROR", status_message=str(span.get("error_class") or "error"))
        obs = self._start(parent, name, _AS_TYPE.get(kind, "span"), event["start"], **kw)
        obs.end(end_time=event["end"])
        return obs

    def shutdown(self) -> None:
        """Flush and close the client. Call it through a bounded runner (``cli._bounded``)."""
        for step in ("flush", "shutdown"):
            try:
                getattr(self.client, step)()
            except Exception as exc:  # noqa: BLE001 - fail open
                self._fail(exc)
                return


def _nest(events: Sequence[dict[str, Any]]) -> Iterator[tuple[dict[str, Any] | None, list]]:
    """Group events: a container (router/role span) adopts the child events (LLM, tool, SQL)
    recorded since the previous container or root-level span; the rest attach to the root.
    The container's start becomes its earliest child's start."""
    pending: list[dict[str, Any]] = []
    for ev in events:
        kind = ev["kind"]
        if kind in CHILD_TYPES:
            pending.append(ev)
        elif kind in CONTAINER_TYPES:
            if pending:
                ev = {**ev, "start": min(ev["start"], *(c["start"] for c in pending))}
            yield ev, pending
            pending = []
        else:
            if pending:
                yield None, pending
                pending = []
            yield None, [ev]
    if pending:
        yield None, pending


def _default_propagate() -> Callable[..., AbstractContextManager[Any]]:
    try:
        from langfuse import propagate_attributes

        return propagate_attributes
    except Exception:  # noqa: BLE001
        return lambda **_: nullcontext()


def _default_factory(**kwargs: Any) -> Any:
    from langfuse import Langfuse

    return Langfuse(**kwargs)


def build_sink(
    env: Mapping[str, str] | None = None,
    *,
    factory: Factory | None = None,
    detector: Detector | None = None,
    propagate: Callable[..., AbstractContextManager[Any]] | None = None,
) -> LangfuseSink | None:
    """The sink, or None when Langfuse is not configured or the client cannot be built."""
    env = os.environ if env is None else env
    if not is_configured(env):
        return None
    host = _host(env)
    try:
        client = (factory or _default_factory)(
            public_key=env[ENV_PUBLIC_KEY].strip(),
            secret_key=env[ENV_SECRET_KEY].strip(),
            base_url=host,
            timeout=CLIENT_TIMEOUT_S,
            flush_interval=FLUSH_INTERVAL_S,
            mask=make_mask(),
        )
    except Exception as exc:  # noqa: BLE001 - fail open: run without Langfuse
        logger.debug("langfuse sink not started: %s", type(exc).__name__)
        return None
    provider = (env.get("OPSFLEET_LLM_PROVIDER") or "gemini").strip().lower() or "gemini"
    return LangfuseSink(client, host=host, provider=provider, detector=detector,
                        propagate=propagate)  # fmt: skip
