"""S3 Investigation (LLD §5-S3). On a cache/precedent miss, two tool-using
investigators from different model families run ReAct loops in parallel (≤3 tool
calls each), then emit a deduped, stance-tagged, credibility-scored evidence log.
Evidence, not verdicts.

Evidence dicts match the `evidence` table: url, domain, title, snippet, stance
(supports|refutes|mentions|context), credibility (deterministic, from the domain —
never trusted from the model), found_by (the investigator's model id)."""
import asyncio
import json
import logging
from urllib.parse import urlparse

from ..config import role, thresholds
from ..services import credibility, events, nim, tools

log = logging.getLogger("juris.s3")

STANCES = {"supports", "refutes", "mentions", "context"}
INVESTIGATOR_BUDGET = 120.0  # wall-clock cap per investigator; timeout → graceful skip

SYSTEM = """You are a fact-checking INVESTIGATOR. Gather EVIDENCE, do NOT reach a verdict.

Rules:
- Use the search tools to find real sources (news, human fact-checks, official pages) about the claim.
- Search in BOTH English and the claim's native language when they differ.
- Run at LEAST ONE query intended to DISCONFIRM your current leaning (bias hygiene): actively look for sources that would prove the claim TRUE if you suspect it false, and vice-versa.
- You may call tools AT MOST {cap} times total, then STOP and output your evidence log.

Output ONLY a JSON object, no prose:
{{"evidence": [{{"url": "...", "title": "...", "snippet": "...", "stance": "supports|refutes|mentions|context"}}]}}
`stance` is the SOURCE's stance toward the claim. Include only sources you actually found via tools."""


def _extract_json(text: str) -> dict:
    """Best-effort parse of a model reply that should be a JSON object (may be fenced)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:].strip() if t.lower().startswith("json") else t.strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                return {}
        return {}


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


async def _investigate_one(claim_en: str, claim_native: str, model: str,
                           tool_names: list[str]) -> tuple[list[dict], list[dict]]:
    """One investigator's ReAct loop. Returns (raw evidence items, tool-call log).
    Hard-caps tool executions at `max_tool_calls_per_investigator` — extra tool calls
    the model requests beyond the budget get a stub response, never a real fn call."""
    cap = thresholds().get("max_tool_calls_per_investigator", 3)
    tool_names = [t for t in tool_names if t in tools.REGISTRY]     # drop deferred tools (numeric_check)
    schemas = tools.schemas(tool_names)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM.format(cap=cap)},
        {"role": "user", "content": f'Claim (English): "{claim_en}"\nClaim (native): "{claim_native}"'},
    ]
    executed = 0
    tool_log: list[dict] = []
    final_text = ""
    for _ in range(cap + 1):
        remaining = cap - executed
        msg = await nim.chat(model, messages, tools=schemas if (schemas and remaining > 0) else None)
        if not (getattr(msg, "tool_calls", None) and remaining > 0):
            final_text = msg.content or ""
            break
        messages.append(msg.model_dump(exclude_none=True))         # assistant turn (carries tool_calls)
        for tc in msg.tool_calls:
            args = _extract_json(tc.function.arguments) if tc.function.arguments else {}
            if executed < cap:
                executed += 1
                tool_log.append({"tool": tc.function.name, "args": args})
                try:
                    result = await tools.call_tool(tc.function.name, **args)
                except Exception as e:                             # bad args / provider down → feed error back
                    result = {"error": str(e)}
            else:
                result = {"error": "tool budget exhausted"}        # must still answer every tool_call_id
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)[:4000]})
    else:
        # used every turn still wanting tools → force the evidence log out
        msg = await nim.chat(model, messages + [
            {"role": "user", "content": "Tool limit reached. Output your evidence log JSON now."}], tools=None)
        final_text = msg.content or ""

    items = _extract_json(final_text).get("evidence", [])
    return (items if isinstance(items, list) else []), tool_log


def _finalize(raw_items: list[dict], model: str) -> list[dict]:
    """Coerce raw model items → valid evidence dicts. Credibility is computed from the
    domain (deterministic); an invalid/missing stance is repaired to neutral 'mentions'."""
    out: list[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        url = (it.get("url") or "").strip()
        if not url:
            continue
        stance = str(it.get("stance", "")).lower().strip()
        if stance not in STANCES:
            stance = "mentions"
        dom = _domain(url)
        out.append({
            "url": url, "domain": dom,
            "title": it.get("title"), "snippet": it.get("snippet"),
            "stance": stance, "credibility": credibility.score(dom), "found_by": model,
        })
    return out


def _dedup(items: list[dict]) -> list[dict]:
    """Collapse by URL across investigators, keeping the higher-credibility copy."""
    best: dict[str, dict] = {}
    for it in items:
        key = it["url"].rstrip("/")
        cur = best.get(key)
        if cur is None or (it.get("credibility") or 0) > (cur.get("credibility") or 0):
            best[key] = it
    return list(best.values())


async def investigate(con, job_id, claim_id, claim_en: str, claim_native: str) -> list[dict]:
    """Run both investigators in parallel, persist deduped evidence, emit `evidence`
    events. One investigator erroring out does not sink the other (graceful degradation)."""
    await events.emit(job_id, "stage", {"stage": "S3_INVESTIGATE", "status": "started", "claim_id": str(claim_id)})
    investigators = role("investigators")
    results = await asyncio.gather(
        *[asyncio.wait_for(
            _investigate_one(claim_en, claim_native, inv["model"], inv.get("tools", [])),
            timeout=INVESTIGATOR_BUDGET,
        ) for inv in investigators],
        return_exceptions=True,   # a TimeoutError/provider error on one → skipped, other still persists
    )
    merged: list[dict] = []
    for inv, res in zip(investigators, results):
        if isinstance(res, Exception):
            log.warning("investigator %s failed: %s", inv["model"], res)
            continue
        items, _tool_log = res
        merged += _finalize(items, inv["model"])

    evidence = _dedup(merged)
    for ev in evidence:
        row_id = await con.fetchval(
            """insert into evidence (claim_id, url, domain, title, snippet, stance, credibility, found_by)
               values ($1, $2, $3, $4, $5, $6, $7, $8) returning id""",
            claim_id, ev["url"], ev["domain"], ev["title"], ev["snippet"],
            ev["stance"], ev["credibility"], ev["found_by"],
        )
        await events.emit(job_id, "evidence", {"evidence_id": str(row_id), "claim_id": str(claim_id), **ev})

    await events.emit(job_id, "stage", {"stage": "S3_INVESTIGATE", "status": "done",
                                        "claim_id": str(claim_id), "count": len(evidence)})
    return evidence
