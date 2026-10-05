"""D-143: dev-only local LLM provider (LM Studio). No network: httpx MockTransport and fakes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
import yaml
from langchain_core.messages import AIMessage, HumanMessage

from opsfleet_agent import config
from opsfleet_agent.config import ConfigError, load_settings, startup_check
from opsfleet_agent.golden.runtime import build_golden_index
from opsfleet_agent.golden.seed import CACHE_FILE, GenaiEmbedder, GoldenIndex, GoldenTrio
from opsfleet_agent.graph import providers as P
from opsfleet_agent.graph.llm import NonRetryableLLMError, TransientLLMError
from opsfleet_agent.guards.scope import ProductScope

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:1234/v1"
CHAT = "qwen/qwen3.8-27b"
EMB = "text-embedding-nomic-embed-text-v1.5"


@pytest.fixture
def gemini_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")


@pytest.fixture
def local_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv(config.PROVIDER_ENV, "lmstudio")


def _yaml(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())
    mutate(raw)
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


# --- switch and settings ---------------------------------------------------------------------


def test_default_provider_is_gemini_and_unchanged(gemini_env):
    s = load_settings(dotenv=False)
    assert s.llm_provider == "gemini" and s.llm_base_url == ""
    assert s.roles["deep_analyst"].model == "gemini-3.8-flash"
    assert s.roles["router"].fallback == "gemini-3.1-flash-lite"
    assert s.embedding_model == "gemini-embedding-001"
    assert "gemini-3.8-flash" in s.limits
    assert P.embedding_prefixes(s) == ("", "")
    assert P.golden_cache_dir(s, Path("data")) == Path("data")
    assert isinstance(P.build_embedder(s), GenaiEmbedder)


def test_gemini_ignores_missing_local_section(gemini_env, tmp_path):
    p = _yaml(tmp_path, lambda raw: raw.pop("local"))
    assert load_settings(p, dotenv=False).llm_provider == "gemini"


def test_gemini_chat_model_is_unchanged(gemini_env):
    from langchain_google_genai import ChatGoogleGenerativeAI

    m = P.chat_model_for(load_settings(dotenv=False), "gemini-3.1-flash-lite")
    assert isinstance(m, ChatGoogleGenerativeAI)
    assert m.max_retries == 1


def test_lmstudio_settings_map_every_role_to_local_model(local_env):
    s = load_settings(dotenv=False)
    assert s.llm_provider == "lmstudio" and s.llm_base_url == BASE
    assert {r.model for r in s.roles.values()} == {CHAT}
    assert set(s.roles) == set(config.ROLES)
    assert all(
        r.fallback is None and r.thinking_level is None and r.thinking_budget is None
        for r in s.roles.values()
    )
    assert s.embedding_model == EMB and s.embedding_dimensionality == 768
    assert s.limits == {}  # no free-tier limiter for the local provider
    assert s.gemini_api_key == ""  # not required


def test_lmstudio_model_ids_are_pure_config(local_env, tmp_path):
    def swap(raw):
        raw["local"] = {
            "chat_model": "qwen/some-other-27b",
            "embedding_model": "my-embed",
            "embedding_dim": 1024,
        }

    s = load_settings(_yaml(tmp_path, swap), dotenv=False)
    assert s.roles["report_writer"].model == "qwen/some-other-27b"
    assert s.embedding_model == "my-embed"
    assert s.embedding_dimensionality == 1024  # from config, not a code constant


def test_default_base_url_is_ipv4_loopback(local_env):
    assert config.DEFAULT_LMSTUDIO_BASE_URL == "http://127.0.0.1:1234/v1"
    assert load_settings(dotenv=False).llm_base_url == "http://127.0.0.1:1234/v1"


@pytest.mark.parametrize("bad", [None, 0, -768, 8193, True, "768", 768.0])
def test_local_embedding_dim_must_be_positive_int(local_env, tmp_path, bad):
    def mutate(raw):
        if bad is None:
            raw["local"].pop("embedding_dim")
        else:
            raw["local"]["embedding_dim"] = bad

    with pytest.raises(ConfigError, match="local.embedding_dim must be a positive integer"):
        load_settings(_yaml(tmp_path, mutate), dotenv=False)


def test_gemini_ignores_invalid_local_embedding_dim(gemini_env, tmp_path):
    p = _yaml(tmp_path, lambda raw: raw["local"].update(embedding_dim=0))
    assert load_settings(p, dotenv=False).embedding_dimensionality == 768


def test_base_url_override_and_validation(local_env, monkeypatch):
    monkeypatch.setenv(config.BASE_URL_ENV, "http://127.0.0.1:9999/v1/")
    assert load_settings(dotenv=False).llm_base_url == "http://127.0.0.1:9999/v1"
    monkeypatch.setenv(config.BASE_URL_ENV, "localhost:1234")
    with pytest.raises(ConfigError, match="must be an http"):
        load_settings(dotenv=False)


def test_unknown_provider_rejected(local_env, monkeypatch):
    monkeypatch.setenv(config.PROVIDER_ENV, "openai")
    with pytest.raises(ConfigError, match="must be one of: gemini, lmstudio"):
        load_settings(dotenv=False)


def test_lmstudio_needs_local_section(local_env, tmp_path):
    with pytest.raises(ConfigError, match="no 'local' section"):
        load_settings(_yaml(tmp_path, lambda raw: raw.pop("local")), dotenv=False)


def test_gemini_still_requires_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="GEMINI_API_KEY is not set"):
        load_settings(dotenv=False)


# --- startup check ---------------------------------------------------------------------------


def test_lmstudio_startup_check_uses_models_endpoint(local_env):
    s = startup_check(lister=lambda: [CHAT, EMB, "other"], dotenv=False)
    assert s.llm_provider == "lmstudio"


def test_lmstudio_missing_model_lists_reported_ids(local_env):
    with pytest.raises(ConfigError) as ei:
        startup_check(lister=lambda: ["qwen/qwen3-30b-a3b-2507", EMB], dotenv=False)
    msg = str(ei.value)
    assert f"Model {CHAT} is not loaded in LM Studio at {BASE}" in msg
    assert "qwen/qwen3-30b-a3b-2507" in msg  # the owner can copy the right id into config
    assert "local.chat_model" in msg


def test_lmstudio_unreachable_message(local_env):
    def refused():
        raise ConnectionRefusedError("refused")

    with pytest.raises(ConfigError) as ei:
        startup_check(lister=refused, dotenv=False)
    assert str(ei.value) == (
        f"LM Studio is not reachable at {BASE}; start the server and load {CHAT}."
    )


def test_base_url_userinfo_is_redacted_in_errors(local_env, monkeypatch, caplog):
    monkeypatch.setenv(config.BASE_URL_ENV, "http://alice:s3cret-pw@127.0.0.1:1234/v1")

    def refused():
        raise ConnectionRefusedError("refused")

    with pytest.raises(ConfigError) as ei:
        startup_check(lister=refused, dotenv=False)
    assert "s3cret-pw" not in str(ei.value) and "alice" not in str(ei.value)
    assert "http://127.0.0.1:1234/v1" in str(ei.value)
    with pytest.raises(ConfigError) as ei:
        startup_check(lister=lambda: ["x"], dotenv=False)
    assert "s3cret-pw" not in str(ei.value)

    url = "http://alice:s3cret-pw@127.0.0.1:1234/v1"
    m = P.LocalChatModel(_Raiser(_refused_error()), base_url=url, model=CHAT)
    with caplog.at_level("WARNING"), pytest.raises(P.LocalProviderUnavailable) as ei:
        m.invoke([HumanMessage("hi")])
    assert "s3cret-pw" not in str(ei.value) and "s3cret-pw" not in caplog.text
    assert "s3cret-pw" not in str(P.map_openai_error(_status(openai.NotFoundError, 404), url, CHAT))


def test_redact_url():
    assert config.redact_url("http://u:p@h:1/v1") == "http://h:1/v1"
    assert config.redact_url("http://u@h/v1") == "http://h/v1"
    assert config.redact_url(BASE) == BASE
    # a "/" in the password: urlsplit would leave part of the userinfo in the path
    out = config.redact_url("http://u:p/ss@h/v1")
    assert out == "http://h/v1"
    assert not any(part in out for part in ("u:", "p/", "ss"))
    assert config.redact_url("http://a@b@h:1/v1") == "http://h:1/v1"


def test_local_embedding_dim_upper_bound(local_env, tmp_path):
    p = _yaml(tmp_path, lambda raw: raw["local"].update(embedding_dim=8192))
    assert load_settings(p, dotenv=False).embedding_dimensionality == 8192
    p = _yaml(tmp_path, lambda raw: raw["local"].update(embedding_dim=8193))
    with pytest.raises(ConfigError, match=r"1\.\.8192"):
        load_settings(p, dotenv=False)


def test_lmstudio_lister_parses_models_response(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": [{"id": CHAT}, {"id": EMB}, {"x": 1}]}).encode()

    def fake_urlopen(req, timeout):
        seen["url"], seen["timeout"] = req.full_url, timeout
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert config.lmstudio_model_lister(BASE)() == [CHAT, EMB]
    assert seen == {"url": f"{BASE}/models", "timeout": config.LMSTUDIO_LIST_TIMEOUT_S}


def test_gemini_startup_check_unchanged_under_default(gemini_env):
    with pytest.raises(ConfigError, match="not available to this key"):
        startup_check(lister=lambda: ["gemini-3.1-flash-lite"], dotenv=False)


# --- chat model --------------------------------------------------------------------------------


def _completion(content: str, extra: dict | None = None, tool_calls=None) -> dict:
    message = {"role": "assistant", "content": content, **(extra or {})}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": CHAT,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_lmstudio_builds_chat_openai_without_thinking(local_env):
    from langchain_openai import ChatOpenAI

    m = P.chat_model_for(load_settings(dotenv=False), CHAT)
    assert isinstance(m, P.LocalChatModel)
    inner = m._inner
    assert isinstance(inner, ChatOpenAI)
    assert inner.openai_api_base == BASE
    assert inner.model_name == CHAT
    assert inner.max_retries == 0
    assert inner.use_responses_api is False
    assert not hasattr(inner, "thinking_level") and not hasattr(inner, "thinking_budget")


def test_local_request_payload_and_reasoning_stripped():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = _completion(
            "<think>The user wants revenue; let me plan.</think>\n\nRevenue rose 4%.",
            extra={"reasoning_content": "hidden chain of thought"},
        )
        return httpx.Response(200, json=body)

    m = P.build_local_chat_model(CHAT, BASE, http_client=_client(handler))
    msg = m.bind_tools([]).invoke([HumanMessage("hi")], timeout=5)
    assert msg.content == "Revenue rose 4%."
    assert "reasoning_content" not in msg.additional_kwargs
    assert msg.usage_metadata["input_tokens"] == 7
    assert len(requests) == 1  # SDK retries off
    req = requests[0]
    assert str(req.url) == f"{BASE}/chat/completions"
    payload = json.loads(req.content)
    assert payload["model"] == CHAT
    assert "tools" not in payload  # empty tool list is not sent
    assert not {"thinking", "thinking_level", "thinking_budget", "thinking_config"} & set(payload)


def test_local_tool_calls_survive_reasoning_strip():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["tools"][0]["function"]["name"] == "run_sql"
        call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "run_sql", "arguments": json.dumps({"sql": "SELECT 1"})},
        }
        return httpx.Response(200, json=_completion("<think>use the tool</think>", None, [call]))

    tool = {
        "name": "run_sql",
        "description": "Run SQL.",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}},
    }
    m = P.build_local_chat_model(CHAT, BASE, http_client=_client(handler))
    msg = m.bind_tools([tool]).invoke([HumanMessage("q")], timeout=5)
    assert msg.content == ""
    assert msg.tool_calls[0]["name"] == "run_sql"
    assert msg.tool_calls[0]["args"] == {"sql": "SELECT 1"}


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("plain answer", "plain answer"),
        ("<think>a</think>answer", "answer"),
        ("<THINK>a\nb</THINK>\n answer ", "answer"),
        ("before <think>x</think> after", "before  after"),
        ('reasoning from the template</think>\n{"a": 1}', '{"a": 1}'),
        ("  <think>truncated reasoning", ""),
        ("<think>one</think>mid<think>two</think>end", "midend"),
        # a tag mentioned in the answer is not reasoning: unchanged
        (
            "Use the tag <think> in prompts. Revenue was 42.",
            "Use the tag <think> in prompts. Revenue was 42.",
        ),
        ("answer <think>not at the start", "answer <think>not at the start"),
        # a bare leading close tag means template-opened reasoning: dropped (dev-only trade-off)
        ("The close tag is </think>. Done.", ". Done."),
        ("reasoning about it</think>Revenue 42.", "Revenue 42."),
        ("reasoning</think>  Revenue 42.", "Revenue 42."),
        ("user mentions <think> tag\n</think>\nRevenue 42.", "Revenue 42."),
        ("<think>a<think>b</think>c</think>X", "X"),
        ("reasoning\n</think>\n\nanswer", "answer"),
        ("reasoning</think>\nanswer <think>x</think> more", "answer  more"),
    ],
)
def test_strip_reasoning(raw, clean):
    assert P.strip_reasoning(raw) == clean


def test_strip_reasoning_warns_without_content(caplog):
    with caplog.at_level("WARNING", logger=P.__name__):
        assert P.strip_reasoning("secret plan</think>Revenue 42.") == "Revenue 42."
    assert "dropped dangling reasoning prefix, 19 chars" in caplog.text
    assert "secret plan" not in caplog.text


def test_strip_reasoning_complete_block_does_not_warn(caplog):
    with caplog.at_level("WARNING", logger=P.__name__):
        assert P.strip_reasoning("<think>a</think>answer") == "answer"
    assert "dropped" not in caplog.text


def test_clean_message_handles_list_content():
    msg = AIMessage(
        content=[
            {"type": "reasoning", "text": "secret"},
            {"type": "text", "text": "<think>x</think>ok"},
        ],
        additional_kwargs={"reasoning_content": "secret"},
    )
    out = P.clean_message(msg)
    assert out.content == [{"type": "text", "text": "ok"}]
    assert out.additional_kwargs == {}


def _req() -> httpx.Request:
    return httpx.Request("POST", f"{BASE}/chat/completions")


def _status(cls, code: int):
    return cls("err", response=httpx.Response(code, request=_req()), body=None)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (openai.APITimeoutError(request=_req()), TransientLLMError),
        (_status(openai.RateLimitError, 429), TransientLLMError),
        (_status(openai.InternalServerError, 500), TransientLLMError),
        (_status(openai.InternalServerError, 503), TransientLLMError),
        (_status(openai.BadRequestError, 400), NonRetryableLLMError),
        (_status(openai.NotFoundError, 404), NonRetryableLLMError),
        (_status(openai.AuthenticationError, 401), NonRetryableLLMError),
        (ValueError("parse"), NonRetryableLLMError),
    ],
)
def test_openai_error_mapping(exc, kind):
    mapped = P.map_openai_error(exc, BASE, CHAT)
    assert type(mapped) is kind


def _refused_error() -> openai.APIConnectionError:
    err = openai.APIConnectionError(request=_req())
    err.__cause__ = httpx.ConnectError("[Errno 61] Connection refused")
    return err


class _Raiser:
    def __init__(self, exc):
        self.exc = exc

    def invoke(self, *a, **k):
        raise self.exc


def test_connection_refused_gives_clear_message():
    mapped = P.map_openai_error(_refused_error(), BASE, CHAT)
    assert isinstance(mapped, P.LocalProviderUnavailable)
    assert isinstance(mapped, NonRetryableLLMError)
    assert str(mapped) == (
        f"LM Studio is not reachable at {BASE}; start the server and load {CHAT}."
    )


def test_connection_refused_found_deep_in_context_chain():
    err = openai.APIConnectionError(request=_req())
    mid = httpx.TransportError("wrapped")
    mid.__context__ = ConnectionRefusedError(61, "Connection refused")
    err.__cause__ = mid
    assert isinstance(P.map_openai_error(err, BASE, CHAT), P.LocalProviderUnavailable)


@pytest.mark.parametrize(
    "cause",
    [None, httpx.ReadError("connection reset by peer"), ConnectionResetError(54, "reset")],
)
def test_other_connection_errors_are_transient(cause):
    err = openai.APIConnectionError(request=_req())
    err.__cause__ = cause
    mapped = P.map_openai_error(err, BASE, CHAT)
    assert type(mapped) is TransientLLMError


def test_cause_chain_walk_is_cycle_safe():
    err = openai.APIConnectionError(request=_req())
    other = ValueError("x")
    err.__cause__, other.__context__ = other, err
    assert type(P.map_openai_error(err, BASE, CHAT)) is TransientLLMError


def test_reset_mid_request_through_chat_model_is_transient():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadError("connection reset by peer", request=request)

    m = P.build_local_chat_model(CHAT, BASE, http_client=_client(handler))
    with pytest.raises(TransientLLMError):
        m.invoke([HumanMessage("hi")], timeout=5)
    assert len(calls) == 1


def test_connection_refused_through_chat_model():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    m = P.build_local_chat_model(CHAT, BASE, http_client=_client(handler))
    with pytest.raises(P.LocalProviderUnavailable, match="not reachable"):
        m.invoke([HumanMessage("hi")], timeout=5)


def test_server_error_through_chat_model_is_transient():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "busy"}})

    m = P.build_local_chat_model(CHAT, BASE, http_client=_client(handler))
    with pytest.raises(TransientLLMError):
        m.invoke([HumanMessage("hi")], timeout=5)
    assert len(calls) == 1  # the wrapper ladder owns retries, not the SDK


# --- embeddings and store isolation ------------------------------------------------------------


class FakeEmbeddingsClient:
    def __init__(self, dim: int = 768):
        self.dim = dim
        self.inputs: list[list[str]] = []
        self.embeddings = self

    def create(self, model, input):
        self.inputs.append(list(input))
        data = [
            SimpleNamespace(index=i, embedding=[float(i + 1)] + [0.0] * (self.dim - 1))
            for i, _ in enumerate(input)
        ]
        return SimpleNamespace(data=list(reversed(data)))


def test_embedder_checks_dimension():
    ok = P.OpenAICompatEmbedder(BASE, EMB, 768, client=FakeEmbeddingsClient(768))
    vecs = ok.embed(["a", "b"])
    assert [v[0] for v in vecs] == [1.0, 2.0]  # ordered by index
    bad = P.OpenAICompatEmbedder(BASE, EMB, 768, client=FakeEmbeddingsClient(1024))
    with pytest.raises(ValueError, match="returned 1024 dimensions, expected 768"):
        bad.embed(["a"])


def test_embedder_via_http_uses_embeddings_endpoint():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        data = [{"object": "embedding", "index": 0, "embedding": [0.1] * 768}]
        return httpx.Response(200, json={"object": "list", "data": data, "model": EMB})

    client = openai.OpenAI(
        base_url=BASE, api_key=P.LOCAL_API_KEY, max_retries=0, http_client=_client(handler)
    )
    vecs = P.OpenAICompatEmbedder(BASE, EMB, 768, client=client).embed(["search_query: x"])
    assert len(vecs[0]) == 768
    assert seen[0][0] == f"{BASE}/embeddings"
    assert seen[0][1]["model"] == EMB and seen[0][1]["input"] == ["search_query: x"]


def _trio(tid: str, question: str) -> GoldenTrio:
    return GoldenTrio(
        trio_id=tid,
        version=1,
        question=question,
        sql="SELECT COUNT(*) FROM `bigquery-public-data.thelook_ecommerce.orders`",
        report_summary="Show the series and the direction of change.",
        tags=("revenue",),
        brands=(),
    )


class RecordingEmbedder:
    def __init__(self, dim: int = 768):
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


def test_golden_prefixes_applied_to_query_and_documents(tmp_path):
    emb = RecordingEmbedder()
    idx = GoldenIndex(
        [_trio("t1", "monthly revenue trend")],
        emb,
        EMB,
        768,
        cache_dir=tmp_path,
        query_prefix=P.LOCAL_QUERY_PREFIX,
        document_prefix=P.LOCAL_DOCUMENT_PREFIX,
    )
    idx.retrieve("revenue by month", ProductScope.all())
    texts = emb.calls[0]
    assert texts[0] == "search_query: revenue by month"
    assert texts[1].startswith("search_document: ")
    assert texts[1].count("search_document: ") == 1 and "search_query" not in texts[1]


def test_golden_default_has_no_prefix(tmp_path):
    emb = RecordingEmbedder()
    idx = GoldenIndex(
        [_trio("t1", "monthly revenue trend")], emb, "gemini-embedding-001", 768, cache_dir=tmp_path
    )
    idx.retrieve("revenue by month", ProductScope.all())
    assert emb.calls[0][0] == "revenue by month"
    assert not emb.calls[0][1].startswith("search_document: ")


def test_build_golden_index_local_wiring(local_env, tmp_path):
    s = load_settings(dotenv=False)
    idx = build_golden_index(s, cache_dir=tmp_path, strict=True)  # lazy: no network
    assert idx is not None
    assert isinstance(idx.embedder, P.OpenAICompatEmbedder)
    assert (idx.query_prefix, idx.document_prefix) == ("search_query: ", "search_document: ")
    assert idx.cache_dir == tmp_path / "lmstudio"
    assert (idx.model, idx.dimensionality) == (EMB, 768)


def test_store_isolation_between_providers(tmp_path, gemini_env, monkeypatch):
    """Vectors of different embedding models never mix, and switching provider keeps the other
    provider's cache intact (separate directories; keys also carry the model id)."""
    trios = [_trio("t1", "monthly revenue trend"), _trio("t2", "orders by country")]
    gemini = load_settings(dotenv=False)
    g_idx = GoldenIndex(
        trios,
        RecordingEmbedder(),
        gemini.embedding_model,
        768,
        cache_dir=P.golden_cache_dir(gemini, tmp_path),
    )
    g_idx.retrieve("revenue", ProductScope.all())
    g_file = tmp_path / CACHE_FILE
    g_before = g_file.read_text()

    monkeypatch.setenv(config.PROVIDER_ENV, "lmstudio")
    local = load_settings(dotenv=False)
    l_emb = RecordingEmbedder()
    l_idx = GoldenIndex(
        trios,
        l_emb,
        local.embedding_model,
        768,
        cache_dir=P.golden_cache_dir(local, tmp_path),
        query_prefix="search_query: ",
        document_prefix="search_document: ",
    )
    l_idx.retrieve("revenue", ProductScope.all())
    # the local index embedded every trio itself: no Gemini vector was reused
    assert len(l_emb.calls[0]) == 1 + len(trios)
    assert (tmp_path / "lmstudio" / CACHE_FILE).exists()
    assert g_file.read_text() == g_before  # Gemini cache untouched
    g_keys = set(json.loads(g_before)["entries"])
    l_keys = set(json.loads((tmp_path / "lmstudio" / CACHE_FILE).read_text())["entries"])
    assert g_keys and l_keys and not (g_keys & l_keys)


