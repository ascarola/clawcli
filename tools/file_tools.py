"""File operation tools: read, write, edit, glob, grep."""
from __future__ import annotations

import os
import re
import glob as glob_module
import subprocess  # nosec B404
from pathlib import Path

# Sensitive paths that are always blocked from read/write/search
_BLOCKED_PATHS = [
    Path("/etc/shadow"),
    Path("/etc/gshadow"),
    Path("/etc/master.passwd"),
    Path("/etc/passwd"),
    Path("/etc/sudoers"),
    Path("/etc/crontab"),
    Path("/proc/self/environ"),
]

_BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".ppk", ".env"}

_BLOCKED_DIRS = [
    Path.home() / ".ssh",
    Path.home() / ".secrets",
    Path.home() / ".gnupg",
    Path.home() / ".aws",
    Path.home() / ".kube",
    Path.home() / ".config" / "gcloud",
    Path("/root/.ssh"),
    Path("/root/.secrets"),
    Path("/root/.gnupg"),
    Path("/root/.aws"),
    Path("/root/.kube"),
    Path("/proc/self"),
]


def _is_sensitive(path: Path) -> bool:
    if path in _BLOCKED_PATHS:
        return True
    if path.suffix.lower() in _BLOCKED_SUFFIXES:
        return True
    for blocked in _BLOCKED_DIRS:
        try:
            path.relative_to(blocked)
            return True
        except ValueError:
            pass
    return False


def read_file(file_path: str, offset: int = None, limit: int = None) -> str:
    path = Path(file_path).expanduser().resolve()
    if _is_sensitive(path):
        return f"Error: Reading {file_path} is not permitted — sensitive path blocked."
    if not path.exists():
        return f"Error: File not found: {file_path}"
    if not path.is_file():
        return f"Error: Not a file: {file_path}"
    try:
        lines = path.read_text(errors="replace").splitlines(keepends=True)
        start = (offset - 1) if offset and offset > 0 else 0
        if start >= len(lines) and lines:
            return f"(offset {offset} is past end of file — {len(lines)} lines total)"
        end = (start + limit) if limit and limit > 0 else None
        selected = lines[start:end]
        result = []
        for i, line in enumerate(selected, start=start + 1):
            result.append(f"{i}\t{line}")
        return "".join(result) if result else "(empty file)"
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(file_path: str, content: str) -> str:
    path = Path(file_path).expanduser().resolve()
    if _is_sensitive(path):
        return f"Error: Writing {file_path} is not permitted — sensitive path blocked."
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"Written {lines} lines to {file_path}"
    except Exception as e:
        return f"Error writing file: {e}"


def _detect_eol(raw: bytes) -> str:
    """Return the dominant line ending in raw bytes: '\\r\\n' or '\\n'."""
    return '\r\n' if b'\r\n' in raw else '\n'


def _read_normalised(path: Path) -> tuple[str, str]:
    """Read file bytes, detect EOL, return (content_with_lf_only, original_eol)."""
    raw = path.read_bytes()
    eol = _detect_eol(raw)
    text = raw.decode('utf-8', errors='replace')
    if eol == '\r\n':
        text = text.replace('\r\n', '\n')
    return text, eol


def _write_restored(path: Path, content: str, eol: str) -> None:
    """Convert content back to original EOL and write as bytes."""
    if eol == '\r\n':
        content = content.replace('\n', '\r\n')
    path.write_bytes(content.encode('utf-8', errors='replace'))


def _fuzzy_find(content: str, old_string: str):
    """Find old_string using whitespace-normalized line comparison.
    Returns (1-indexed line number, actual matched text) or None."""
    old_lines = old_string.splitlines()
    if not old_lines:
        return None
    old_stripped = [line.strip() for line in old_lines]
    if all(not s for s in old_stripped):
        return None
    file_lines = content.splitlines(keepends=True)
    n = len(old_lines)
    for i in range(len(file_lines) - n + 1):
        chunk_stripped = [file_lines[i + j].rstrip("\n").strip() for j in range(n)]
        if chunk_stripped == old_stripped:
            return (i + 1, "".join(file_lines[i : i + n]))
    return None


def edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    path = Path(file_path).expanduser().resolve()
    if _is_sensitive(path):
        return f"Error: Editing {file_path} is not permitted — sensitive path blocked."
    if not path.exists():
        return f"Error: File not found: {file_path}"
    try:
        content, eol = _read_normalised(path)
        # Normalise caller-supplied strings to LF so matching works on any EOL file
        old_norm = old_string.replace('\r\n', '\n')
        new_norm = new_string.replace('\r\n', '\n')
        count = content.count(old_norm)
        if count == 0:
            fuzzy = _fuzzy_find(content, old_norm)
            if fuzzy:
                lineno, actual = fuzzy
                return (
                    f"Error: old_string not found exactly in {file_path} "
                    f"(whitespace mismatch near line {lineno}). "
                    f"Actual text at that location:\n{actual}\n"
                    f"Retry edit_file using the exact text shown above as old_string, "
                    f"or use replace_lines with start_line={lineno}."
                )
            return (
                f"Error: old_string not found in {file_path}. "
                f"Re-read the file to confirm the exact text before retrying."
            )
        if count > 1 and not replace_all:
            return (
                f"Error: old_string appears {count} times in {file_path} — "
                f"set replace_all=true or add more surrounding context to make it unique"
            )
        new_content = content.replace(old_norm, new_norm, -1 if replace_all else 1)
        _write_restored(path, new_content, eol)
        replaced = count if replace_all else 1
        return f"Replaced {replaced} occurrence(s) in {file_path}"
    except Exception as e:
        return f"Error editing file: {e}"


