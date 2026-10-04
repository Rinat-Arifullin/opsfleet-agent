"""Read tracer JSONL files and summarise them (AC-16.3).

The reader is defensive: malformed lines are skipped and counted, only allowlisted fields
survive loading, and every string passes through the tracer's redaction (defence in depth).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opsfleet_agent.obs import tracer as tr

_COMMON = frozenset(tr._COMMON)


@dataclass
class LoadResult:
    spans: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    files: int = 0


def _clean_span(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    stype = raw.get("type")
    if stype not in tr.SPAN_FIELDS:
        return None
    allowed = _COMMON | set(tr.SPAN_FIELDS[stype])
    kept = {k: v for k, v in raw.items() if k in allowed}
    return tr.drop_sensitive(kept)


def _safe_session(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:64]


def load_spans(trace_dir: str | Path, session_id: str | None = None) -> LoadResult:
    """Load one session's file, or every `*.jsonl` in the directory when `session_id` is None."""
    d = Path(trace_dir)
    if session_id is not None:
        paths = [d / f"{_safe_session(session_id)}.jsonl"]
    else:
        paths = sorted(d.glob("*.jsonl")) if d.is_dir() else []
    res = LoadResult()
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        res.files += 1
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                span = _clean_span(json.loads(line))
            except (ValueError, RecursionError):
                span = None
            if span is None:
                res.skipped += 1
            else:
                res.spans.append(span)
    return res


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(1, -(-len(s) * p // 100))
    return float(s[int(k) - 1])


def _num(v: Any) -> float:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0.0


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def _feedback_lines(store: Any, session_id: str | None) -> list[str]:
    """Feedback counts, thumbs-down rate and thumbs-down turns with trace ids (AC-25.3)."""
    c = store.counts(session_id)
    rate = f"{100.0 * c['down_rate']:.1f}%" if c["down_rate"] is not None else "n/a"
    out = [f"Feedback: up={c['up']}, down={c['down']}, thumbs-down rate {rate}"]
    for r in store.list_down(session_id, limit=20):
        why = f" reason={r.reason}" if r.reason else ""
        out.append(f"  down: turn {tr.scrub_text(r.turn_id, 80)}, trace {r.trace_id or 'n/a'}{why}")
    return out


def metrics_summary(
    trace_dir: str | Path, session_id: str | None = None, feedback: Any = None
) -> str:
    """Text summary for one session (`session_id`) or all sessions in `trace_dir`.

    `feedback` is an optional `FeedbackStore`; its counts are appended (AC-25.3).
    """
    fb = _feedback_lines(feedback, session_id) if feedback is not None else []
    text = _spans_summary(trace_dir, session_id)
    return "\n".join([text, *fb]) if fb else text


def _spans_summary(trace_dir: str | Path, session_id: str | None) -> str:
    res = load_spans(trace_dir, session_id)
    spans = res.spans
    scope = f"session {_safe_session(session_id)}" if session_id else "all sessions"
    if not spans:
        msg = f"No trace data found for {scope}."
        if res.skipped:
            msg += f" ({res.skipped} unreadable line(s) skipped)"
        return msg

    by_type = Counter(s["type"] for s in spans)
    errors = sum(1 for s in spans if s.get("status") == "error" or s["type"] == "error")
    turns = [s for s in spans if s["type"] == "turn"]
    outcomes = Counter(str(t.get("outcome", "unknown")) for t in turns)
    llm = [s for s in spans if s["type"] == "llm"]
    sql = [s for s in spans if s["type"] == "sql"]
    roles = [s for s in spans if s["type"] == "role"]
    guards = [s for s in spans if s["type"] == "guard"]

    lat_src = turns or spans
    lats = [_num(s.get("duration_ms")) for s in lat_src if "duration_ms" in s]
    tin = sum(_num(s.get("tokens_in")) for s in llm)
    tout = sum(_num(s.get("tokens_out")) for s in llm)
    billed = sum(_num(s.get("bytes_billed")) for s in sql)
    fallbacks = sum(1 for s in llm if s.get("fallback_used"))
    retried = sum(1 for s in roles if _num(s.get("retries")) > 0)

    out = [f"Metrics for {scope} ({res.files} file(s), {len(spans)} spans)"]
    if res.skipped:
        out.append(f"  skipped unreadable lines: {res.skipped}")
    out.append(
        "Spans by type: " + ", ".join(f"{t}={by_type[t]}" for t in tr.SPAN_TYPES if by_type[t])
    )
    out.append(f"Error rate: {_pct(errors, len(spans))} ({errors}/{len(spans)} spans)")
    out.append(
        f"Turns: {len(turns)}"
        + (" (" + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())) + ")" if turns else "")
    )
    for k in ("ok", "refused", "failed"):
        if turns:
            out.append(f"  {k} rate: {_pct(outcomes.get(k, 0), len(turns))}")
    label = "turn" if turns else "span"
    out.append(
        f"Latency ({label}): p50={percentile(lats, 50):.0f} ms, p95={percentile(lats, 95):.0f} ms"
    )
    calls_per_turn = f"{len(llm) / len(turns):.2f}" if turns else "n/a"
    out.append(
        f"LLM calls: {len(llm)} ({calls_per_turn} per turn); "
        f"fallback rate {_pct(fallbacks, len(llm))}"
    )
    by_model = Counter(str(s.get("model", "unknown")) for s in llm)
    for m, c in sorted(by_model.items()):
        out.append(f"  model {m}: {c}")
    by_role = Counter(str(s.get("agent", "unknown")) for s in roles)
    for r, c in sorted(by_role.items()):
        out.append(f"  role {r}: {c}")
    out.append(f"Self-correction (roles with retries): {_pct(retried, len(roles))}")
    out.append(f"Tokens: in={tin:.0f}, out={tout:.0f}")
    out.append(f"BigQuery: {len(sql)} queries, bytes billed={billed:.0f}")
    hits = Counter(str(g.get("rule") or g.get("verdict") or "unknown") for g in guards)
    out.append(
        "Guardrail triggers: "
        + (", ".join(f"{k}={v}" for k, v in sorted(hits.items())) if hits else "none")
    )
    return "\n".join(out)