def test_content_key_includes_prefixes_and_gemini_key_is_unchanged():
    import hashlib

    t = _trio("t1", "monthly revenue trend")
    norm = {
        "id": "t1",
        "v": 1,
        "q": "monthly revenue trend",
        "sql": "select count(*) from `bigquery-public-data.thelook_ecommerce.orders`",
        "s": "show the series and the direction of change.",
        "tags": ["revenue"],
        "brands": [],
    }
    old = json.dumps([norm, "gemini-embedding-001", 768], sort_keys=True, separators=(",", ":"))
    legacy = hashlib.sha256(old.encode("utf-8")).hexdigest()
    assert t.content_key("gemini-embedding-001", 768) == legacy  # existing caches stay valid
    assert t.content_key("gemini-embedding-001", 768, "", "") == legacy
    base = t.content_key(EMB, 768)
    a = t.content_key(EMB, 768, "search_query: ", "search_document: ")
    b = t.content_key(EMB, 768, "search_query: ", "doc: ")
    c = t.content_key(EMB, 768, "query: ", "search_document: ")
    assert len({base, a, b, c}) == 4


def test_changing_prefix_reembeds_cached_vectors(tmp_path):
    trios = [_trio("t1", "monthly revenue trend")]
    first = RecordingEmbedder()
    GoldenIndex(
        trios,
        first,
        EMB,
        768,
        cache_dir=tmp_path,
        query_prefix="search_query: ",
        document_prefix="search_document: ",
    ).retrieve("revenue", ProductScope.all())
    assert len(first.calls[0]) == 2
    same = RecordingEmbedder()
    GoldenIndex(
        trios,
        same,
        EMB,
        768,
        cache_dir=tmp_path,
        query_prefix="search_query: ",
        document_prefix="search_document: ",
    ).retrieve("revenue", ProductScope.all())
    assert same.calls[0] == ["search_query: revenue"]  # trio vector came from the cache
    changed = RecordingEmbedder()
    GoldenIndex(
        trios,
        changed,
        EMB,
        768,
        cache_dir=tmp_path,
        query_prefix="search_query: ",
        document_prefix="doc: ",
    ).retrieve("revenue", ProductScope.all())
    assert len(changed.calls[0]) == 2 and changed.calls[0][1].startswith("doc: ")


def test_local_requests_disable_reasoning_by_default():
    """Qwen3 on LM Studio otherwise thinks for minutes at ~10 tok/s and blows the 60 s attempt
    timeout and the 120 s turn deadline; ``reasoning_effort="none"`` turns thinking off."""
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=_completion("Pong."))

    assert P.LOCAL_REASONING_EFFORT == "none"
    m = P.build_local_chat_model(CHAT, BASE, http_client=_client(handler))
    m.invoke([HumanMessage("ping")], timeout=5)
    off = P.build_local_chat_model(CHAT, BASE, http_client=_client(handler), reasoning_effort=None)
    off.invoke([HumanMessage("ping")], timeout=5)
    assert payloads[0]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in payloads[1]  # None omits the field for servers that reject it


def test_chat_model_for_local_sets_reasoning_effort(local_env):
    m = P.chat_model_for(load_settings(dotenv=False), CHAT)
    assert m._inner.reasoning_effort == "none"

