"""Settings, models.yaml validation and the startup check.

This is the only module that reads the environment.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROLES = (
    "router",
    "light_path",
    "quick_analyst",
    "deep_analyst",
    "report_writer",
    "report_verifier",
    "library_agent",
    "summary",
    "judge",
    "fallback",
)
THINKING_KEYS = ("thinking_level", "thinking_budget")
# Only these keys may ever be logged. Secrets (GEMINI_API_KEY) are never in this list.
SAFE_KEYS = ("google_cloud_project", "models_path", "limiter_fraction")

ModelLister = Callable[[], Iterable[str]]


class ConfigError(Exception):
    """Raised with one actionable line; never contains secret values."""


@dataclass(frozen=True)
class ModelLimits:
    rpm: int
    rpd: int
    tpm: int


@dataclass(frozen=True)
class RoleModel:
    model: str
    fallback: str | None = None
    thinking_level: str | None = None
    thinking_budget: int | None = None


@dataclass(frozen=True)
class Settings:
    google_cloud_project: str
    gemini_api_key: str = field(repr=False)
    models_path: Path
    roles: dict[str, RoleModel]
    embedding_model: str
    embedding_dimensionality: int
    limits: dict[str, ModelLimits]
    limiter_fraction: float
    small_cell_k: int = 5
    bq_unavailable_retry_delay_s: float = 2.0

    def configured_model_ids(self) -> list[str]:
        ids: list[str] = []
        for r in self.roles.values():
            for m in (r.model, r.fallback):
                if m and m not in ids:
                    ids.append(m)
        if self.embedding_model not in ids:
            ids.append(self.embedding_model)
        return ids


def default_models_path() -> Path:
    override = os.environ.get("OPSFLEET_MODELS_YAML")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "config" / "models.yaml"


def safe_config_view(settings: Settings) -> dict[str, Any]:
    """Allowlisted, non-secret view of the settings, the only form that may be logged."""
    return {k: str(getattr(settings, k)) for k in SAFE_KEYS}


def parse_models_yaml(
    path: Path,
) -> tuple[dict[str, RoleModel], str, int, dict[str, ModelLimits], float]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise ConfigError(
            f"{path.name} cannot be read or is not valid YAML. Fix it or restore it from git."
        ) from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} is invalid: expected a mapping at the top level.")
    try:
        roles_raw = raw["roles"]
        roles: dict[str, RoleModel] = {}
        for name in ROLES:
            if name not in roles_raw:
                raise ConfigError(f"{path.name} is invalid: role '{name}' is missing.")
            entry = roles_raw[name]
            if not isinstance(entry, dict) or not entry.get("model"):
                raise ConfigError(f"{path.name} is invalid: role '{name}' needs a 'model'.")
            if all(entry.get(k) is not None for k in THINKING_KEYS):
                raise ConfigError(
                    f"{path.name} is invalid: role '{name}' sets both thinking_level and "
                    "thinking_budget. Set only one."
                )
            roles[name] = RoleModel(
                model=str(entry["model"]),
                fallback=entry.get("fallback"),
                thinking_level=entry.get("thinking_level"),
                thinking_budget=entry.get("thinking_budget"),
            )
        emb = raw["embedding"]
        lim = raw["limits"]
        limits = {
            str(m): ModelLimits(rpm=int(v["rpm"]), rpd=int(v["rpd"]), tpm=int(v["tpm"]))
            for m, v in lim["models"].items()
        }
        return (
            roles,
            str(emb["model"]),
            int(emb["output_dimensionality"]),
            limits,
            float(lim["limiter_fraction"]),
        )
    except ConfigError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ConfigError(
            f"{path.name} is invalid: a required section or field is missing or malformed."
        ) from None


SMALL_CELL_K_RANGE = (2, 1_000)
RETRY_DELAY_RANGE_S = (0.0, 30.0)


def parse_tunables(path: Path) -> tuple[int, float]:
    """Optional `policy.small_cell_k` and `bq.unavailable_retry_delay_s` (HLD 4.0.7)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise ConfigError(
            f"{path.name} cannot be read or is not valid YAML. Fix it or restore it from git."
        ) from None
    policy = (raw or {}).get("policy") or {}
    bq = (raw or {}).get("bq") or {}
    if not isinstance(policy, dict) or not isinstance(bq, dict):
        raise ConfigError(f"{path.name} is invalid: 'policy' and 'bq' must be mappings.")
    k = policy.get("small_cell_k", 5)
    lo, hi = SMALL_CELL_K_RANGE
    if isinstance(k, bool) or not isinstance(k, int) or not lo <= k <= hi:
        raise ConfigError(
            f"{path.name} is invalid: policy.small_cell_k must be an integer {lo}..{hi}."
        )
    delay = bq.get("unavailable_retry_delay_s", 2.0)
    dlo, dhi = RETRY_DELAY_RANGE_S
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not dlo <= delay <= dhi:
        raise ConfigError(
            f"{path.name} is invalid: bq.unavailable_retry_delay_s must be a number {dlo}..{dhi}."
        )
    return k, float(delay)


def load_settings(models_path: Path | None = None, *, dotenv: bool = True) -> Settings:
    """Load settings. Raises ConfigError (one line, no secret values) on the first failure."""
    if dotenv:
        load_dotenv()
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        raise ConfigError("GOOGLE_CLOUD_PROJECT is not set. See README → Setup.")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise ConfigError("GEMINI_API_KEY is not set. See README → Setup.")
    path = models_path or default_models_path()
    roles, emb_model, emb_dim, limits, fraction = parse_models_yaml(path)
    small_cell_k, retry_delay_s = parse_tunables(path)
    return Settings(
        google_cloud_project=project,
        gemini_api_key=key,
        models_path=path,
        roles=roles,
        embedding_model=emb_model,
        embedding_dimensionality=emb_dim,
        limits=limits,
        limiter_fraction=fraction,
        small_cell_k=small_cell_k,
        bq_unavailable_retry_delay_s=retry_delay_s,
    )


def genai_model_lister(api_key: str) -> ModelLister:
    """Real lister: google-genai models.list. Network; never used in unit tests."""

    def _list() -> list[str]:
        from google import genai

        client = genai.Client(api_key=api_key)
        return [m.name.removeprefix("models/") for m in client.models.list() if m.name]

    return _list


def startup_check(
    lister: ModelLister | None = None,
    models_path: Path | None = None,
    *,
    dotenv: bool = True,
) -> Settings:
    """Stop at the first failure with one actionable ConfigError line."""
    settings = load_settings(models_path, dotenv=dotenv)
    list_models = lister or genai_model_lister(settings.gemini_api_key)
    try:
        available = {m.removeprefix("models/") for m in list_models()}
    except Exception:
        raise ConfigError(
            "Could not list Gemini models (network or key problem). "
            "Check connectivity and GEMINI_API_KEY. See README → Setup."
        ) from None
    for model_id in settings.configured_model_ids():
        if model_id not in available:
            raise ConfigError(
                f"Model {model_id} is not available to this key. "
                "Set a listed model in config/models.yaml."
            )
    return settings
