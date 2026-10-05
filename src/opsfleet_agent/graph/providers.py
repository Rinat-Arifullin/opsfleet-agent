"""LLM provider dispatch (D-143): Gemini (default) or a dev-only local OpenAI-compatible server.

The local provider targets LM Studio (``OPSFLEET_LLM_PROVIDER=lmstudio``). Nothing here makes a
network call at construction time; clients are lazy. Model ids are config values only: no code
path assumes a particular local model.

* **Chat**: ``ChatOpenAI`` with SDK retries off (the LLMWrapper ladder owns retries), no Gemini
  thinking params, and OpenAI SDK errors mapped onto ``TransientLLMError`` /
  ``NonRetryableLLMError``. Reasoning output (``<think>...</think>`` in content, or a separate
  ``reasoning_content`` field) is removed before the message reaches any parser or the user.
* **Embeddings**: ``/v1/embeddings`` with task prefixes, and the vector size checked against
  the configured dimension. The local provider keeps its golden vectors in a separate cache
  directory, and cache keys already carry the model id, so vectors of different models never mix.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from opsfleet_agent.config import LMSTUDIO, redact_url
from opsfleet_agent.graph.llm import (
    TRANSIENT_STATUS,
    NonRetryableLLMError,
    TransientLLMError,
    build_chat_model,
    classify_error,
)

log = logging.getLogger(__name__)

LOCAL_API_KEY = "lm-studio"  # placeholder: LM Studio ignores the key, the SDK requires one
LOCAL_CHAT_TIMEOUT_S = 60.0  # per attempt; the wrapper also passes a deadline-bound timeout
LOCAL_EMBED_TIMEOUT_S = 10.0
LOCAL_QUERY_PREFIX = "search_query: "
LOCAL_DOCUMENT_PREFIX = "search_document: "
LOCAL_CACHE_SUBDIR = "lmstudio"
REASONING_KEYS = ("reasoning_content", "reasoning")
# Local thinking models (e.g. Qwen3 on LM Studio) otherwise reason for hundreds of tokens per
# call at ~10 tok/s, which blows the 60 s attempt timeout and the 120 s turn deadline.
# LM Studio honours the OpenAI ``reasoning_effort`` field; ``None`` omits it from the request.
LOCAL_REASONING_EFFORT: str | None = "none"

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_LEADING_OPEN = re.compile(r"\A\s*<think>", re.IGNORECASE)
_ANY_TAG = re.compile(r"</?think>", re.IGNORECASE)
_CLOSE = re.compile(r"</think>\s*", re.IGNORECASE)
# A close tag alone on its line: the end of a template-opened reasoning prefix (Qwen-style
# "...\n</think>\n\nanswer"), even when the reasoning mentions "<think>" mid-line.
_OWN_LINE_CLOSE = re.compile(r"^[ \t]*</think>[ \t]*(?:\r?\n|\Z)\s*", re.IGNORECASE | re.MULTILINE)


def is_local(settings: Any) -> bool:
    return getattr(settings, "llm_provider", "gemini") == LMSTUDIO


def _drop_through(text: str, match: re.Match[str]) -> str:
    log.warning("local provider: dropped dangling reasoning prefix, %d chars", match.end())
    return text[match.end() :]


def strip_reasoning(text: str) -> str:
    """Remove reasoning from model text. Dev-only provider: not leaking reasoning wins over
    keeping an answer that happens to contain a bare ``</think>``.

    1. If the first think tag is a closing ``</think>``, the opening tag lived in the chat
       template: everything up to and including it (plus following whitespace) goes.
    2. Otherwise, if the content does not open with ``<think>`` and a ``</think>`` stands alone
       on its line, that line ends a template-opened prefix (one that mentions ``<think>``
       mid-line): everything through the last such line goes.
    3. Complete ``<think>...</think>`` blocks go wherever they are.
    4. If a ``</think>`` still remains (nested or unbalanced tags), everything through the last
       remaining one goes.
    5. An unterminated ``<think>`` goes (with everything after it) only when it opens the
       content: a truncated reasoning block is not an answer.

    Rules 1, 2 and 4 log a warning with the dropped length, never the content. A ``<think>``
    mentioned mid-answer with no closing tag ("Use the tag <think> in prompts.") is left alone,
    and text without the tags is returned unchanged."""
    if not isinstance(text, str) or "think>" not in text.lower():
        return text
    out = text
    first = _ANY_TAG.search(out)
    if first and first.group(0).startswith("</"):
        out = _drop_through(out, _CLOSE.match(out, first.start()))
    elif not _LEADING_OPEN.match(out):
        own_line = list(_OWN_LINE_CLOSE.finditer(out))
        if own_line:
            out = _drop_through(out, own_line[-1])
    out = _THINK_BLOCK.sub("", out)
    closes = list(_CLOSE.finditer(out))
    if closes:
        out = _drop_through(out, closes[-1])
    if _LEADING_OPEN.match(out):
        out = ""
    return out.strip() if out != text else text


class LocalProviderUnavailable(NonRetryableLLMError):
    """The local server refused the connection: not retried, the message says what to do."""


_REFUSED_NAMES = frozenset({"ConnectionRefusedError", "ConnectError"})
MAX_CAUSE_DEPTH = 10


def _connection_refused(exc: BaseException) -> bool:
    """True when the ``__cause__``/``__context__`` chain holds ``ConnectionRefusedError`` or
    ``httpx.ConnectError`` (the server is not listening). Bounded walk, cycle-safe."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack and len(seen) < MAX_CAUSE_DEPTH:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if {c.__name__ for c in type(cur).__mro__} & _REFUSED_NAMES:
            return True
        stack.extend(e for e in (cur.__cause__, cur.__context__) if e is not None)
    return False


