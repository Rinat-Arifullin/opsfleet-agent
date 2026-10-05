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

# D-143: dev-only local LLM provider. Gemini stays the default; unset means unchanged behaviour.
PROVIDER_ENV = "OPSFLEET_LLM_PROVIDER"
BASE_URL_ENV = "OPSFLEET_LLM_BASE_URL"
GEMINI = "gemini"
LMSTUDIO = "lmstudio"
PROVIDERS = (GEMINI, LMSTUDIO)
# 127.0.0.1, not localhost: avoids IPv6 localhost resolution issues on macOS.
MAX_EMBEDDING_DIM = 8192  # sanity bound for local.embedding_dim
DEFAULT_LMSTUDIO_BASE_URL = "http://127.0.0.1:1234/v1"


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
    quota_llm_per_hour: int = 300  # per user, iteration 24 (D-138)
    quota_llm_per_day: int = 2000
    quota_bq_bytes_per_day: int = 100_000_000_000
    llm_provider: str = GEMINI
    llm_base_url: str = ""

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


QUOTA_KEYS = ("llm_per_hour", "llm_per_day", "bq_bytes_per_day")
QUOTA_DEFAULTS = {"llm_per_hour": 300, "llm_per_day": 2000, "bq_bytes_per_day": 100_000_000_000}


def parse_quota(path: Path) -> dict[str, int]:
    """Optional `quota:` mapping (D-138): positive ints; an unknown key is an error."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise ConfigError(
            f"{path.name} cannot be read or is not valid YAML. Fix it or restore it from git."
        ) from None
    section = (raw or {}).get("quota")
    if section is None:
        return dict(QUOTA_DEFAULTS)
    if not isinstance(section, dict):
        raise ConfigError(f"{path.name} is invalid: 'quota' must be a mapping.")
    out = dict(QUOTA_DEFAULTS)
    for key, value in section.items():
        if key not in QUOTA_KEYS:
            raise ConfigError(
                f"{path.name} is invalid: unknown quota key '{key}' "
                f"(allowed: {', '.join(QUOTA_KEYS)})."
            )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"{path.name} is invalid: quota.{key} must be a positive integer.")
        out[key] = value
    return out


def parse_local_section(path: Path) -> tuple[str, str, int]:
    """`local.chat_model`, `local.embedding_model` and `local.embedding_dim` (D-143). Read only
    under lmstudio, so a missing `local:` section never affects the Gemini path. Model ids are
    pure config values."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise ConfigError(
            f"{path.name} cannot be read or is not valid YAML. Fix it or restore it from git."
        ) from None
    local = (raw or {}).get("local") if isinstance(raw, dict) else None
    if not isinstance(local, dict):
        raise ConfigError(
            f"{path.name} has no 'local' section; {PROVIDER_ENV}={LMSTUDIO} needs "
            "local.chat_model, local.embedding_model and local.embedding_dim."
        )
    chat, emb = local.get("chat_model"), local.get("embedding_model")
    if not isinstance(chat, str) or not chat.strip() or not isinstance(emb, str) or not emb.strip():
        raise ConfigError(
            f"{path.name} is invalid: local.chat_model and local.embedding_model must be "
            "non-empty strings."
        )
    dim = local.get("embedding_dim")
    if isinstance(dim, bool) or not isinstance(dim, int) or not 1 <= dim <= MAX_EMBEDDING_DIM:
        raise ConfigError(
            f"{path.name} is invalid: local.embedding_dim must be a positive integer "
            f"(1..{MAX_EMBEDDING_DIM})."
        )
    return chat.strip(), emb.strip(), dim


def redact_url(url: str) -> str:
    """The URL with any userinfo (``user:pass@``) removed, for error and log messages.

    Works on the raw string, not ``urlsplit``: a password containing ``/`` makes ``urlsplit``
    put part of the userinfo into the path. Everything from ``//`` through the last ``@`` goes;
    an ``@`` in a path would over-redact, which is harmless in a message."""
    if not isinstance(url, str):
        return "<invalid url>"
    scheme_end = url.find("//")
    if scheme_end < 0 or "@" not in url[scheme_end + 2 :]:
        return url
    return url[: scheme_end + 2] + url[url.rindex("@") + 1 :]


