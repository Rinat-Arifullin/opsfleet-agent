import json

from opsfleet_agent.commands.trace import render_trace
from opsfleet_agent.obs import tracer as tr
from opsfleet_agent.obs.metrics import metrics_summary


def _session(tmp_path):
    t = tr.Tracer(tmp_path, "s1")
    root = t.record("turn", outcome="failed", turn_id="t1", duration_ms=900, tokens_in_total=10)
    r = t.record("role", agent="analyst", turn_id="t1", parent_id=root["span_id"], retries=1)
    t.record(
        "llm",
        model="m-pro",
        tokens_in=100,
        tokens_out=40,
        fallback_used=True,
        turn_id="t1",
        parent_id=r["span_id"],
        duration_ms=300,
        status="ok",
    )
    t.record(
        "sql",
        purpose="x",
        sql_text="SELECT 1",
        bytes_billed=2048,
        turn_id="t1",
        parent_id=r["span_id"],
        status="error",
        error_class="BadRequest",
        duration_ms=120,
    )
    t.record("guard", rule="pii_block", verdict="block", turn_id="t1", parent_id=root["span_id"])
    t2 = t.record("turn", outcome="ok", turn_id="t2", duration_ms=100)
    t.record(
        "llm",
        model="m-flash",
        tokens_in=5,
        tokens_out=5,
        turn_id="t2",
        parent_id=t2["span_id"],
        status="ok",
    )
    return t


def test_trace_viewer_renders_failed_span(tmp_path):
    t = _session(tmp_path)
    out = render_trace(tmp_path, "t1", "s1")
    line = next(ln for ln in out.splitlines() if "sql" in ln)
    assert "FAILED" in line and "error_class=BadRequest" in line and "120ms" in line
    assert "status=error" in line
    assert out.index("turn:") < out.index("role:") < out.index("sql:")
    assert "Failed spans: 1" in out
    assert "t2" not in out
    assert "no trace found" in render_trace(tmp_path, "nope").lower()
    assert t.path.exists()


def test_trace_viewer_skips_malformed_and_unlisted_fields(tmp_path):
    t = _session(tmp_path)
    with t.path.open("a") as f:
        f.write("{not json\n")
        f.write('{"type": "turn", "turn_id": "t1", "outcome": "ok"\n')  # truncated
        f.write(
            json.dumps(
                {
                    "type": "llm",
                    "turn_id": "t1",
                    "model": "m",
                    "name": "llm",
                    "span_id": "zz",
                    "prompt_text": "LEAK-ME-123",
                    "customer_name": "LEAK-ME-456",
                    "tokens_in": 1,
                }
            )
            + "\n"
        )
    out = render_trace(tmp_path, "t1", "s1")
    assert "LEAK-ME" not in out
    assert "Skipped unreadable lines: 2" in out
    assert "LEAK-ME" not in metrics_summary(tmp_path, "s1")


def test_metrics_summary(tmp_path):
    _session(tmp_path)
    out = metrics_summary(tmp_path, "s1")
    assert "Turns: 2" in out and "failed=1" in out and "ok=1" in out
    assert "Tokens: in=105, out=45" in out
    assert "bytes billed=2048" in out
    assert "model m-pro: 1" in out and "model m-flash: 1" in out
    assert "pii_block=1" in out
    assert "p50=100 ms, p95=900 ms" in out
    assert "Error rate: 14.3%" in out  # 1 of 7 spans
    assert "No trace data" in metrics_summary(tmp_path / "missing")
    assert "1 file(s), 7 spans" in metrics_summary(tmp_path)  # aggregate over the directory
