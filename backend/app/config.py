"""Loads config.yaml (role→model matrix, thresholds) + env at runtime."""
import os
from functools import lru_cache
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


@lru_cache(maxsize=1)
def cfg() -> dict:
    return yaml.safe_load(_CONFIG_PATH.read_text())


def role(name: str) -> dict | list:
    """Resolve a role entry from config.yaml (e.g. 'normalizer', 'verifier')."""
    return cfg()["roles"][name]


def default_provider_name() -> str:
    return cfg().get("default_provider", "default")


def provider(name: str | None = None) -> dict:
    conf = cfg()
    # Backward-compatible with the old single-provider shape.
    if "providers" not in conf:
        return conf["provider"]
    provider_name = name or default_provider_name()
    return conf["providers"][provider_name]


def role_provider_name(role_name: str) -> str:
    entry = role(role_name)
    if not isinstance(entry, dict):
        return default_provider_name()
    return entry.get("provider", default_provider_name())


def model_provider_name(model_id: str) -> str:
    for name, entry in cfg()["roles"].items():
        if isinstance(entry, dict) and entry.get("model") == model_id:
            return entry.get("provider", default_provider_name())
    return default_provider_name()


def thresholds() -> dict:
    return cfg()["thresholds"]


def mesh_api_key(provider_name: str | None = None) -> str:
    auth_env = provider(provider_name)["auth_env"]
    key = os.environ.get(auth_env)
    if not key:
        raise RuntimeError(f"{auth_env} not set in environment")
    return key


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set in environment")
    return url


def public_base_url() -> str:
    """Absolute origin of the frontend — used to build clickable investigation/verdict links
    for WhatsApp (relative paths aren't tappable in a chat)."""
    return os.environ.get("PUBLIC_BASE_URL", "https://juris-eta.vercel.app").rstrip("/")
