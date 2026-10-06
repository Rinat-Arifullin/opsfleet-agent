"""RC-1: the startup ADC + BigQuery dry-run check. Fakes only, no network."""

from __future__ import annotations

from typing import Any

import pytest
from google.api_core import exceptions as gexc
from google.auth import exceptions as auth_exc

from opsfleet_agent.bq.preflight import PREFLIGHT_SQL, bq_startup_check
from opsfleet_agent.config import ConfigError


class FakeClient:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    def query(self, query: str, job_config: Any = None, **kwargs: Any) -> Any:
        self.calls.append((query, job_config, kwargs))
        if self.error is not None:
            raise self.error
        return object()


DRY = {"dry_run": True}


def _check(client: FakeClient, project: str = "my-proj") -> Any:
    return bq_startup_check(project, lambda _p: client, job_config_factory=lambda: DRY)


def test_ok_returns_the_client_after_one_bounded_dry_run() -> None:
    client = FakeClient()
    assert _check(client) is client
    [(sql, config, kwargs)] = client.calls
    assert sql == PREFLIGHT_SQL and config is DRY
    assert kwargs["timeout"] > 0


def test_default_job_config_is_a_dry_run() -> None:
    client = FakeClient()
    bq_startup_check("my-proj", lambda _p: client)
    config = client.calls[0][1]
    assert config.dry_run is True and config.use_query_cache is False


def test_missing_adc_while_building_the_client() -> None:
    def make(_project: str) -> Any:
        raise auth_exc.DefaultCredentialsError("Your default credentials were not found")

    with pytest.raises(ConfigError, match="gcloud auth application-default login"):
        bq_startup_check("my-proj", make)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (auth_exc.RefreshError("expired"), "application-default login"),
        (gexc.Forbidden("Access Denied: User a@b.c lacks bigquery.jobs.create"), "jobUser"),
        (gexc.Forbidden("BigQuery API has not been used in project 1"), "gcloud services enable"),
        (gexc.NotFound("Not found: Project my-proj"), "Check GOOGLE_CLOUD_PROJECT"),
        (gexc.ServiceUnavailable("backend"), "network problem"),
        (ConnectionError("reset"), "network problem"),
        (RuntimeError("boom"), "RuntimeError"),
    ],
)
def test_dry_run_failure_is_one_actionable_line(error: BaseException, expected: str) -> None:
    with pytest.raises(ConfigError) as info:
        _check(FakeClient(error))
    msg = str(info.value)
    assert expected in msg and "\n" not in msg
    assert "a@b.c" not in msg  # provider text (which can name the account) is dropped
    assert info.value.__cause__ is None
