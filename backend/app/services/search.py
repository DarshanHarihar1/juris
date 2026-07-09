"""Search layer. SearXNG wrapper, Jina fetcher, and merged search+fetch evidence
rows. Provider failures degrade to [] / {} so retrieval never crashes the pipeline."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import logging
import os
import re
from urllib.parse import parse_qs, urlparse

import httpx

from ..config import thresholds

log = logging.getLogger("juris.search")

_TIMEOUT = 10.0
_WAKE_TIMEOUT = 45.0
_RETRY_STATUS = {502, 503, 504}
_SEARCH_ATTEMPTS = 3
_SEARCH_RETRY_DELAY = 12.0
_WAKE_ATTEMPTS = 4
_WAKE_RETRY_DELAY = 10.0
_DATE_RE = re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b")


def _searxng_base() -> str | None:
    """Render hostport is `service:port` with no scheme; httpx needs http://."""
    raw = os.environ.get("SEARXNG_URL", "").strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw.rstrip("/")
    return f"http://{raw.rstrip('/')}"


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


async def _searxng_get(
    params: dict,
    *,
    timeout: float = _TIMEOUT,
    max_attempts: int = _SEARCH_ATTEMPTS,
    retry_delay: float = _SEARCH_RETRY_DELAY,
) -> list[dict] | None:
    """GET SearXNG /search; retry 502/503/504 and timeouts (cold-start on Render free tier)."""
    base = _searxng_base()
    if not base:
        return None
    url = f"{base}/search"
    q = params.get("q", "")
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(url, params=params)
                if r.status_code in _RETRY_STATUS and attempt < max_attempts:
                    log.warning(
                        "SearXNG HTTP %s url=%s query=%r attempt=%d/%d, retrying in %.0fs",
                        r.status_code, url, q, attempt, max_attempts, retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    continue
                r.raise_for_status()
                return r.json().get("results", [])
        except httpx.TimeoutException as exc:
            last_exc = exc
            if attempt < max_attempts:
                log.warning(
                    "SearXNG timeout url=%s query=%r attempt=%d/%d, retrying in %.0fs",
                    url, q, attempt, max_attempts, retry_delay,
                )
                await asyncio.sleep(retry_delay)
                continue
            log.error("SearXNG timeout url=%s query=%r: %s", url, q, exc)
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            log.error(
                "SearXNG HTTP %s url=%s query=%r body=%r: %s",
                exc.response.status_code, url, q, exc.response.text[:200], exc,
            )
            return None
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt < max_attempts:
                log.warning(
                    "SearXNG request failed url=%s query=%r attempt=%d/%d, retrying in %.0fs: %s",
                    url, q, attempt, max_attempts, retry_delay, exc,
                )
                await asyncio.sleep(retry_delay)
                continue
            log.error("SearXNG request failed url=%s query=%r: %s", url, q, exc)
        except ValueError as exc:
            last_exc = exc
            log.error("SearXNG invalid JSON url=%s query=%r: %s", url, q, exc)
        except Exception:
            log.exception("SearXNG unexpected error url=%s query=%r", url, q)
            return None

    if last_exc:
        return None
    return None


async def warm_searxng() -> bool:
    """Pre-wake SearXNG on cold Render free tier before verify searches run."""
    base = _searxng_base()
    if not base:
        log.warning("warm_searxng skipped: SEARXNG_URL is unset")
        return False
    results = await _searxng_get(
        {"q": "test", "format": "json"},
        timeout=_WAKE_TIMEOUT,
        max_attempts=_WAKE_ATTEMPTS,
        retry_delay=_WAKE_RETRY_DELAY,
    )
    if results is not None:
        log.info("SearXNG warm ok base=%s", base)
        return True
    log.warning("SearXNG warm failed base=%s", base)
    return False


async def web_search(
    query: str,
    recency: str | None = None,
    site: str | None = None,
    categories: str | None = None,
) -> list[dict]:
    """General web search via SearXNG (private Render service). SEARXNG_URL unset or
    unreachable → []. recency ∈ {day,week,month,year}."""
    base = _searxng_base()
    if not base:
        log.warning("web_search skipped: SEARXNG_URL is unset")
        return []
    q = f"site:{site} {query}" if site else query
    params = {"q": q, "format": "json"}
    if recency:
        params["time_range"] = recency
    if categories:
        params["categories"] = categories
    results = await _searxng_get(params)
    if results is None:
        return []
    return [
        {"url": canonical_url(x.get("url", "")), "title": x.get("title"), "snippet": x.get("content"),
         "domain": _domain(canonical_url(x.get("url", ""))), "published_at": x.get("publishedDate"),
         "score": x.get("score") or 0.0}
        for x in results if x.get("url")
    ]


# Heuristic markers for string claims (normalize no longer emits is_time_sensitive).
_TIME_SENSITIVE_RE = re.compile(
    r"\b(current|currently|now|today|latest|incumbent|reigning|"
    r"chief\s+minister|\bcm\b|prime\s+minister|\bpm\b|president|governor|"
    r"mayor|ceo|as\s+of)\b",
    re.I,
)


def _claim_attr(claim, name: str, default=None):
    if isinstance(claim, str):
        return default
    if isinstance(claim, dict):
        return claim.get(name, default)
    return getattr(claim, name, default)


def _is_time_sensitive(claim) -> bool:
    flagged = _claim_attr(claim, "is_time_sensitive", _claim_attr(claim, "time_sensitive", None))
    if flagged is not None:
        return bool(flagged)
    text = claim if isinstance(claim, str) else str(_claim_attr(claim, "text_norm", claim) or "")
    return bool(_TIME_SENSITIVE_RE.search(text))


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
        score = hit.get("score") or 0.0
        published = _parse_date(hit.get("published_at"))
        recency = published.toordinal() if published else 0
        if _is_time_sensitive(claim):
            return (-score, -recency)
        return (-score, 0)
    return key


def _evidence_row(hit: dict, evidence_id: str, content: str | None = None, fetch_failed: bool = False) -> dict:
    row = {
        "id": evidence_id,
        "url": hit.get("url", ""),
        "domain": hit.get("domain", ""),
        "title": hit.get("title") or "",
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
    """Merged search+auto-fetch. Returns evidence rows e1, e2, ... after temporal
    filtering, score ranking, and top-N Jina fetch."""
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
        filtered.append({**hit, "url": url, "domain": domain})

    filtered = temporal_filter(filtered, claim)
    filtered.sort(key=_rank_key(claim))
    # Design: SearXNG top-5 → Jina full text for those URLs.
    max_results = min(ts.get("max_results", 5), 5)
    max_autofetch = min(ts.get("max_autofetch", 5), max_results)
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
    jina_url = f"https://r.jina.ai/{url}"
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            r = await c.get(jina_url, headers=headers)
            r.raise_for_status()
            return {"url": url, "text": r.text[:4000]}
    except httpx.TimeoutException as exc:
        log.warning("Jina fetch timeout url=%r: %s", url, exc)
        return {}
    except httpx.HTTPStatusError as exc:
        log.warning(
            "Jina fetch HTTP %s url=%r body=%r: %s",
            exc.response.status_code, url, exc.response.text[:120], exc,
        )
        return {}
    except httpx.RequestError as exc:
        log.warning("Jina fetch request failed url=%r: %s", url, exc)
        return {}
    except Exception:
        log.exception("Jina fetch unexpected error url=%r", url)
        return {}
