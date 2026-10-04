"""`/trace <turn>` renderer (AC-16.2). Plain function returning a string; wired in iteration 19."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opsfleet_agent.obs import tracer as tr
from opsfleet_agent.obs.metrics import load_spans

_COMMON_SHOWN = frozenset(tr._COMMON)


def _fields(span: dict[str, Any]) -> str:
    extra = []
    for k in tr.SPAN_FIELDS.get(span["type"], ()):
        if k in span and span[k] is not None:
            # Values were already allowlisted and redacted on load; scrub again on print.
            v = tr.drop_sensitive(span[k])
            extra.append(f"{k}={v}")
    return "  ".join(extra)


def _line(span: dict[str, Any]) -> str:
    failed = span.get("status") == "error" or span["type"] == "error"
    mark = "!! FAILED " if failed else ""
    parts = [f"{mark}{span['type']}:{span.get('name', span['type'])}"]
    if span.get("status") is not None:
        parts.append(f"status={span['status']}")
    if span.get("error_class"):
        parts.append(f"error_class={span['error_class']}")
    if span.get("duration_ms") is not None:
        parts.append(f"{span['duration_ms']}ms")
    head = " ".join(str(tr.drop_sensitive(p)) for p in parts)
    rest = _fields(span)
    return f"{head}  {rest}" if rest else head


def render_trace(trace_dir: str | Path, turn: str | int, session_id: str | None = None) -> str:
    """Render the span tree of one turn (matched on `turn_id`, or a span_id of a turn span)."""
    res = load_spans(trace_dir, session_id)
    want = str(turn)
    spans = [s for s in res.spans if str(s.get("turn_id")) == want]
    if not spans:
        spans = [s for s in res.spans if s["type"] == "turn" and s.get("span_id") == want]
    if not spans:
        note = f" ({res.skipped} unreadable line(s) skipped)" if res.skipped else ""
        return f"No trace found for turn {tr.scrub_text(want, 80)}.{note}"

    spans.sort(key=lambda s: s.get("ts") if isinstance(s.get("ts"), int | float) else 0)
    ids = {s.get("span_id") for s in spans if s.get("span_id")}
    turn_root = next(
        (s["span_id"] for s in spans if s["type"] == "turn" and s.get("span_id")), None
    )
    children: dict[Any, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for s in spans:
        pid = s.get("parent_id")
        if pid in ids and pid != s.get("span_id"):
            children.setdefault(pid, []).append(s)
        elif s["type"] != "turn" and turn_root and s.get("span_id") != turn_root:
            children.setdefault(turn_root, []).append(s)  # orphan: hang under the turn span
        else:
            roots.append(s)

    lines = [f"Trace for turn {tr.scrub_text(want, 80)} ({len(spans)} spans)"]
    seen: set[int] = set()

    def walk(s: dict[str, Any], depth: int) -> None:
        if id(s) in seen or depth > 20:
            return
        seen.add(id(s))
        lines.append("  " * depth + ("- " if depth else "") + _line(s))
        for c in children.get(s.get("span_id"), []):
            walk(c, depth + 1)

    for r in roots:
        walk(r, 0)
    failed = [s for s in spans if s.get("status") == "error" or s["type"] == "error"]
    lines.append(f"Failed spans: {len(failed)}")
    if res.skipped:
        lines.append(f"Skipped unreadable lines: {res.skipped}")
    return "\n".join(lines)
