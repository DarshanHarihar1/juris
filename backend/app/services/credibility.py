"""Source credibility scorer (LLD §7). Static tier lookup from data/domains.yaml;
unknown domain → 0.35. Sub-domains fall back to their parent domain.
ponytail: table lookup only. Per-page signals (HTTPS/byline/dateline/domain-age)
are a nice-to-have (LLD §0) — add if a golden claim needs finer scoring."""
from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).parent.parent / "data" / "domains.yaml"
UNKNOWN = 0.35


@lru_cache(maxsize=1)
def _table() -> dict[str, float]:
    return yaml.safe_load(_PATH.read_text()) or {}


def score(domain: str) -> float:
    if not domain:
        return UNKNOWN
    d = domain.lower().strip().removeprefix("www.")
    table = _table()
    if d in table:
        return table[d]
    parts = d.split(".")                        # sub.example.com → try example.com, then .com
    for i in range(1, len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in table:
            return table[cand]
    return UNKNOWN
