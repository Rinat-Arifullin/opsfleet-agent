"""JSONL tracer with allowlisted span fields and one shared redaction function.

HLD §9.1. Every sink (JSONL writer, logging filter, error helper) goes through
`drop_sensitive()`, so a secret or dropped key is handled in exactly one place.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import sqlglot
from sqlglot import exp

log = logging.getLogger(__name__)

SPAN_TYPES = (
    "turn", "router", "role", "llm", "tool", "guard", "sql", "delete", "error", "context",
)  # fmt: skip

DROPPED = "[dropped]"
SECRET = "[secret]"
MAX_STR = 500
MAX_DEPTH = 6
MAX_ITEMS = 50

# Dropped by key (HLD §9.1), before any pattern scrub. Matching is case-insensitive:
# exact for the structural keys, substring for anything that smells like a secret.
DROP_KEYS_EXACT = frozenset({"pending_action", "__interrupt__"})
DROP_KEY_SUBSTRINGS = ("key", "token", "secret", "password", "auth", "proof", "credential")
# Allowlisted metric / structural names that merely contain a secret-like substring.
SAFE_KEYS = frozenset(
    {"tokens_in", "tokens_out", "tokens_in_total", "tokens_out_total", "args_keys"}
)


def is_sensitive_key(key: str) -> bool:
    k = key.strip().lower()
    if k in SAFE_KEYS:
        return False
    return k in DROP_KEYS_EXACT or any(sub in k for sub in DROP_KEY_SUBSTRINGS)


_COMMON = (
    "trace_id",
    "span_id",
    "parent_id",
    "type",
    "name",
    "ts",
    "duration_ms",
    "status",
    "session_id",
    "turn_id",
    "error_class",
)
SPAN_FIELDS: dict[str, tuple[str, ...]] = {
    "turn": (
        "outcome",
        "path",
        "label",
        "resumed",
        "tokens_in_total",
        "tokens_out_total",
        "bytes_billed_total",
        "llm_calls_total",
        "sql_queries_total",
        "cache_hits",
        "persona_version",
    ),
    "router": ("label", "route", "escalated", "escalation_reason", "model"),
    "role": ("agent", "model", "prompt_version", "retries", "llm_calls", "sql"),
    "llm": (
        "model",
        "tokens_in",
        "tokens_out",
        "retries",
        "fallback_used",
        "limiter_wait_ms",
        "tool_calls",
    ),
    "tool": (
        "tool", "outcome", "error_code", "args_keys", "rows", "truncated",
        "search_path",  # iteration 37: ranked | substring | substring_fallback
        # iteration 38: + hybrid | hybrid_substring; the embedding was unavailable (degraded)
        "semantic_unavailable",
    ),
    "guard": ("rule_hits", "verdict", "rule", "redaction_count", "grounding_flags"),
    "sql": (
        "purpose",
        "sql_text",
        "sql_hash",
        "policy_verdict",
        "rule",
        "dry_run_bytes",
        "bytes_billed",
        "rows",
        "truncated",
        "suppressed_groups",
        "cache_hit",
    ),
    "delete": ("event", "pending_action_id", "count", "audit_event_id"),
    "error": ("code", "message"),
    # Iteration 15 (load_context, HLD line 1737): counts and versions only, never content.
    "context": ("history_turns", "context_dropped", "persona_version", "prefs_version", "golden"),
}
# Only when capture_llm_text is on (dev / eval runs).
LLM_TEXT_FIELDS = ("prompt_redacted", "completion_redacted")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Unregistered credential shapes: Google API keys, OAuth access tokens, bearer tokens,
# and key/token query or assignment parameters.
_API_KEY_SHAPE = re.compile(r"AIza[0-9A-Za-z_-]{35}")
_OAUTH_TOKEN = re.compile(r"ya29\.[0-9A-Za-z_.~+/=-]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_KV_PARAM = re.compile(
    r"(?i)\b((?:[a-z0-9_-]*(?:key|token|secret|password|passwd|signature))=)[^&\s\"',;]+"
)
_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a runtime secret value; it is replaced wherever it appears in a string."""
    if value and len(value) >= 4:
        _secrets.add(value)


