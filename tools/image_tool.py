"""Vision tool — sends local images/PDFs to a vision-capable Ollama model and returns its response."""

from __future__ import annotations

import base64
from pathlib import Path

import requests


_SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}

# Pages with fewer than this many characters are treated as scanned (image-only).
_SCANNED_THRESHOLD = 100

# Safety caps to avoid runaway requests.
_MAX_NATIVE_PAGES = 100
_MAX_OCR_PAGES = 20


def _vision_call(
    image_b64: str,
    prompt: str,
    vision_model: str,
    ollama_url: str,
    timeout: int,
) -> str:
    """Send a single base64 image to the Ollama vision model and return the text response."""
    payload = {
        "model": vision_model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
    }
    try:
        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=timeout,
        )
        if resp.status_code == 400 and "multimodal" in resp.text.lower():
            return (
                f"Error: '{vision_model}' does not support vision/images. "
                f"Pull a vision-capable model (e.g. `ollama pull llava`) "
                f"and set it with `/set vision_model llava:latest`."
            )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        return content if content else "(no response from vision model)"
    except requests.exceptions.ConnectionError:
        return f"Error: could not connect to Ollama at {ollama_url}"
    except requests.exceptions.Timeout:
        return f"Error: vision model timed out after {timeout}s"
    except requests.exceptions.HTTPError:
        return f"Error: Ollama returned {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return f"Error: {e}"


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
    return _vision_call(image_b64, prompt, vision_model, ollama_url, timeout)


def read_pdf(
    file_path: str,
    prompt: str = "Extract all text from this page.",
    vision_model: str = "",
    ollama_url: str = "http://localhost:11434",
    timeout: int = 120,
) -> str:
    """Extract text from a PDF. Native text pages are read directly; scanned/image-only
    pages are rendered and sent to the vision model for OCR."""
    try:
        import fitz  # pymupdf
    except ImportError:
        return (
            "Error: pymupdf is not installed. Run `pip install pymupdf` to enable PDF support."
        )

    p = Path(file_path).expanduser()
    if not p.exists():
        return f"Error: file not found: {file_path}"
    if p.suffix.lower() != ".pdf":
        return f"Error: expected a .pdf file, got '{p.suffix}'."
    if p.stat().st_size > 100 * 1024 * 1024:
        return "Error: PDF exceeds 100 MB limit."

    try:
        doc = fitz.open(str(p))
    except Exception as e:
        return f"Error: could not open PDF: {e}"

    total_pages = len(doc)
    if total_pages == 0:
        return "Error: PDF has no pages."

    pages = doc
    scanned_pages_processed = 0
    parts: list[str] = []
    prefix = f"[{total_pages} page{'s' if total_pages != 1 else ''}]\n"

    for i, page in enumerate(pages):
        page_num = i + 1
        if page_num > _MAX_NATIVE_PAGES:
            parts.append(f"\n(Stopped after {_MAX_NATIVE_PAGES} pages.)")
            break

        text = page.get_text().strip()

        if len(text) >= _SCANNED_THRESHOLD:
            # Native text page
            header = f"\n=== Page {page_num} ===\n" if total_pages > 1 else ""
            parts.append(header + text)
        else:
            # Scanned/image-only page — OCR via vision model
            if not vision_model:
                header = f"\n=== Page {page_num} (scanned — no vision_model set) ===\n" if total_pages > 1 else ""
                parts.append(header + "(skipped: set vision_model to OCR this page)")
                continue
            if scanned_pages_processed >= _MAX_OCR_PAGES:
                parts.append(f"\n=== Page {page_num} ===\n(skipped: OCR limit of {_MAX_OCR_PAGES} scanned pages reached)")
                continue

            # Render at 2× for better OCR accuracy
            mat = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=mat)
            img_b64 = base64.b64encode(pix.tobytes("png")).decode()
            ocr_result = _vision_call(img_b64, prompt, vision_model, ollama_url, timeout)
            scanned_pages_processed += 1

            header = f"\n=== Page {page_num} (OCR) ===\n" if total_pages > 1 else ""
            parts.append(header + ocr_result)

    doc.close()
    return prefix + "\n".join(parts).strip()
