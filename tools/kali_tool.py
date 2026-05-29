"""Kali Linux security tool integration via mcp-kali-server REST API."""

import requests

DESTRUCTIVE_TOOLS = {"hydra", "john", "sqlmap", "metasploit", "command"}

_TOOL_ENDPOINTS = {
    "nmap":       "/api/tools/nmap",
    "nikto":      "/api/tools/nikto",
    "gobuster":   "/api/tools/gobuster",
    "dirb":       "/api/tools/dirb",
    "wpscan":     "/api/tools/wpscan",
    "enum4linux":  "/api/tools/enum4linux",
    "sqlmap":     "/api/tools/sqlmap",
    "hydra":      "/api/tools/hydra",
    "john":       "/api/tools/john",
    "metasploit": "/api/tools/metasploit",
    "command":    "/api/command",
}

_DEFAULTS = {
    "nmap":     {"scan_type": "-sV", "additional_args": "-T4 -Pn"},
    "gobuster": {"wordlist": "/usr/share/wordlists/dirb/common.txt"},
    "dirb":     {"wordlist": "/usr/share/wordlists/dirb/common.txt"},
    "hydra":    {"password_file": "/usr/share/wordlists/rockyou.txt"},
    "john":     {"wordlist": "/usr/share/wordlists/rockyou.txt"},
}


def check_health(base_url: str, timeout: int = 10) -> tuple[bool, str]:
    """GET /health — returns (reachable, status_message)."""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/health", timeout=timeout)
        if resp.status_code == 200:
            return True, f"Kali server reachable at {base_url}"
        return False, f"Kali server returned HTTP {resp.status_code}"
    except requests.ConnectionError:
        return False, f"Cannot connect to Kali server at {base_url}"
    except requests.Timeout:
        return False, f"Kali server timed out at {base_url}"
    except Exception as e:
        return False, f"Kali server error: {e}"


def run_tool(base_url: str, tool: str, params: dict, timeout: int = 300) -> dict:
    """POST to the tool endpoint and return the JSON response dict."""
    endpoint = _TOOL_ENDPOINTS.get(tool)
    if not endpoint:
        return {"success": False, "stdout": "", "stderr": f"Unknown tool: {tool}", "return_code": -1}

    # Apply sensible defaults without overriding caller-supplied values
    for key, val in _DEFAULTS.get(tool, {}).items():
        params.setdefault(key, val)

    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}{endpoint}",
            json=params,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.Timeout:
        return {"success": False, "timed_out": True, "stdout": "", "stderr": "Request timed out", "return_code": -1}
    except requests.HTTPError as e:
        return {"success": False, "timed_out": False, "stdout": "", "stderr": str(e), "return_code": -1}
    except Exception as e:
        return {"success": False, "timed_out": False, "stdout": "", "stderr": str(e), "return_code": -1}


def format_result(tool: str, result: dict) -> str:
    """Render a tool result dict into a structured string for the model to interpret."""
    parts = [f"=== {tool.upper()} RESULTS ==="]
    parts.append(f"success={result.get('success', False)}  return_code={result.get('return_code', '?')}")
    if result.get("timed_out"):
        parts.append("WARNING: tool timed out — output may be partial")
    if result.get("partial_results"):
        parts.append("NOTE: partial results returned")
    stdout = (result.get("stdout") or "").strip()
    stderr = (result.get("stderr") or "").strip()
    if stdout:
        parts.append("\nSTDOUT:\n" + stdout)
    if stderr:
        parts.append("\nSTDERR:\n" + stderr)
    if not stdout and not stderr:
        parts.append("(no output)")
    return "\n".join(parts)
