"""Domain credibility table (data/domains.yaml).

Static tier lookup so evidence from a wire service / official body outranks
an unlisted domain, without any LLM call.
"""
from functools import lru_cache
from pathlib import Path

import yaml

_DOMAINS_PATH = Path(__file__).parent.parent / "data" / "domains.yaml"

DEFAULT_TIER = 3
TIER_SCORES = {1: 90, 2: 60, 3: 30}


@lru_cache(maxsize=1)
def _table() -> dict[str, int]:
    raw = yaml.safe_load(_DOMAINS_PATH.read_text()) or {}
    table: dict[str, int] = {}
    for key, domains in raw.items():
        tier = int(key.removeprefix("tier_"))
        for domain in domains or []:
            table[domain.lower().removeprefix("www.")] = tier
    return table


def tier_for(domain: str) -> int:
    """Credibility tier for a domain (1 = highest). Unlisted domains default to DEFAULT_TIER."""
    return _table().get((domain or "").lower().removeprefix("www."), DEFAULT_TIER)


def score_for(domain: str) -> int:
    """0-100 credibility score derived from tier_for()."""
    return TIER_SCORES.get(tier_for(domain), TIER_SCORES[DEFAULT_TIER])
