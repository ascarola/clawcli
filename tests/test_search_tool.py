"""Tests for web_fetch's SSRF defences.

The threat is the model being talked into fetching something on the local
network — by a prompt-injected page, or just by guessing at internal URLs.
Blocking the *first* URL is not enough, because a public host can answer with
a redirect to a private one.
"""

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import search_tool
from tools.search_tool import _is_private_ip, _resolve_url, web_fetch


# ── Address classification ───────────────────────────────────────────────────

@pytest.mark.parametrize("ip", [
    "10.1.2.3", "172.16.0.1", "192.168.1.243", "127.0.0.1",
    "169.254.169.254",           # cloud metadata
    "0.0.0.0", "::1", "fc00::1",
])
def test_private_addresses_rejected(ip):
    assert _is_private_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "203.0.113.10", "2606:4700::1111"])
def test_public_addresses_accepted(ip):
    assert not _is_private_ip(ip)


def test_unparseable_address_treated_as_private():
    """Fail closed: anything we cannot classify must not be fetched."""
    assert _is_private_ip("not-an-ip")


# ── Scheme restriction ───────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://h/x", "ftp://h/x", "data:text/html,x",
])
def test_non_http_schemes_blocked(url):
    assert _resolve_url(url) is None


def test_ip_literal_private_blocked_without_dns():
    assert _resolve_url("http://192.168.1.1/") is None
    assert _resolve_url("http://127.0.0.1:11434/api/tags") is None


# ── DNS handling ─────────────────────────────────────────────────────────────

def test_all_resolved_addresses_must_be_public(monkeypatch):
    """A name publishing both a public and a private A record must be refused."""
    def both(*a, **k):
        return [(2, 1, 6, "", ("203.0.113.10", 443)),
                (2, 1, 6, "", ("192.168.1.5", 443))]
    monkeypatch.setattr(search_tool.socket, "getaddrinfo", both)
    assert _resolve_url("https://split-horizon.example") is None


def test_public_only_resolution_accepted(monkeypatch):
    monkeypatch.setattr(search_tool.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("203.0.113.10", 443))])
    assert _resolve_url("https://ok.example") == ("203.0.113.10", "ok.example")


def test_dns_timeout_is_actually_bounded(monkeypatch):
    """Regression: a context-managed pool joined its worker, ignoring the timeout."""
    def slow(*a, **k):
        time.sleep(10)
        return [(2, 1, 6, "", ("203.0.113.1", 443))]
    monkeypatch.setattr(search_tool.socket, "getaddrinfo", slow)
    start = time.monotonic()
    assert _resolve_url("https://slow.example", timeout=1.0) is None
    assert time.monotonic() - start < 3.0, "timeout did not bound the wait"


# ── Redirect re-validation ───────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, headers=None, text="<html>ok</html>"):
        self.status_code = status
        self.headers = headers or {"content-type": "text/html"}
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {}


def _fetch_with(monkeypatch, hops):
    """Drive web_fetch over a scripted sequence of responses, all hosts public."""
    monkeypatch.setattr(search_tool, "_resolve_url",
                        lambda u, timeout=5.0: None if "private" in u or "192.168." in u
                        or "127.0.0.1" in u else ("203.0.113.10", "host.example"))
    seen = []

    def fake_once(url, ip, host, port):
        seen.append(url)
        return hops.pop(0)

    monkeypatch.setattr(search_tool, "_fetch_once", fake_once)
    return web_fetch("http://start.example/"), seen


def test_redirect_to_private_address_blocked(monkeypatch):
    out, seen = _fetch_with(monkeypatch, [
        FakeResp(302, {"location": "http://192.168.1.243:5010/v1/models"}),
    ])
    assert "not permitted" in out
    assert "192.168.1.243" in out
    assert len(seen) == 1, "must not issue a request to the private target"


def test_redirect_to_loopback_blocked(monkeypatch):
    out, _ = _fetch_with(monkeypatch, [
        FakeResp(302, {"location": "http://127.0.0.1:11434/api/tags"}),
    ])
    assert "not permitted" in out


def test_legitimate_redirect_still_followed(monkeypatch):
    out, seen = _fetch_with(monkeypatch, [
        FakeResp(302, {"location": "http://elsewhere.example/final"}),
        FakeResp(200, {"content-type": "text/html"}, "<html>arrived</html>"),
    ])
    assert "arrived" in out
    assert len(seen) == 2


def test_relative_redirect_resolved_against_current_url(monkeypatch):
    out, seen = _fetch_with(monkeypatch, [
        FakeResp(302, {"location": "/final"}),
        FakeResp(200, {"content-type": "text/html"}, "<html>arrived</html>"),
    ])
    assert "arrived" in out
    assert seen[1] == "http://start.example/final"


def test_redirect_chain_is_capped(monkeypatch):
    hops = [FakeResp(302, {"location": f"http://h{i}.example/"}) for i in range(20)]
    out, seen = _fetch_with(monkeypatch, hops)
    assert "too many redirects" in out
    assert len(seen) <= search_tool._MAX_REDIRECTS + 1


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_all_redirect_codes_revalidated(monkeypatch, code):
    out, _ = _fetch_with(monkeypatch, [
        FakeResp(code, {"location": "http://192.168.1.1/"}),
    ])
    assert "not permitted" in out


def test_direct_private_fetch_still_blocked():
    assert "not permitted" in web_fetch("http://192.168.1.62:11434/api/tags")
