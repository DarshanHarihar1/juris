"""Tool registry. Verify exposes /search only; verdict is structured JSON (not a tool)."""
from . import search


async def _search(query: str, time_range: str | None = None, claim=None,
                  evidence_seq_start: int = 1) -> list[dict]:
    if claim is None:
        return await search.web_search(query, recency=time_range)
    return await search.search(query, time_range=time_range, claim=claim,
                               evidence_seq_start=evidence_seq_start)


REGISTRY: dict[str, dict] = {
    "search": {
        "fn": _search,
        "schema": {"type": "function", "function": {
            "name": "search",
            "description": (
                "Web/news search. REQUIRED argument: query (string). "
                "Returns the top-5 results by score (score, url, title, content), "
                "with full page text fetched for those URLs. "
                "Call whenever the claim may be time-sensitive or parametric knowledge is insufficient."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Required. Search query for the underlying fact, not the raw claim text.",
                    },
                    "time_range": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                        "description": "Optional SearXNG time filter for recent/time-sensitive claims.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }},
    },
}


def schemas(names: list[str]) -> list[dict]:
    return [REGISTRY[n]["schema"] for n in names]


async def call_tool(name: str, **kwargs):
    return await REGISTRY[name]["fn"](**kwargs)