def _provider_from_env() -> tuple[str, str]:
    provider = os.environ.get(PROVIDER_ENV, "").strip().lower() or GEMINI
    if provider not in PROVIDERS:
        raise ConfigError(f"{PROVIDER_ENV} must be one of: {', '.join(PROVIDERS)}.")
    if provider == GEMINI:
        return provider, ""
    url = os.environ.get(BASE_URL_ENV, "").strip() or DEFAULT_LMSTUDIO_BASE_URL
    if not url.startswith(("http://", "https://")):
        raise ConfigError(
            f"{BASE_URL_ENV} must be an http(s) URL, e.g. {DEFAULT_LMSTUDIO_BASE_URL}."
        )
    return provider, url.rstrip("/")


def load_settings(models_path: Path | None = None, *, dotenv: bool = True) -> Settings:
    """Load settings. Raises ConfigError (one line, no secret values) on the first failure."""
    if dotenv:
        load_dotenv()
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        raise ConfigError("GOOGLE_CLOUD_PROJECT is not set. See README → Setup.")
    provider, base_url = _provider_from_env()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key and provider == GEMINI:
        raise ConfigError("GEMINI_API_KEY is not set. See README → Setup.")
    path = models_path or default_models_path()
    roles, emb_model, emb_dim, limits, fraction = parse_models_yaml(path)
    small_cell_k, retry_delay_s = parse_tunables(path)
    quota = parse_quota(path)
    if provider == LMSTUDIO:
        # Every role maps to the one local chat model: no fallback (it would be the same model),
        # no Gemini thinking params, no free-tier limiter.
        chat_model, emb_model, emb_dim = parse_local_section(path)
        roles = {name: RoleModel(model=chat_model) for name in roles}
        limits = {}
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
        quota_llm_per_hour=quota["llm_per_hour"],
        quota_llm_per_day=quota["llm_per_day"],
        quota_bq_bytes_per_day=quota["bq_bytes_per_day"],
        llm_provider=provider,
        llm_base_url=base_url,
    )


def genai_model_lister(api_key: str) -> ModelLister:
    """Real lister: google-genai models.list. Network; never used in unit tests."""

    def _list() -> list[str]:
        from google import genai

        client = genai.Client(api_key=api_key)
        return [m.name.removeprefix("models/") for m in client.models.list() if m.name]

    return _list


LMSTUDIO_LIST_TIMEOUT_S = 5.0


def lmstudio_model_lister(base_url: str, timeout_s: float = LMSTUDIO_LIST_TIMEOUT_S) -> ModelLister:
    """Real lister: GET <base_url>/models on the OpenAI-compatible server. Network; one attempt."""

    def _list() -> list[str]:
        import json
        import urllib.request

        req = urllib.request.Request(f"{base_url}/models", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (http(s) only)
            body = json.loads(resp.read().decode("utf-8"))
        return [str(m["id"]) for m in body.get("data", []) if isinstance(m, dict) and m.get("id")]

    return _list


MAX_LISTED_IDS = 20


def _lmstudio_check(settings: Settings, lister: ModelLister | None) -> Settings:
    url = redact_url(settings.llm_base_url)
    chat_model = settings.roles["router"].model
    list_models = lister or lmstudio_model_lister(settings.llm_base_url)
    try:
        available = [str(m) for m in list_models()]
    except Exception:
        raise ConfigError(
            f"LM Studio is not reachable at {url}; start the server and load {chat_model}."
        ) from None
    shown = ", ".join(available[:MAX_LISTED_IDS]) or "none"
    if len(available) > MAX_LISTED_IDS:
        shown += f", ... ({len(available) - MAX_LISTED_IDS} more)"
    for model_id in settings.configured_model_ids():
        if model_id not in available:
            raise ConfigError(
                f"Model {model_id} is not loaded in LM Studio at {url}. "
                f"LM Studio reports: {shown}. Load it, or copy one of these ids into "
                "local.chat_model / local.embedding_model in config/models.yaml."
            )
    return settings


def startup_check(
    lister: ModelLister | None = None,
    models_path: Path | None = None,
    *,
    dotenv: bool = True,
) -> Settings:
    """Stop at the first failure with one actionable ConfigError line."""
    settings = load_settings(models_path, dotenv=dotenv)
    if settings.llm_provider == LMSTUDIO:
        return _lmstudio_check(settings, lister)
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
