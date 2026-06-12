"""MCP (Model Context Protocol) server integration via Streamable HTTP transport."""
from __future__ import annotations

import json
import requests


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, url: str, token: str = "", timeout: int = 30):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session_id: str | None = None
        self._tools: list[dict] = []

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, method: str, params: dict, req_id: int | None = 1) -> dict:
        payload: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if req_id is not None:
            payload["id"] = req_id
        resp = requests.post(self.url, json=payload, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        if req_id is None:
            return {}  # notification — no response expected
        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            return self._parse_sse(resp.text)
        data = resp.json()
        if "error" in data:
            raise MCPError(f"MCP error: {data['error']}")
        return data.get("result", {})

    def _parse_sse(self, text: str) -> dict:
        for line in text.splitlines():
            if line.startswith("data: "):
                try:
                    msg = json.loads(line[6:])
                    if "result" in msg:
                        return msg["result"]
                    if "error" in msg:
                        raise MCPError(f"MCP error: {msg['error']}")
                except (json.JSONDecodeError, KeyError):
                    pass
        raise MCPError("No result found in SSE response")

    def initialize(self) -> bool:
        """Handshake with the MCP server. Returns True on success."""
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "clawcli", "version": "1.3.0"},
                },
            }
            resp = requests.post(
                self.url, json=payload, headers=self._headers(), timeout=self.timeout
            )
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "json" in ct and "error" in resp.json():
                return False  # HTTP 200 but JSON-RPC error (bad token, etc.)
            self.session_id = resp.headers.get("Mcp-Session-Id")
            # Send initialized notification (fire-and-forget, some servers require it)
            try:
                self._post("notifications/initialized", {}, req_id=None)
            except Exception:
                pass
            return True
        except Exception:
            return False

    def list_tools(self) -> list[dict]:
        result = self._post("tools/list", {}, req_id=2)
        self._tools = result.get("tools", [])
        return self._tools

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self._post("tools/call", {"name": name, "arguments": arguments}, req_id=3)
        content = result.get("content", [])
        if not content:
            return str(result) if result else "(no output)"
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "image":
                    parts.append(f"[image data — not displayable in terminal]")
                else:
                    parts.append(json.dumps(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)


def mcp_tools_to_ollama(tools: list[dict]) -> list[dict]:
    """Convert MCP tool definitions to Ollama function call format."""
    result = []
    for tool in tools:
        name = tool.get("name", "")
        schema = tool.get("inputSchema", {"type": "object", "properties": {}})
        desc = tool.get("description", "")
        # If the schema only has a single 'params' property, hint the model to use
        # {"params": {...}} argument format to avoid flat-arg confusion.
        props = schema.get("properties", {})
        if list(props.keys()) == ["params"]:
            desc = f"{desc} (pass arguments as: params={{...}})"
        result.append({
            "type": "function",
            "function": {
                "name": f"mcp__{name}",
                "description": f"[MCP] {desc}",
                "parameters": schema,
            },
        })
    return result


def check_mcp_health(url: str, token: str = "") -> tuple[bool, str]:
    try:
        client = MCPClient(url, token)
        if not client.initialize():
            return False, "initialize failed — check URL and token"
        tools = client.list_tools()
        return True, f"{len(tools)} tool(s) available"
    except requests.exceptions.ConnectionError:
        return False, "connection refused"
    except requests.exceptions.Timeout:
        return False, "timed out"
    except Exception as e:
        return False, str(e)
