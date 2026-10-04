import json
import logging

import pytest

from opsfleet_agent.obs import tracer as tr

EMAIL = "jane.doe@example.com"
NAME = "Jane Testperson"
SENT = {
    "GEMINI_API_KEY": "SENTINEL-gemini-key-0001",
    "LANGGRAPH_AES_KEY": "SENTINEL-aes-key-0002",
    "K_delete": "SENTINEL-kdelete-0003",
    "token": "SENTINEL-delete-token-0004",
    "proof": "SENTINEL-delete-proof-0005",
}


@pytest.fixture(autouse=True)
def _secrets():
    tr.clear_secrets()
    yield
    tr.clear_secrets()


def read(t):
    return [json.loads(line) for line in t.path.read_text().splitlines()]


def test_trace_redaction(tmp_path):
    t = tr.Tracer(tmp_path, "s1")
    t.record(
        "sql",
        sql_text=f"SELECT 1 -- {EMAIL}",
        rows=3,
        result_text=f"{NAME} {EMAIL}",
        customer_name=NAME,
    )
    (span,) = read(t)
    assert EMAIL not in json.dumps(span)
    assert NAME not in json.dumps(span)
    assert span["sql_text"] == "SELECT ?"  # comment stripped, literal replaced
    assert "result_text" not in span and span["rows"] == 3


def test_trace_spans_carry_no_pii(tmp_path):
    t = tr.Tracer(tmp_path, "s1")
    t.record("turn", outcome="ok", user_message=f"I am {NAME}, {EMAIL}", answer_text=NAME)
    t.record("tool", tool="x", outcome="ok", args={"email": EMAIL}, result=NAME)
    t.record("error", message=f"failed for {EMAIL}")
    raw = t.path.read_text()
    assert EMAIL not in raw and NAME not in raw


def test_trace_has_all_span_types(tmp_path):
    assert set(tr.SPAN_TYPES) == {
        "turn",
        "router",
        "role",
        "llm",
        "tool",
        "guard",
        "sql",
        "delete",
        "error",
    }
    t = tr.Tracer(tmp_path, "s1")
    for st in tr.SPAN_TYPES:
        t.record(st)
    assert [s["type"] for s in read(t)] == list(tr.SPAN_TYPES)
    with pytest.raises(ValueError):
        t.record("bogus")


def test_llm_text_capture_off_by_default_in_prod(tmp_path):
    off = tr.Tracer(tmp_path, "off")
    off.record("llm", model="m", prompt_redacted="hello", completion_redacted="world")
    assert "prompt_redacted" not in read(off)[0] and "completion_redacted" not in read(off)[0]
    on = tr.Tracer(tmp_path, "on", capture_llm_text=True)
    on.record("llm", model="m", prompt_redacted=f"hello {EMAIL}", completion_redacted="world")
    s = read(on)[0]
    assert s["completion_redacted"] == "world" and EMAIL not in s["prompt_redacted"]


def test_bounded_string_length(tmp_path):
    t = tr.Tracer(tmp_path, "s1", max_str=50)
    t.record("sql", sql_text="x" * 10_000)
    assert len(read(t)[0]["sql_text"]) < 100


def test_dropped_keys_and_span_context(tmp_path):
    out = tr.drop_sensitive({"pending_action": {"a": 1}, "__interrupt__": 1, "proof": "p", "ok": 1})
    assert out == {
        "pending_action": "[dropped]",
        "__interrupt__": "[dropped]",
        "proof": "[dropped]",
        "ok": 1,
    }
    t = tr.Tracer(tmp_path, "s1")
    with pytest.raises(KeyError):
        with t.span("tool", tool="x"):
            raise KeyError("k")
    s = read(t)[0]
    assert s["status"] == "error" and s["error_class"] == "KeyError" and "duration_ms" in s


