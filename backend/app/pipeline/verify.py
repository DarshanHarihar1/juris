"""v2 Verify stage: one iterative tool-calling verifier agent per claim.

The code loop owns budgets, tool execution, evidence shaping, event emission,
and citation enforcement. The model only chooses the next tool or final verdict.
"""
import json
import time
from datetime import date
from urllib.parse import urlparse

from pydantic import ValidationError

from ..config import thresholds
from ..models import NormalizedClaim, Verdict
from ..services import citations, credibility, events, nim, search, tools

VERIFY_PROMPT = """You are a fact-checking investigator. Verify ONE claim using the tools
provided. Today's date is {today}. Your training data has a cutoff, so for
anything that may have changed recently, TRUST RETRIEVED EVIDENCE OVER YOUR
OWN MEMORY.

CLAIM: {text_norm}
Original message language: {lang}
Time-sensitive: {time_sensitive}
Volatility: {volatility}
Applies as of: {as_of_date}

Tools:
- search(query, time_range?): web/news search. Results are credibility-scored,
  date-filtered, and the top results include fetched page content.
- fetch_page(url): read full text for a result when snippets are not enough.
- factcheck_search(query): search professional fact-checks for viral claims.
- final_verdict(...): submit the final verdict. Call it as soon as settled.

Investigation rules:
1. Briefly state what evidence would settle the claim, then use one tool.
2. Never paste the raw claim verbatim as a query unless unavoidable.
3. Time-sensitive claims: prefer recent sources and use time_range.
4. Past-anchored claims: verify what was true as of that date.
5. After each tool result, decide: settled -> final_verdict; otherwise make one
   targeted new query. Do not repeat failed queries.
6. You have at most {max_steps} tool rounds. Spend them on the crux.

Verdict rules:
- Every factual sentence in explanation must cite evidence as [e:e1].
- TRUE / MOSTLY_TRUE / MISLEADING / FALSE require two independent sources, or
  one professional fact-check directly addressing this claim.
- MISLEADING means the core fact is real but framing, numbers, dates, or context
  distort it.
- Static claims with no useful search results may use your own knowledge only
  with used_parametric_knowledge=true and confidence <= 70.
- Time-sensitive claims with thin/conflicting evidence are UNVERIFIABLE.
"""

NUDGE_USE_TOOLS = "Use exactly one available tool now. If the answer is settled, call final_verdict."
FORCE_VERDICT = "Tool budget exhausted. Call final_verdict now using only the evidence already shown."
TOOL_NAMES = ["search", "fetch_page", "factcheck_search", "final_verdict"]


def _claim_attr(claim, name: str, default=None):
    if isinstance(claim, dict):
        return claim.get(name, default)
    return getattr(claim, name, default)


def _claim_text(claim) -> str:
    return _claim_attr(claim, "text_norm", str(claim))


def _claim_block(claim: NormalizedClaim | dict, lang: str) -> str:
    return (
        f'Claim: "{_claim_text(claim)}"\n'
        f'Native claim: "{_claim_attr(claim, "text_norm_native", _claim_text(claim))}"\n'
        f"Language: {lang}\n"
        f"Time-sensitive: {_claim_attr(claim, 'is_time_sensitive', _claim_attr(claim, 'time_sensitive', False))}\n"
        f"As-of date: {_claim_attr(claim, 'as_of_date', None) or 'present day'}\n"
        f"Volatility: {_claim_attr(claim, 'volatility', 'slow')}"
    )


def _safe_summary(text: str | None) -> str:
    summary = "Choosing the next verification step."
    if text:
        summary = " ".join(text.strip().split())
        for marker in ("Action:", "Tool:", "\n"):
            summary = summary.split(marker, 1)[0].strip() or summary
    return summary[:240]


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


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _next_id(evidence_log: list[dict]) -> int:
    return len(evidence_log) + 1


def _factcheck_rows(results: list[dict], evidence_seq_start: int) -> list[dict]:
    rows: list[dict] = []
    for i, hit in enumerate(results, evidence_seq_start):
        url = search.canonical_url(hit.get("url", ""))
        domain = hit.get("domain") or _domain(url)
        content = " ".join(
            part for part in [
                f"Claim: {hit.get('claim')}" if hit.get("claim") else "",
                f"Rating: {hit.get('rating')}" if hit.get("rating") else "",
                f"Publisher: {hit.get('publisher')}" if hit.get("publisher") else "",
                hit.get("snippet") or "",
            ] if part
        )
        rows.append({
            "id": f"e{i}",
            "url": url,
            "domain": domain,
            "title": hit.get("title") or "",
            "credibility": credibility.score(domain),
            "published_at": hit.get("published_at"),
            "content": content,
            "fetch_failed": False,
        })
    return rows


def _fetch_row(url: str, page: dict, evidence_id: str) -> dict:
    domain = _domain(url)
    text = page.get("text") or ""
    return {
        "id": evidence_id,
        "url": url,
        "domain": domain,
        "title": "",
        "credibility": credibility.score(domain),
        "published_at": None,
        "content": text[:thresholds().get("fetch_truncate_chars", 3000)] if text else "",
        "fetch_failed": not bool(text),
    }


async def _emit_evidence(job_id, claim_id, rows: list[dict]) -> None:
    for row in rows:
        data = {"evidence_id": row["id"], **row}
        if claim_id is not None:
            data["claim_id"] = str(claim_id)
        await events.emit(job_id, "evidence", data)


