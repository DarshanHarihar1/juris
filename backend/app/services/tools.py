"""Tool registry (LLD §2). Maps tool-name → (OpenAI tool-calling JSON schema,
async Python fn). Existing v1 tools remain available; v2 Verify adds search()
and final_verdict()."""
from ..models import Verdict
from . import credibility, search


async def _search(query: str, time_range: str | None = None, claim=None,
                  evidence_seq_start: int = 1) -> list[dict]:
    if claim is None:
        return await search.web_search(query, recency=time_range)
    return await search.search(query, time_range=time_range, claim=claim,
                               evidence_seq_start=evidence_seq_start)


async def _web_search(query: str, recency: str | None = None) -> list[dict]:
    return await search.web_search(query, recency=recency)


async def _factcheck_search(query: str) -> list[dict]:
    return await search.factcheck_search(query)


async def _source_credibility(domain: str) -> dict:
    return {"domain": domain, "credibility": credibility.score(domain)}


async def _fetch_page(url: str) -> dict:
    return await search.fetch_page(url)


async def _final_verdict(**kwargs) -> dict:
    return kwargs


REGISTRY: dict[str, dict] = {
    "search": {
        "fn": _search,
        "schema": {"type": "function", "function": {
            "name": "search",
            "description": "v2 merged web/news search. Returns credibility-scored evidence rows; top results include fetched page content.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Search query for the underlying fact, not the raw claim text."},
                "time_range": {"type": "string", "enum": ["day", "week", "month", "year"],
                               "description": "Optional SearXNG time filter for recent/time-sensitive claims."},
            }, "required": ["query"]},
        }},
    },
    "web_search": {
        "fn": _web_search,
        "schema": {"type": "function", "function": {
            "name": "web_search",
            "description": "General web/news search. Returns url, title, snippet, domain.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
                "recency": {"type": "string", "enum": ["day", "week", "month", "year"],
                            "description": "optional time filter"},
            }, "required": ["query"]},
        }},
    },
    "factcheck_search": {
        "fn": _factcheck_search,
        "schema": {"type": "function", "function": {
            "name": "factcheck_search",
            "description": "Search human fact-checks (Google Fact Check API + IFCN sites) for a claim.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"]},
        }},
    },
    "source_credibility": {
        "fn": _source_credibility,
        "schema": {"type": "function", "function": {
            "name": "source_credibility",
            "description": "Return a 0–1 credibility score for a source domain.",
            "parameters": {"type": "object", "properties": {
                "domain": {"type": "string"},
            }, "required": ["domain"]},
        }},
    },
    "fetch_page": {
        "fn": _fetch_page,
        "schema": {"type": "function", "function": {
            "name": "fetch_page",
            "description": "Fetch and read the text content of a URL. Use after web_search to verify what a page actually says about the question.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "Full URL to fetch"},
            }, "required": ["url"]},
        }},
    },
    "final_verdict": {
        "fn": _final_verdict,
        "schema": {"type": "function", "function": {
            "name": "final_verdict",
            "description": "Submit the final claim verdict. Every factual sentence in explanation must cite evidence as [e:e1].",
            "parameters": Verdict.model_json_schema(),
        }},
    },
}


def schemas(names: list[str]) -> list[dict]:
    return [REGISTRY[n]["schema"] for n in names]


async def call_tool(name: str, **kwargs):
    return await REGISTRY[name]["fn"](**kwargs)