def test_secrets_never_in_traces_logs_or_errors(tmp_path, caplog):
    for v in SENT.values():
        tr.register_secret(v)
    t = tr.Tracer(tmp_path, "s1", capture_llm_text=True)
    t.record("turn", outcome=f"ok {SENT['GEMINI_API_KEY']}")
    t.record(
        "llm",
        model="m",
        prompt_redacted=f"key={SENT['LANGGRAPH_AES_KEY']}",
        completion_redacted=SENT["K_delete"],
    )
    t.record(
        "delete",
        event="previewed",
        pending_action_id="p1",
        pending_action={"token": SENT["token"]},
        proof=SENT["proof"],
        audit_event_id=f"x{SENT['token']}",
    )
    t.record("tool", tool="t", args={"proof": SENT["proof"], "k_delete": SENT["K_delete"]})

    logger = logging.getLogger("opsfleet.test_secrets")
    logger.addFilter(tr.RedactingFilter())
    seen: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(self.format(record))

    h = Capture()
    h.setFormatter(logging.Formatter("%(message)s %(exc_text)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.info("key %s and %s", SENT["GEMINI_API_KEY"], SENT["token"])
    try:
        raise RuntimeError(f"bad {SENT['LANGGRAPH_AES_KEY']} {SENT['proof']}")
    except RuntimeError as e:
        logger.exception("failed")
        err = tr.format_error(e)
        t.record_error(e)
    logger.removeHandler(h)

    blob = t.path.read_text() + "\n".join(seen) + err
    for name, v in SENT.items():
        assert v not in blob, name


@pytest.fixture
def log_factory():
    tr.install_log_filter()
    yield
    tr.uninstall_log_filter()


def test_log_redaction_covers_child_loggers_and_late_handlers(log_factory):
    secret = "SENTINEL-factory-secret-9"
    tr.register_secret(secret)
    seen: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(self.format(record))

    child = logging.getLogger("opsfleet.deep.child")
    child.setLevel(logging.INFO)
    h = Capture()
    h.setFormatter(logging.Formatter("%(message)s|%(exc_text)s"))
    child.addHandler(h)  # added after install
    try:
        child.info("hello %s", secret)
        try:
            raise RuntimeError(f"boom {secret}")
        except RuntimeError:
            child.exception("failed %s", secret)
    finally:
        child.removeHandler(h)
    assert len(seen) == 2
    assert all(secret not in line for line in seen)
    assert "[secret]" in seen[0] and "RuntimeError" in seen[1]
    tr.install_log_filter()  # idempotent
    assert logging.getLogRecordFactory() is tr._installed_factory


@pytest.mark.parametrize(
    "text",
    [
        "AIza" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R",
        "tok ya29.a0AfH6SMBxyz_abc-123",
        "Authorization: Bearer abc.def-123",
        "https://x/y?key=abcdef123&z=1",
        "https://x/y?access_token=abcdef123",
        "api_key=abcdef123",
    ],
)
def test_pattern_scrubs_unregistered_credentials(text):
    out = tr.scrub_text(text)
    assert "[secret]" in out
    for frag in ("AIza", "ya29.", "abc.def", "abcdef123"):
        assert frag not in out


def test_url_encoded_registered_secret_scrubbed():
    s = "pa ss/w+rd&x"
    tr.register_secret(s)
    from urllib.parse import quote, quote_plus

    for form in (quote(s, safe=""), quote_plus(s), quote(s)):
        assert s not in tr.scrub_text(f"u?q={form}") and form not in tr.scrub_text(f"u?q={form}")


def test_sql_literals_replaced(tmp_path):
    t = tr.Tracer(tmp_path, "s1")
    sql = (
        "SELECT id FROM u WHERE phone = '+15551234567' AND name = 'Jane Testperson' AND id = 98765"
    )
    t.record("sql", sql_text=sql)
    s = read(t)[0]
    for lit in ("5551234567", "Jane", "98765"):
        assert lit not in s["sql_text"]
    assert "?" in s["sql_text"] and s["sql_hash"]


def test_sql_unparseable_fails_closed(tmp_path):
    t = tr.Tracer(tmp_path, "s1")
    t.record("sql", sql_text="SELEC ((( 'Jane Testperson' from from")
    s = read(t)[0]
    assert "sql_text" not in s and s["sql_hash"]
    assert "Jane" not in json.dumps(s)


@pytest.mark.parametrize(
    "key",
    ["authorization", "access_token", "apiKey", "x-goog-api-key", "Client_Secret", "my_credential"],
)
def test_drop_keys_substring_case_insensitive(key):
    assert tr.drop_sensitive({key: "v"}) == {key: "[dropped]"}


def test_safe_metric_keys_not_dropped():
    out = tr.drop_sensitive({"tokens_in": 5, "args_keys": ["a"], "pending_action_id": "p"})
    assert out == {"tokens_in": 5, "args_keys": ["a"], "pending_action_id": "p"}


def test_callers_cannot_overwrite_fixed_fields(tmp_path):
    t = tr.Tracer(tmp_path, "s1")
    t.record("tool", name="n", type="turn", ts=0, span_id="evil", session_id="other", tool="x")
    s = read(t)[0]
    assert s["type"] == "tool" and s["ts"] != 0 and s["span_id"] != "evil"
    assert s["session_id"] == "s1"


def test_nested_values_scrubbed_and_bounded(tmp_path):
    t = tr.Tracer(tmp_path, "s1", max_str=40)
    deep: dict = {"v": EMAIL}
    for _ in range(20):
        deep = {"n": deep}
    t.record(
        "tool",
        tool="x",
        args_keys={"a": [EMAIL, {"b": "y" * 1000, "token": "t"}], "deep": deep},
        rows=[EMAIL] * 500,
    )
    raw = t.path.read_text()
    assert EMAIL not in raw and "[depth]" in raw
    s = json.loads(raw)
    assert len(s["rows"]) == tr.MAX_ITEMS
    assert len(s["args_keys"]["a"][1]["b"]) < 80
    assert s["args_keys"]["a"][1]["token"] == "[dropped]"


def test_trace_permissions(tmp_path):
    d = tmp_path / "tr"
    d.mkdir(mode=0o755)
    f = d / "s1.jsonl"
    f.write_text("")
    f.chmod(0o644)
    tr.Tracer(d, "s1").record("turn")
    assert (d.stat().st_mode & 0o777) == 0o700
    assert (f.stat().st_mode & 0o777) == 0o600
    tr.Tracer(tmp_path / "new" / "x", "s2").record("turn")
    assert ((tmp_path / "new" / "x").stat().st_mode & 0o777) == 0o700


def test_record_failure_does_not_mask_original_exception(tmp_path, monkeypatch):
    t = tr.Tracer(tmp_path, "s1")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(t, "record", boom)
    with pytest.raises(KeyError):
        with t.span("tool", tool="x"):
            raise KeyError("orig")
    with t.span("tool", tool="x"):  # also does not raise on the ok path
        pass