async def _run_tool(name: str, args: dict, claim, evidence_log: list[dict]) -> tuple[object, list[dict]]:
    start = _next_id(evidence_log)
    if name == "search":
        rows = await search.search(
            args.get("query", ""),
            time_range=args.get("time_range"),
            claim=claim,
            evidence_seq_start=start,
        )
        return rows, rows
    if name == "factcheck_search":
        rows = _factcheck_rows(await search.factcheck_search(args.get("query", "")), start)
        return rows, rows
    if name == "fetch_page":
        url = search.canonical_url(args.get("url", ""))
        page = await search.fetch_page(url)
        row = _fetch_row(url, page, f"e{start}") if url else {}
        return page, [row] if row else []
    return {"error": f"unknown tool {name}"}, []


def _verdict_from_call(tool_call) -> Verdict | None:
    try:
        return Verdict.model_validate(_args(tool_call))
    except ValidationError:
        return None


async def _force_verdict(messages: list[dict], evidence_log: list[dict], timeout: float) -> Verdict:
    final_tools = tools.schemas(["final_verdict"])
    msg = await nim.chat(
        None,
        messages + [{"role": "user", "content": FORCE_VERDICT}],
        tools=final_tools,
        role_name="verifier",
        timeout=max(1.0, timeout),
        tool_choice="final_verdict",
    )
    call = (getattr(msg, "tool_calls", None) or [None])[0]
    verdict = _verdict_from_call(call) if call else None
    if verdict is None:
        verdict = Verdict(
            verdict="UNVERIFIABLE",
            confidence=25,
            explanation="The verifier could not produce a valid cited verdict.",
            key_evidence=[],
            evidence_conflict="unresolved",
            used_parametric_knowledge=False,
        )
    return citations.enforce(verdict, evidence_log)


async def _verify(job_id, claim: NormalizedClaim | dict, *, claim_id=None, lang: str = "en") -> tuple[Verdict, list[dict]]:
    """Run one verifier agent for one claim and return the enforced Verdict plus evidence."""
    ts = thresholds()
    max_steps = ts.get("max_verify_steps", 6)
    budget = float(ts.get("verify_budget_s", 75))
    deadline = time.monotonic() + budget
    evidence_log: list[dict] = []
    schemas = tools.schemas(TOOL_NAMES)
    messages: list[dict] = [
        {"role": "system", "content": VERIFY_PROMPT.format(
            today=date.today().isoformat(),
            text_norm=_claim_text(claim),
            lang=lang,
            time_sensitive=_claim_attr(claim, "is_time_sensitive", _claim_attr(claim, "time_sensitive", False)),
            volatility=_claim_attr(claim, "volatility", "slow"),
            as_of_date=_claim_attr(claim, "as_of_date", None) or "present day",
            max_steps=max_steps,
        )},
        {"role": "user", "content": _claim_block(claim, lang)},
    ]

    if claim_id is not None:
        await events.emit(job_id, "stage", {"stage": "VERIFY", "status": "started", "claim_id": str(claim_id)})

    for step in range(1, max_steps + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        msg = await nim.chat(None, messages, tools=schemas, role_name="verifier", timeout=remaining)
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            messages.append(_assistant_message(msg))
            messages.append({"role": "user", "content": NUDGE_USE_TOOLS})
            continue

        call = tool_calls[0]
        name = call.function.name
        args = _args(call)
        await events.emit(job_id, "verify_step", {
            "step": step,
            "claim_id": str(claim_id) if claim_id is not None else None,
            "thought_summary": _safe_summary(getattr(msg, "content", None)),
            "query": args.get("query") or args.get("url"),
            "settled": name == "final_verdict",
        })

        if name == "final_verdict":
            verdict = _verdict_from_call(call)
            if verdict is None:
                messages.append(_assistant_message(msg))
                messages.append({"role": "user", "content": "Your final_verdict arguments failed schema validation. Call final_verdict once more with valid JSON."})
                return await _force_verdict(messages, evidence_log, max(1.0, deadline - time.monotonic()))
            final = citations.enforce(verdict, evidence_log)
            if claim_id is not None:
                await events.emit(job_id, "stage", {"stage": "VERIFY", "status": "done", "claim_id": str(claim_id),
                                                    "verdict": final.verdict})
            return final, evidence_log

        messages.append(_assistant_message(msg))
        result, new_rows = await _run_tool(name, args, claim, evidence_log)
        evidence_log.extend(new_rows)
        if new_rows:
            await _emit_evidence(job_id, claim_id, new_rows)
        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)[:12000]})

    final = await _force_verdict(messages, evidence_log, max(1.0, deadline - time.monotonic()))
    if claim_id is not None:
        await events.emit(job_id, "stage", {"stage": "VERIFY", "status": "done", "claim_id": str(claim_id),
                                            "verdict": final.verdict, "exhausted": True})
    return final, evidence_log


async def verify(job_id, claim: NormalizedClaim | dict, *, claim_id=None, lang: str = "en") -> Verdict:
    verdict, _evidence = await _verify(job_id, claim, claim_id=claim_id, lang=lang)
    return verdict


async def verify_with_evidence(job_id, claim: NormalizedClaim | dict, *, claim_id=None, lang: str = "en") -> tuple[Verdict, list[dict]]:
    return await _verify(job_id, claim, claim_id=claim_id, lang=lang)
