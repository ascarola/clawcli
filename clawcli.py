#!/usr/bin/env python3
"""CLAWCLI — A Claude Code-like AI assistant powered by Ollama/gemma4:26b"""
from __future__ import annotations

import warnings
# Suppress urllib3's LibreSSL warning on macOS — harmless, just noise
warnings.filterwarnings("ignore", message=".*LibreSSL.*")
warnings.filterwarnings("ignore", message=".*NotOpenSSLWarning.*")

import os
import sys
import json
import re
import subprocess
import threading
import uuid
import socket
import platform
import argparse
import signal
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.spinner import Spinner
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame

# ── Paths ────────────────────────────────────────────────────────────────────
CLAWCLI_DIR = Path(__file__).resolve().parent  # resolve symlink before .parent


class LoopAbortError(Exception):
    """Raised when the user types 'a' at a confirmation prompt to abort the entire agentic loop."""


def get_version() -> str:
    version_file = CLAWCLI_DIR / "VERSION"
    if version_file.exists():
        v = version_file.read_text().strip()
        if v:
            return v
    try:
        result = subprocess.run(
            ["git", "-C", str(CLAWCLI_DIR), "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=3,
        )
        v = result.stdout.strip()
        return v if v else "unknown"
    except Exception:
        return "unknown"


VERSION = get_version()
CONFIG_FILE  = CLAWCLI_DIR / "config.json"
MEMORY_FILE  = CLAWCLI_DIR / "memory" / "MEMORY.md"
SYSPROMPT    = CLAWCLI_DIR / "system_prompt.txt"
HISTORY_FILE = Path.home() / ".clawcli_history"
SESSIONS_DIR = CLAWCLI_DIR / "sessions"

# ── Console ──────────────────────────────────────────────────────────────────
console = Console()


def _start_update_check() -> threading.Thread:
    """Fetch from origin in a background thread; check result with _finish_update_check()."""
    result: list[str] = []

    def _worker():
        try:
            # ls-remote checks remote HEAD without touching FETCH_HEAD or local refs
            r = subprocess.run(
                ["git", "-C", str(CLAWCLI_DIR), "ls-remote", "origin", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return
            remote = r.stdout.split()[0]
            local = subprocess.run(
                ["git", "-C", str(CLAWCLI_DIR), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
            if local and remote and local != remote:
                result.append("available")
        except Exception:
            pass

    t = threading.Thread(target=_worker, daemon=True)
    t.result = result  # type: ignore[attr-defined]
    t.start()
    return t


def _finish_update_check(t: threading.Thread) -> None:
    """Join the update thread (brief wait) and print notice if update is ready."""
    t.join(timeout=0.2)
    if getattr(t, "result", None) and t.result:  # type: ignore[attr-defined]
        console.print("[dim]Update available — run [bold]clawcli update[/bold] to install.[/dim]")

# ── Tool imports ─────────────────────────────────────────────────────────────
sys.path.insert(0, str(CLAWCLI_DIR))
from tools import TOOL_DEFINITIONS, KALI_TOOL_DEFINITIONS
from tools.file_tools import read_file, write_file, edit_file, replace_lines, glob_files, grep_files
from tools.bash_tool import execute_bash
from tools.search_tool import web_search, web_fetch
from tools.mcp_tool import MCPClient, mcp_tools_to_ollama, check_mcp_health

# ── MCP state ────────────────────────────────────────────────────────────────
_mcp_client: MCPClient | None = None
_mcp_tool_definitions: list[dict] = []


def init_mcp(config: dict, quiet: bool = False) -> None:
    """Connect to MCP server and load its tools into _mcp_tool_definitions."""
    global _mcp_client, _mcp_tool_definitions
    url   = config.get("mcp_server_url", "").strip()
    token = config.get("mcp_bearer_token", "").strip()
    _mcp_client = None
    _mcp_tool_definitions = []
    if not url:
        return
    try:
        client = MCPClient(url, token)
        if not client.initialize():
            if not quiet:
                console.print("[yellow]⚠ MCP: initialize failed — check URL and token[/yellow]")
            return
        tools = client.list_tools()
        _mcp_client = client
        excluded = set(config.get("mcp_excluded_tools", []))
        all_defs = mcp_tools_to_ollama(tools)
        _mcp_tool_definitions = [t for t in all_defs if t["function"]["name"] not in excluded]
        if not quiet:
            hidden = len(all_defs) - len(_mcp_tool_definitions)
            suffix = f" ({hidden} excluded)" if hidden else ""
            console.print(f"[dim]MCP: {len(_mcp_tool_definitions)} tool(s) loaded from {url}{suffix}[/dim]")
    except Exception as e:
        if not quiet:
            console.print(f"[yellow]⚠ MCP error: {e}[/yellow]")
from tools.kali_tool import check_health as kali_health, run_tool as kali_run, format_result as kali_fmt, DESTRUCTIVE_TOOLS as KALI_DESTRUCTIVE


def load_config() -> dict:
    defaults_file = CLAWCLI_DIR / "config.defaults.json"
    cfg = {}
    if defaults_file.exists():
        try:
            cfg = json.loads(defaults_file.read_text())
        except Exception:
            pass
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    return cfg


def detect_context_window(config: dict) -> None:
    """Update config['context_window'] with the model's actual max from Ollama."""
    try:
        ollama_url = config.get("ollama_url", "http://localhost:11434")
        info = requests.post(f"{ollama_url}/api/show", json={"name": config["model"]}, timeout=10).json()
        model_info = info.get("model_info", {})
        ctx = next((v for k, v in model_info.items() if "context_length" in k), None)
        if ctx:
            config["context_window"] = ctx
    except (requests.RequestException, ValueError, KeyError):
        pass  # Ollama unreachable or model unknown — keep config.json value


def load_memory() -> str:
    if MEMORY_FILE.exists():
        return MEMORY_FILE.read_text()
    return ""


def _sanitize_memory(value: str, max_len: int = 500) -> str:
    value = value.strip()
    value = re.sub(r"#+\s*", "", value)        # strip markdown headers
    value = re.sub(r"\n{2,}", " ", value)       # collapse blank lines that break prompt structure
    value = re.sub(r"[\x00-\x08\x0b-\x1f]", "", value)  # strip control chars (keep \t \n)
    return value[:max_len]


def _sanitize_section(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9 _-]", "", value)
    return value[:60].strip()


def save_memory(section: str, content: str) -> str:
    section = _sanitize_section(section)
    content = _sanitize_memory(content)
    if not section or not content:
        return "Memory not saved — empty section or content after sanitization."
    text = MEMORY_FILE.read_text() if MEMORY_FILE.exists() else "# CLAWCLI Memory\n\n"
    marker = f"## {section}"
    if marker in text:
        idx = text.index(marker) + len(marker)
        next_section = text.find("\n## ", idx)
        if next_section == -1:
            text = text[:idx] + "\n" + f"- {content}\n" + text[idx:]
        else:
            text = text[:next_section] + "\n" + f"- {content}\n" + text[next_section:]
    else:
        text += f"\n## {section}\n- {content}\n"
    MEMORY_FILE.write_text(text)
    return f"Memory saved to section '{section}'"


def detect_env_info() -> str:
    uname = platform.uname()
    lines = [
        f"- OS: {uname.system} {uname.release} ({platform.version()})",
        f"- Machine: {uname.machine}  Hostname: {socket.gethostname()}",
        f"- Python: {sys.version.split()[0]}",
        f"- Shell: {os.environ.get('SHELL', 'unknown')}",
        f"- User: {os.environ.get('USER') or os.environ.get('USERNAME', 'unknown')}",
        f"- Home: {Path.home()}",
    ]
    if uname.system == "Darwin":
        lines.append(f"- macOS version: {platform.mac_ver()[0]}")
    elif uname.system == "Linux":
        try:
            rel = platform.freedesktop_os_release()
            lines.append(f"- Distro: {rel.get('PRETTY_NAME', 'Linux')}")
        except OSError:
            pass  # /etc/os-release absent on some minimal Linux installs
    elif uname.system == "Windows":
        lines.append(f"- Windows: {platform.win32_ver()[0]}")
    return "\n".join(lines)


def _sanitize_prompt_value(value: str) -> str:
    return re.sub(r"[\r\n\x00-\x1f]", " ", value)


def build_system_prompt(config: dict) -> str:
    base = SYSPROMPT.read_text() if SYSPROMPT.exists() else "You are CLAWCLI, an AI coding assistant."
    base = base.replace("{date}", datetime.now().strftime("%Y-%m-%d"))
    base = base.replace("{model}", _sanitize_prompt_value(config.get("model", "unknown")))
    base = base.replace("{env_info}", _sanitize_prompt_value(detect_env_info()))
    assistant_name = _sanitize_prompt_value(config.get("assistant_name", "CLAWCLI"))
    user_name      = _sanitize_prompt_value(config.get("user_name", ""))
    base = base.replace("CLAWCLI", assistant_name)
    if user_name:
        base += f"\n\nThe user's name is {user_name}. Address them by name naturally."
    if not config.get("searxng_url"):
        base += "\n\nNote: SearXNG is not configured — web_search is unavailable. Do not attempt research prompts or suggest web searches."
    kali_url = config.get("kali_server_url", "")
    if kali_url:
        base += (
            f"\n\n## Kali Security Scanning\n"
            f"A Kali Linux security server is configured at {kali_url}. "
            f"Use the kali_scan tool for security assessments.\n"
            f"Standard recon workflow: (1) nmap with scan_type=-sV and additional_args=-T4 -Pn to "
            f"discover ports/services, (2) nikto or gobuster against any web ports found, "
            f"(3) wpscan if WordPress is detected. Chain these automatically without asking.\n"
            f"Always present findings in plain English with severity context — never dump raw tool output.\n"
            f"sqlmap, hydra, and john are destructive/high-noise: always warn the user and call "
            f"kali_scan (the tool will prompt for confirmation automatically).\n"
            f"If the user asks about security scanning without a specific target, ask for the target IP/URL first."
        )
    else:
        base += "\n\nNote: Kali security scanning is not configured. Use /kali <url> to enable it."
    if _mcp_tool_definitions:
        base += (
            f"\n\n## MCP Tools\n"
            f"An MCP server is connected with {len(_mcp_tool_definitions)} tool(s) available. "
            f"**Only call an MCP tool when the user's request is explicitly an action or requires "
            f"data that is impossible to answer without it** — for example: send an email, check "
            f"a calendar, create a ticket, trigger a workflow. "
            f"The ## Environment section above already contains this machine's hostname, OS, CPU, "
            f"and RAM — never call a system-info MCP tool to answer questions about this machine. "
            f"Never call an MCP tool speculatively or to refresh data you already have in context."
        )
    memory = load_memory()
    if memory.strip():
        mem_block = f"\n\n## Persistent Memory\n{memory}"
        if "\n## Environment" in base:
            base = base.replace("\n## Environment", mem_block + "\n## Environment", 1)
        else:
            base += mem_block
    return base


def confirm_bash(command: str, description: str) -> bool:
    console.print(f"\n[yellow]Bash command requires confirmation:[/yellow]")
    console.print(f"[dim]  {description}[/dim]")
    console.print(f"[white]  $ {command}[/white]")
    try:
        answer = input("  Allow? [y/N/a=abort] ").strip().lower()
        if answer in ("a", "abort"):
            raise LoopAbortError()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def confirm_file_write(path: str, action: str) -> bool:
    console.print(f"\n[yellow]File {action} requires confirmation:[/yellow]")
    console.print(f"[white]  {path}[/white]")
    try:
        answer = input("  Allow? [y/N/a=abort] ").strip().lower()
        if answer in ("a", "abort"):
            raise LoopAbortError()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def confirm_kali_destructive(tool: str, target: str) -> bool:
    console.print(
        f"\n[bold red]⚠ {tool.upper()} is a potentially destructive / high-noise tool.[/bold red]"
    )
    console.print(f"[dim]  Target: {target}[/dim]")
    try:
        answer = input(f"  Proceed with {tool} against {target}? [y/N/a=abort] ").strip().lower()
        if answer in ("a", "abort"):
            raise LoopAbortError()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def dispatch_tool(name: str, args: dict, config: dict, confirm: bool = False) -> str:
    ollama_url  = config.get("ollama_url", "http://localhost:11434")
    searxng_url = config.get("searxng_url", "")

    try:
        if name == "read_file":
            return read_file(args["file_path"], args.get("offset"), args.get("limit"))

        elif name == "write_file":
            if config.get("confirm_write") and not confirm_file_write(args["file_path"], "write"):
                return "File write denied by user."
            return write_file(args["file_path"], args["content"])

        elif name == "edit_file":
            if config.get("confirm_write") and not confirm_file_write(args["file_path"], "edit"):
                return "File edit denied by user."
            return edit_file(
                args["file_path"],
                args["old_string"],
                args["new_string"],
                args.get("replace_all", False),
            )

        elif name == "replace_lines":
            if config.get("confirm_write") and not confirm_file_write(args["file_path"], "edit"):
                return "File edit denied by user."
            return replace_lines(
                args["file_path"],
                args["start_line"],
                args["end_line"],
                args.get("new_content", ""),
            )

        elif name == "glob_files":
            return glob_files(args["pattern"], args.get("directory"))

        elif name == "grep_files":
            return grep_files(
                args["pattern"],
                args.get("path"),
                args.get("glob"),
                args.get("output_mode", "files_with_matches"),
                args.get("case_insensitive", False),
                args.get("context_lines", 0),
            )

        elif name == "bash":
            return execute_bash(
                args["command"],
                args.get("timeout", 300),
                args.get("description"),
                config_dir=str(CLAWCLI_DIR),
                confirm_callback=confirm_bash if confirm else None,
                max_timeout=config.get("bash_max_timeout", 1800),
            )

        elif name == "web_search":
            return web_search(args["query"], searxng_url, args.get("num_results", 10))

        elif name == "web_fetch":
            return web_fetch(args["url"], args.get("max_chars", 8000))

        elif name == "save_memory":
            return save_memory(args["section"], args["content"])

        elif name == "kali_scan":
            kali_url = config.get("kali_server_url", "").rstrip("/")
            if not kali_url:
                return "Error: Kali server not configured. Use /kali <url> to set it."
            tool   = args.get("tool", "")
            params = dict(args.get("params") or {})
            reason = args.get("reason", "")
            if not tool:
                return "Error: kali_scan requires a 'tool' argument."

            # Health check before any scan
            ok, health_msg = kali_health(kali_url)
            if not ok:
                return f"Error: {health_msg}"

            # Destructive tools require explicit user confirmation
            if tool in KALI_DESTRUCTIVE:
                target = (params.get("target") or params.get("url")
                          or params.get("hash_file") or "unknown target")
                if not confirm_kali_destructive(tool, target):
                    return f"Scan cancelled by user."

            result = kali_run(kali_url, tool, params, timeout=config.get("kali_timeout", 300))
            return kali_fmt(tool, result)

        elif name.startswith("mcp__"):
            if _mcp_client is None:
                return "Error: MCP server not connected. Use /mcp <url> to configure it."
            mcp_name = name[5:]  # strip mcp__ prefix
            return _mcp_client.call_tool(mcp_name, args)

        else:
            return f"Unknown tool: {name}"

    except LoopAbortError:
        raise  # let it propagate up to run_agentic_loop
    except KeyError as e:
        return f"Tool error: missing required argument {e}"
    except Exception as e:
        return f"Tool error ({name}): {e}"


# Broad: includes demonstratives — used only to detect whether fast-path should be skipped
_PRONOUN_RE = re.compile(
    r"\b(he|she|him|her|they|them|it|this|that|these|those)\b(?!')", re.IGNORECASE
)
# Narrow: personal pronouns only, no apostrophe contractions — used for substitution
_SUBST_RE = re.compile(
    r"\b(he|she|him|her|they|them|it)\b(?!')", re.IGNORECASE
)
_PROPER_NOUN_RE = re.compile(r'\b([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,})*)\b')
_NOUN_STOPWORDS = {
    "The", "This", "That", "These", "Those", "There", "Then", "When", "Where",
    "What", "Which", "Who", "How", "Why", "Based", "Search", "Note", "Also",
    "Here", "If", "In", "On", "At", "For", "To", "Of", "And", "Or", "But",
    "Is", "Are", "Was", "Were", "Be", "As", "An", "By", "From", "With",
    "Tell", "Show", "Give", "Get", "Find", "Look",
}
# Filler words that add no value at end of a search query after pronoun substitution
_QUERY_FILLER_RE = re.compile(
    r'\s+\b(more|further|again|now|please|additional|details|information)\b\s*$',
    re.IGNORECASE
)


def _extract_entity(messages: list, skip_content: str = "") -> str:
    """Return the most recently user-mentioned proper noun phrase, falling back to assistant text."""
    def _candidates_from(text: str) -> list[str]:
        return [
            noun for noun in _PROPER_NOUN_RE.findall(text)
            if noun not in _NOUN_STOPWORDS and len(noun) >= 3
        ]

    def _best(candidates: list[str]) -> str:
        multi = [c for c in candidates if ' ' in c]
        return multi[0] if multi else candidates[0]

    # User messages are the most reliable — they contain what the user explicitly named
    for m in reversed(messages[-20:]):
        if m.get("role") != "user":
            continue
        content = (m.get("content") or "").strip()
        if not content or content == skip_content:
            continue
        found = _candidates_from(content)
        if found:
            return _best(found)

    # Fall back to most recent assistant text
    for m in reversed(messages[-20:]):
        if m.get("role") != "assistant":
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        found = _candidates_from(content)
        if found:
            return _best(found)

    return ""


def _rewrite_search_query(query: str, messages: list) -> str:
    """Replace personal pronouns in a search query with the entity from recent conversation history."""
    if not _SUBST_RE.search(query):
        return query
    entity = _extract_entity(messages, skip_content=query)
    if not entity:
        return query
    rewritten = _SUBST_RE.sub(entity, query).strip()
    rewritten = _QUERY_FILLER_RE.sub("", rewritten).strip()
    return rewritten or query


_LATEX_SYMBOLS = {
    r"\rightarrow": "→", r"\to": "→", r"\leftarrow": "←", r"\Rightarrow": "⇒",
    r"\Leftarrow": "⇐", r"\leftrightarrow": "↔", r"\approx": "≈", r"\ne": "≠",
    r"\neq": "≠", r"\leq": "≤", r"\geq": "≥", r"\le": "≤", r"\ge": "≥",
    r"\times": "×", r"\div": "÷", r"\infty": "∞", r"\pm": "±", r"\cdot": "·",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ", r"\lambda": "λ",
    r"\mu": "μ", r"\sigma": "σ", r"\pi": "π", r"\Sigma": "Σ",
}
_LATEX_INLINE_RE = re.compile(r'\$([^$\n]+?)\$')


def _delatex(text: str) -> str:
    """Convert inline LaTeX math ($...$) to Unicode equivalents for terminal display."""
    def _convert(m: re.Match) -> str:
        inner = m.group(1).strip()
        for cmd, uni in _LATEX_SYMBOLS.items():
            inner = inner.replace(cmd, uni)
        return inner.strip("\\{} ")
    return _LATEX_INLINE_RE.sub(_convert, text)


def render_tool_call(name: str, args: dict):
    console.print(f"\n[bold cyan]⚙ {name}[/bold cyan] ", end="")
    key_args = {k: v for k, v in args.items() if k not in ("content",)}
    if key_args:
        parts = [f"[dim]{k}=[/dim][white]{rich_escape(str(v)[:200])}[/white]" for k, v in key_args.items()]
        console.print(" ".join(parts))
    else:
        console.print()


def chat(messages: list, config: dict, stream: bool = True) -> dict:
    url   = config.get("ollama_url", "http://localhost:11434") + "/api/chat"
    model = config.get("model", "gemma4:26b")
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOL_DEFINITIONS
            + (KALI_TOOL_DEFINITIONS if config.get("kali_server_url") else [])
            + _mcp_tool_definitions,
        "stream": stream,
        "options": {
            "temperature": config.get("temperature", 0.1),
            "num_ctx": config.get("context_window", 8192),
        },
    }
    # 'think' is a top-level chat parameter in the Ollama API, not a model option
    if config.get("think") is not None:
        payload["think"] = config["think"]
    resp = requests.post(url, json=payload, stream=stream, timeout=config.get("ollama_timeout", 1800))
    resp.raise_for_status()

    if not stream:
        return resp.json()

    # Streaming: accumulate content and render live as Markdown
    full_content      = ""
    tool_calls        = []
    prompt_tokens     = 0
    completion_tokens = 0
    REPEAT_WINDOW     = 120  # chars to inspect for repetition
    REPEAT_CHECK_EVERY = 30  # check every N chunks

    def _is_repetitive(text: str) -> bool:
        if len(text) < REPEAT_WINDOW:
            return False
        tail = text[-REPEAT_WINDOW:]
        for pat_len in range(1, 8):
            pat = tail[:pat_len]
            expected = (pat * ((REPEAT_WINDOW // pat_len) + 1))[:REPEAT_WINDOW]
            if tail == expected:
                return True
        return False

    console.print()
    chunk_count = 0
    RENDER_EVERY = 15  # re-render every N chunks to avoid terminal overflow artifacts
    with Live(Spinner("dots", text="[dim]thinking…[/dim]"), console=console, refresh_per_second=12) as live:
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg           = chunk.get("message", {})
            delta_content = msg.get("content", "")
            delta_tools   = msg.get("tool_calls", [])

            if delta_content:
                full_content += delta_content
                chunk_count  += 1
                if chunk_count % RENDER_EVERY == 0:
                    live.update(Markdown(_delatex(full_content)))

                if chunk_count % REPEAT_CHECK_EVERY == 0 and _is_repetitive(full_content):
                    resp.close()
                    live.update(Markdown(_delatex(full_content.rstrip())))
                    console.print("\n[yellow]⚠ Runaway repetition detected — output truncated.[/yellow]")
                    break

            if delta_tools:
                tool_calls.extend(delta_tools)

            if chunk.get("done"):
                prompt_tokens     = chunk.get("prompt_eval_count", 0)
                completion_tokens = chunk.get("eval_count", 0)
                if full_content:
                    live.update(Markdown(_delatex(full_content)))
                break

    if not full_content:
        console.print()  # blank line after tool-only responses

    return {
        "message": {
            "role": "assistant",
            "content": full_content,
            "tool_calls": tool_calls,
        },
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def is_research_prompt(text: str) -> bool:
    return bool(re.match(r"^research\s+.+", text.strip(), re.IGNORECASE))


def extract_research_query(text: str) -> str:
    m = re.match(r"^research\s+(.+)", text.strip(), re.IGNORECASE)
    return m.group(1) if m else text


def run_agentic_loop(user_input: str, messages: list, config: dict) -> list:
    searxng_url = config.get("searxng_url", "")
    max_iters   = config.get("max_tool_iterations", 20)

    if is_research_prompt(user_input):
        query = extract_research_query(user_input)
        if _PRONOUN_RE.search(query):
            # Pronouns mean the user is referring to something from prior context.
            # Skip the fast-path and let the model resolve the reference with full
            # conversation history, then call web_search itself.
            pass
        else:
            console.print(f"[cyan]Searching:[/cyan] {query}")
            results = web_search(query, searxng_url)
            user_input = f"Research: {query}\n\nSearch results:\n{results}\n\nPlease analyze and summarize these results."

    messages.append({"role": "user", "content": user_input})

    last_call_hash = None  # reset per turn, not persisted across user messages
    for iteration in range(max_iters):
        try:
            response = chat(messages, config, stream=True)
        except requests.RequestException as e:
            console.print(f"[red]Ollama error: {e}[/red]")
            messages.pop()
            return messages

        msg = response.get("message", {})
        content    = msg.get("content", "")
        tool_calls = msg.get("tool_calls", [])
        prompt_tokens = response.get("prompt_tokens", 0)
        ctx_window    = config.get("context_window", 131072)

        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls if tool_calls else None,
        })

        if not tool_calls:
            if prompt_tokens:
                pct = (prompt_tokens / ctx_window) * 100
                if pct >= 90:
                    ctx_color = "red"
                elif pct >= 70:
                    ctx_color = "yellow"
                else:
                    ctx_color = "dim"
                console.print(
                    f"[{ctx_color}]  context: {prompt_tokens:,} / {ctx_window:,} tokens ({pct:.1f}%)[/{ctx_color}]"
                )
                threshold = config.get("auto_compact_threshold", 0.80)
                if threshold and (pct / 100) >= threshold:
                    console.print(
                        f"[yellow]⚠ Context at {pct:.0f}% — auto-compacting to free space...[/yellow]"
                    )
                    messages = compact_messages(messages, config)
            break

        # Prepare tool calls: resolve pronouns and display all before executing
        _READ_ONLY_TOOLS = {"read_file", "glob_files", "grep_files", "web_search", "web_fetch"}
        tool_infos = []
        for tc in tool_calls:
            fn   = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            if name == "web_search" and "query" in args:
                original = args["query"]
                args["query"] = _rewrite_search_query(args["query"], messages)
                if args["query"] != original:
                    console.print(f"[dim]  (resolved: \"{original}\" → \"{args['query']}\")[/dim]")

            render_tool_call(name, args)
            tool_infos.append((name, args))

        # Run read-only batches in parallel; write/bash/kali run sequentially to avoid races
        use_parallel = (
            len(tool_infos) > 1
            and all(n in _READ_ONLY_TOOLS for n, _ in tool_infos)
        )
        try:
            if use_parallel:
                with ThreadPoolExecutor(max_workers=min(len(tool_infos), 4)) as pool:
                    futures = [
                        pool.submit(dispatch_tool, n, a, config, config.get("confirm_bash", False))
                        for n, a in tool_infos
                    ]
                    raw_results = [f.result() for f in futures]
            else:
                raw_results = [
                    dispatch_tool(n, a, config, confirm=config.get("confirm_bash", False))
                    for n, a in tool_infos
                ]
        except LoopAbortError:
            # Answer every pending tool call so the history has no dangling
            # calls to confuse the model on the next turn
            messages.extend(
                {"role": "tool", "content": "Tool execution aborted by user.", "name": n}
                for n, _ in tool_infos
            )
            console.print("[yellow]Aborted — returning to prompt.[/yellow]")
            return messages

        # Post-process: loop detection, size cap, preview, collect
        max_result_chars = config.get("max_tool_result_chars", 20000)
        tool_results = []
        for (name, args), result in zip(tool_infos, raw_results):
            try:
                _args_str = json.dumps(args, sort_keys=True)
            except (TypeError, ValueError):
                _args_str = repr(args)
            call_hash = hash((name, _args_str, result))
            if call_hash == last_call_hash:
                result = f"Loop detected: tool '{name}' returned the same result twice in a row. Try a different approach."
            last_call_hash = call_hash

            if len(result) > max_result_chars:
                result = result[:max_result_chars] + f"\n\n[... truncated — {len(result) - max_result_chars:,} chars omitted]"

            preview = result[:1000] + "…" if len(result) > 1000 else result
            console.print(f"[dim]  → {rich_escape(preview)}[/dim]")

            tool_results.append({"role": "tool", "content": result, "name": name})

        messages.extend(tool_results)
        # Don't break on loop detection — let the model see the message and pivot.
        # max_iters caps runaway loops.

    return messages


def print_welcome(config: dict):
    model  = config.get("model", "gemma4:26b")
    name   = config.get("assistant_name", "CLAWCLI")
    cwd    = os.getcwd()
    console.print(f"🦞 [bold white]{name}[/bold white] [dim]{VERSION}[/dim]")
    console.print(f"[dim]{model} · Ollama[/dim]")
    console.print(f"[dim]{cwd}[/dim]")
    console.print(f"[dim]Type a task, 'research <topic>' to search, /help[/dim]")
    if not config.get("confirm_bash", True):
        console.print("[yellow]⚠  confirm_bash is disabled — all commands run without prompting[/yellow]")


def show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help               — show this help\n"
        "  /update             — pull latest version (restart to apply)\n"
        "  /doctor             — check Ollama, SearXNG, and dependencies\n"
        "  /memory             — show current memory\n"
        "  /clear              — clear conversation history\n"
        "  /compact            — summarize history to free context window\n"
        "  /undo               — remove the last exchange from history\n"
        "  /export [file]      — save conversation to a Markdown file\n"
        "  /config             — show current config\n"
        "  /cwd <path>         — change working directory\n"
        "  /model              — interactive model picker (↑↓ to navigate)\n"
        "  /model <name>       — switch Ollama model directly\n"
        "  /searxng <url>      — set SearXNG URL (or /searxng to check, /searxng disable)\n"
        "  /kali <url>         — set Kali server URL (or /kali to check, /kali disable)\n"
        "  /mcp <url>             — set MCP server URL (or /mcp to check, /mcp disable)\n"
        "  /mcp token <value>     — set MCP bearer token\n"
        "  /mcp tools             — list tools loaded from the MCP server\n"
        "  /mcp exclude <name>    — hide a tool from the model\n"
        "  /mcp include <name>    — re-enable a previously excluded tool\n"
        "  /think <on|off|default> — enable/disable model thinking mode (for Qwen3 etc.)\n"
        "  /set <key> <value>  — set a config value for this session and save (run /set to list all)\n"
        "  /exit               — quit and save session\n\n"
        "[bold]Special prompts:[/bold]\n"
        "  research <topic>  — search SearXNG then summarize\n\n"
        "[bold]Sessions:[/bold]\n"
        "  clawcli sessions             — list saved sessions\n"
        "  clawcli --continue           — resume the last saved session\n"
        "  clawcli --resume <id>        — resume a specific session by ID\n"
        "  clawcli --no-confirm         — skip bash confirmation this session\n"
        "  clawcli --confirm            — force bash confirmation this session\n\n"
        "[bold]Key bindings:[/bold]\n"
        "  Enter           — submit\n"
        "  Shift+Enter     — newline (iTerm2/kitty; see README for Terminal.app setup)\n"
        "  Ctrl+J          — newline (works everywhere)\n"
        "  Alt+Enter       — newline (Linux terminals)\n"
        "  Ctrl+C          — cancel / exit\n"
        "  Up/Down         — history navigation",
        title="CLAWCLI Help",
        border_style="blue",
    ))


def compact_messages(messages: list, config: dict) -> list:
    non_system = [m for m in messages if m.get("role") != "system"]
    if not non_system:
        console.print("[dim]Nothing to compact.[/dim]")
        return messages

    history_text = "\n".join(
        f"{m['role'].upper()}: {str(m.get('content', ''))[:500]}"
        for m in non_system
    )
    summary_prompt = (
        "Summarize the conversation below into a concise context block. Include: "
        "the main goal, key decisions, files created or modified, current state, "
        "and what remains to be done. Be specific — this will replace the full history.\n\n"
        f"{history_text}"
    )
    console.print("[dim]Compacting conversation...[/dim]")
    system_msg = next((m for m in messages if m.get("role") == "system"), None)
    try:
        response = chat(
            [{"role": "system", "content": system_msg.get("content", "") if system_msg else ""},
             {"role": "user", "content": summary_prompt}],
            config,
            stream=False,
        )
        summary = response["message"]["content"]
    except Exception as e:
        console.print(f"[red]Compact failed: {e}[/red]")
        return messages

    new_messages = []
    if system_msg:
        new_messages.append(system_msg)
    new_messages.append({
        "role": "user",
        "content": "[Conversation compacted — summary of prior context below]",
    })
    new_messages.append({"role": "assistant", "content": summary})
    before = len(non_system)
    console.print(f"[dim]Compacted {before} messages → 2. Use /config to check context usage.[/dim]")
    return new_messages


# Keys that /set is allowed to modify, with their type and description
_SETTABLE_KEYS: dict[str, tuple[str, str]] = {
    "max_tool_iterations":   ("int",   "Max agentic loop iterations per turn"),
    "max_tool_result_chars": ("int",   "Truncate tool results at this many characters"),
    "auto_compact_threshold":("float", "Auto-compact at this context fraction (0 to disable)"),
    "temperature":           ("float", "Model temperature"),
    "ollama_timeout":        ("int",   "Ollama HTTP timeout in seconds"),
    "bash_max_timeout":      ("int",   "Max bash command timeout in seconds"),
    "kali_timeout":          ("int",   "Kali server request timeout in seconds"),
    "confirm_bash":          ("bool",  "Prompt before unapproved bash commands"),
    "confirm_write":         ("bool",  "Prompt before writing files"),
}


def _model_switch(name: str, config: dict, messages: list) -> None:
    config["model"] = name
    detect_context_window(config)
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
    for m in messages:
        if m.get("role") == "system":
            m["content"] = build_system_prompt(config)
            break
    console.print(f"[dim]Model switched to: {name}  (context: {config['context_window']:,}) — saved[/dim]")


def _model_picker(models: list, current: str) -> str | None:
    """Full-screen arrow-key model picker. Returns selected model name or None on cancel."""
    idx = [0]
    for i, m in enumerate(models):
        if m["name"] == current:
            idx[0] = i
            break
    result = [None]

    def get_content():
        lines = [("bold", " Switch Model\n\n")]
        for i, m in enumerate(models):
            name = m["name"]
            size_gb = m["size"] / 1e9
            params = m.get("details", {}).get("parameter_size", "")
            quant  = m.get("details", {}).get("quantization_level", "")
            info   = f"  [{params} {quant} {size_gb:.1f}GB]".replace("[  ", "[").strip()
            active = "  ← active" if name == current else ""
            if i == idx[0]:
                lines.append(("class:sel", f"  ▶ {name}  {info}{active}\n"))
            else:
                lines.append(("class:dim", f"    {name}  {info}{active}\n"))
        lines.append(("class:hint", "\n  ↑↓ navigate   Enter confirm   Escape cancel"
                                    "   (list fetched live from Ollama)\n"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        idx[0] = (idx[0] - 1) % len(models)

    @kb.add("down")
    def _(event):
        idx[0] = (idx[0] + 1) % len(models)

    @kb.add("enter")
    def _(event):
        result[0] = models[idx[0]]["name"]
        event.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        event.app.exit()

    picker_style = Style.from_dict({
        "sel":  "bold reverse",
        "dim":  "",
        "hint": "italic ansigray",
    })

    app = Application(
        layout=Layout(Frame(Window(FormattedTextControl(get_content, focusable=True)))),
        key_bindings=kb,
        style=picker_style,
        full_screen=True,
        mouse_support=False,
    )
    app.run()
    return result[0]


def handle_slash_command(cmd: str, config: dict, messages: list, session_id: str = None) -> tuple[bool, list]:
    parts = cmd.strip().split(None, 1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == "/help":
        show_help()
        return True, messages

    elif command == "/memory":
        mem = load_memory()
        console.print(Markdown(mem) if mem.strip() else "[dim]Memory is empty.[/dim]")
        return True, messages

    elif command == "/clear":
        system_msg = next((m for m in messages if m.get("role") == "system"), None)
        messages.clear()
        messages.append(system_msg or {"role": "system", "content": build_system_prompt(config)})
        console.print("[dim]Conversation cleared.[/dim]")
        return True, messages

    elif command == "/compact":
        messages = compact_messages(messages, config)
        return True, messages

    elif command == "/config":
        display = {k: ("***" if k == "mcp_bearer_token" and v else v) for k, v in config.items()}
        console.print(Panel(json.dumps(display, indent=2), title="Config", border_style="dim"))
        return True, messages

    elif command == "/cwd":
        if arg:
            try:
                os.chdir(Path(arg).expanduser())
                console.print(f"[dim]Working directory: {os.getcwd()}[/dim]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")
        else:
            console.print(os.getcwd())
        return True, messages

    elif command == "/model":
        if arg and arg != "list":
            # Direct switch — /model <name>
            _model_switch(arg, config, messages)
        else:
            # Interactive picker — /model or /model list
            ollama_url = config.get("ollama_url", "http://localhost:11434")
            try:
                data = requests.get(f"{ollama_url}/api/tags", timeout=10).json()
                models = data.get("models", [])
                if not models:
                    console.print("[yellow]No models found on Ollama server.[/yellow]")
                    return True, messages
                current = config.get("model", "")
                selected = _model_picker(models, current)
                if selected:
                    _model_switch(selected, config, messages)
            except Exception as e:
                console.print(f"[red]Could not fetch models: {e}[/red]")
        return True, messages

    elif command == "/update":
        do_update()
        console.print("[dim]Restart clawcli to run the new version.[/dim]")
        return True, messages

    elif command == "/doctor":
        do_doctor(config)
        return True, messages

    elif command == "/kali":
        if arg.lower() == "disable":
            config.pop("kali_server_url", None)
            CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
            for m in messages:
                if m.get("role") == "system":
                    m["content"] = build_system_prompt(config)
                    break
            console.print("[dim]Kali server disabled and removed from config.[/dim]")
        elif arg:
            url = arg.rstrip("/")
            ok, msg = kali_health(url)
            if ok:
                config["kali_server_url"] = url
                CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
                # Rebuild system message with new Kali context
                for m in messages:
                    if m.get("role") == "system":
                        m["content"] = build_system_prompt(config)
                        break
                console.print(f"[green]✓[/green]  Kali server set to {url}")
            else:
                console.print(f"[red]✗[/red]  {msg}")
                console.print("[dim]  Server saved anyway — use /kali disable to remove it.[/dim]")
                config["kali_server_url"] = url
                CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
        else:
            kali_url = config.get("kali_server_url", "")
            if kali_url:
                ok, msg = kali_health(kali_url)
                status = "[green]✓  reachable[/green]" if ok else "[red]✗  unreachable[/red]"
                console.print(f"Kali server: [cyan]{kali_url}[/cyan]  {status}")
            else:
                console.print("[dim]Kali server not configured. Usage: /kali <url>[/dim]")
        return True, messages

    elif command == "/undo":
        last_user_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if (messages[i].get("role") == "user"
                    and messages[i].get("content") != "[Conversation compacted — summary of prior context below]"):
                last_user_idx = i
                break
        if last_user_idx is None:
            console.print("[dim]Nothing to undo.[/dim]")
        else:
            removed = len(messages) - last_user_idx
            messages = messages[:last_user_idx]
            console.print(f"[dim]Undid last exchange ({removed} messages removed).[/dim]")
        return True, messages

    elif command == "/export":
        filename = arg.strip() if arg else f"clawcli-export-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.md"
        path = Path(filename).expanduser()
        lines = [f"# CLAWCLI Export — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"]
        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if not content.strip() or role == "system":
                continue
            if role == "user":
                lines.append(f"## You\n\n{content.strip()}\n\n")
            elif role == "assistant":
                lines.append(f"## Assistant\n\n{content.strip()}\n\n")
            elif role == "tool":
                snippet = content[:2000] + ("…" if len(content) > 2000 else "")
                lines.append(f"### Tool: {m.get('name', '?')}\n\n```\n{snippet}\n```\n\n")
        path.write_text("".join(lines))
        console.print(f"[dim]Exported to {path}[/dim]")
        return True, messages

    elif command == "/searxng":
        if arg.lower() == "disable":
            config.pop("searxng_url", None)
            CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
            for m in messages:
                if m.get("role") == "system":
                    m["content"] = build_system_prompt(config)
                    break
            console.print("[dim]SearXNG disabled and removed from config.[/dim]")
        elif arg:
            url = arg.rstrip("/")
            try:
                resp = requests.get(
                    f"{url}/search",
                    params={"q": "test", "format": "json"},
                    timeout=5,
                )
                resp.raise_for_status()
                config["searxng_url"] = url
                CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
                for m in messages:
                    if m.get("role") == "system":
                        m["content"] = build_system_prompt(config)
                        break
                console.print(f"[green]✓[/green]  SearXNG set to {url}")
            except Exception as e:
                console.print(f"[red]✗[/red]  Cannot reach SearXNG at {url}: {e}")
                console.print("[dim]  URL not saved — fix the connection and try again.[/dim]")
        else:
            searxng_url = config.get("searxng_url", "")
            if searxng_url:
                try:
                    resp = requests.get(
                        f"{searxng_url}/search",
                        params={"q": "test", "format": "json"},
                        timeout=5,
                    )
                    resp.raise_for_status()
                    status = "[green]✓  reachable[/green]"
                except Exception:
                    status = "[red]✗  unreachable[/red]"
                console.print(f"SearXNG: [cyan]{searxng_url}[/cyan]  {status}")
            else:
                console.print("[dim]SearXNG not configured. Usage: /searxng <url>[/dim]")
        return True, messages

    elif command == "/mcp":
        parts2 = arg.strip().split(None, 1)
        sub = parts2[0].lower() if parts2 else ""
        if sub == "disable":
            config.pop("mcp_server_url", None)
            config.pop("mcp_bearer_token", None)
            CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
            init_mcp(config, quiet=True)
            console.print("[dim]MCP server removed from config.[/dim]")
        elif sub == "token":
            token_val = parts2[1].strip() if len(parts2) > 1 else ""
            if not token_val:
                console.print("[red]Usage: /mcp token <value>[/red]")
            else:
                config["mcp_bearer_token"] = token_val
                CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
                console.print("[dim]MCP bearer token saved. Reconnecting...[/dim]")
                init_mcp(config)
        elif sub == "tools":
            excluded = set(config.get("mcp_excluded_tools", []))
            if _mcp_tool_definitions or excluded:
                console.print(f"[bold]MCP tools ({len(_mcp_tool_definitions)} active):[/bold]")
                for t in _mcp_tool_definitions:
                    fn = t["function"]
                    desc = rich_escape(fn.get("description", "").replace("[MCP] ", "")[:80])
                    console.print(f"  [cyan]{fn['name']}[/cyan]  [dim]{desc}[/dim]")
                if excluded:
                    console.print(f"  [dim]Excluded: {rich_escape(', '.join(sorted(excluded)))}[/dim]")
            else:
                console.print("[dim]No MCP tools loaded.[/dim]")
        elif sub == "exclude":
            tool_name = parts2[1].strip() if len(parts2) > 1 else ""
            if not tool_name:
                console.print("[red]Usage: /mcp exclude <tool_name>[/red]")
            else:
                excluded = config.get("mcp_excluded_tools", [])
                if tool_name not in excluded:
                    excluded.append(tool_name)
                    config["mcp_excluded_tools"] = excluded
                    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
                    init_mcp(config, quiet=True)
                console.print(f"[dim]MCP tool excluded: {tool_name}[/dim]")
        elif sub == "include":
            tool_name = parts2[1].strip() if len(parts2) > 1 else ""
            if not tool_name:
                console.print("[red]Usage: /mcp include <tool_name>[/red]")
            else:
                excluded = config.get("mcp_excluded_tools", [])
                if tool_name in excluded:
                    excluded.remove(tool_name)
                    config["mcp_excluded_tools"] = excluded
                    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
                    init_mcp(config, quiet=True)
                console.print(f"[dim]MCP tool re-included: {tool_name}[/dim]")
        elif sub and not sub.startswith("http"):
            console.print("[red]Unknown /mcp subcommand.[/red]  Usage: /mcp <url> | token <val> | tools | exclude <name> | include <name> | disable")
        elif sub:
            # /mcp <url>
            url = parts2[0].strip()
            token = config.get("mcp_bearer_token", "")
            ok, msg = check_mcp_health(url, token)
            if ok:
                config["mcp_server_url"] = url
                CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
                init_mcp(config, quiet=True)
                console.print(f"[green]✓[/green]  MCP server set to {url} — {msg} — saved")
            else:
                console.print(f"[red]✗[/red]  Cannot connect to MCP server at {url}: {msg}")
                console.print("[dim]  Fix the connection and try again, or set a token with /mcp token <value>[/dim]")
        else:
            # /mcp alone — show status
            mcp_url = config.get("mcp_server_url", "")
            token_set = bool(config.get("mcp_bearer_token", ""))
            if mcp_url:
                ok, msg = check_mcp_health(mcp_url, config.get("mcp_bearer_token", ""))
                status = f"[green]✓  {msg}[/green]" if ok else f"[red]✗  {msg}[/red]"
                token_status = "[green]set[/green]" if token_set else "[dim]not set[/dim]"
                console.print(f"MCP server: [cyan]{mcp_url}[/cyan]  {status}  token: {token_status}")
                if _mcp_tool_definitions:
                    names = rich_escape(", ".join(t["function"]["name"] for t in _mcp_tool_definitions))
                    console.print(f"  Tools loaded: {names}")
            else:
                console.print("[dim]MCP server not configured. Usage: /mcp <url>[/dim]")
        return True, messages

    elif command == "/think":
        arg = arg.strip().lower()
        if arg in ("on", "true", "1"):
            config["think"] = True
            CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
            console.print("[green]Thinking mode ON[/green] — model will show reasoning (saved to config.json)")
        elif arg in ("off", "false", "0"):
            config["think"] = False
            CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
            console.print("[yellow]Thinking mode OFF[/yellow] — reasoning suppressed (saved to config.json)")
        elif arg in ("default", "reset"):
            config.pop("think", None)
            CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
            console.print("[dim]Thinking mode reset to model default (removed from config.json)[/dim]")
        elif arg == "":
            pass  # no-op — just show current status below
        else:
            console.print(f"[red]Unknown argument:[/red] '{arg}'  — use: /think on | off | default")
            return True, messages
        think_val = config.get("think")
        if think_val is True:
            status = "[green]on[/green]"
        elif think_val is False:
            status = "[yellow]off[/yellow]"
        else:
            status = "[dim]model default[/dim]"
        console.print(f"think = {status}")
        return True, messages

    elif command == "/set":
        parts2 = arg.strip().split(None, 1)
        if not parts2 or not parts2[0]:
            # Show all settable keys and current values
            rows = []
            for key, (typ, desc) in _SETTABLE_KEYS.items():
                val = config.get(key, "[dim]unset[/dim]")
                rows.append(f"  [cyan]{key}[/cyan] = [yellow]{val}[/yellow]  [dim]({typ}) {desc}[/dim]")
            console.print("[bold]Settable config keys:[/bold]\n" + "\n".join(rows))
            console.print("\n[dim]Usage: /set <key> <value>   or   /set <key> default[/dim]")
            return True, messages
        key = parts2[0].lower()
        if key not in _SETTABLE_KEYS:
            console.print(f"[red]Unknown key:[/red] '{key}'. Run [bold]/set[/bold] to see valid keys.")
            return True, messages
        typ, desc = _SETTABLE_KEYS[key]
        raw = parts2[1].strip() if len(parts2) > 1 else ""
        if not raw:
            console.print(f"[dim]{key} = {config.get(key)}[/dim]")
            return True, messages
        if raw.lower() == "default":
            # Load from defaults file
            defaults_file = CLAWCLI_DIR / "config.defaults.json"
            try:
                defaults = json.loads(defaults_file.read_text())
                config[key] = defaults[key]
            except Exception:
                console.print(f"[red]Could not read defaults for {key}[/red]")
                return True, messages
            CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
            console.print(f"[dim]{key} reset to default: {config[key]} — saved[/dim]")
            return True, messages
        try:
            if typ == "int":
                config[key] = int(raw)
            elif typ == "float":
                config[key] = float(raw)
            elif typ == "bool":
                if raw.lower() in ("true", "1", "yes", "on"):
                    config[key] = True
                elif raw.lower() in ("false", "0", "no", "off"):
                    config[key] = False
                else:
                    raise ValueError
        except ValueError:
            console.print(f"[red]Invalid value:[/red] '{raw}' — expected {typ}")
            return True, messages
        CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
        console.print(f"[dim]{key} = {config[key]} — saved[/dim]")
        return True, messages

    elif command in ("/exit", "/quit", "/q"):
        if session_id:
            save_session(session_id, messages, os.getcwd())
            print_resume_hint(session_id)
        else:
            console.print("[dim]Goodbye.[/dim]")
        sys.exit(0)

    return False, messages


def new_session_id() -> str:
    return str(uuid.uuid4())


def save_session(session_id: str, messages: list, cwd: str):
    SESSIONS_DIR.mkdir(mode=0o700, exist_ok=True)
    path = SESSIONS_DIR / f"{session_id}.json"
    # Strip system message — it's rebuilt from current state on resume
    payload = {
        "id": session_id,
        "saved_at": datetime.now().isoformat(),
        "cwd": cwd,
        "messages": [m for m in messages if m.get("role") != "system"],
    }
    path.write_text(json.dumps(payload, indent=2))
    path.chmod(0o600)


def purge_old_sessions(days: int = 30):
    if not SESSIONS_DIR.exists():
        return
    cutoff = datetime.now().timestamp() - days * 86400
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def _secure_sessions_dir():
    """Fix permissions on any existing session files that are world-readable."""
    if not SESSIONS_DIR.exists():
        return
    try:
        SESSIONS_DIR.chmod(0o700)
    except OSError:
        pass
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            f.chmod(0o600)
        except OSError:
            pass


def load_session(session_id: str) -> tuple[list, str]:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        console.print(f"[red]Session not found: {session_id}[/red]")
        sys.exit(1)
    payload = json.loads(path.read_text())
    return payload["messages"], payload.get("cwd", os.getcwd())


def list_sessions():
    SESSIONS_DIR.mkdir(exist_ok=True)
    files = sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        console.print("[dim]No saved sessions.[/dim]")
        return
    console.print(f"[bold]Saved sessions:[/bold] ({len(files)} total)\n")
    for f in files:
        try:
            data = json.loads(f.read_text())
            saved_at = data.get("saved_at", "")[:19].replace("T", " ")
            cwd = data.get("cwd", "")
            msgs = [m for m in data.get("messages", []) if m.get("role") == "user"]
            first = msgs[0]["content"][:60] + "…" if msgs else "(empty)"
            sid = f.stem
            console.print(f"  [cyan]{sid}[/cyan]")
            console.print(f"  [dim]{saved_at}  {cwd}[/dim]")
            console.print(f"  [white]{rich_escape(first)}[/white]\n")
        except Exception:
            console.print(f"  [dim]{f.stem}[/dim] (unreadable)\n")


def print_resume_hint(session_id: str):
    console.print(f"\n[dim]Session saved. To resume:[/dim]")
    console.print(f"[bold cyan]  clawcli --resume {session_id}[/bold cyan]")


def do_doctor(config: dict):
    import sys as _sys
    console.print("\n[bold]CLAWCLI Doctor[/bold]  [dim]checking your setup...[/dim]\n")
    issues = 0

    # Python version
    pv = _sys.version_info
    if pv >= (3, 9):
        console.print(f"[green]✓[/green]  Python {pv.major}.{pv.minor}.{pv.micro}")
    else:
        console.print(f"[red]✗[/red]  Python {pv.major}.{pv.minor}.{pv.micro} — 3.9+ required")
        issues += 1

    # curl_cffi
    try:
        import curl_cffi as _cc
        console.print(f"[green]✓[/green]  curl_cffi {_cc.__version__} (Cloudflare bypass enabled)")
    except ImportError:
        console.print("[yellow]![/yellow]  curl_cffi not installed — web_fetch may be blocked by bot-detection")
        console.print("     Fix: pip install curl_cffi")
        issues += 1

    # Ollama
    ollama_url = config.get("ollama_url", "http://localhost:11434")
    model      = config.get("model", "")
    try:
        resp   = requests.get(f"{ollama_url}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        console.print(f"[green]✓[/green]  Ollama reachable at {ollama_url}")
        if model in models:
            console.print(f"[green]✓[/green]  Model '{model}' available")
        else:
            console.print(f"[yellow]![/yellow]  Model '{model}' not found on this server")
            if models:
                console.print(f"     Available: {', '.join(models[:6])}")
            console.print(f"     Fix: ollama pull {model}")
            issues += 1
    except Exception as e:
        console.print(f"[red]✗[/red]  Cannot reach Ollama at {ollama_url}")
        console.print(f"     {e}")
        issues += 1

    # SearXNG (optional)
    searxng_url = config.get("searxng_url", "")
    if searxng_url:
        try:
            resp = requests.get(
                f"{searxng_url.rstrip('/')}/search",
                params={"q": "test", "format": "json"},
                timeout=5,
            )
            resp.raise_for_status()
            console.print(f"[green]✓[/green]  SearXNG reachable at {searxng_url}")
        except Exception as e:
            console.print(f"[red]✗[/red]  SearXNG unreachable at {searxng_url}: {e}")
            issues += 1
    else:
        console.print("[dim]-[/dim]  SearXNG not configured (web search disabled — optional)")

    # Kali server (optional)
    kali_url = config.get("kali_server_url", "")
    if kali_url:
        ok, msg = kali_health(kali_url)
        if ok:
            console.print(f"[green]✓[/green]  Kali server reachable at {kali_url}")
        else:
            console.print(f"[red]✗[/red]  Kali server unreachable: {msg}")
            console.print(f"     Fix: ensure mcp-kali-server is running, or /kali disable")
            issues += 1
    else:
        console.print("[dim]-[/dim]  Kali security scanning not configured (optional — /kali <url> to enable)")

    # MCP server (optional)
    mcp_url = config.get("mcp_server_url", "")
    if mcp_url:
        ok, msg = check_mcp_health(mcp_url, config.get("mcp_bearer_token", ""))
        if ok:
            console.print(f"[green]✓[/green]  MCP server reachable at {mcp_url} — {msg}")
        else:
            console.print(f"[red]✗[/red]  MCP server unreachable: {msg}")
            console.print(f"     Fix: check server is running, or verify token with /mcp token <value>")
            issues += 1
    else:
        console.print("[dim]-[/dim]  MCP server not configured (optional — /mcp <url> to enable)")

    # Config file
    if CONFIG_FILE.exists():
        console.print(f"[green]✓[/green]  Config: {CONFIG_FILE}")
    else:
        console.print(f"[yellow]![/yellow]  No config.json — run install.sh or copy from config.defaults.json")
        issues += 1

    # Memory
    if MEMORY_FILE.exists():
        console.print(f"[green]✓[/green]  Memory: {MEMORY_FILE} ({MEMORY_FILE.stat().st_size} bytes)")
    else:
        console.print("[dim]-[/dim]  No memory file yet (created on first save)")

    console.print()
    if issues == 0:
        console.print("[green]All checks passed.[/green]")
    else:
        console.print(f"[yellow]{issues} issue(s) found — see above.[/yellow]")


def do_update():
    console.print("[dim]Updating CLAWCLI from origin/main...[/dim]")
    result = subprocess.run(  # nosec B603 B607 — fixed args, no user input
        ["git", "pull", "--ff-only", "origin", "main"],
        cwd=str(CLAWCLI_DIR),
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        # ff-only fails after a force-push (e.g. history squash) — reset hard instead
        console.print("[dim]Fast-forward failed; resetting to origin/main...[/dim]")
        subprocess.run(  # nosec B603 B607
            ["git", "fetch", "origin"],
            cwd=str(CLAWCLI_DIR), capture_output=True,
        )
        result = subprocess.run(  # nosec B603 B607
            ["git", "reset", "--hard", "origin/main"],
            cwd=str(CLAWCLI_DIR), capture_output=True, text=True,
        )
        output = (result.stdout + result.stderr).strip()
        console.print(output)
        if result.returncode != 0:
            console.print("[red]Update failed.[/red]")
            sys.exit(1)
    else:
        console.print(output)

    # Fetch tags separately — git pull doesn't reliably fetch them
    subprocess.run(  # nosec B603 B607
        ["git", "fetch", "--tags"],
        cwd=str(CLAWCLI_DIR),
        capture_output=True,
    )

    if "Already up to date" not in output:
        console.print("[dim]Re-installing dependencies...[/dim]")
        # Prefer venv pip if present, fall back to current interpreter
        venv_pip = CLAWCLI_DIR / ".venv" / "bin" / "pip"
        pip_cmd = [str(venv_pip)] if venv_pip.exists() else [sys.executable, "-m", "pip"]
        pip = subprocess.run(  # nosec B603 B607 — fixed args, no user input
            pip_cmd + ["install", "-q", "--upgrade", "-r", str(CLAWCLI_DIR / "requirements.txt")],
            capture_output=True, text=True,
        )
        if pip.returncode != 0:
            # Filter out noise from system-installed packages pip can't manage
            real_errors = "\n".join(
                l for l in pip.stderr.splitlines()
                if "uninstall-no-record-file" not in l and "Cannot uninstall" not in l
                and "no RECORD file" not in l and "installed by debian" not in l
                and "installed by" not in l.lower() and l.strip()
            ).strip()
            if real_errors:
                console.print(f"[yellow]pip warning:[/yellow] {real_errors}")
    console.print("[green]Up to date.[/green]")


_SLASH_COMMANDS = [
    ("/help",              "show help"),
    ("/update",            "pull latest version (restart to apply)"),
    ("/doctor",            "check Ollama, SearXNG, dependencies"),
    ("/memory",            "show persistent memory"),
    ("/clear",             "clear conversation history"),
    ("/compact",           "summarize history to free context window"),
    ("/undo",              "remove the last exchange from history"),
    ("/export [file]",     "save conversation to a Markdown file"),
    ("/config",            "show current config"),
    ("/cwd <path>",        "change working directory"),
    ("/model",             "interactive model picker (↑↓ arrow keys)"),
    ("/model <name>",      "switch Ollama model directly"),
    ("/searxng <url>",     "set SearXNG URL (or /searxng to check status, /searxng disable)"),
    ("/kali <url>",        "set Kali security server URL (or /kali to check status, /kali disable)"),
    ("/mcp <url>",            "set MCP server URL (or /mcp to check, /mcp tools, /mcp disable)"),
    ("/mcp token <value>",   "set MCP bearer token and reconnect"),
    ("/mcp exclude <name>",  "hide an MCP tool from the model"),
    ("/mcp include <name>",  "re-enable a previously excluded MCP tool"),
    ("/think <on|off|default>", "enable/disable model thinking mode (Qwen3 etc.)"),
    ("/set <key> <value>", "set a config value and save (run /set alone to list all keys)"),
    ("/exit",              "quit and save session"),
]


class SlashCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        typed = text.lower()
        for cmd, desc in _SLASH_COMMANDS:
            # Match on the fixed part of the command (before any placeholder)
            fixed = cmd.split(" <")[0].split(" [")[0]
            if fixed.startswith(typed) or cmd.startswith(typed):
                # For commands with placeholders, insert up to the placeholder
                # so the user can continue typing the argument
                insert = fixed + (" " if "<" in cmd else "")
                yield Completion(
                    insert,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )


def main():
    parser = argparse.ArgumentParser(
        prog="clawcli",
        description="CLAWCLI — AI coding assistant powered by Ollama",
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt (non-interactive)")
    parser.add_argument("--model", "-m", help="Override Ollama model")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming output")
    parser.add_argument("--resume", metavar="SESSION_ID", help="Resume a saved session")
    parser.add_argument("--continue", dest="resume_last", action="store_true", help="Resume the last saved session")
    parser.add_argument("--confirm", action="store_true", default=None, help="Require confirmation before running bash commands")
    parser.add_argument("--no-confirm", action="store_true", help="Run bash commands without confirmation (overrides config)")
    parser.add_argument("--version", action="version", version=f"CLAWCLI {VERSION}")
    args = parser.parse_args()

    config = load_config()
    if args.model:
        config["model"] = args.model
    if args.no_stream:
        config["stream"] = False
    if args.no_confirm:
        config["confirm_bash"] = False
    elif args.confirm:
        config["confirm_bash"] = True

    _secure_sessions_dir()   # fix permissions on any existing world-readable files
    purge_old_sessions(days=30)

    update_thread = _start_update_check()  # runs concurrently during startup

    detect_context_window(config)
    init_mcp(config)

    system_prompt = build_system_prompt(config)
    messages = [{"role": "system", "content": system_prompt}]

    # Built-in subcommands
    if args.prompt and args.prompt[0] == "update":
        do_update()
        return

    if args.prompt and args.prompt[0] == "doctor":
        do_doctor(config)
        return

    if args.prompt and args.prompt[0] == "sessions":
        list_sessions()
        return

    # One-shot mode (no session save)
    if args.prompt:
        user_input = " ".join(args.prompt)
        messages = run_agentic_loop(user_input, messages, config)
        return

    # Session setup
    session_id = new_session_id()
    if args.resume_last:
        SESSIONS_DIR.mkdir(exist_ok=True)
        files = sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            console.print("[red]No saved sessions to resume.[/red]")
            sys.exit(1)
        args.resume = files[0].stem

    if args.resume:
        prior_messages, saved_cwd = load_session(args.resume)
        messages.extend(prior_messages)
        session_id = args.resume
        try:
            os.chdir(saved_cwd)
        except OSError:
            pass  # saved cwd no longer exists — stay in current directory
        console.print(f"\n[dim]Resumed session {session_id}[/dim]\n")
        for m in prior_messages:
            role = m.get("role")
            content = m.get("content") or ""
            if not content.strip():
                continue
            if role == "user":
                console.print(f"[bold cyan]You:[/bold cyan] {rich_escape(content.strip())}")
            elif role == "assistant":
                console.print(Markdown(content.strip()))
            console.print()

    # Interactive REPL
    console.clear()
    print_welcome(config)
    _finish_update_check(update_thread)

    # Mutable holder so the SIGTERM handler always sees the latest messages list
    messages_holder = [messages]

    def _sigterm_handler(signum, frame):
        try:
            save_session(session_id, messages_holder[0], os.getcwd())
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)
    try:
        signal.signal(signal.SIGHUP, _sigterm_handler)  # terminal closed
    except (AttributeError, ValueError):
        pass  # SIGHUP unavailable (Windows) or not in main thread

    kb = KeyBindings()

    def _insert_newline(event):
        event.current_buffer.insert_text("\n")

    # Shift+Enter: bind to the CSI sequence most terminals send (iTerm2, kitty, etc.)
    # Mac Terminal.app: enable this via Preferences → Profiles → Keyboard →
    #   add binding: Shift+Enter → send escape sequence → [13;2u
    kb.add("\x1b", "[", "1", "3", ";", "2", "u")(_insert_newline)

    kb.add("escape", "enter")(_insert_newline)  # Alt/Option+Enter
    kb.add("c-j")(_insert_newline)              # Ctrl+J (works everywhere)

    @kb.add("enter")             # Enter → submit
    def _submit(event):
        event.current_buffer.validate_and_handle()

    session = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        style=Style.from_dict({"prompt": "bold cyan"}),
        multiline=True,
        key_bindings=kb,
        completer=SlashCompleter(),
        complete_while_typing=True,
    )

    try:
        while True:
            try:
                cwd_short = os.getcwd().replace(str(Path.home()), "~")
                user_input = session.prompt(f"\n[{cwd_short}] > ", default="")
            except KeyboardInterrupt:
                console.print("\n[dim]Cancelled. Type /exit to quit.[/dim]")
                continue
            except EOFError:
                save_session(session_id, messages, os.getcwd())
                print_resume_hint(session_id)
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input.startswith("/"):
                handled, messages = handle_slash_command(user_input, config, messages, session_id)
                messages_holder[0] = messages
                if handled:
                    continue

            messages = run_agentic_loop(user_input, messages, config)
            messages_holder[0] = messages
            try:
                save_session(session_id, messages, os.getcwd())  # autosave each turn
            except OSError:
                pass
    except Exception as _exc:
        console.print(f"[red]Unexpected error: {rich_escape(str(_exc))}[/red]")
        try:
            save_session(session_id, messages_holder[0], os.getcwd())
            print_resume_hint(session_id)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
