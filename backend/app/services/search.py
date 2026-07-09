"""Search layer. SearXNG wrapper, Jina fetcher, fact-check lookup, and v2 merged
search+fetch evidence rows. Provider failures degrade to [] / {} so retrieval
never crashes the pipeline."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import yaml

from ..config import thresholds
from . import credibility

_FACTCHECKERS = Path(__file__).parent.parent / "data" / "factcheckers.yaml"
_TIMEOUT = 10.0
_DATE_RE = re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b")


@lru_cache(maxsize=1)
def factcheckers() -> list[str]:
    return yaml.safe_load(_FACTCHECKERS.read_text()) or []


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def canonical_url(url: str) -> str:
    """Unwrap common redirect wrappers so attribution uses the destination URL."""
    if not url:
        return ""
    parsed = urlparse(url)
    domain = (parsed.netloc or "").lower().removeprefix("www.")
    if domain in {"google.com", "google.co.in"} and parsed.path == "/url":
        target = parse_qs(parsed.query).get("url", [""])[0]
        if target:
            return target
    return url


async def web_search(
    query: str,
    recency: str | None = None,
    site: str | None = None,
    categories: str | None = None,
) -> list[dict]:
    """General web search via SearXNG (private Render service). SEARXNG_URL unset or
    unreachable → []. recency ∈ {day,week,month,year}."""
    base = os.environ.get("SEARXNG_URL")
    if not base:
        return []
    q = f"site:{site} {query}" if site else query
    params = {"q": q, "format": "json"}
    if recency:
        params["time_range"] = recency
    if categories:
        params["categories"] = categories
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{base.rstrip('/')}/search", params=params)
            r.raise_for_status()
            results = r.json().get("results", [])
    except Exception:
        return []                               # SearXNG down → empty, never crash
    return [
        {"url": canonical_url(x.get("url", "")), "title": x.get("title"), "snippet": x.get("content"),
         "domain": _domain(canonical_url(x.get("url", ""))), "published_at": x.get("publishedDate")}
        for x in results if x.get("url")
    ]


def _claim_attr(claim, name: str, default=None):
    if isinstance(claim, dict):
        return claim.get(name, default)
    return getattr(claim, name, default)


def _is_time_sensitive(claim) -> bool:
    return bool(_claim_attr(claim, "is_time_sensitive", _claim_attr(claim, "time_sensitive", False)))


def _parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc)
        return dt.date()
    except (TypeError, ValueError):
        pass
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def temporal_filter(hits: list[dict], claim) -> list[dict]:
    """Keep undated evidence, but drop pre-anchor evidence for past-anchored,
    time-sensitive claims. This is the hard temporal validity constraint."""
    as_of = _parse_date(_claim_attr(claim, "as_of_date"))
    if not _is_time_sensitive(claim) or as_of is None:
        return hits
    floor = as_of - timedelta(days=thresholds().get("temporal_slack_days", 3))
    return [h for h in hits if _parse_date(h.get("published_at")) is None or _parse_date(h.get("published_at")) >= floor]


def _rank_key(claim):
    def key(hit: dict):
        cred = hit.get("credibility") or 0.0
        published = _parse_date(hit.get("published_at"))
        recency = published.toordinal() if published else 0
        if _is_time_sensitive(claim):
            return (-cred, -recency)
        return (-cred, 0)
    return key


def _evidence_row(hit: dict, evidence_id: str, content: str | None = None, fetch_failed: bool = False) -> dict:
    row = {
        "id": evidence_id,
        "url": hit.get("url", ""),
        "domain": hit.get("domain", ""),
        "title": hit.get("title") or "",
        "credibility": hit.get("credibility") or 0.0,
        "published_at": hit.get("published_at"),
        "fetch_failed": fetch_failed,
    }
    if content is not None:
        row["content"] = content
    else:
        row["snippet"] = hit.get("snippet") or ""
    return row


async def search(
    query: str,
    time_range: str | None = None,
    *,
    claim,
    evidence_seq_start: int = 1,
) -> list[dict]:
    """v2 merged search+auto-fetch tool. Returns stable evidence rows e1, e2, ...
    after credibility filtering, temporal filtering, ranking, and top-N Jina fetch."""
    ts = thresholds()
    hits = await web_search(
        query,
        recency=time_range,
        categories="news" if _is_time_sensitive(claim) else None,
    )
    if not hits and _is_time_sensitive(claim):
        hits = await web_search(query, recency=time_range)

    filtered = []
    seen: set[str] = set()
    for hit in hits:
        url = canonical_url(hit.get("url", ""))
        if not url or url.rstrip("/") in seen:
            continue
        seen.add(url.rstrip("/"))
        domain = hit.get("domain") or _domain(url)
        cred = credibility.score(domain)
        if cred < 0.3:
            continue
        filtered.append({**hit, "url": url, "domain": domain, "credibility": cred})

    filtered = temporal_filter(filtered, claim)
    filtered.sort(key=_rank_key(claim))
    max_results = ts.get("max_results", 6)
    max_autofetch = min(ts.get("max_autofetch", 2), max_results)
    selected = filtered[:max_results]
    top = selected[:max_autofetch]

    pages = await asyncio.gather(*[fetch_page(h["url"]) for h in top], return_exceptions=True)
    rows: list[dict] = []
    truncate = ts.get("fetch_truncate_chars", 3000)
    for i, hit in enumerate(selected, evidence_seq_start):
        content = None
        fetch_failed = False
        if i - evidence_seq_start < len(top):
            page = pages[i - evidence_seq_start]
            if isinstance(page, Exception) or not isinstance(page, dict) or not page.get("text"):
                fetch_failed = True
            else:
                content = page["text"][:truncate]
        rows.append(_evidence_row(hit, f"e{i}", content=content, fetch_failed=fetch_failed))
    return rows


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
                        url = canonical_url(url)
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
