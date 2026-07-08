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
from ..models import ClaimQuestions, QASource, QuestionAnswer
from ..services import credibility, events, nim, search, tools

log = logging.getLogger("juris.s3")

INVESTIGATOR_BUDGET = 120.0  # wall-clock cap for ALL question-answering; split across questions

DECOMPOSE_SYSTEM = """You are a claim analyst. Break the claim into AT MOST 3 specific,
self-contained factual sub-questions whose answers together would verify or refute it.
Each question must be answerable from a single web search.
Set time_sensitive=true if answers depend on who/what is current right now.

Output ONLY JSON: {"questions": ["..."], "time_sensitive": false}"""

QA_SYSTEM = """You are a fact-checking INVESTIGATOR. Answer the question below using web evidence.
Today's date: {today}.

Rules:
- Use web_search or factcheck_search to find relevant pages.
- For time-sensitive questions you MUST include recency="week" in your first web_search call.
- Use fetch_page on the 1–2 most relevant URLs to read what those pages actually say.
- Base your answer ONLY on what the fetched pages contain — not your own knowledge.
- Include in "sources" every URL you actually fetched or found via search.
- IMPORTANT: If the fetched page was published before 2025 and asks about a current fact
  (who holds office now, current prices, etc.) the page is STALE — set answerable to false.
- If no fetched page directly answers the question with current information, set answerable to false.
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


async def _decompose(claim_en: str, claim_native: str) -> ClaimQuestions:
    messages = [
        {"role": "system", "content": DECOMPOSE_SYSTEM},
        {"role": "user", "content": f'Claim (English): "{claim_en}"\nClaim (native): "{claim_native}"'},
    ]
    try:
        resp = await nim.call("decomposer", messages, response_schema=ClaimQuestions)
        obj = resp.parsed
        if obj and obj.questions:
            return obj
    except Exception as e:
        log.warning("decompose failed: %s", e)
    return ClaimQuestions(questions=[claim_en], time_sensitive=False)


async def _seed_search(question: str) -> str:
    """Run a recency=week search and return top-3 results as a formatted string."""
    results = await search.web_search(question, recency="week")
    if not results:
        return ""
    lines = []
    for r in results[:3]:
        lines.append(
            f"- URL: {r['url']}\n"
            f"  Title: {r.get('title', '')}\n"
            f"  Snippet: {r.get('snippet', '')}"
        )
    return "Seed search results (recency=week — use fetch_page on the most relevant URL):\n" + "\n".join(lines)


async def _answer_question(
    question: str, claim_en: str, claim_native: str,
    model: str, tool_names: list[str], time_sensitive: bool,
) -> QuestionAnswer | None:
    """One investigator's ReAct loop to answer a single sub-question."""
    cap = thresholds().get("max_tool_calls_per_investigator", 3)
    avail = [t for t in tool_names if t in tools.REGISTRY]
    schemas = tools.schemas(avail)
    seed = (await _seed_search(question)) if time_sensitive else ""
    seed_block = f"\n\n{seed}" if seed else ""
    messages: list[dict] = [
        {"role": "system", "content": QA_SYSTEM.format(cap=cap, today=date.today().isoformat())},
        {"role": "user", "content": (
            f'Claim (English): "{claim_en}"\n'
            f'Claim (native): "{claim_native}"\n'
            f'Question to answer: "{question}"{seed_block}'
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
        QASource(url=s.get("url", ""), title=s.get("title", ""), snippet=s.get("snippet", ""))
        for s in raw.get("sources", [])
        if isinstance(s, dict) and s.get("url")
    ]
    return QuestionAnswer(
        question=question,
        answer=raw.get("answer", ""),
        answerable=bool(raw.get("answerable", True)),
        sources=sources,
    )


def _to_evidence_rows(qa: QuestionAnswer, found_by: str) -> list[dict]:
    """Expand one QuestionAnswer into one evidence row per source URL."""
    rows = []
    for src in (qa.sources or []):
        url = (src.url or "").strip()
        if not url:
            continue
        dom = _domain(url)
        rows.append({
            "url": url, "domain": dom,
            "title": src.title or "", "snippet": src.snippet or "",
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


async def investigate(con, job_id, claim_id, claim_en: str, claim_native: str) -> list[dict]:
    """Decompose claim, answer sub-questions across both investigator families in
    parallel, persist grounded evidence rows, emit events."""
    await events.emit(job_id, "stage", {"stage": "S3_INVESTIGATE", "status": "started",
                                        "claim_id": str(claim_id)})

    # Step 1: decompose
    decomposed = await _decompose(claim_en, claim_native)
    max_q = thresholds().get("max_questions", 3)
    questions = decomposed.questions[:max_q]
    log.info("job=%s claim=%s questions=%s time_sensitive=%s",
             job_id, claim_id, questions, decomposed.time_sensitive)

    investigators = role("investigators")
    per_q_budget = INVESTIGATOR_BUDGET / max(len(questions), 1)

    # Step 2: answer all (question × investigator) tasks fully in parallel
    tasks = [(q, inv) for q in questions for inv in investigators]
    results = await asyncio.gather(*[
        asyncio.wait_for(
            _answer_question(
                q, claim_en, claim_native,
                inv["model"], inv.get("tools", []),
                decomposed.time_sensitive,
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

    evidence = _dedup(merged)

    # Step 4: persist rows that have a URL; placeholder (no-URL) rows stay in-memory only
    for ev in evidence:
        if not ev.get("url"):
            continue
        row_id = await con.fetchval(
            """insert into evidence
               (claim_id, url, domain, title, snippet, stance, credibility, found_by,
                question, answer, answerable)
               values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
               returning id""",
            claim_id,
            ev["url"], ev["domain"],
            ev.get("title") or None, ev.get("snippet") or None,
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
