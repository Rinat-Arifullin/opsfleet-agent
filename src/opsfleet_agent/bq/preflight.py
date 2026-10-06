"""BigQuery startup check: ADC is present and a dry run in ``GOOGLE_CLOUD_PROJECT`` works.

Runs once at CLI startup, before the first prompt, so a missing login, a missing role or a
disabled API is one actionable line instead of a failure in the middle of the first question.
The dry run bills nothing and executes nothing. Provider error text is used only to pick the
message and is then dropped (it can name the account); the project id comes from config.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from opsfleet_agent.bq.client import DEFAULT_API_TIMEOUT_S, WarehouseClient, bounded_retry
from opsfleet_agent.config import ConfigError

PREFLIGHT_SQL = "SELECT order_id FROM `bigquery-public-data.thelook_ecommerce.orders` LIMIT 1"

_AUTH_TYPES = {"DefaultCredentialsError", "RefreshError", "Unauthorized"}
_NETWORK_TYPES = {"ServiceUnavailable", "DeadlineExceeded", "TransportError", "RetryError"}


def _names(exc: BaseException) -> set[str]:
    return {t.__name__ for t in type(exc).__mro__}


def preflight_message(exc: BaseException, project: str) -> str:
    """One actionable line for a failed client build or dry run."""
    names = _names(exc)
    text = str(exc)
    if names & _AUTH_TYPES:
        return (
            "Google credentials (ADC) are missing or expired. "
            "Run: gcloud auth application-default login. See README → Setup."
        )
    if "Forbidden" in names:
        if "has not been used" in text or "SERVICE_DISABLED" in text:
            return (
                f"The BigQuery API is not enabled in project {project}. "
                f"Run: gcloud services enable bigquery.googleapis.com --project {project}."
            )
        return (
            f"BigQuery refused a dry run in project {project}: the account needs "
            "roles/bigquery.jobUser (or roles/bigquery.user) there. See README → Setup."
        )
    if "NotFound" in names or "BadRequest" in names:
        return (
            f"BigQuery could not use project {project}. "
            "Check GOOGLE_CLOUD_PROJECT (the project id, not its name). See README → Setup."
        )
    if names & _NETWORK_TYPES or isinstance(exc, OSError | TimeoutError):
        return "Could not reach BigQuery (network problem). Check connectivity and retry."
    return f"BigQuery startup check failed ({type(exc).__name__}). See README → Setup."


def _dry_run_config() -> Any:
    from google.cloud import bigquery

    return bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)


def bq_startup_check(
    project: str,
    make_client: Callable[[str], WarehouseClient],
    *,
    job_config_factory: Callable[[], Any] = _dry_run_config,
    timeout_s: float = DEFAULT_API_TIMEOUT_S,
) -> WarehouseClient:
    """Build the client (resolves ADC) and dry-run one query; return the client.

    Raises ``ConfigError`` with one line on any failure. The retry is bounded by ``timeout_s``.
    """
    try:
        client = make_client(project)
        client.query(
            PREFLIGHT_SQL,
            job_config=job_config_factory(),
            timeout=timeout_s,
            retry=bounded_retry(timeout_s),
        )
    except Exception as exc:
        raise ConfigError(preflight_message(exc, project)) from None
    return client
