"""File operation tools: read, write, edit, glob, grep."""

import os
import re
import glob as glob_module
import subprocess
from pathlib import Path


def read_file(file_path: str, offset: int = None, limit: int = None) -> str:
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return f"Error: File not found: {file_path}"
    if not path.is_file():
        return f"Error: Not a file: {file_path}"
    try:
        lines = path.read_text(errors="replace").splitlines(keepends=True)
        start = (offset - 1) if offset and offset > 0 else 0
        end = (start + limit) if limit else None
        selected = lines[start:end]
        result = []
        for i, line in enumerate(selected, start=start + 1):
            result.append(f"{i}\t{line}")
        return "".join(result) if result else "(empty file)"
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(file_path: str, content: str) -> str:
    path = Path(file_path).expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"Written {lines} lines to {file_path}"
    except Exception as e:
        return f"Error writing file: {e}"


def edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return f"Error: File not found: {file_path}"
    try:
        content = path.read_text(errors="replace")
        count = content.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {file_path}"
        if count > 1 and not replace_all:
            return f"Error: old_string appears {count} times in {file_path} — set replace_all=true or provide more context to make it unique"
        new_content = content.replace(old_string, new_string, -1 if replace_all else 1)
        path.write_text(new_content)
        replaced = count if replace_all else 1
        return f"Replaced {replaced} occurrence(s) in {file_path}"
    except Exception as e:
        return f"Error editing file: {e}"


def glob_files(pattern: str, directory: str = None) -> str:
    base = Path(directory).expanduser().resolve() if directory else Path.cwd()
    try:
        matches = sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
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
    if path:
        cmd.append(str(Path(path).expanduser().resolve()))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = result.stdout.strip()
        return out if out else "No matches found"
    except subprocess.TimeoutExpired:
        return "Error: search timed out"
    except Exception as e:
        return f"Error: {e}"


def _grep_search(pattern, path, glob_pat, output_mode, case_insensitive, context_lines):
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
    target = str(Path(path).expanduser().resolve()) if path else "."
    cmd.append(target)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = result.stdout.strip()
        return out if out else "No matches found"
    except subprocess.TimeoutExpired:
        return "Error: search timed out"
    except Exception as e:
        return f"Error: {e}"
