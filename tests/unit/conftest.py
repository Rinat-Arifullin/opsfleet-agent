import socket

import pytest


@pytest.fixture(autouse=True)
def _block_network(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return

    def _blocked(*args, **kwargs):
        raise RuntimeError("network access is blocked in unit tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


@pytest.fixture(autouse=True)
def _default_llm_provider(monkeypatch):
    """D-143: a developer's local-provider env never leaks into unit tests."""
    monkeypatch.delenv("OPSFLEET_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPSFLEET_LLM_BASE_URL", raising=False)
    for name in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
        "LANGFUSE_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
