"""LLM client — sole gateway for every model call (MeshAPI OpenAI-compatible API).
Wrapped with LangSmith for optional tracing."""
import asyncio
import json
from dataclasses import dataclass
from typing import Any

from langsmith.wrappers import wrap_openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from ..config import model_provider_name, mesh_api_key, provider, role, role_provider_name

Message = dict[str, Any]
Tool = dict[str, Any]

# Per-provider semaphores cap concurrent in-flight requests so claim verification
# stays under each provider's rate ceiling.
_sems: dict[str, asyncio.Semaphore] = {}

# Per-request ceiling. The OpenAI client default is 600s, which would
# let one slow model stall a whole job; on timeout call() falls back to the fallback model.
MESH_TIMEOUT = 45.0

# Provider-specific knobs — openai SDK may not accept them as top-level kwargs.
_EXTRA_BODY_KEYS = frozenset({"reasoning_effort", "include_reasoning", "reasoning_format"})

# Providers whose routed models reject response_format (json_object/json_schema).
# Callers still get valid JSON via the "Return ONLY JSON" prompt instructions +
# the existing schema-validation retry in call() below.
_NO_RESPONSE_FORMAT_PROVIDERS = frozenset({"meshapi"})

_clients: dict[str, AsyncOpenAI] = {}


def _get_client(provider_name: str) -> AsyncOpenAI:
    client = _clients.get(provider_name)
    if client is None:
        cfg = provider(provider_name)
        client = wrap_openai(
            AsyncOpenAI(base_url=cfg["base_url"], api_key=mesh_api_key(provider_name)),
            tracing_extra={"metadata": {"service": "juris-backend"}},
        )
        _clients[provider_name] = client
    return client


def _get_semaphore(provider_name: str) -> asyncio.Semaphore:
    sem = _sems.get(provider_name)
    if sem is None:
        sem = asyncio.Semaphore(provider(provider_name).get("rate_limit", 40))
        _sems[provider_name] = sem
    return sem


@dataclass
class MeshResponse:
    text: str
    model: str
    parsed: BaseModel | None = None


def _resolve_model(role_name: str) -> tuple[str, dict, str]:
    """Return (model_id, params, provider_name) for a role."""
    entry = role(role_name)
    if isinstance(entry, dict):
        params = {k: v for k, v in entry.items() if k not in ("model", "tools", "dims")}
        provider_name = params.pop("provider", role_provider_name(role_name))
        if "temp" in params:                       # config uses `temp`; API wants `temperature`
            params["temperature"] = params.pop("temp")
        return entry["model"], params, provider_name
    raise ValueError(f"role '{role_name}' is not a single-model role (got {type(entry).__name__})")


def _split_params(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move provider-specific keys into extra_body for the OpenAI SDK."""
    params = dict(params)
    extra = {k: params.pop(k) for k in list(params) if k in _EXTRA_BODY_KEYS}
    return params, extra


async def call(
    role_name: str | None,
    messages: list[Message],
    tools: list[Tool] | None = None,
    response_schema: type[BaseModel] | None = None,
    model_id: str | None = None,
) -> MeshResponse:
    # model_id override remains available for explicit one-off model calls.
    model, params, provider_name = (
        model_id,
        {},
        role_provider_name(role_name) if role_name else model_provider_name(model_id),
    ) if model_id else _resolve_model(role_name)
    fallback = provider(provider_name)["fallback_model"]

    async def _once(model_id: str, extra_user: str | None = None) -> Any:
        msgs = messages + ([{"role": "user", "content": extra_user}] if extra_user else [])
        std, extra = _split_params(params)
        kwargs: dict[str, Any] = {"model": model_id, "messages": msgs, "timeout": MESH_TIMEOUT, **std}
        if tools:
            kwargs["tools"] = tools
        if response_schema is not None and provider_name not in _NO_RESPONSE_FORMAT_PROVIDERS:
            kwargs["response_format"] = {"type": "json_object"}
        if extra:
            kwargs["extra_body"] = extra
        async with _get_semaphore(provider_name):
            return await _get_client(provider_name).chat.completions.create(**kwargs)

    def _wrap(resp: Any, model_id: str) -> MeshResponse:
        text = resp.choices[0].message.content or ""
        parsed = response_schema.model_validate_json(text) if response_schema else None
        return MeshResponse(text=text, model=model_id, parsed=parsed)

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
    message object (.content, .tool_calls) for the verifier loop.
    Callers may pass an explicit model_id or resolve a single-model role."""
    params: dict[str, Any] = {}
    provider_name: str
    if model_id is None:
        if role_name is None:
            raise ValueError("chat() requires model_id or role_name")
        model_id, params, provider_name = _resolve_model(role_name)
    else:
        provider_name = role_provider_name(role_name) if role_name else model_provider_name(model_id)
    std, extra = _split_params(params)
    kwargs: dict[str, Any] = {"model": model_id, "messages": messages, "timeout": timeout or MESH_TIMEOUT, **std}
    if tools:
        # parallel_tool_calls=False: gpt-oss does not support parallel tool calls;
        # keep one-at-a-time for the verifier loop.
        kwargs["tools"] = tools
        kwargs["parallel_tool_calls"] = False
    if tool_choice is not None:
        kwargs["tool_choice"] = (
            {"type": "function", "function": {"name": tool_choice}}
            if isinstance(tool_choice, str) else tool_choice
        )
    if extra:
        kwargs["extra_body"] = extra
    async with _get_semaphore(provider_name):
        resp = await _get_client(provider_name).chat.completions.create(**kwargs)
    return resp.choices[0].message
