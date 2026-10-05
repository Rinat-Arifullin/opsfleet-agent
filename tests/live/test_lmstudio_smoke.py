"""Live smoke for the dev-only local provider (D-143). Needs LM Studio running with the models
from ``local:`` in config/models.yaml loaded.
Run: ``uv run pytest -m live tests/live/test_lmstudio_smoke.py -q``.

Deselected by default (``addopts = -m 'not live'``); skipped when LM Studio is not reachable.
Uses ``OPSFLEET_LLM_BASE_URL`` when set. No Gemini key and no BigQuery are needed.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from opsfleet_agent import config
from opsfleet_agent.graph import providers as P

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv(config.PROVIDER_ENV, config.LMSTUDIO)
    mp.setenv("GOOGLE_CLOUD_PROJECT", "unused-for-this-smoke")
    try:
        s = config.load_settings(dotenv=False)
        try:
            config.lmstudio_model_lister(s.llm_base_url)()
        except Exception:
            pytest.skip(f"LM Studio not reachable at {s.llm_base_url}")
        yield config.startup_check(dotenv=False)
    finally:
        mp.undo()


def test_local_chat_answers_without_reasoning(settings):
    m = P.chat_model_for(settings, settings.roles["router"].model)
    msg = m.invoke([HumanMessage("Reply with the single word: pong")], timeout=60)
    assert isinstance(msg.content, str) and msg.content.strip()
    assert "<think>" not in msg.content.lower() and "</think>" not in msg.content.lower()


def test_local_embeddings_have_configured_dimension(settings):
    vecs = P.build_embedder(settings).embed(["search_query: monthly revenue"])
    assert len(vecs) == 1 and len(vecs[0]) == settings.embedding_dimensionality
