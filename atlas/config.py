"""Configuration: research-mode policies, user settings, secrets (env only).

Non-secret settings live in <data_dir>/settings.json. Secrets (API keys) are
read ONLY from environment variables and are never written to disk or returned
by the API (only a masked hint is exposed).
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    d = Path(os.getenv("ATLAS_DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Research modes: real policies, not labels.
# ---------------------------------------------------------------------------
MODES: dict[str, dict] = {
    "light": {
        "label": "Light", "min_sources": 3, "target_sources": 5, "max_sources": 5,
        "queries_per_round": 1, "max_rounds": 1, "subquestions": False,
        "follow_citations": False, "cross_check": False, "fetch_pages": 4,
        "providers": ["web"], "results_per_query": 6, "time_budget_s": 90,
        "max_retries": 1, "concurrency": 2, "llm_context_chars": 12000,
        "excerpt_chars": 2000, "report_style": "concise",
    },
    "medium": {
        "label": "Medium", "min_sources": 10, "target_sources": 15, "max_sources": 20,
        "queries_per_round": 4, "max_rounds": 1, "subquestions": False,
        "follow_citations": False, "cross_check": False, "fetch_pages": 15,
        "providers": ["web", "docs", "scholar"], "results_per_query": 8,
        "time_budget_s": 300, "max_retries": 2, "concurrency": 3,
        "llm_context_chars": 30000, "excerpt_chars": 2500, "report_style": "brief",
    },
    "high": {
        "label": "High", "min_sources": 30, "target_sources": 40, "max_sources": 50,
        "queries_per_round": 8, "max_rounds": 2, "subquestions": True,
        "follow_citations": True, "cross_check": False, "fetch_pages": 40,
        "providers": ["web", "docs", "scholar", "news"], "results_per_query": 10,
        "time_budget_s": 900, "max_retries": 2, "concurrency": 4,
        "llm_context_chars": 60000, "excerpt_chars": 3000, "report_style": "detailed",
    },
    "max": {
        "label": "Max", "min_sources": 50, "target_sources": 75, "max_sources": 100,
        "queries_per_round": 12, "max_rounds": 3, "subquestions": True,
        "follow_citations": True, "cross_check": True, "fetch_pages": 80,
        "providers": ["web", "docs", "scholar", "news"], "results_per_query": 10,
        "time_budget_s": 1800, "max_retries": 3, "concurrency": 4,
        "llm_context_chars": 90000, "excerpt_chars": 3000, "report_style": "comprehensive",
    },
}

# Hard ceilings so settings can never make a run unbounded.
HARD_LIMITS = {"max_sources": 150, "time_budget_s": 3600, "fetch_pages": 150,
               "concurrency": 8, "max_rounds": 5, "llm_context_chars": 200000}

DEFAULT_SETTINGS: dict = {
    "local_base_url": "http://127.0.0.1:8080/v1",
    "local_model": "",
    "default_provider": "hybrid",          # local | hybrid | openai | browser
    "allow_external_providers": False,      # must be turned on explicitly
    "openai_model": "gpt-4o-mini",
    "openai_base_url": "https://api.openai.com/v1",
    "search_providers": {                    # adapter -> enabled
        "duckduckgo": True, "openalex": True, "crossref": True, "arxiv": True,
        "wikipedia": True, "gdelt": True, "searxng": False, "brave": False,
    },
    "searxng_url": "",
    "mode_overrides": {},                   # {"max": {"time_budget_s": 600}}
    "fetch": {"max_bytes": 2_000_000, "max_redirects": 4, "timeout_s": 12,
              "cache_ttl_hours": 72, "respect_robots": True,
              "user_agent": "ATLASResearch/1.0 (local research tool)"},
    "storage": {"max_text_per_source": 60000, "backup_keep": 10},
    "onion": {"enabled": False, "socks_url": "socks5h://127.0.0.1:9050",
              "allowlist": [], "max_bytes": 500_000, "timeout_s": 45},
}

SECRET_ENV = {"openai": "OPENAI_API_KEY", "brave": "BRAVE_API_KEY"}

_lock = threading.Lock()


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k == "mode_overrides" and isinstance(v, dict):      # free-form mapping: take as stored
            out[k] = copy.deepcopy(v)
        elif k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> dict:
    p = settings_path()
    stored = {}
    if p.exists():
        try:
            stored = json.loads(p.read_text())
        except (OSError, ValueError):
            stored = {}
    s = _merge(DEFAULT_SETTINGS, stored if isinstance(stored, dict) else {})
    env_base = os.getenv("ATLAS_LOCAL_BASE_URL")
    if env_base and "local_base_url" not in stored:
        s["local_base_url"] = env_base
    env_model = os.getenv("ATLAS_LOCAL_MODEL")
    if env_model and "local_model" not in stored:
        s["local_model"] = env_model
    s["local_base_url"] = str(s["local_base_url"]).rstrip("/")
    return s


class SettingsError(ValueError):
    pass


def validate_settings_patch(patch: dict) -> dict:
    """Validate and coerce a user settings patch. Unknown keys are rejected."""
    if not isinstance(patch, dict):
        raise SettingsError("Settings must be an object.")
    clean: dict = {}
    for k, v in patch.items():
        if k not in DEFAULT_SETTINGS:
            raise SettingsError(f"Unknown setting: {k}")
        default = DEFAULT_SETTINGS[k]
        if isinstance(default, bool):
            if not isinstance(v, bool):
                raise SettingsError(f"{k} must be true or false.")
        elif isinstance(default, str):
            if not isinstance(v, str) or len(v) > 500:
                raise SettingsError(f"{k} must be a short string.")
            v = v.strip()
            if k in ("local_base_url", "openai_base_url", "searxng_url") and v:
                if not v.startswith(("http://", "https://")):
                    raise SettingsError(f"{k} must start with http:// or https://")
            if k == "default_provider" and v not in ("local", "hybrid", "openai", "browser"):
                raise SettingsError("default_provider must be local, hybrid, openai or browser.")
        elif isinstance(default, dict):
            if not isinstance(v, dict):
                raise SettingsError(f"{k} must be an object.")
            if k == "mode_overrides":
                v = _clean_overrides(v)
            elif k == "search_providers":
                for name, on in v.items():
                    if name not in default or not isinstance(on, bool):
                        raise SettingsError(f"Bad search provider entry: {name}")
            else:
                for sub, sv in v.items():
                    if sub not in default:
                        raise SettingsError(f"Unknown setting: {k}.{sub}")
                    if isinstance(default[sub], bool) and not isinstance(sv, bool):
                        raise SettingsError(f"{k}.{sub} must be true or false.")
                    if isinstance(default[sub], int) and not isinstance(default[sub], bool):
                        if not isinstance(sv, int) or isinstance(sv, bool) or sv < 0 or sv > 50_000_000:
                            raise SettingsError(f"{k}.{sub} must be a non-negative integer.")
                    if isinstance(default[sub], list):
                        if not isinstance(sv, list) or not all(isinstance(i, str) and len(i) < 200 for i in sv):
                            raise SettingsError(f"{k}.{sub} must be a list of strings.")
                    if isinstance(default[sub], str) and not isinstance(sv, str):
                        raise SettingsError(f"{k}.{sub} must be a string.")
        clean[k] = v
    return clean


def _clean_overrides(v: dict) -> dict:
    out: dict = {}
    for mode, ov in v.items():
        if mode not in MODES or not isinstance(ov, dict):
            raise SettingsError(f"Bad mode override: {mode}")
        out[mode] = {}
        for key, val in ov.items():
            if key not in HARD_LIMITS:
                raise SettingsError(f"Mode override {mode}.{key} is not adjustable.")
            if not isinstance(val, int) or isinstance(val, bool) or val < 1:
                raise SettingsError(f"{mode}.{key} must be a positive integer.")
            out[mode][key] = min(val, HARD_LIMITS[key])
    return out


def save_settings(patch: dict) -> dict:
    clean = validate_settings_patch(patch)
    with _lock:
        p = settings_path()
        try:
            stored = json.loads(p.read_text()) if p.exists() else {}
        except (OSError, ValueError):
            stored = {}
        for k, v in clean.items():
            if isinstance(v, dict) and isinstance(stored.get(k), dict) and k != "mode_overrides":
                stored[k].update(v)
            else:
                stored[k] = v
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(stored, indent=2))
        os.replace(tmp, p)
    return load_settings()


def get_mode_policy(mode: str, settings: dict | None = None) -> dict:
    if mode not in MODES:
        raise KeyError(mode)
    settings = settings or load_settings()
    pol = copy.deepcopy(MODES[mode])
    for k, v in settings.get("mode_overrides", {}).get(mode, {}).items():
        if k in HARD_LIMITS:
            pol[k] = min(int(v), HARD_LIMITS[k])
    # keep policy internally consistent
    pol["target_sources"] = min(pol["target_sources"], pol["max_sources"])
    pol["min_sources"] = min(pol["min_sources"], pol["target_sources"])
    pol["fetch_pages"] = min(pol["fetch_pages"], HARD_LIMITS["fetch_pages"])
    return pol


def secret(name: str) -> str:
    env = SECRET_ENV.get(name)
    return os.getenv(env, "").strip() if env else ""


def mask(value: str) -> str:
    if not value:
        return ""
    return "•" * 8 + value[-2:] if len(value) > 8 else "•" * 8
