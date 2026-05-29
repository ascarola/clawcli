"""Web search via SearXNG and URL fetching."""

import re
import json
import ipaddress
import socket
import concurrent.futures
import requests
from urllib.parse import urlparse, urlunparse

try:
    from curl_cffi import requests as _cffi_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _PRIVATE_NETS)
    except ValueError:
        return True


def _resolve_url(url: str, timeout: float = 5.0) -> tuple[str, str] | None:
    """Resolve url hostname to IP once. Returns (resolved_ip, host) or None if blocked/failed."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if not host:
            return None
        # If host is an IP literal, check it directly without DNS resolution
        try:
            ipaddress.ip_address(host)  # raises ValueError for hostnames
            if _is_private_ip(host):
                return None
            return (host, host)
        except ValueError:
            pass  # not an IP literal — fall through to DNS resolution
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(socket.getaddrinfo, host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
            addrs = future.result(timeout=timeout)
        if not addrs:
            return None
        ip = addrs[0][4][0]
        if _is_private_ip(ip):
            return None
        return (ip, host)
    except (socket.gaierror, concurrent.futures.TimeoutError, Exception):
        return None


def web_search(query: str, searxng_url: str, num_results: int = 10) -> str:
    if not searxng_url:
        return "Web search is unavailable — SearXNG is not configured. Set searxng_url in config.json."
    try:
        params = {
            "q": query,
            "format": "json",
            "engines": "google,bing,duckduckgo",
            "language": "en",
        }
        resp = requests.get(
            f"{searxng_url.rstrip('/')}/search",
            params=params,
            timeout=15,
            headers={"User-Agent": "CLAWCLI/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])[:num_results]
        if not results:
            return "No search results found."
        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")
            url = r.get("url", "")
            snippet = r.get("content", "")
            lines.append(f"{i}. {title}")
            lines.append(f"   URL: {url}")
            if snippet:
                lines.append(f"   {snippet[:300]}")
            lines.append("")
        return "\n".join(lines)
    except requests.RequestException as e:
        return f"Search error: {e}"
    except Exception as e:
        return f"Error: {e}"


def web_fetch(url: str, max_chars: int = 8000) -> str:
    # Resolve DNS once and pin the IP — prevents DNS rebinding (check and connect use same address)
    resolved = _resolve_url(url)
    if resolved is None:
        return f"Error: Fetching private/internal addresses is not permitted: {url}"
    ip, host = resolved
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        if _CURL_CFFI_AVAILABLE:
            # Pass resolve hint so curl uses the already-checked IP
            resp = _cffi_requests.get(
                url,
                impersonate="chrome",
                timeout=20,
                resolve=[f"{host}:{port}:{ip}"],
            )
        else:
            # Note: requests fallback re-resolves DNS and does not pin the IP checked above.
            # DNS rebinding protection is incomplete on this path. Install curl_cffi to fix.
            resp = requests.get(
                url,
                timeout=20,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; CLAWCLI/1.0)",
                    "Accept": "text/html,application/xhtml+xml,text/plain",
                },
            )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            return json.dumps(resp.json(), indent=2)[:max_chars]
        text = resp.text
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"Fetch error: {e}"
