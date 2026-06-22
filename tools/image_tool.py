"""Vision tool — sends a local image to a vision-capable Ollama model and returns its response."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import requests


_SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def read_image(
    file_path: str,
    prompt: str = "Describe this image in detail.",
    vision_model: str = "",
    ollama_url: str = "http://localhost:11434",
    timeout: int = 120,
) -> str:
    """Base64-encode a local image and ask the vision model about it."""
    p = Path(file_path).expanduser()
    if not p.exists():
        return f"Error: file not found: {file_path}"
    if p.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        return (
            f"Error: unsupported image format '{p.suffix}'. "
            f"Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
        )
    if p.stat().st_size > 20 * 1024 * 1024:
        return "Error: image exceeds 20 MB limit."

    image_b64 = base64.b64encode(p.read_bytes()).decode()

    payload = {
        "model": vision_model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [image_b64],
            }
        ],
    }

    try:
        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("message", {}).get("content", "")
        return content if content else "(no response from vision model)"
    except requests.exceptions.ConnectionError:
        return f"Error: could not connect to Ollama at {ollama_url}"
    except requests.exceptions.Timeout:
        return f"Error: vision model timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"
