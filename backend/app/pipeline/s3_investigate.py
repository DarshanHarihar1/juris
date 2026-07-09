"""S3 Investigation — QA-decomposition mode (Phase 3b).

Flow:
  1. Decompose: claim → ≤3 focused sub-questions (+ time_sensitive flag).
  2. Answer: for each question, both investigators (different families) run a
     ReAct loop: web_search/factcheck_search to find candidates, fetch_page to
     read the actual page, then output a grounded answer + sources.
     All (question × investigator) tasks run in parallel.
  3. Assemble: one evidence row per (question, source URL). Credibility is
     deterministic (domain table, never model-set). Dedup by (question, url).

Evidence rows carry question/answer/answerable instead of a stance label.
S4 jurors receive the answered-question log; they no longer judge stance-tagged
snippet strings they never read.
"""
import asyncio
import json
import logging
from datetime import date
from urllib.parse import urlparse

from ..config import role, thresholds
from ..models import ClaimQuestions, EvidenceRelevance, QASource, QueryBundle, QuestionAnswer
from ..services import credibility, events, nim, search, tools

log = logging.getLogger("juris.s3")

INVESTIGATOR_BUDGET = 120.0  # wall-clock cap for ALL question-answering; split across questions

DECOMPOSE_SYSTEM = """You are a claim analyst. Break the claim into AT MOST 3 specific,
self-contained factual sub-questions whose answers together would verify or refute it.
Each question MUST include all essential context from the original claim (year, full event name, named entity).
NEVER create trivially answerable questions (e.g. "Does X exist?" or a bare year like "2026").
Each question must be answerable from a single web search.
Set time_sensitive=true if answers depend on current events, or if the claim references events in 2025 or later.

Output ONLY JSON: {"questions": ["..."], "time_sensitive": false}"""

QUERY_SYSTEM = """You generate web search queries for claim verification.
Today's date: {today}.

Rules:
- Generate AT MOST 3 short, keyword-style search queries.
- Do NOT repeat the raw claim verbatim unless unavoidable.
- For time-sensitive claims, include current-time anchors like the year/month or words like "current".
- Prefer entity + office/event + date style queries over conversational questions.

Output ONLY JSON: {{"queries": ["..."]}}"""

RELEVANCE_SYSTEM = """You label whether one evidence item is useful for verifying a claim.

Rules:
- supports: the evidence directly supports the claim.
- refutes: the evidence directly refutes the claim.
- irrelevant: the evidence is off-topic, only shares keywords, or does not directly answer the claim.
- Be strict. Old unrelated fact-check pages that merely mention the same person/event are irrelevant.

Output ONLY JSON: {{"label": "supports"|"refutes"|"irrelevant"}}"""

QA_SYSTEM = """You are a fact-checking INVESTIGATOR. Answer the question below using web evidence.
Today's date: {today}.

Rules:
- Use web_search or factcheck_search to find relevant pages.
- For time-sensitive questions you MUST include recency="week" in your first web_search call.
- SEED SHORTCUT: If "Seed search results" are provided above AND a snippet there EXPLICITLY
  states the answer (e.g. "India wins T20 World Cup 2026", "SKY led India to title"), answer
  directly from that snippet — set answerable=true and include the seed URL as your source.
  Seed results come from recency=week searches and are already verified-recent; skip the
  pre-2025 staleness check for them. Only use fetch_page when the snippet is ambiguous.
- Use fetch_page on the 1–2 most relevant URLs to read what those pages actually say.
- Base your answer on fetched pages or (per the SEED SHORTCUT above) clear seed snippets.
- Include in "sources" every URL you actually fetched or found via search.
- IMPORTANT: If the fetched page was published before 2025 and asks about a current fact
  (who holds office now, current prices, etc.) the page is STALE — set answerable to false.
- If no fetched page or seed snippet directly answers the question, set answerable to false.
- You may call tools AT MOST {cap} times total, then output your JSON.

Output ONLY JSON:
{{"question": "...", "answer": "...", "answerable": true, "sources": [{{"url": "...", "title": "...", "snippet": "..."}}]}}"""


def _extract_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:].strip() if t.lower().startswith("json") else t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                pass
    return {}


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


import re as _re

_RECENT_YEAR = _re.compile(r"\b20(2[5-9])\b")