def forget_secret(value: str) -> None:
    """Stop scrubbing one registered value (it no longer exists, e.g. a closed delete's
    proof), so the scrubber set stays bounded. Unknown values are ignored."""
    _secrets.discard(value)


def clear_secrets() -> None:
    _secrets.clear()


def scrub_text(text: str, max_len: int = MAX_STR) -> str:
    for s in sorted(_secrets, key=len, reverse=True):
        for form in {s, quote(s, safe=""), quote_plus(s), quote(s)}:
            text = text.replace(form, SECRET)
    text = _API_KEY_SHAPE.sub(SECRET, text)
    text = _OAUTH_TOKEN.sub(SECRET, text)
    text = _BEARER.sub(f"Bearer {SECRET}", text)
    text = _KV_PARAM.sub(lambda m: m.group(1) + SECRET, text)
    text = _EMAIL.sub("[email]", text)
    if len(text) > max_len:
        text = text[:max_len] + "...[truncated]"
    return text


_LITERAL_TYPES = tuple(
    getattr(exp, n)
    for n in ("Literal", "ByteString", "HexString", "BitString", "RawString", "National")
    if hasattr(exp, n)
)


def sanitize_sql(sql: str) -> tuple[str | None, str]:
    """Replace every string/number literal with `?`. Returns (text_or_None, hash).

    Fails closed: if the SQL does not parse, the text is dropped and only the hash is kept.
    """
    digest = hashlib.sha256(sql.encode("utf-8", "replace")).hexdigest()[:16]
    try:
        trees = [t for t in sqlglot.parse(sql, read="bigquery") if t is not None]
        parts = []
        for tree in trees:
            tree = tree.transform(
                lambda n: exp.Placeholder() if isinstance(n, _LITERAL_TYPES) else n
            )
            parts.append(tree.sql(dialect="bigquery", comments=False))
        if not parts:
            return None, digest
        return "; ".join(parts), digest
    except Exception:  # noqa: BLE001 - any failure means drop the text
        return None, digest


def drop_sensitive(obj: Any, max_len: int = MAX_STR, _depth: int = 0) -> Any:
    """Drop secret-bearing keys, scrub strings, bound sizes. Used by every sink."""
    if isinstance(obj, str):
        return scrub_text(obj, max_len)
    if isinstance(obj, bool | int | float) or obj is None:
        return obj
    if _depth >= MAX_DEPTH:
        return "[depth]"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= MAX_ITEMS:
                break
            ks = scrub_text(str(k), 100)
            if is_sensitive_key(str(k)):
                out[ks] = DROPPED
            else:
                out[ks] = drop_sensitive(v, max_len, _depth + 1)
        return out
    if isinstance(obj, list | tuple | set | frozenset):
        return [drop_sensitive(v, max_len, _depth + 1) for v in list(obj)[:MAX_ITEMS]]
    return scrub_text(f"<{type(obj).__name__}>", max_len)


def format_error(exc: BaseException) -> str:
    """Error string safe for users, logs and traces: class + scrubbed message, no traceback."""
    return f"{type(exc).__name__}: {scrub_text(str(exc), 300)}"


def _redact_record(record: logging.LogRecord) -> None:
    try:
        msg = record.getMessage()
    except Exception:  # noqa: BLE001
        msg = str(record.msg)
    msg = scrub_text(msg, 2000)
    record.args = None
    if record.exc_info and record.exc_info[1] is not None:
        msg = f"{msg} | {format_error(record.exc_info[1])}"
    record.msg = msg
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None


