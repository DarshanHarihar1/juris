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


def provider() -> dict:
    return cfg()["provider"]


def thresholds() -> dict:
    return cfg()["thresholds"]


def nim_api_key() -> str:
    key = os.environ.get(provider()["auth_env"])
    if not key:
        raise RuntimeError(f"{provider()['auth_env']} not set in environment")
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
