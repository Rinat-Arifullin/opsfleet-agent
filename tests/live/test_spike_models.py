"""Live spike (iteration 2). Run ONCE by the owner: ``uv run pytest -m live tests/live -q``.

Deselected by default (``addopts = -m 'not live'``). Not under ``tests/unit``, so the unit socket
block does not apply. One tiny generation call per distinct configured generation model id, and one
embedding call. Needs ``GEMINI_API_KEY`` in the environment or in ``.env``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from dotenv import load_dotenv
from google import genai
from google.genai import types

pytestmark = pytest.mark.live

MODELS_YAML = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


def _config() -> dict:
    return yaml.safe_load(MODELS_YAML.read_text())


def _generation_model_ids() -> list[str]:
    ids: set[str] = set()
    for role in _config()["roles"].values():
        ids.add(role["model"])
        if role.get("fallback"):
            ids.add(role["fallback"])
    return sorted(ids)


@pytest.fixture(scope="module")
def client() -> genai.Client:
    load_dotenv()
    return genai.Client()  # reads GEMINI_API_KEY from the environment


@pytest.mark.parametrize("model_id", _generation_model_ids())
def test_generation_model_id_answers(client, model_id):
    resp = client.models.generate_content(
        model=model_id,
        contents="Reply with the single word: ok",
        config=types.GenerateContentConfig(max_output_tokens=256),
    )
    assert resp.text and resp.text.strip()


def test_embedding_model_returns_768_dimensions(client):
    emb = _config()["embedding"]
    resp = client.models.embed_content(
        model=emb["model"],
        contents="spike",
        config=types.EmbedContentConfig(output_dimensionality=emb["output_dimensionality"]),
    )
    assert emb["output_dimensionality"] == 768
    assert len(resp.embeddings[0].values) == 768