def map_openai_error(exc: BaseException, base_url: str, model: str) -> Exception:
    """OpenAI SDK error -> the wrapper's failure classes (matched by class name and status).

    A connection error is fatal only when the server refused it (nothing listening); any other
    connection failure (a reset mid-request, say) is transient, still bounded by the wrapper."""
    if isinstance(exc, (TransientLLMError, NonRetryableLLMError)):
        return exc
    names = {c.__name__ for c in type(exc).__mro__}
    if "APITimeoutError" in names:
        return TransientLLMError("APITimeoutError")
    if "APIConnectionError" in names:
        if _connection_refused(exc):
            return LocalProviderUnavailable(
                f"LM Studio is not reachable at {redact_url(base_url)}; "
                f"start the server and load {model}."
            )
        return TransientLLMError("APIConnectionError")
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status in TRANSIENT_STATUS:
            return TransientLLMError(f"http_{status}")
        if status == 404:
            return NonRetryableLLMError(
                f"Model {model} is not loaded in LM Studio at {redact_url(base_url)} (http_404)."
            )
        return NonRetryableLLMError(f"http_{status}")
    return classify_error(exc)


class LocalChatModel:
    """The subset of the LangChain chat-model interface the adapters use: ``invoke`` and
    ``bind_tools``. Wraps ``ChatOpenAI`` (or a bound runnable) to map errors and strip reasoning."""

    def __init__(self, inner: Any, *, base_url: str, model: str) -> None:
        self._inner = inner
        self._base_url = base_url
        self._model = model
        self._warned = False

    def bind_tools(self, tools: Sequence[Any]) -> LocalChatModel:
        tools = list(tools)
        if not tools:  # an empty `tools` list is rejected by some OpenAI-compatible servers
            return self
        return LocalChatModel(
            self._inner.bind_tools(tools), base_url=self._base_url, model=self._model
        )

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        try:
            msg = self._inner.invoke(messages, **kwargs)
        except Exception as exc:
            mapped = map_openai_error(exc, self._base_url, self._model)
            if isinstance(mapped, LocalProviderUnavailable) and not self._warned:
                self._warned = True
                log.warning("%s", mapped)
            raise mapped from None
        return clean_message(msg)


