"""NVIDIA NIM client — the sole gateway for every model call (LLD §2).
OpenAI-compatible. One call() function: role→model resolution, structured-output
validation, one retry on schema failure, fallback model on provider error, and a
global semaphore under the ~40 req/min free-tier ceiling."""
import asyncio
import json
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from ..config import nim_api_key, provider, role

Message = dict[str, Any]
Tool = dict[str, Any]

# Global semaphore shared by every call — caps concurrent in-flight NIM requests
# so concurrent claim verification stays under the free-tier ceiling.
# ponytail: concurrency cap, not a token-bucket. Add time-based limiting if 429s persist.
_sem = asyncio.Semaphore(provider().get("rate_limit", 40))

# Per-request ceiling for every NIM call. The OpenAI client default is 600s, which would
# let one slow model stall a whole job; on timeout call() falls back to the fallback model.
NIM_TIMEOUT = 45.0

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(base_url=provider()["base_url"], api_key=nim_api_key())
    return _client


@dataclass
class NimResponse:
    text: str
    model: str
    parsed: BaseModel | None = None


def _resolve_model(role_name: str) -> tuple[str, dict]:
    """Return (model_id, params) for a role. Params carry temp etc. where present."""
    entry = role(role_name)
    if isinstance(entry, dict):
        params = {k: v for k, v in entry.items() if k not in ("model", "tools", "dims")}
        if "temp" in params:                       # config uses `temp`; API wants `temperature`
            params["temperature"] = params.pop("temp")
        return entry["model"], params
    raise ValueError(f"role '{role_name}' is not a single-model role (got {type(entry).__name__})")


async def call(
    role_name: str | None,
    messages: list[Message],
    tools: list[Tool] | None = None,
    response_schema: type[BaseModel] | None = None,
    model_id: str | None = None,
) -> NimResponse:
    # model_id override remains available for explicit one-off model calls.
    model, params = (model_id, {}) if model_id else _resolve_model(role_name)
    fallback = provider()["fallback_model"]

    async def _once(model_id: str, extra_user: str | None = None) -> Any:
        msgs = messages + ([{"role": "user", "content": extra_user}] if extra_user else [])
        kwargs: dict[str, Any] = {"model": model_id, "messages": msgs, "timeout": NIM_TIMEOUT, **params}
        if tools:
            kwargs["tools"] = tools
        if response_schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        async with _sem:
            return await _get_client().chat.completions.create(**kwargs)

    def _wrap(resp: Any, model_id: str) -> NimResponse:
        text = resp.choices[0].message.content or ""
        parsed = response_schema.model_validate_json(text) if response_schema else None
        return NimResponse(text=text, model=model_id, parsed=parsed)

    # 1. primary model
    try:
        resp = await _once(model)
        try:
            return _wrap(resp, model)
        except (ValidationError, json.JSONDecodeError) as e:
            # 2. one retry, feeding the validation error back to the model
            resp = await _once(model, extra_user=f"Your previous reply failed schema validation: {e}. Reply with ONLY valid JSON matching the schema.")
            return _wrap(resp, model)
    except Exception:
        # 3. provider error (timeout/429/5xx) → fallback model, same tier
        resp = await _once(fallback)
        return _wrap(resp, fallback)


async def chat(
    model_id: str | None,
    messages: list[Message],
    tools: list[Tool] | None = None,
    *,
    role_name: str | None = None,
    timeout: float | None = None,
    tool_choice: str | dict | None = None,
) -> Any:
    """One raw chat completion under the shared semaphore, returning the assistant
    message object (.content, .tool_calls) for the v2 verifier loop.
    Callers may pass an explicit model_id or resolve a single-model role."""
    params: dict[str, Any] = {}
    if model_id is None:
        if role_name is None:
            raise ValueError("chat() requires model_id or role_name")
        model_id, params = _resolve_model(role_name)
    kwargs: dict[str, Any] = {"model": model_id, "messages": messages, "timeout": timeout or NIM_TIMEOUT, **params}
    if tools:
        # parallel_tool_calls=False: some NIM models (e.g. llama-3.1-8b) 500 on multi
        # tool-calls per turn ("only supports single tool-calls at once"). One at a time.
        kwargs["tools"] = tools
        kwargs["parallel_tool_calls"] = False
    if tool_choice is not None:
        kwargs["tool_choice"] = (
            {"type": "function", "function": {"name": tool_choice}}
            if isinstance(tool_choice, str) else tool_choice
        )
    async with _sem:
        resp = await _get_client().chat.completions.create(**kwargs)
    return resp.choices[0].message


