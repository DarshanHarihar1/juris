"""Stage 2 — Verify: one iterative FIRE-style agent per sub-claim.

Single /search tool (SearXNG top-5 → Jina). Final answer is structured JSON
(SubClaimVerdict) validated with Pydantic — not a tool call. Temporal guard
rejects parametric-only time-sensitive true/false.
"""
import json
import time
from datetime import date
from urllib.parse import urlparse

from langsmith import traceable
from openai import BadRequestError
from pydantic import ValidationError

from ..config import thresholds
from ..models import SubClaimVerdict
from ..services import events, nim, search, tools

VERIFY_PROMPT = """You are a fact-checker and verifier. Use your own knowledge for stable, general
facts, but your knowledge has a cutoff. **Today is {today}.** For anything
time-sensitive — current office-holders, recent events, "latest", dated claims —
you MUST use search. Detected claim language: {lang}; you may query in that
language and in English.

CLAIM: {claim}

You have access to ONE tool:
- **search** — Web/news search. Required arg: query (string). Optional: time_range
  (day|week|month|year). Returns top-5 results with page text. Call it whenever
  the claim may be time-sensitive or your parametric knowledge is insufficient.
  You may call it multiple times with refined queries.

When you are ready to settle, do NOT call a tool. Reply with ONLY JSON matching:
{{"verdict":"true"|"false"|"unverifiable","explanation":"<non-empty>","evidence":["https://..."]}}

Rules:
1. Prefer one targeted search over guessing. Do not paste the raw claim as the query.
2. Time-sensitive claims (office-holders, "current", "now", recent events): you MUST
   search, and evidence MUST include retrieved URLs from search results.
3. Static/general facts may use parametric knowledge with evidence=[].
4. If evidence is thin or conflicting, return unverifiable.
5. You have at most {max_steps} tool rounds, then you must settle with JSON.
6. Never invent tools (no open, browser_search, final_verdict, etc.). Only search.
"""

NUDGE_SEARCH_OR_SETTLE = (
    "Either call the search tool with a non-empty query, or settle now with ONLY "
    'JSON: {"verdict":"true"|"false"|"unverifiable","explanation":"...","evidence":["https://..."]}.'
)
NUDGE_BAD_TOOL = (
    "Invalid tool use. The only allowed tool is search with required string argument "
    "`query` (optional time_range). Or settle with JSON verdict — no other tools."
)
FORCE_VERDICT = (
    "Tool budget exhausted. Reply with ONLY JSON using the evidence already shown. "
    'Schema: {"verdict":"true"|"false"|"unverifiable","explanation":"<non-empty>",'
    '"evidence":["https://..."]}. If evidence is insufficient, verdict must be unverifiable.'
)
TEMPORAL_NUDGE = (
    "Rejected: this claim is time-sensitive, but evidence had no retrieved URLs. "
    "Call search again, then settle with JSON including evidence URLs from the results."
)
SCHEMA_RETRY = (
    "Your previous reply failed schema validation: {err}. "
    "Reply with ONLY valid JSON matching "
    '{{"verdict":"true"|"false"|"unverifiable","explanation":"<non-empty>",'
    '"evidence":["https://..."]}}.'
)
TOOL_NAMES = ["search"]
MAX_SCHEMA_RETRIES = 2


def _claim_text(claim) -> str:
    if isinstance(claim, str):
        return claim
    if isinstance(claim, dict):
        return str(claim.get("text_norm") or claim)
    return str(getattr(claim, "text_norm", claim))


def _is_time_sensitive(claim) -> bool:
    return search._is_time_sensitive(claim)