def clean_message(msg: Any) -> Any:
    """Strip reasoning from an AIMessage-like object: ``<think>`` blocks in the content and any
    reasoning field in ``additional_kwargs``. Tool calls and usage are kept."""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        new_content: Any = strip_reasoning(content)
    elif isinstance(content, list):
        new_content = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("reasoning", "thinking"):
                continue
            if isinstance(part, str):
                part = strip_reasoning(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                part = {**part, "text": strip_reasoning(part["text"])}
            new_content.append(part)
    else:
        return msg
    extra = dict(getattr(msg, "additional_kwargs", None) or {})
    for k in REASONING_KEYS:
        extra.pop(k, None)
    copy = getattr(msg, "model_copy", None)
    if callable(copy):
        return copy(update={"content": new_content, "additional_kwargs": extra})
    return msg


def build_local_chat_model(
    model: str,
    base_url: str,
    *,
    http_client: Any = None,
    reasoning_effort: str | None = LOCAL_REASONING_EFFORT,
) -> LocalChatModel:
    """``ChatOpenAI`` against the local server. In openai, ``max_retries=0`` means the initial
    request only. Construction makes no network call. ``http_client`` is a test seam."""
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": LOCAL_API_KEY,
        "max_retries": 0,
        "timeout": LOCAL_CHAT_TIMEOUT_S,
        "use_responses_api": False,  # LM Studio serves /chat/completions
    }
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    if http_client is not None:
        kwargs["http_client"] = http_client
    inner = ChatOpenAI(**kwargs)
    return LocalChatModel(inner, base_url=base_url, model=model)


def chat_model_for(settings: Any, model: str) -> Any:
    """The chat model for ``model`` under the configured provider (Gemini path unchanged)."""
    if is_local(settings):
        return build_local_chat_model(model, settings.llm_base_url)
    cfg = next((r for r in settings.roles.values() if r.model == model), None)
    return build_chat_model(
        model,
        settings.gemini_api_key,
        thinking_level=getattr(cfg, "thinking_level", None),
        thinking_budget=getattr(cfg, "thinking_budget", None),
    )


class OpenAICompatEmbedder:
    """Embedder over ``/v1/embeddings``. Lazy client, one attempt, bounded timeout, no retry.
    A vector of the wrong size raises, so it never reaches the store."""

    def __init__(
        self,
        base_url: str,
        model: str,
        dimensionality: int,
        *,
        timeout_s: float = LOCAL_EMBED_TIMEOUT_S,
        client: Any = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._dim = dimensionality
        self._timeout_s = timeout_s
        self._client = client

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self._base_url,
                api_key=LOCAL_API_KEY,
                max_retries=0,
                timeout=self._timeout_s,
            )
        resp = self._client.embeddings.create(model=self._model, input=list(texts))
        data = sorted(resp.data, key=lambda d: getattr(d, "index", 0))
        vecs = [list(d.embedding) for d in data]
        for v in vecs:
            if len(v) != self._dim:
                raise ValueError(
                    f"embedding model {self._model} returned {len(v)} dimensions, "
                    f"expected {self._dim}"
                )
        return vecs


def build_embedder(settings: Any) -> Any:
    model, dim = settings.embedding_model, settings.embedding_dimensionality
    if is_local(settings):
        return OpenAICompatEmbedder(settings.llm_base_url, model, dim)
    from opsfleet_agent.golden.seed import GenaiEmbedder

    return GenaiEmbedder(settings.gemini_api_key, model, dim)


def embedding_prefixes(settings: Any) -> tuple[str, str]:
    """(query prefix, document prefix); empty for Gemini."""
    if is_local(settings):
        return LOCAL_QUERY_PREFIX, LOCAL_DOCUMENT_PREFIX
    return "", ""


def golden_cache_dir(settings: Any, cache_dir: Path | None) -> Path | None:
    """Gemini keeps ``cache_dir``; the local provider uses a subdirectory, so switching provider
    neither mixes vectors nor evicts the Gemini cache."""
    if cache_dir is None or not is_local(settings):
        return cache_dir
    return Path(cache_dir) / LOCAL_CACHE_SUBDIR
