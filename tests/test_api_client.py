"""Tests for the dual-backend API layer.

The critical property is backward compatibility: an existing config.json that
only knows about `ollama_url` must produce byte-identical requests to what
CLAWCLI sent before the gateway support landed — same URL, same payload, and
crucially no Authorization header.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import api_client


# ── Fixtures ─────────────────────────────────────────────────────────────────

LEGACY = {"ollama_url": "http://localhost:11434", "model": "gemma4:26b",
          "temperature": 0.1, "context_window": 131072}

GATEWAY = {"api_base": "http://gateway.example:5010/v1", "api_key": "sk-gw-test",
           "model": "gemma4:26b", "temperature": 0.1, "context_window": 131072}


class FakeResponse:
    """Minimal stand-in for a streaming requests.Response."""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        for line in self._lines:
            yield line.encode() if isinstance(line, str) else line


# ── Format resolution ────────────────────────────────────────────────────────

def test_legacy_config_resolves_to_ollama():
    assert api_client.resolve_format(LEGACY) == "ollama"
    assert api_client.is_gateway(LEGACY) is False


def test_v1_suffix_infers_openai():
    assert api_client.resolve_format(GATEWAY) == "openai"


def test_explicit_format_overrides_inference():
    assert api_client.resolve_format({**LEGACY, "api_format": "openai"}) == "openai"
    assert api_client.resolve_format({**GATEWAY, "api_format": "ollama"}) == "ollama"


def test_api_base_falls_back_to_ollama_url():
    assert api_client.api_base(LEGACY) == "http://localhost:11434"
    assert api_client.api_base({"api_base": "", "ollama_url": "http://h:1/"}) == "http://h:1"


def test_bare_host_without_version_suffix_stays_ollama():
    # A gateway mounted at the root, or any non-versioned host, must not be
    # mistaken for OpenAI — that is what api_format is for.
    assert api_client.resolve_format({"ollama_url": "http://ollama.example:11434"}) == "ollama"


# ── Auth ─────────────────────────────────────────────────────────────────────

def test_no_key_sends_no_auth_header():
    """The backward-compatibility guarantee: bare Ollama sees no auth header."""
    assert api_client.auth_headers(LEGACY) == {}


def test_key_produces_bearer_header():
    assert api_client.auth_headers(GATEWAY) == {"Authorization": "Bearer sk-gw-test"}


def test_env_var_overrides_config_key(monkeypatch):
    monkeypatch.setenv(api_client.API_KEY_ENV, "sk-gw-from-env")
    assert api_client.api_key(GATEWAY) == "sk-gw-from-env"


def test_env_var_used_when_config_has_none(monkeypatch):
    monkeypatch.setenv(api_client.API_KEY_ENV, "sk-gw-env")
    assert api_client.auth_headers(LEGACY) == {"Authorization": "Bearer sk-gw-env"}


def test_redact_key_keeps_prefix_and_suffix():
    assert api_client.redact_key("sk-gw-abcdefghijklmnop") == "sk-gw-a…mnop"
    assert api_client.redact_key("short") == "***"
    assert api_client.redact_key("") == ""


# ── URLs ─────────────────────────────────────────────────────────────────────

def test_urls_for_each_backend():
    assert api_client.chat_url(LEGACY) == "http://localhost:11434/api/chat"
    assert api_client.models_url(LEGACY) == "http://localhost:11434/api/tags"
    assert api_client.chat_url(GATEWAY) == "http://gateway.example:5010/v1/chat/completions"
    assert api_client.models_url(GATEWAY) == "http://gateway.example:5010/v1/models"


# ── Payloads ─────────────────────────────────────────────────────────────────

def test_ollama_payload_unchanged():
    """Legacy payload shape must be preserved exactly, including num_ctx."""
    p = api_client.build_chat_payload(LEGACY, [{"role": "user", "content": "hi"}], [], True)
    assert p["model"] == "gemma4:26b"
    assert p["stream"] is True
    assert p["options"] == {"temperature": 0.1, "num_ctx": 131072}
    assert "stream_options" not in p
    assert p["messages"] == [{"role": "user", "content": "hi"}]


def test_ollama_payload_keeps_think_toggle():
    p = api_client.build_chat_payload({**LEGACY, "think": True}, [], [], True)
    assert p["think"] is True


def test_openai_payload_shape():
    p = api_client.build_chat_payload(GATEWAY, [{"role": "user", "content": "hi"}], [], True)
    assert p["temperature"] == 0.1
    assert p["stream_options"] == {"include_usage": True}
    assert "options" not in p          # Ollama-only nesting
    assert "num_ctx" not in json.dumps(p)


def test_openai_payload_omits_think():
    p = api_client.build_chat_payload({**GATEWAY, "think": True}, [], [], True)
    assert "think" not in p


def test_openai_message_conversion_adds_tool_call_ids():
    history = [
        {"role": "user", "content": "list /tmp"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "bash", "arguments": {"command": "ls"}}}]},
        {"role": "tool", "name": "bash", "content": "a.txt"},
    ]
    out = api_client._openai_messages(history)
    assert out[1]["tool_calls"][0]["id"] == out[2]["tool_call_id"]
    # arguments must be a JSON string on the wire, not a dict
    assert out[1]["tool_calls"][0]["function"]["arguments"] == '{"command": "ls"}'
    assert out[2]["role"] == "tool"


def test_openai_message_conversion_drops_null_tool_calls():
    out = api_client._openai_messages([{"role": "assistant", "content": "hi", "tool_calls": None}])
    assert "tool_calls" not in out[0]


# ── Streaming ────────────────────────────────────────────────────────────────

def test_ollama_stream_content_and_usage():
    resp = FakeResponse([
        json.dumps({"message": {"content": "Hel"}}),
        json.dumps({"message": {"content": "lo"}}),
        json.dumps({"done": True, "prompt_eval_count": 12, "eval_count": 3}),
    ])
    events = list(api_client.iter_stream(resp, "ollama"))
    assert [e["content"] for e in events if "content" in e] == ["Hel", "lo"]
    assert events[-1] == {"done": True, "tool_calls": [],
                          "prompt_tokens": 12, "completion_tokens": 3}


def test_ollama_stream_collects_tool_calls():
    resp = FakeResponse([
        json.dumps({"message": {"tool_calls": [{"function": {"name": "bash",
                                                             "arguments": {"command": "ls"}}}]}}),
        json.dumps({"done": True, "prompt_eval_count": 5, "eval_count": 1}),
    ])
    events = list(api_client.iter_stream(resp, "ollama"))
    assert events[-1]["tool_calls"][0]["function"]["name"] == "bash"


def test_ollama_stream_skips_malformed_lines():
    resp = FakeResponse(["not json", json.dumps({"message": {"content": "x"}}),
                         json.dumps({"done": True})])
    events = list(api_client.iter_stream(resp, "ollama"))
    assert [e["content"] for e in events if "content" in e] == ["x"]


def test_openai_stream_content_and_usage():
    resp = FakeResponse([
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":66,"completion_tokens":19}}',
        'data: [DONE]',
    ])
    events = list(api_client.iter_stream(resp, "openai"))
    assert [e["content"] for e in events if "content" in e] == ["Hel", "lo"]
    assert events[-1]["prompt_tokens"] == 66
    assert events[-1]["completion_tokens"] == 19


def test_openai_stream_reassembles_fragmented_tool_calls():
    """Cloud providers split a call across chunks; fragments must be joined by index."""
    resp = FakeResponse([
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"function":{"name":"bash","arguments":"{\\"comm"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"and\\": \\"ls\\"}"}}]}}]}',
        'data: [DONE]',
    ])
    events = list(api_client.iter_stream(resp, "openai"))
    calls = events[-1]["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "bash"
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls"}


def test_openai_stream_handles_parallel_tool_calls():
    resp = FakeResponse([
        'data: {"choices":[{"delta":{"tool_calls":['
        '{"index":1,"id":"b","function":{"name":"two","arguments":"{}"}},'
        '{"index":0,"id":"a","function":{"name":"one","arguments":"{}"}}]}}]}',
        'data: [DONE]',
    ])
    calls = list(api_client.iter_stream(resp, "openai"))[-1]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["one", "two"]  # sorted by index


def test_openai_stream_ends_without_done_sentinel():
    """A truncated stream must still yield a done event rather than hang."""
    resp = FakeResponse(['data: {"choices":[{"delta":{"content":"x"}}]}'])
    events = list(api_client.iter_stream(resp, "openai"))
    assert events[-1]["done"] is True


# ── Non-streaming ────────────────────────────────────────────────────────────

def test_parse_nonstream_both_formats():
    ollama = api_client.parse_nonstream(
        {"message": {"content": "hi"}, "prompt_eval_count": 4, "eval_count": 2}, "ollama")
    assert ollama["content"] == "hi" and ollama["prompt_tokens"] == 4

    openai = api_client.parse_nonstream(
        {"choices": [{"message": {"content": "hi"}}],
         "usage": {"prompt_tokens": 4, "completion_tokens": 2}}, "openai")
    assert openai["content"] == "hi" and openai["prompt_tokens"] == 4


# ── Vision ───────────────────────────────────────────────────────────────────

def test_vision_payload_ollama_uses_images_list():
    p = api_client.vision_payload(LEGACY, "B64", "describe", "llava")
    assert p["messages"][0]["images"] == ["B64"]
    assert p["messages"][0]["content"] == "describe"


def test_vision_payload_openai_uses_data_uri():
    p = api_client.vision_payload(GATEWAY, "B64", "describe", "qwen3-vl:8b", "image/jpeg")
    parts = p["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "describe"}
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64,B64"


# ── Model listing ────────────────────────────────────────────────────────────

def test_list_models_normalizes_both_backends(monkeypatch):
    class R:
        status_code = 200
        def __init__(self, payload): self._p = payload
        def json(self): return self._p
        def raise_for_status(self): pass

    monkeypatch.setattr(api_client.requests, "get", lambda *a, **k: R(
        {"models": [{"name": "gemma4:26b", "size": 2e10,
                     "details": {"parameter_size": "26B", "quantization_level": "Q4"}}]}))
    entry = api_client.list_models(LEGACY)[0]
    assert entry["name"] == "gemma4:26b" and entry["params"] == "26B"

    monkeypatch.setattr(api_client.requests, "get", lambda *a, **k: R(
        {"data": [{"id": "claude-opus-5", "owned_by": "anthropic",
                   "gateway": {"backend": "anthropic", "context_length": 200000,
                               "capabilities": ["completion", "tools"]}}]}))
    entry = api_client.list_models(GATEWAY)[0]
    assert entry["name"] == "claude-opus-5"
    assert entry["context_length"] == 200000
    assert entry["backend"] == "anthropic"


def test_auth_error_raised_on_401(monkeypatch):
    class R:
        status_code = 401
        def json(self): return {}
        def raise_for_status(self): pass

    monkeypatch.setattr(api_client.requests, "get", lambda *a, **k: R())
    with pytest.raises(api_client.AuthError):
        api_client.list_models(GATEWAY)