def _urls_from_log(evidence_log: list[dict]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for row in evidence_log:
        url = (row.get("url") or "").strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _looks_like_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _args(tool_call) -> dict:
    raw = getattr(tool_call.function, "arguments", None) or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _assistant_message(msg) -> dict:
    if hasattr(msg, "model_dump"):
        return msg.model_dump(exclude_none=True)
    return dict(msg)


def _safe_summary(text: str | None) -> str:
    # Empty when the model sent a bare tool call — the UI shows the query instead
    # of a filler sentence.
    if not text:
        return ""
    summary = " ".join(text.strip().split())
    for marker in ("Action:", "Tool:", "\n"):
        summary = summary.split(marker, 1)[0].strip() or summary
    return summary[:240]


def _agent_rows_for_prompt(rows: list[dict]) -> list[dict]:
    """Shape tool results as top-5 {score, url, title, content} for the agent."""
    out = []
    for row in rows[:5]:
        out.append({
            "score": row.get("score") or 0.0,
            "url": row.get("url") or "",
            "title": row.get("title") or "",
            "content": (row.get("content") or row.get("snippet") or "")[:2000],
        })
    return out


def _parse_verdict_text(text: str | None) -> SubClaimVerdict:
    """Parse model text into SubClaimVerdict; raises ValidationError/ValueError on failure."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty response")
    # Strip optional markdown fences.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    return SubClaimVerdict.model_validate_json(raw)


def _temporal_guard_ok(claim, verdict: SubClaimVerdict) -> bool:
    """Time-sensitive true/false must carry retrieved URLs."""
    if not _is_time_sensitive(claim):
        return True
    if verdict.verdict == "unverifiable":
        return True
    return any(_looks_like_url(u) for u in verdict.evidence)


def _fallback_verdict(evidence_log: list[dict], explanation: str | None = None) -> SubClaimVerdict:
    urls = _urls_from_log(evidence_log)
    return SubClaimVerdict(
        verdict="unverifiable",
        explanation=explanation or "Could not produce a valid cited verdict from available evidence.",
        evidence=urls,
    )


def _fill_evidence(verdict: SubClaimVerdict, evidence_log: list[dict]) -> SubClaimVerdict:
    if not verdict.evidence and evidence_log and verdict.verdict != "unverifiable":
        verdict.evidence = _urls_from_log(evidence_log)
    if verdict.verdict in ("true", "false") and not verdict.evidence:
        urls = _urls_from_log(evidence_log)
        if not urls:
            return SubClaimVerdict(
                verdict="unverifiable",
                explanation=verdict.explanation or "Insufficient retrieved evidence.",
                evidence=[],
            )
        verdict.evidence = urls
    return verdict


@traceable(name="search", run_type="tool")
async def _search_tool(query: str, time_range: str | None, claim, evidence_seq_start: int) -> list[dict]:
    return await search.search(
        query, time_range=time_range, claim=claim, evidence_seq_start=evidence_seq_start,
    )


async def _emit_evidence(job_id, claim_id, rows: list[dict]) -> None:
    if job_id is None:
        return
    for row in rows:
        data = {"evidence_id": row.get("id"), **{k: v for k, v in row.items() if k != "content"}}
        if claim_id is not None:
            data["claim_id"] = str(claim_id)
        try:
            await events.emit(job_id, "evidence", data)
        except Exception:
            pass  # ponytail: live tests may run without DB


async def _force_verdict(messages: list[dict], evidence_log: list[dict], timeout: float) -> SubClaimVerdict:
    """Budget exhausted → one structured JSON call with schema retry via nim.call."""
    try:
        resp = await nim.call(
            "verifier",
            messages + [{"role": "user", "content": FORCE_VERDICT}],
            response_schema=SubClaimVerdict,
        )
        if resp.parsed is None:
            return _fallback_verdict(evidence_log)
        return _fill_evidence(resp.parsed, evidence_log)  # type: ignore[arg-type]
    except Exception:
        return _fallback_verdict(evidence_log)


@traceable(name="verify", run_type="chain")
async def _verify(job_id, claim, *, claim_id=None, lang: str = "en") -> SubClaimVerdict:
    ts = thresholds()
    max_steps = ts.get("max_verify_steps", 6)
    budget = float(ts.get("verify_budget_s", 75))
    deadline = time.monotonic() + budget
    evidence_log: list[dict] = []
    schemas = tools.schemas(TOOL_NAMES)
    today = date.today().isoformat()
    claim_text = _claim_text(claim)
    claim_for_search = claim if not isinstance(claim, str) else claim

    messages: list[dict] = [
        {"role": "system", "content": VERIFY_PROMPT.format(
            today=today, lang=lang, claim=claim_text, max_steps=max_steps,
        )},
        {"role": "user", "content": f'Verify this claim: "{claim_text}"'},
    ]

    if claim_id is not None and job_id is not None:
        try:
            await events.emit(job_id, "stage", {"stage": "VERIFY", "status": "started", "claim_id": str(claim_id)})
        except Exception:
            pass

    temporal_rejects = 0
    schema_retries = 0
    for step in range(1, max_steps + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            msg = await nim.chat(None, messages, tools=schemas, role_name="verifier", timeout=remaining)
        except BadRequestError as e:
            err = str(e).lower()
            if "tool_use_failed" in err or "tool call validation" in err:
                messages.append({"role": "user", "content": NUDGE_BAD_TOOL})
                continue
            raise

        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            # Model settled (or stalled) with text — parse as SubClaimVerdict.
            messages.append(_assistant_message(msg))
            try:
                verdict = _parse_verdict_text(getattr(msg, "content", None))
            except (ValidationError, json.JSONDecodeError, ValueError) as e:
                schema_retries += 1
                if schema_retries > MAX_SCHEMA_RETRIES:
                    break
                messages.append({"role": "user", "content": SCHEMA_RETRY.format(err=e)})
                continue
            schema_retries = 0
            if not _temporal_guard_ok(claim_for_search, verdict):
                temporal_rejects += 1
                messages.append({"role": "user", "content": TEMPORAL_NUDGE})
                if temporal_rejects >= 2:
                    break
                continue
            verdict = _fill_evidence(verdict, evidence_log)
            if claim_id is not None and job_id is not None:
                try:
                    await events.emit(job_id, "stage", {
                        "stage": "VERIFY", "status": "done", "claim_id": str(claim_id),
                        "verdict": verdict.verdict,
                    })
                except Exception:
                    pass
            return verdict

        call = tool_calls[0]
        name = call.function.name
        args = _args(call)
        if job_id is not None:
            try:
                await events.emit(job_id, "verify_step", {
                    "step": step,
                    "claim_id": str(claim_id) if claim_id is not None else None,
                    "thought_summary": _safe_summary(getattr(msg, "content", None)),
                    "query": args.get("query"),
                    "settled": False,
                })
            except Exception:
                pass

        if name != "search":
            messages.append(_assistant_message(msg))
            messages.append({"role": "user", "content": NUDGE_BAD_TOOL})
            continue

        query = (args.get("query") or "").strip()
        if not query:
            messages.append(_assistant_message(msg))
            messages.append({"role": "user", "content": NUDGE_BAD_TOOL})
            continue

        messages.append(_assistant_message(msg))
        rows = await _search_tool(
            query,
            args.get("time_range"),
            claim_for_search,
            len(evidence_log) + 1,
            langsmith_extra={"metadata": {"job_id": str(job_id) if job_id else None, "query": query}},
        )
        evidence_log.extend(rows)
        if rows:
            await _emit_evidence(job_id, claim_id, rows)
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(_agent_rows_for_prompt(rows))[:12000],
        })
        # After search, nudge settle-or-search so the model doesn't stall.
        messages.append({"role": "user", "content": NUDGE_SEARCH_OR_SETTLE})

    final = await _force_verdict(messages, evidence_log, max(1.0, deadline - time.monotonic()))
    if not _temporal_guard_ok(claim_for_search, final):
        final = SubClaimVerdict(
            verdict="unverifiable",
            explanation=final.explanation or "Time-sensitive claim lacked retrieved evidence.",
            evidence=_urls_from_log(evidence_log),
        )
    if claim_id is not None and job_id is not None:
        try:
            await events.emit(job_id, "stage", {
                "stage": "VERIFY", "status": "done", "claim_id": str(claim_id),
                "verdict": final.verdict, "exhausted": True,
            })
        except Exception:
            pass
    return final


async def verify(job_id, claim, *, claim_id=None, lang: str = "en") -> SubClaimVerdict:
    return await verify_with_evidence(job_id, claim, claim_id=claim_id, lang=lang)


async def verify_with_evidence(job_id, claim, *, claim_id=None, lang: str = "en") -> SubClaimVerdict:
    """Returns SubClaimVerdict (evidence URLs are on the model)."""
    return await _verify(
        job_id, claim, claim_id=claim_id, lang=lang,
        langsmith_extra={"metadata": {
            "job_id": str(job_id) if job_id else None,
            "claim_id": str(claim_id) if claim_id else None,
            "lang": lang,
        }},
    )
