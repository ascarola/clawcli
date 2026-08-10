"""Backend-agnostic LLM API layer.

CLAWCLI talks to either:

  * **ollama** — a bare Ollama host, native API (`/api/chat`, `/api/tags`,
    `/api/show`), no authentication.
  * **openai** — an OpenAI-compatible endpoint such as a local AI gateway
    (`/chat/completions`, `/models`), bearer-token authenticated.

Everything above this module works in Ollama-ish terms: a message dict with
`content` and `tool_calls`, where a tool call is
`{"function": {"name": ..., "arguments": ...}}` and `arguments` may be a dict
(Ollama) or a JSON string (OpenAI). Callers already handle both.

The two wire formats differ in ways this module hides:

  * framing — Ollama streams newline-delimited JSON, OpenAI streams SSE
    `data:` lines terminated by `[DONE]`
  * tool calls — Ollama emits whole call objects, OpenAI emits fragments that
    must be reassembled by `index`
  * usage — `prompt_eval_count`/`eval_count` vs a `usage` object
  * sampling — Ollama nests these under `options` and accepts `num_ctx`;
    OpenAI has no context-size parameter at all
  * images — Ollama takes a bare base64 list, OpenAI takes `image_url`
    content parts holding a data URI
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterator

import requests

# Environment variable that overrides the configured key, so the token need
# not be written to config.json at all.
API_KEY_ENV = "CLAWCLI_API_KEY"

DEFAULT_BASE = "http://localhost:11434"


# ── Configuration resolution ─────────────────────────────────────────────────

def api_base(config: dict) -> str:
    """Base URL for the LLM API. `api_base` wins; `ollama_url` is the legacy name."""
    base = (config.get("api_base") or "").strip()
    if not base:
        base = (config.get("ollama_url") or "").strip()
    return (base or DEFAULT_BASE).rstrip("/")


def api_key(config: dict) -> str:
    """Bearer token, environment first so a key need never be persisted."""
    env = os.environ.get(API_KEY_ENV, "").strip()
    if env:
        return env
    return (config.get("api_key") or "").strip()


def resolve_format(config: dict) -> str:
    """Return 'openai' or 'ollama'.

    An explicit `api_format` is honoured; 'auto' (the default) infers the
    format from the URL, since OpenAI-compatible endpoints are conventionally
    mounted under a version prefix like /v1.
    """
    fmt = (config.get("api_format") or "auto").strip().lower()
    if fmt in ("openai", "ollama"):
        return fmt
    base = api_base(config)
    tail = base.rsplit("/", 1)[-1].lower()
    return "openai" if tail.startswith("v") and tail[1:].isdigit() else "ollama"


def is_gateway(config: dict) -> bool:
    """True when talking to an OpenAI-compatible endpoint rather than bare Ollama."""
    return resolve_format(config) == "openai"


def auth_headers(config: dict) -> dict:
    key = api_key(config)
    return {"Authorization": f"Bearer {key}"} if key else {}


def redact_key(key: str) -> str:
    """Mask a token for display, keeping enough to identify which key it is."""
    if not key:
        return ""
    if len(key) <= 12:
        return "***"
    return f"{key[:7]}…{key[-4:]}"


def chat_url(config: dict) -> str:
    base = api_base(config)
    return f"{base}/chat/completions" if is_gateway(config) else f"{base}/api/chat"


def models_url(config: dict) -> str:
    base = api_base(config)
    return f"{base}/models" if is_gateway(config) else f"{base}/api/tags"


class AuthError(Exception):
    """Raised when the endpoint rejects (or demands) credentials."""


def _check_auth(resp: requests.Response, config: dict) -> None:
    if resp.status_code in (401, 403):
        where = api_base(config)
        if api_key(config):
            raise AuthError(
                f"{where} rejected the API key (HTTP {resp.status_code}). "
                f"Set a valid key with /key <token>."
            )
        raise AuthError(
            f"{where} requires an API key (HTTP {resp.status_code}). "
            f"Set one with /key <token> or export {API_KEY_ENV}."
        )


# ── Request payloads ─────────────────────────────────────────────────────────

def build_chat_payload(config: dict, messages: list, tools: list, stream: bool) -> dict:
    """Shape a chat request for the active backend."""
    model = config.get("model", "")
    temperature = config.get("temperature", 0.1)

    if is_gateway(config):
        payload: dict[str, Any] = {
            "model": model,
            "messages": _openai_messages(messages),
            "stream": stream,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        if stream:
            # Without this the final chunk carries no token accounting, and the
            # context meter in the agentic loop goes blank.
            payload["stream_options"] = {"include_usage": True}
        # num_ctx and think have no OpenAI equivalent — the gateway sets the
        # context size from its own model registry, and surfaces any reasoning
        # trace as `reasoning_content` rather than a request-side toggle.
        return payload

    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "options": {
            "temperature": temperature,
            "num_ctx": config.get("context_window", 8192),
        },
    }
    if tools:
        payload["tools"] = tools
    # 'think' is a top-level chat parameter in the Ollama API, not a model option
    if config.get("think") is not None:
        payload["think"] = config["think"]
    return payload


def _openai_messages(messages: list) -> list:
    """Convert Ollama-shaped history to OpenAI's schema.

    Tool results carry `name` in both, but OpenAI additionally requires
    `tool_call_id`; assistant turns must drop a null `tool_calls` key.
    """
    out = []
    # Map each tool result back to the call it answers, in order.
    pending_ids: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
            calls = m.get("tool_calls")
            if calls:
                converted = []
                pending_ids = []
                for i, tc in enumerate(calls):
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if not isinstance(args, str):
                        args = json.dumps(args)
                    call_id = tc.get("id") or f"call_{i}"
                    pending_ids.append(call_id)
                    converted.append({
                        "id": call_id,
                        "type": "function",
                        "function": {"name": fn.get("name", ""), "arguments": args},
                    })
                msg["tool_calls"] = converted
            out.append(msg)
        elif role == "tool":
            call_id = pending_ids.pop(0) if pending_ids else "call_0"
            out.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": m.get("content") or "",
            })
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


# ── Streaming ────────────────────────────────────────────────────────────────

def post_chat(config: dict, messages: list, tools: list, stream: bool) -> requests.Response:
    payload = build_chat_payload(config, messages, tools, stream)
    headers = {"Content-Type": "application/json", **auth_headers(config)}
    resp = requests.post(
        chat_url(config),
        json=payload,
        headers=headers,
        stream=stream,
        timeout=config.get("ollama_timeout", 1800),
    )
    _check_auth(resp, config)
    resp.raise_for_status()
    return resp


def iter_stream(resp: requests.Response, fmt: str) -> Iterator[dict]:
    """Yield normalized stream events, hiding the wire format.

    Events are either ``{"content": str}`` for a text delta, or a single final
    ``{"done": True, "tool_calls": [...], "prompt_tokens": int,
    "completion_tokens": int}``. Tool calls are fully reassembled before the
    done event fires.
    """
    if fmt == "openai":
        yield from _iter_openai(resp)
    else:
        yield from _iter_ollama(resp)


def _iter_ollama(resp: requests.Response) -> Iterator[dict]:
    tool_calls: list = []
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = chunk.get("message", {})
        delta = msg.get("content", "")
        if delta:
            yield {"content": delta}
        if msg.get("tool_calls"):
            tool_calls.extend(msg["tool_calls"])
        if chunk.get("done"):
            yield {
                "done": True,
                "tool_calls": tool_calls,
                "prompt_tokens": chunk.get("prompt_eval_count", 0),
                "completion_tokens": chunk.get("eval_count", 0),
            }
            return
    yield {"done": True, "tool_calls": tool_calls, "prompt_tokens": 0, "completion_tokens": 0}


def _iter_openai(resp: requests.Response) -> Iterator[dict]:
    # Fragments arrive keyed by index; a call's name and arguments may be split
    # across any number of chunks, so accumulate until the stream ends.
    slots: dict[int, dict] = {}
    prompt_tokens = 0
    completion_tokens = 0

    for line in resp.iter_lines():
        if not line:
            continue
        text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
        if not text.startswith("data:"):
            continue
        data = text[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue

        usage = chunk.get("usage") or {}
        if usage:
            prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
            completion_tokens = usage.get("completion_tokens", completion_tokens)

        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}

        content = delta.get("content")
        if content:
            yield {"content": content}

        for frag in delta.get("tool_calls") or []:
            idx = frag.get("index", 0)
            slot = slots.setdefault(
                idx, {"id": None, "function": {"name": "", "arguments": ""}}
            )
            if frag.get("id"):
                slot["id"] = frag["id"]
            fn = frag.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] = fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]

    tool_calls = [slots[i] for i in sorted(slots)]
    yield {
        "done": True,
        "tool_calls": tool_calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def parse_nonstream(data: dict, fmt: str) -> dict:
    """Normalize a non-streaming response to {'content', 'tool_calls', tokens}."""
    if fmt == "openai":
        choices = data.get("choices") or [{}]
        msg = choices[0].get("message") or {}
        usage = data.get("usage") or {}
        return {
            "content": msg.get("content") or "",
            "tool_calls": msg.get("tool_calls") or [],
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        }
    msg = data.get("message", {})
    return {
        "content": msg.get("content", ""),
        "tool_calls": msg.get("tool_calls") or [],
        "prompt_tokens": data.get("prompt_eval_count", 0),
        "completion_tokens": data.get("eval_count", 0),
    }


# ── Model discovery ──────────────────────────────────────────────────────────

def list_models(config: dict, timeout: int = 10) -> list[dict]:
    """Return normalized model entries for both backends.

    Each entry has `name` plus whatever metadata the backend offers:
    `size`/`params`/`quant` from Ollama, `context_length`/`capabilities`/
    `backend` from a gateway.
    """
    headers = auth_headers(config)
    resp = requests.get(models_url(config), headers=headers, timeout=timeout)
    _check_auth(resp, config)
    resp.raise_for_status()
    data = resp.json()

    if is_gateway(config):
        out = []
        for entry in data.get("data", []):
            gw = entry.get("gateway") or {}
            out.append({
                "name": entry.get("id", ""),
                "size": None,
                "params": None,
                "quant": None,
                "context_length": gw.get("context_length"),
                "capabilities": gw.get("capabilities") or [],
                "backend": gw.get("backend") or gw.get("type") or entry.get("owned_by", ""),
                "description": gw.get("description") or "",
            })
        return out

    out = []
    for m in data.get("models", []):
        details = m.get("details") or {}
        out.append({
            "name": m.get("name", ""),
            "size": m.get("size"),
            "params": details.get("parameter_size", ""),
            "quant": details.get("quantization_level", ""),
            "context_length": None,
            "capabilities": [],
            "backend": "ollama",
            "description": "",
        })
    return out


def model_context_length(config: dict, model: str, timeout: int = 10) -> int | None:
    """Best-known max context for `model`, or None if the backend won't say."""
    if is_gateway(config):
        # /v1/models already carries context_length — no per-model call needed.
        for entry in list_models(config, timeout=timeout):
            if entry["name"] == model:
                ctx = entry.get("context_length")
                return int(ctx) if ctx else None
        return None

    info = requests.post(
        f"{api_base(config)}/api/show", json={"name": model}, timeout=timeout
    ).json()
    model_info = info.get("model_info", {})
    ctx = next((v for k, v in model_info.items() if "context_length" in k), None)
    return int(ctx) if ctx else None


# ── Vision ───────────────────────────────────────────────────────────────────

def vision_payload(config: dict, image_b64: str, prompt: str, model: str, mime: str = "image/png") -> dict:
    """Single-image chat request in whichever schema the backend speaks."""
    if is_gateway(config):
        return {
            "model": model,
            "stream": False,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                ],
            }],
        }
    return {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
    }