async def _decompose(claim_en: str, claim_native: str) -> ClaimQuestions:
    messages = [
        {"role": "system", "content": DECOMPOSE_SYSTEM},
        {"role": "user", "content": f'Claim (English): "{claim_en}"\nClaim (native): "{claim_native}"'},
    ]
    try:
        resp = await nim.call("decomposer", messages, response_schema=ClaimQuestions)
        obj = resp.parsed
        if obj and obj.questions:
            # ponytail: force time_sensitive for any claim mentioning 2025–2029
            if _RECENT_YEAR.search(claim_en) or _RECENT_YEAR.search(claim_native or ""):
                obj.time_sensitive = True
            return obj
    except Exception as e:
        log.warning("decompose failed: %s", e)
    ts = bool(_RECENT_YEAR.search(claim_en))
    return ClaimQuestions(questions=[claim_en], time_sensitive=ts)


async def _generate_queries(
    claim_en: str,
    question: str,
    time_sensitive: bool,
    as_of_date: str | None,
) -> list[str]:
    messages = [
        {"role": "system", "content": QUERY_SYSTEM.format(today=date.today().isoformat())},
        {"role": "user", "content": (
            f'Claim: "{claim_en}"\n'
            f'Question: "{question}"\n'
            f"time_sensitive: {str(time_sensitive).lower()}\n"
            f'as_of_date: "{as_of_date or ""}"'
        )},
    ]
    try:
        resp = await nim.call("query_generator", messages, response_schema=QueryBundle)
        parsed = resp.parsed
        if parsed and parsed.queries:
            out: list[str] = []
            for q in parsed.queries:
                q = (q or "").strip()
                if q and q not in out:
                    out.append(q)
                if len(out) == 3:
                    break
            if out:
                return out
    except Exception as e:
        log.warning("query generation failed: %s", e)
    return [question]


def _published_date_map(results: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in results or []:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        published_at = item.get("published_at")
        if url and published_at:
            out[url.rstrip("/")] = str(published_at)
    return out


async def _seed_search(question: str) -> tuple[str, dict[str, str]]:
    """Run a recency=week search, fall back to month if empty, return top-3 results + dates."""
    results = await search.web_search(question, recency="week")
    if not results:
        results = await search.web_search(question, recency="month")
    if not results:
        return "", {}
    lines = []
    for r in results[:3]:
        lines.append(
            f"- URL: {r['url']}\n"
            f"  Title: {r.get('title', '')}\n"
            f"  Snippet: {r.get('snippet', '')}"
        )
    return (
        "Seed search results (recency=week→month — use fetch_page on the most relevant URL):\n" + "\n".join(lines),
        _published_date_map(results),
    )


async def _seed_search_queries(queries: list[str], time_sensitive: bool) -> tuple[str, dict[str, str]]:
    combined_lines: list[str] = []
    combined_dates: dict[str, str] = {}
    for query in queries[:3]:
        results = await search.web_search(query, recency="week" if time_sensitive else None)
        if not results and time_sensitive:
            results = await search.web_search(query, recency="month")
        if not results:
            continue
        combined_dates.update(_published_date_map(results))
        for r in results[:2]:
            combined_lines.append(
                f"- Query: {query}\n"
                f"  URL: {r['url']}\n"
                f"  Title: {r.get('title', '')}\n"
                f"  Snippet: {r.get('snippet', '')}"
            )
    if not combined_lines:
        return "", {}
    return (
        "Seed search results (generated queries — use fetch_page on the most relevant URL):\n"
        + "\n".join(combined_lines),
        combined_dates,
    )


async def _answer_question(
    question: str, claim_en: str, claim_native: str,
    model: str, tool_names: list[str], time_sensitive: bool,
    claim_seed: str = "",
    search_queries: list[str] | None = None,
) -> QuestionAnswer | None:
    """One investigator's ReAct loop to answer a single sub-question."""
    cap = thresholds().get("max_tool_calls_per_investigator", 3)
    avail = [t for t in tool_names if t in tools.REGISTRY]
    schemas = tools.schemas(avail)
    query_list = search_queries or [question]
    seed_text, seed_dates = await _seed_search_queries(query_list, time_sensitive)
    combined_seed = "\n\n".join(s for s in (seed_text, claim_seed) if s)
    seed_block = f"\n\n{combined_seed}" if combined_seed else ""
    query_block = "\n".join(f"- {q}" for q in query_list[:3])
    source_dates = dict(seed_dates)
    messages: list[dict] = [
        {"role": "system", "content": QA_SYSTEM.format(cap=cap, today=date.today().isoformat())},
        {"role": "user", "content": (
            f'Claim (English): "{claim_en}"\n'
            f'Claim (native): "{claim_native}"\n'
            f'Question to answer: "{question}"\n'
            f"Suggested search queries:\n{query_block}{seed_block}"
        )},
    ]
    executed = 0
    final_text = ""
    for _ in range(cap + 1):
        remaining = cap - executed
        msg = await nim.chat(model, messages, tools=schemas if (schemas and remaining > 0) else None)
        if not (getattr(msg, "tool_calls", None) and remaining > 0):
            final_text = msg.content or ""
            break
        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls:
            if executed >= cap:
                break
            executed += 1
            args = _extract_json(tc.function.arguments) if tc.function.arguments else {}
            try:
                result = await tools.call_tool(tc.function.name, **args)
            except Exception as e:
                result = {"error": str(e)}
            if isinstance(result, list):
                source_dates.update(_published_date_map(result))
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result)[:4000]})
    else:
        # exhausted turns without a final text — force one
        msg = await nim.chat(model, messages + [{"role": "user", "content": "Output your answer JSON now."}], tools=None)
        final_text = msg.content or ""

    raw = _extract_json(final_text)
    if not raw:
        return None

    sources = [
        QASource(
            url=s.get("url", ""),
            title=s.get("title", ""),
            snippet=s.get("snippet", ""),
            published_at=source_dates.get((s.get("url", "") or "").rstrip("/")),
        )
        for s in raw.get("sources", [])
        if isinstance(s, dict) and s.get("url")
    ]
    return QuestionAnswer(
        question=question,
        answer=raw.get("answer", ""),
        answerable=bool(raw.get("answerable", True)),
        sources=sources,
    )


