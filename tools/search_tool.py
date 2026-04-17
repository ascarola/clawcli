"""Web search via SearXNG and URL fetching."""

import re
import json
import requests
from urllib.parse import urljoin


def web_search(query: str, searxng_url: str, num_results: int = 10) -> str:
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
    try:
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
    except requests.RequestException as e:
        return f"Fetch error: {e}"
    except Exception as e:
        return f"Error: {e}"