def replace_lines(file_path: str, start_line: int, end_line: int, new_content: str) -> str:
    path = Path(file_path).expanduser().resolve()
    if _is_sensitive(path):
        return f"Error: Editing {file_path} is not permitted — sensitive path blocked."
    if not path.exists():
        return f"Error: File not found: {file_path}"
    try:
        text, eol = _read_normalised(path)
        lines = text.splitlines(keepends=True)
        total = len(lines)
        if start_line < 1 or start_line > total:
            return f"Error: start_line {start_line} out of range (file has {total} lines)"
        if end_line < start_line or end_line > total:
            return f"Error: end_line {end_line} out of range (file has {total} lines)"
        # Normalise caller-supplied content to LF — _write_restored converts back
        new_content = new_content.replace('\r\n', '\n')
        original_last_had_newline = lines[end_line - 1].endswith("\n")
        replacement = []
        if new_content:
            needs_newline = (
                (lines[end_line:] and not new_content.endswith("\n"))
                or (not lines[end_line:] and original_last_had_newline
                    and not new_content.endswith("\n"))
            )
            if needs_newline:
                new_content += "\n"
            replacement = [new_content]
        result = lines[: start_line - 1] + replacement + lines[end_line:]
        _write_restored(path, "".join(result), eol)
        removed = end_line - start_line + 1
        added = new_content.count("\n") if new_content else 0
        return f"Replaced lines {start_line}–{end_line} ({removed} line(s) → {added} line(s)) in {file_path}"
    except Exception as e:
        return f"Error editing file: {e}"


def glob_files(pattern: str, directory: str = None) -> str:
    base = Path(directory).expanduser().resolve() if directory else Path.cwd()
    try:
        import itertools
        raw = base.glob(pattern)
        # Cap generator early to avoid exhausting memory on huge trees
        candidates = list(itertools.islice(raw, 5000))
        # Filter out sensitive paths
        matches = [p for p in candidates if not _is_sensitive(p.resolve())]
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            return f"No files matched pattern: {pattern}"
        return "\n".join(str(p) for p in matches[:200])
    except Exception as e:
        return f"Error: {e}"


def grep_files(
    pattern: str,
    path: str = None,
    glob: str = None,
    output_mode: str = "files_with_matches",
    case_insensitive: bool = False,
    context_lines: int = 0,
) -> str:
    cmd = ["rg" if _has_rg() else "grep", "-r"]
    if _has_rg():
        return _rg_search(pattern, path, glob, output_mode, case_insensitive, context_lines)
    return _grep_search(pattern, path, glob, output_mode, case_insensitive, context_lines)


def _has_rg() -> bool:
    import shutil
    return shutil.which("rg") is not None


def _rg_search(pattern, path, glob_pat, output_mode, case_insensitive, context_lines):
    resolved_path = Path(path).expanduser().resolve() if path else None
    if resolved_path and _is_sensitive(resolved_path):
        return f"Error: Searching {path} is not permitted — sensitive path blocked."
    cmd = ["rg"]
    if case_insensitive:
        cmd.append("-i")
    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    else:
        cmd.extend(["-n"])
        if context_lines:
            cmd.extend(["-C", str(context_lines)])
    if glob_pat:
        cmd.extend(["--glob", glob_pat])
    cmd.append(pattern)
    if resolved_path:
        cmd.append(str(resolved_path))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # nosec B603 B607
        out = result.stdout.strip()
        return out if out else "No matches found"
    except subprocess.TimeoutExpired:
        return "Error: search timed out"
    except OSError as e:
        return f"Error: {e}"


def _grep_search(pattern, path, glob_pat, output_mode, case_insensitive, context_lines):
    resolved_path = Path(path).expanduser().resolve() if path else None
    if resolved_path and _is_sensitive(resolved_path):
        return f"Error: Searching {path} is not permitted — sensitive path blocked."
    cmd = ["grep", "-r"]
    if case_insensitive:
        cmd.append("-i")
    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    else:
        cmd.append("-n")
        if context_lines:
            cmd.extend([f"-C{context_lines}"])
    if glob_pat:
        cmd.extend(["--include", glob_pat])
    cmd.append(pattern)
    target = str(resolved_path) if resolved_path else "."
    cmd.append(target)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # nosec B603 B607
        out = result.stdout.strip()
        return out if out else "No matches found"
    except subprocess.TimeoutExpired:
        return "Error: search timed out"
    except OSError as e:
        return f"Error: {e}"
