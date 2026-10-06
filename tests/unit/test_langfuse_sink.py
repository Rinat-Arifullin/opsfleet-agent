"""Iteration 40: the optional Langfuse sink. Offline: the client is a fake, no network."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from opsfleet_agent.commands import CommandContext, _langfuse_line
from opsfleet_agent.graph.llm import LLMResponse
from opsfleet_agent.obs import langfuse_sink as lf
from opsfleet_agent.obs import tracer as tr
from opsfleet_agent.roles.analyst import ModelTurn, ToolCall

# Synthetic values only (org policy): none of these is a real person, number or key.
EMAIL = "jane.doe@example.com"
PHONE = "+1 202 555 0143"
NAME = "Jane Testperson"
SECRET = "SENTINEL-langfuse-secret-0001"
ROWS = "ROWDATA-SENTINEL-0002"
PROOF = "SENTINEL-delete-proof-0003"
KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "LANGFUSE_BASE_URL")
ENV = {
    "LANGFUSE_PUBLIC_KEY": "pk-lf-test-placeholder",
    "LANGFUSE_SECRET_KEY": "sk-lf-test-placeholder",
    "LANGFUSE_HOST": "http://localhost:3000",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    tr.clear_secrets()
    yield
    tr.clear_secrets()


# --- fakes -----------------------------------------------------------------------------------


class FakeObs:
    def __init__(self, client: FakeClient, name: str, as_type: str, kw: dict[str, Any]) -> None:
        self.client, self.name, self.as_type, self.kw = client, name, as_type, dict(kw)
        self.children: list[FakeObs] = []
        self.updates: list[dict[str, Any]] = []
        self.ended = False
        self.trace_id = "trace-0001"

    def start_observation(self, *, name: str, as_type: str = "span", **kw: Any) -> FakeObs:
        if self.client.fail:
            raise RuntimeError("langfuse down")
        child = FakeObs(self.client, name, as_type, kw)
        self.children.append(child)
        return child

    def update(self, **kw: Any) -> FakeObs:
        self.updates.append(kw)
        return self

    def end(self, *, end_time: int | None = None) -> FakeObs:
        self.ended = True
        return self

    def dump(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "as_type": self.as_type,
            "kw": self.kw,
            "updates": self.updates,
            "children": [c.dump() for c in self.children],
        }


class FakeClient:
    def __init__(self, fail: bool = False, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.fail = fail
        self.roots: list[FakeObs] = []
        self.calls: list[str] = []

    def start_observation(self, *, name: str, as_type: str = "span", **kw: Any) -> FakeObs:
        if self.fail:
            raise RuntimeError("langfuse down")
        root = FakeObs(self, name, as_type, kw)
        self.roots.append(root)
        return root

    def flush(self) -> None:
        self.calls.append("flush")
        if self.fail:
            raise RuntimeError("langfuse down")

    def shutdown(self) -> None:
        self.calls.append("shutdown")


class FakePropagate:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @contextmanager
    def __call__(self, **kw: Any):
        self.calls.append(kw)
        yield


class FakeDetector:
    """Stands in for the NER: masks the synthetic person name."""

    def mask(self, text: str) -> Any:
        return SimpleNamespace(text=text.replace(NAME, "<PERSON>"))


@dataclass
class Result:
    text: str
    outcome: str = "answered"
    label: str = "qa"
    route: str = "quick"


def make_sink(fail: bool = False, detector: Any = None):
    client = FakeClient(fail=fail)
    prop = FakePropagate()
    sink = lf.build_sink(ENV, factory=lambda **kw: client, detector=detector, propagate=prop)
    assert sink is not None
    return sink, client, prop


def walk(obs: FakeObs):
    yield obs
    for c in obs.children:
        yield from walk(c)


# --- on/off ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"LANGFUSE_PUBLIC_KEY": "pk-lf-x", "LANGFUSE_SECRET_KEY": "sk-lf-x"},
        {"LANGFUSE_PUBLIC_KEY": "pk-lf-x", "LANGFUSE_HOST": "http://localhost:3000"},
        {**ENV, "LANGFUSE_SECRET_KEY": "  "},
    ],
)
def test_disabled_without_all_keys_builds_no_client(env):
    def factory(**_):
        raise AssertionError("client must not be built")

    assert lf.build_sink(env, factory=factory) is None


def test_disabled_by_default_from_process_env():
    assert not lf.is_configured()
    assert lf.build_sink(factory=lambda **_: pytest.fail("client built")) is None


def test_enabled_passes_mask_timeout_and_base_url():
    seen: dict[str, Any] = {}
    env = {
        **{k: v for k, v in ENV.items() if k != "LANGFUSE_HOST"},
        "LANGFUSE_BASE_URL": "http://h:3000",
    }
    sink = lf.build_sink(env, factory=lambda **kw: seen.update(kw) or FakeClient())
    assert sink is not None
    assert seen["base_url"] == "http://h:3000"
    assert seen["timeout"] == lf.CLIENT_TIMEOUT_S
    assert callable(seen["mask"])
    assert EMAIL not in json.dumps(seen["mask"](data={"q": EMAIL}))


def test_client_construction_error_fails_open():
    def factory(**_):
        raise ValueError("bad host")

    assert lf.build_sink(ENV, factory=factory) is None


# --- structure -------------------------------------------------------------------------------


def _router_invoke(model, messages, timeout):
    return LLMResponse("qa", 11, 2)


def _scenario(sink, tracer):
    """A quick-analyst turn: router call, one failed and one good analyst call with run_sql."""
    router = sink.wrap_llm("llm.chat", _router_invoke)
    attempts = iter([TimeoutError("slow"), None])

    def analyst(model, messages, tools, timeout):
        err = next(attempts)
        if err is not None:
            raise err
        call = ToolCall("c1", "run_sql", {"sql": "SELECT 1 FROM t WHERE x = 'abc'"})
        return LLMResponse(ModelTurn("checking", (call,)), 120, 30)

    analyst = sink.wrap_llm("llm.tools", analyst)

    def run():
        tracer.record("guard", "input", verdict="allow")
        router("flash", [SimpleNamespace(role="user", content="revenue?")], 5.0)
        tracer.record("router", "router", label="qa", route="quick", model="flash")
        msgs = [{"role": "system", "content": "rules"}, {"role": "user", "content": "revenue?"}]
        with pytest.raises(TimeoutError):
            analyst("pro", msgs, [SimpleNamespace(name="run_sql")], 5.0)
        analyst("pro", msgs, [SimpleNamespace(name="run_sql")], 5.0)
        # the JSONL llm span of the same attempt is not a second Langfuse generation
        tracer.record("llm", "analyst_quick", model="pro", outcome="ok", attempt=2)
        tracer.record("sql", "run_sql", sql_text="SELECT 1 FROM t WHERE x = 'abc'", rows=3,
                      bytes_billed=1024, cache_hit=False)  # fmt: skip
        tracer.record("tool", "run_sql", tool="run_sql", outcome="ok", rows=3)
        tracer.record("role", "analyst_quick", agent="analyst_quick", model="pro", llm_calls=2)
        tracer.record("guard", "grounding", verdict="allow", grounding_flags=[])
        tracer.record("guard", "output", verdict="allow", rule_hits=[])
        tracer.record("turn", outcome="answered", path="quick", label="qa")
        return Result("Revenue was 10.")

    return run


def test_one_trace_per_turn_with_nested_observations(tmp_path):
    sink, client, prop = make_sink()
    tracer = tr.Tracer(tmp_path, "sess-1")
    tracer.extra_sink = sink.on_span
    run = sink.traced(_scenario(sink, tracer), session_id="sess-1", user_id="u-analyst",
                      turn_id="turn-1", question="What was revenue?")  # fmt: skip

    assert run().text == "Revenue was 10."
    assert len(client.roots) == 1
    root = client.roots[0]
    assert (root.name, root.as_type) == ("turn", "agent")
    assert root.kw["input"] == "What was revenue?"
    assert root.updates[-1]["output"] == "Revenue was 10."
    assert root.ended
    assert prop.calls == [
        {
            "session_id": "sess-1",
            "user_id": "u-analyst",
            "trace_name": "turn",
            "metadata": {"turn_id": "turn-1", "outcome": "answered", "label": "qa",
                         "route": "quick", "provider": "gemini"},
        }
    ]  # fmt: skip

    kids = [(c.name, c.as_type) for c in root.children]
    assert kids == [
        ("guard:input", "guardrail"),
        ("router:router", "agent"),
        ("role:analyst_quick", "agent"),
        ("guard:grounding", "guardrail"),
        ("guard:output", "guardrail"),
        ("turn_summary", "span"),
    ]
    router_obs, role_obs = root.children[1], root.children[2]
    [gen] = router_obs.children
    assert (gen.name, gen.as_type, gen.kw["model"]) == ("llm.chat", "generation", "flash")
    assert gen.kw["usage_details"] == {"input": 11, "output": 2}
    assert gen.kw["output"] == "qa"
    assert gen.kw["input"] == [{"role": "user", "content": "revenue?"}]

    names = [(c.name, c.as_type) for c in role_obs.children]
    assert names == [
        ("llm.tools", "generation"),
        ("llm.tools", "generation"),
        ("sql:run_sql", "tool"),
        ("tool:run_sql", "tool"),
    ]
    failed, ok, sql, _tool = role_obs.children
    assert failed.kw["level"] == "ERROR" and failed.kw["status_message"] == "TimeoutError"
    assert failed.kw["metadata"]["attempt"] == 1 and failed.kw["metadata"]["status"] == "error"
    assert ok.kw["metadata"]["attempt"] == 2 and ok.kw["metadata"]["tools"] == ["run_sql"]
    [call] = ok.kw["output"]["tool_calls"]
    assert call["args"]["sql"] is not None and "'abc'" not in call["args"]["sql"]
    assert sql.kw["metadata"]["rows"] == 3 and "'abc'" not in sql.kw["metadata"]["sql_text"]
    assert root.children[3].kw["output"] == {"verdict": "allow", "grounding_flags": []}

    assert sink.trace_id_for("turn-1") == "trace-0001"
    assert sink.last_trace_id == "trace-0001"


def test_spans_outside_a_turn_are_ignored(tmp_path):
    sink, client, _ = make_sink()
    tracer = tr.Tracer(tmp_path, "s")
    tracer.extra_sink = sink.on_span
    tracer.record("turn", "turn_cancelled", outcome="cancelled")
    assert sink.wrap_llm("llm.chat", _router_invoke)("m", [], 1.0).value == "qa"
    assert client.roots == []


def test_cancelled_turn_is_traced_and_reraises():
    sink, client, prop = make_sink()

    def boom():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        sink.traced(boom, session_id="s", user_id="u", turn_id="t", question="q")()
    assert prop.calls[0]["metadata"]["outcome"] == "cancelled"
    assert len(client.roots) == 1


def test_error_turn_is_marked_and_reraises():
    sink, client, prop = make_sink()

    def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        sink.traced(boom, session_id="s", user_id="u", turn_id="t", question="q")()
    assert prop.calls[0]["metadata"]["outcome"] == "error"
    assert client.roots[0].updates[-1]["level"] == "ERROR"


# --- redaction -------------------------------------------------------------------------------


def test_redaction_question_answer_prompts_rows_secrets(tmp_path):
    tr.register_secret(SECRET)
    sink, client, prop = make_sink(detector=FakeDetector())
    tracer = tr.Tracer(tmp_path, "s")
    tracer.extra_sink = sink.on_span
    invoke = sink.wrap_llm(
        "llm.tools",
        lambda model, messages, tools, timeout: LLMResponse(
            ModelTurn(
                f"Contact {NAME} at {EMAIL} or {PHONE}",
                (
                    ToolCall(
                        "c1", "run_sql", {"sql": f"SELECT * FROM users WHERE email = '{EMAIL}'"}
                    ),
                    ToolCall("c2", "get_schema", {"table": "orders", "api_key": SECRET}),
                ),
            ),
            1,
            1,
        ),  # fmt: skip
    )

    def run():
        invoke(
            "pro",
            [
                {"role": "system", "content": f"key is {SECRET}"},
                {"role": "user", "content": f"I am {NAME}, {EMAIL}, {PHONE}"},
                {"role": "tool", "name": "run_sql", "content": json.dumps({"rows": [ROWS]})},
            ],
            [],
            5.0,
        )
        tracer.record("delete", "delete", event="preview", pending_action={"proof": PROOF},
                      __interrupt__=PROOF, proof=PROOF, count=1)  # fmt: skip
        tracer.record("sql", "run_sql", sql_text=f"SELECT 1 WHERE e = '{EMAIL}'", rows=1)
        return Result(f"Write to {EMAIL} or call {PHONE}; {NAME}. {SECRET}")

    sink.traced(run, session_id="s", user_id="u", turn_id="t",
                question=f"Email {EMAIL}, phone {PHONE}, I am {NAME}")()  # fmt: skip

    blob = json.dumps([r.dump() for r in client.roots] + prop.calls, default=str)
    for raw in (EMAIL, PHONE, NAME, SECRET, ROWS, PROOF, "pending_action", "__interrupt__"):
        assert raw not in blob, raw
    gen = next(o for o in walk(client.roots[0]) if o.as_type == "generation")
    assert gen.kw["input"][2]["content"].startswith("[tool result omitted:")
    assert gen.kw["output"]["tool_calls"][1]["args"]["api_key"] == tr.DROPPED
    assert "<PERSON>" in client.roots[0].kw["input"]


def test_mask_function_drops_sensitive_keys_and_scrubs_strings():
    mask = lf.make_mask()
    out = mask(
        data={
            "pending_action": {"id": "p1"},
            "__interrupt__": [1],
            "proof": PROOF,
            "nested": {"text": f"{EMAIL} {PHONE}", "secret_key": "x"},
            "items": [EMAIL],
        }
    )
    blob = json.dumps(out)
    for raw in (EMAIL, PHONE, PROOF, "p1"):
        assert raw not in blob
    assert out["pending_action"] == tr.DROPPED
    assert out["nested"]["secret_key"] == tr.DROPPED
    assert mask(data=f"call {PHONE}") == "call <PHONE>"


def test_failing_detector_fails_closed():
    class Broken:
        def mask(self, text):
            raise RuntimeError("ner down")

    sink, client, _ = make_sink(detector=Broken())
    sink.traced(lambda: Result(f"hi {NAME}"), session_id="s", user_id="u", turn_id="t",
                question=f"I am {NAME}")()  # fmt: skip
    root = client.roots[0]
    assert root.kw["input"] == lf.REDACTED
    assert root.updates[-1]["output"] == lf.REDACTED


# --- fail-open -------------------------------------------------------------------------------


def test_raising_client_never_breaks_a_turn(tmp_path, caplog):
    sink, client, _ = make_sink(fail=True)
    tracer = tr.Tracer(tmp_path, "s")
    tracer.extra_sink = sink.on_span
    invoke = sink.wrap_llm("llm.chat", _router_invoke)

    def run():
        invoke("m", [], 1.0)
        tracer.record("turn", outcome="answered")
        return Result("ok")

    with caplog.at_level("DEBUG", logger=lf.__name__):
        assert (
            sink.traced(run, session_id="s", user_id="u", turn_id="t", question="q")().text == "ok"
        )
        assert sink.enabled is False
        # Disabled: later turns run untouched and are not traced.
        assert (
            sink.traced(run, session_id="s", user_id="u", turn_id="t2", question="q")().text == "ok"
        )
    assert [r.levelname for r in caplog.records] == ["DEBUG"]
    assert "langfuse down" not in caplog.text  # type name only
    sink.shutdown()  # flush raises: swallowed
    assert client.calls == ["flush"]


def test_raising_extra_sink_never_breaks_the_tracer(tmp_path):
    tracer = tr.Tracer(tmp_path, "s")

    def bad(_):
        raise RuntimeError("x")

    tracer.extra_sink = bad
    assert tracer.record("turn", outcome="answered")["outcome"] == "answered"


def test_wrapped_invoke_passes_exceptions_through_unchanged():
    sink, _, _ = make_sink()
    err = ConnectionError("down")

    def invoke(*_):
        raise err

    def run():
        with pytest.raises(ConnectionError) as info:
            sink.wrap_llm("llm.chat", invoke)("m", [], 1.0)
        assert info.value is err
        return Result("ok")

    sink.traced(run, session_id="s", user_id="u", turn_id="t", question="q")()


def test_shutdown_flushes_then_shuts_down():
    sink, client, _ = make_sink()
    sink.shutdown()
    assert client.calls == ["flush", "shutdown"]


# --- /trace ----------------------------------------------------------------------------------


def test_trace_command_shows_langfuse_id_only_when_known():
    sink, _, _ = make_sink()
    sink.traced(lambda: Result("ok"), session_id="s", user_id="u", turn_id="t1", question="q")()
    ctx = CommandContext(user_id="u", session_id="s", langfuse=sink)
    assert _langfuse_line(ctx, "t1") == (
        "\nLangfuse trace: trace-0001 (http://localhost:3000/trace/trace-0001)"
    )
    assert _langfuse_line(ctx, "other") == ""
    assert _langfuse_line(CommandContext(user_id="u", session_id="s"), "t1") == ""
