"""Web search via SearXNG and URL fetching."""
from __future__ import annotations

import re
import json
import ipaddress
import socket
import concurrent.futures
import requests
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

try:
    from curl_cffi import requests as _cffi_requests
    _CURL_CFFI_AVAILABLE = True
    # curl_cffi 0.16 dropped the `resolve=` keyword in favour of passing raw
    # libcurl options. CurlOpt.RESOLVE is how we pin the checked IP on both,
    # so probe once and pick the right spelling at call time.
    try:
        from curl_cffi import CurlOpt as _CurlOpt
        _CURL_CFFI_USES_CURL_OPTIONS = True
    except ImportError:
        _CurlOpt = None
        _CURL_CFFI_USES_CURL_OPTIONS = False
except ImportError:
    _CURL_CFFI_AVAILABLE = False
    _CurlOpt = None
    _CURL_CFFI_USES_CURL_OPTIONS = False


def _pin_kwargs(host: str, port: int, ip: str) -> dict:
    """Keyword args telling curl to use `ip` for `host:port`, across curl_cffi versions."""
    entry = f"{host}:{port}:{ip}"
    if _CURL_CFFI_USES_CURL_OPTIONS:
        return {"curl_options": {_CurlOpt.RESOLVE: [entry]}}
    return {"resolve": [entry]}  # curl_cffi < 0.16

class _TextExtractor(HTMLParser):
    """Strip tags and skip script/style content for plain-text extraction."""
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        return parser.get_text()
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


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


# Shared, never shut down: a ThreadPoolExecutor used as a context manager calls
# shutdown(wait=True) on exit, which blocks until getaddrinfo returns however
# long that takes — silently defeating the timeout passed to future.result().
# Keeping one pool alive lets the timeout actually bound the wait.
_DNS_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="clawcli-dns"
)

# Only these can be fetched. Anything else (file:, gopher:, ftp:, data:) is a
# way to reach local resources that IP checks do not cover.
_ALLOWED_SCHEMES = {"http", "https"}


def _resolve_url(url: str, timeout: float = 5.0) -> tuple[str, str] | None:
    """Resolve url hostname to IP. Returns (resolved_ip, host) or None if blocked/failed."""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
            return None
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
        future = _DNS_POOL.submit(
            socket.getaddrinfo, host, port, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
        addrs = future.result(timeout=timeout)
        if not addrs:
            return None
        # Every address the name resolves to must be public, not just the first:
        # a host publishing both a public and a private A record would otherwise
        # be reachable whenever the private one sorted first.
        for addr in addrs:
            if _is_private_ip(addr[4][0]):
                return None
        return (addrs[0][4][0], host)
    except Exception:
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


_MAX_REDIRECTS = 5
_REDIRECT_CODES = {301, 302, 303, 307, 308}


def _fetch_once(url: str, ip: str, host: str, port: int):
    """Single request with redirects disabled, connecting to the vetted IP."""
    if _CURL_CFFI_AVAILABLE:
        # Pass resolve hint so curl uses the already-checked IP
        return _cffi_requests.get(
            url,
            impersonate="chrome",
            timeout=20,
            allow_redirects=False,
            **_pin_kwargs(host, port, ip),
        )
    # Note: requests fallback re-resolves DNS and does not pin the IP checked
    # above. DNS rebinding protection is incomplete on this path. Install
    # curl_cffi to fix. Redirects are still validated hop by hop.
    return requests.get(
        url,
        timeout=20,
        allow_redirects=False,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; CLAWCLI/1.0)",
            "Accept": "text/html,application/xhtml+xml,text/plain",
        },
    )


def web_fetch(url: str, max_chars: int = 8000) -> str:
    # Redirects are followed manually: letting the HTTP client follow them
    # would send the *redirect target* unvalidated, so a public URL answering
    # 302 -> http://192.168.1.10/ would reach the private network despite the
    # check below. Every hop is re-resolved and re-checked.
    current = url
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            resolved = _resolve_url(current)
            if resolved is None:
                where = "" if current == url else f" (redirected from {url})"
                return (
                    f"Error: Fetching private/internal addresses is not "
                    f"permitted: {current}{where}"
                )
            ip, host = resolved
            parsed = urlparse(current)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)

            resp = _fetch_once(current, ip, host, port)

            location = resp.headers.get("location", "")
            if resp.status_code in _REDIRECT_CODES and location:
                # Resolve relative Locations against the URL that issued them.
                current = urljoin(current, location)
                continue

            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "json" in content_type:
                return json.dumps(resp.json(), indent=2)[:max_chars]
            return _html_to_text(resp.text)[:max_chars]

        return f"Error: too many redirects (more than {_MAX_REDIRECTS}) starting at {url}"
    except Exception as e:
        return f"Fetch error: {e}"
