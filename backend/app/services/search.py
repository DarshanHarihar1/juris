"""Search layer (LLD §5-S2). SearXNG JSON wrapper + Google Fact Check API client +
IFCN site-restricted precedent search. Every call degrades to [] on failure — a
down search provider must never crash the pipeline (Phase 2 resilience criterion)."""
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

_FACTCHECKERS = Path(__file__).parent.parent / "data" / "factcheckers.yaml"
_TIMEOUT = 10.0


@lru_cache(maxsize=1)
def factcheckers() -> list[str]:
    return yaml.safe_load(_FACTCHECKERS.read_text()) or []


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


async def web_search(query: str, recency: str | None = None, site: str | None = None) -> list[dict]:
    """General web search via SearXNG (private Render service). SEARXNG_URL unset or
    unreachable → []. recency ∈ {day,week,month,year}."""
    base = os.environ.get("SEARXNG_URL")
    if not base:
        return []
    q = f"site:{site} {query}" if site else query
    params = {"q": q, "format": "json"}
    if recency:
        params["time_range"] = recency
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{base.rstrip('/')}/search", params=params)
            r.raise_for_status()
            results = r.json().get("results", [])
    except Exception:
        return []                               # SearXNG down → empty, never crash
    return [
        {"url": x.get("url"), "title": x.get("title"), "snippet": x.get("content"),
         "domain": _domain(x.get("url", "")), "published_at": x.get("publishedDate")}
        for x in results if x.get("url")
    ]


async def fetch_page(url: str) -> dict:
    """Fetch a URL via Jina Reader (r.jina.ai), which renders JS and returns clean Markdown.
    Falls back to {} on any error. Optional JINA_API_KEY unlocks higher rate limits."""
    if not url:
        return {}
    headers = {"Accept": "text/plain", "X-Return-Format": "markdown"}
    key = os.environ.get("JINA_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            r = await c.get(f"https://r.jina.ai/{url}", headers=headers)
            r.raise_for_status()
            return {"url": url, "text": r.text[:4000]}
    except Exception:
        return {}


async def factcheck_search(query: str) -> list[dict]:
    """Human fact-check precedent lookup: Google Fact Check Tools Claim Search API
    (GOOGLE_FACTCHECK_API_KEY) + IFCN site: searches via SearXNG. Both best-effort."""
    out: list[dict] = []
    key = os.environ.get("GOOGLE_FACTCHECK_API_KEY")
    if key:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(
                    "https://factchecktools.googleapis.com/v1alpha1/claims:search",
                    params={"query": query, "key": key, "languageCode": "en"},
                )
                r.raise_for_status()
                for claim in r.json().get("claims", []):
                    for rev in claim.get("claimReview", []):
                        url = rev.get("url", "")
                        out.append({
                            "url": url, "domain": _domain(url),
                            "title": rev.get("title") or claim.get("text"),
                            "publisher": (rev.get("publisher") or {}).get("name"),
                            "rating": rev.get("textualRating"),
                            "published_at": rev.get("reviewDate"),
                            "claim": claim.get("text"),
                        })
        except Exception:
            pass
    for site in factcheckers():                 # IFCN site-restricted fallback
        out += await web_search(query, site=site)
    return out