# ponytail: LLMs hallucinate example.com/placeholder.com when search returns empty;
# google/bing search SERPs are never real content — filter both.
_HALLUCINATED_DOMAINS = frozenset({"example.com", "example.org", "example.net", "placeholder.com"})
_SERP_PREFIXES = ("google.com/search", "bing.com/search", "duckduckgo.com/?q=", "search.yahoo.com/search")


def _to_evidence_rows(qa: QuestionAnswer, found_by: str) -> list[dict]:
    """Expand one QuestionAnswer into one evidence row per source URL."""
    rows = []
    for src in (qa.sources or []):
        url = (src.url or "").strip()
        if not url:
            continue
        dom = _domain(url)
        if dom in _HALLUCINATED_DOMAINS:
            continue  # filter LLM-hallucinated placeholder sources
        if any(p in url for p in _SERP_PREFIXES):
            continue  # filter search engine result pages — not real content
        rows.append({
            "url": url, "domain": dom,
            "title": src.title or "", "snippet": src.snippet or "",
            "published_at": src.published_at,
            "stance": None,  # QA mode: no document-level stance
            "credibility": credibility.score(dom),
            "found_by": found_by,
            "question": qa.question,
            "answer": qa.answer,
            "answerable": qa.answerable,
        })
    return rows


def _dedup(items: list[dict]) -> list[dict]:
    """Collapse by (question, url) keeping higher-credibility copy per pair."""
    best: dict[tuple, dict] = {}
    for it in items:
        key = (it.get("question", ""), (it.get("url") or "").rstrip("/"))
        cur = best.get(key)
        if cur is None or (it.get("credibility") or 0) > (cur.get("credibility") or 0):
            best[key] = it
    return list(best.values())


async def _filter_relevant_evidence(claim_en: str, evidence: list[dict]) -> list[dict]:
    filtered: list[dict] = []
    for ev in evidence:
        if not ev.get("url"):
            filtered.append(ev)
            continue
        messages = [
            {"role": "system", "content": RELEVANCE_SYSTEM},
            {"role": "user", "content": (
                f'Claim: "{claim_en}"\n'
                f'Question: "{ev.get("question", "")}"\n'
                f'Answer: "{ev.get("answer", "")}"\n'
                f'Title: "{ev.get("title", "")}"\n'
                f'Snippet: "{ev.get("snippet", "")}"'
            )},
        ]
        try:
            resp = await nim.call("query_generator", messages, response_schema=EvidenceRelevance)
            parsed = resp.parsed
            if parsed is None or parsed.label == "irrelevant":
                continue
            ev["stance"] = parsed.label
            filtered.append(ev)
        except Exception:
            filtered.append(ev)
    return filtered