class RedactingFilter(logging.Filter):
    """Logging filter: formats the record, scrubs it, drops exception tracebacks.

    Note: a filter only covers the logger/handler it is attached to. `install_log_filter`
    uses the record factory instead, which covers every logger and handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


_installed_factory: Any = None


def install_log_filter() -> None:
    """Redact every log record at creation (all loggers, all handlers, exception text).

    Idempotent. Wraps the current record factory.
    """
    global _installed_factory
    current = logging.getLogRecordFactory()
    if current is _installed_factory:
        return
    inner = current

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = inner(*args, **kwargs)
        _redact_record(record)
        return record

    _installed_factory = factory
    logging.setLogRecordFactory(factory)


def uninstall_log_filter() -> None:
    """Test helper: restore the default record factory."""
    global _installed_factory
    logging.setLogRecordFactory(logging.LogRecord)
    _installed_factory = None


class Tracer:
    """Writes one JSON line per span to `<trace_dir>/<session_id>.jsonl`."""

    def __init__(
        self,
        trace_dir: str | Path = "traces",
        session_id: str | None = None,
        capture_llm_text: bool = False,
        max_str: int = MAX_STR,
    ) -> None:
        self.trace_dir = Path(trace_dir)
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.capture_llm_text = capture_llm_text
        self.max_str = max_str
        # Optional second sink (iteration 40: Langfuse). It receives the already cleaned span
        # (allowlisted, SQL sanitized, sensitive keys dropped) and must never break a record.
        self.extra_sink: Callable[[dict[str, Any]], None] | None = None

    @property
    def path(self) -> Path:
        # session_id is reduced to safe characters: it names a file.
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", self.session_id)[:64]
        return self.trace_dir / f"{safe}.jsonl"

    def _allowed(self, span_type: str) -> set[str]:
        allowed = set(_COMMON) | set(SPAN_FIELDS[span_type])
        if span_type == "llm" and self.capture_llm_text:
            allowed |= set(LLM_TEXT_FIELDS)
        return allowed

    def record(self, span_type: str, name: str | None = None, **fields: Any) -> dict[str, Any]:
        if span_type not in SPAN_TYPES:
            raise ValueError(f"unknown span type: {span_type}")
        allowed = self._allowed(span_type)
        span: dict[str, Any] = {k: v for k, v in fields.items() if k in allowed}
        # Fixed fields are applied last so callers cannot overwrite them.
        span.update(
            {
                "type": span_type,
                "name": name or span_type,
                "session_id": self.session_id,
                "ts": time.time(),
                "span_id": uuid.uuid4().hex[:12],
            }
        )
        for field in ("sql_text", "sql"):
            val = span.get(field)
            if isinstance(val, str) and (field == "sql_text" or span_type == "role"):
                text, digest = sanitize_sql(val)
                if text is None:
                    span.pop(field)
                else:
                    span[field] = text
                if span_type == "sql":
                    span["sql_hash"] = digest
        clean = drop_sensitive(span, self.max_str)
        self.trace_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.trace_dir, 0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(clean, ensure_ascii=False, default=str) + "\n")
        if self.extra_sink is not None:
            try:
                self.extra_sink(clean)
            except Exception:  # noqa: BLE001 - an optional sink never breaks the trace or turn
                pass
        return clean

    @contextmanager
    def span(
        self, span_type: str, name: str | None = None, **fields: Any
    ) -> Iterator[dict[str, Any]]:
        """Context manager; the body may add fields to the yielded dict. Records on exit."""
        extra: dict[str, Any] = dict(fields)
        start = time.monotonic()
        try:
            yield extra
            extra.setdefault("status", "ok")
        except BaseException as exc:
            extra["status"] = "error"
            extra["error_class"] = type(exc).__name__
            raise
        finally:
            extra["duration_ms"] = int((time.monotonic() - start) * 1000)
            try:
                self.record(span_type, name, **extra)
            except Exception as rec_exc:  # noqa: BLE001 - tracing must not mask the body's error
                log.warning("trace record failed: %s", format_error(rec_exc))

    def record_error(self, exc: BaseException, name: str = "error") -> dict[str, Any]:
        return self.record(
            "error",
            name,
            error_class=type(exc).__name__,
            code=type(exc).__name__,
            message=format_error(exc),
        )
