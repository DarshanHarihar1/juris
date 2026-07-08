"""Tool registry (LLD §2). Maps tool-name → (OpenAI tool-calling JSON schema,
async Python fn). Built in Phase 2; Phase 3 investigators call schemas()/call_tool().
numeric_check / source deeper tools are deferred (LLD §0)."""
from . import credibility, search


async def _web_search(query: str, recency: str | None = None) -> list[dict]:
    return await search.web_search(query, recency=recency)


async def _factcheck_search(query: str) -> list[dict]:
    return await search.factcheck_search(query)


async def _source_credibility(domain: str) -> dict:
    return {"domain": domain, "credibility": credibility.score(domain)}


async def _fetch_page(url: str) -> dict:
    return await search.fetch_page(url)


REGISTRY: dict[str, dict] = {
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
}


def schemas(names: list[str]) -> list[dict]:
    return [REGISTRY[n]["schema"] for n in names]


async def call_tool(name: str, **kwargs):
    return await REGISTRY[name]["fn"](**kwargs)