async def investigate(
    con,
    job_id,
    claim_id,
    claim_en: str,
    claim_native: str,
    *,
    is_time_sensitive: bool = False,
    as_of_date: str | None = None,
) -> list[dict]:
    """Decompose claim, answer sub-questions across both investigator families in
    parallel, persist grounded evidence rows, emit events."""
    await events.emit(job_id, "stage", {"stage": "S3_INVESTIGATE", "status": "started",
                                        "claim_id": str(claim_id)})

    # Step 1: decompose
    decomposed = await _decompose(claim_en, claim_native)
    if is_time_sensitive:
        decomposed.time_sensitive = True
    max_q = thresholds().get("max_questions", 3)
    questions = decomposed.questions[:max_q]
    log.info("job=%s claim=%s questions=%s time_sensitive=%s",
             job_id, claim_id, questions, decomposed.time_sensitive)
    query_plan = {
        q: await _generate_queries(claim_en, q, decomposed.time_sensitive, as_of_date)
        for q in questions
    }

    investigators = role("investigators")
    per_q_budget = INVESTIGATOR_BUDGET / max(len(questions), 1)

    # When time_sensitive, also seed-search the original claim so investigators
    # have direct evidence about it even when sub-questions are off-target.
    claim_seed, _claim_seed_dates = (await _seed_search(claim_en)) if decomposed.time_sensitive else ("", {})

    # Step 2: answer all (question × investigator) tasks fully in parallel
    tasks = [(q, inv) for q in questions for inv in investigators]
    results = await asyncio.gather(*[
        asyncio.wait_for(
            _answer_question(
                q, claim_en, claim_native,
                inv["model"], inv.get("tools", []),
                decomposed.time_sensitive,
                claim_seed=claim_seed,
                search_queries=query_plan.get(q),
            ),
            timeout=per_q_budget,
        )
        for q, inv in tasks
    ], return_exceptions=True)

    all_answers: list[tuple[QuestionAnswer, str]] = []
    for (q, inv), res in zip(tasks, results):
        if isinstance(res, Exception):
            log.warning("investigator %s q=%r failed: %s", inv["model"], q, res)
        elif res is not None:
            all_answers.append((res, inv["model"]))

    log.info("job=%s claim=%s answered %d/%d tasks", job_id, claim_id,
             len(all_answers), len(tasks))

    # Step 3: assemble + dedup evidence rows
    merged: list[dict] = []
    for qa, found_by in all_answers:
        merged += _to_evidence_rows(qa, found_by)

    # If investigators found nothing, add unanswerable placeholder so S4 can apply 1b gate
    if not merged:
        for q in questions:
            merged.append({
                "url": "", "domain": "", "title": "", "snippet": "",
                "stance": None, "credibility": 0.0, "found_by": "none",
                "question": q, "answer": "", "answerable": False,
            })

    evidence = await _filter_relevant_evidence(claim_en, _dedup(merged))

    # Step 4: persist rows that have a URL; placeholder (no-URL) rows stay in-memory only
    for ev in evidence:
        if not ev.get("url"):
            continue
        row_id = await con.fetchval(
            """insert into evidence
               (claim_id, url, domain, title, snippet, published_at, stance, credibility, found_by,
                question, answer, answerable)
               values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
               returning id""",
            claim_id,
            ev["url"], ev["domain"],
            ev.get("title") or None, ev.get("snippet") or None,
            ev.get("published_at"),
            ev.get("stance"),
            ev.get("credibility"), ev.get("found_by"),
            ev.get("question"), ev.get("answer"), ev.get("answerable"),
        )
        ev["id"] = str(row_id)
        await events.emit(job_id, "evidence",
                          {"evidence_id": str(row_id), "claim_id": str(claim_id), **ev})

    await events.emit(job_id, "stage", {"stage": "S3_INVESTIGATE", "status": "done",
                                        "claim_id": str(claim_id), "count": len(evidence)})
    return evidence
