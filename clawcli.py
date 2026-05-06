#!/usr/bin/env python3
"""CLAWCLI — A Claude Code-like AI assistant powered by Ollama/gemma4:26b"""

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

import requests
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.spinner import Spinner
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.completion import Completer, Completion

# ── Paths ────────────────────────────────────────────────────────────────────
CLAWCLI_DIR = Path(__file__).resolve().parent  # resolve symlink before .parent


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
from tools import TOOL_DEFINITIONS
from tools.file_tools import read_file, write_file, edit_file, glob_files, grep_files
from tools.bash_tool import execute_bash
from tools.search_tool import web_search, web_fetch


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


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
        answer = input("  Allow? [y/N] ").strip().lower()
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
            return write_file(args["file_path"], args["content"])

        elif name == "edit_file":
            return edit_file(
                args["file_path"],
                args["old_string"],
                args["new_string"],
                args.get("replace_all", False),
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
            )

        elif name == "web_search":
            return web_search(args["query"], searxng_url, args.get("num_results", 10))

        elif name == "web_fetch":
            return web_fetch(args["url"], args.get("max_chars", 8000))

        elif name == "save_memory":
            return save_memory(args["section"], args["content"])

        else:
            return f"Unknown tool: {name}"

    except KeyError as e:
        return f"Tool error: missing required argument {e}"
    except Exception as e:
        return f"Tool error ({name}): {e}"


_PRONOUN_RE = re.compile(
    r'\b(he|she|him|her|they|them|it|this|that|these|those)\b', re.IGNORECASE
)
_PROPER_NOUN_RE = re.compile(r'\b([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,})*)\b')
_NOUN_STOPWORDS = {
    "The", "This", "That", "These", "Those", "There", "Then", "When", "Where",
    "What", "Which", "Who", "How", "Why", "Based", "Search", "Note", "Also",
    "Here", "If", "In", "On", "At", "For", "To", "Of", "And", "Or", "But",
    "Is", "Are", "Was", "Were", "Be", "As", "An", "By", "From", "With",
}


def _rewrite_search_query(query: str, messages: list) -> str:
    """Replace pronouns in a search query with proper nouns from recent conversation history."""
    if not _PRONOUN_RE.search(query):
        return query
    candidates: list[str] = []
    for m in reversed(messages[-20:]):
        if m.get("role") not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        if not content or content == query:
            continue
        for noun in _PROPER_NOUN_RE.findall(content):
            if noun not in _NOUN_STOPWORDS and len(noun) > 3:
                candidates.append(noun)
        if candidates:
            break
    if not candidates:
        return query
    primary = candidates[0]
    return _PRONOUN_RE.sub(primary, query).strip()


def render_tool_call(name: str, args: dict):
    console.print(f"\n[bold cyan]⚙ {name}[/bold cyan] ", end="")
    key_args = {k: v for k, v in args.items() if k not in ("content",)}
    if key_args:
        parts = [f"[dim]{k}=[/dim][white]{str(v)[:200]}[/white]" for k, v in key_args.items()]
        console.print(" ".join(parts))
    else:
        console.print()


def chat(messages: list, config: dict, stream: bool = True) -> dict:
    url   = config.get("ollama_url", "http://localhost:11434") + "/api/chat"
    model = config.get("model", "gemma4:26b")
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOL_DEFINITIONS,
        "stream": stream,
        "options": {
            "temperature": config.get("temperature", 0.1),
            "num_ctx": config.get("context_window", 8192),
        },
    }
    resp = requests.post(url, json=payload, stream=stream, timeout=config.get("ollama_timeout", 600))
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
    with Live(Spinner("dots", text="[dim]thinking…[/dim]"), console=console, refresh_per_second=12, vertical_overflow="visible") as live:
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
                live.update(Markdown(full_content))

                if chunk_count % REPEAT_CHECK_EVERY == 0 and _is_repetitive(full_content):
                    resp.close()
                    live.update(Markdown(full_content.rstrip()))
                    console.print("\n[yellow]⚠ Runaway repetition detected — output truncated.[/yellow]")
                    break

            if delta_tools:
                tool_calls.extend(delta_tools)

            if chunk.get("done"):
                prompt_tokens     = chunk.get("prompt_eval_count", 0)
                completion_tokens = chunk.get("eval_count", 0)
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
        resolved = _rewrite_search_query(query, messages)
        if resolved != query:
            console.print(f"[dim]  (resolved: \"{query}\" → \"{resolved}\")[/dim]")
            query = resolved
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
                console.print(
                    f"[dim]  context: {prompt_tokens:,} / {ctx_window:,} tokens ({pct:.1f}%)[/dim]"
                )
            break

        # Execute all tool calls and collect results
        tool_results = []
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
            result = dispatch_tool(name, args, config, confirm=config.get("confirm_bash", False))

            call_hash = hash((name, json.dumps(args, sort_keys=True), result))
            if call_hash == last_call_hash:
                result = f"Loop detected: tool '{name}' returned the same result twice in a row. Try a different approach."
            last_call_hash = call_hash

            # Show brief result preview
            preview = result[:1000] + "…" if len(result) > 1000 else result
            console.print(f"[dim]  → {preview}[/dim]")

            tool_results.append({
                "role": "tool",
                "content": result,
                "name": name,
            })

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
        "  /help          — show this help\n"
        "  /update        — pull latest version (restart to apply)\n"
        "  /doctor        — check Ollama, SearXNG, and dependencies\n"
        "  /memory        — show current memory\n"
        "  /clear         — clear conversation history\n"
        "  /compact       — summarize history to free context window\n"
        "  /config        — show current config\n"
        "  /cwd <path>    — change working directory\n"
        "  /model list    — list available Ollama models\n"
        "  /model <name>  — switch Ollama model\n"
        "  /exit          — quit and save session\n\n"
        "[bold]Special prompts:[/bold]\n"
        "  research <topic>  — search SearXNG then summarize\n\n"
        "[bold]Sessions:[/bold]\n"
        "  clawcli sessions             — list saved sessions\n"
        "  clawcli --resume <id>        — resume a session\n"
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
            [{"role": "system", "content": system_msg["content"] if system_msg else ""},
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
        messages.clear()
        console.print("[dim]Conversation cleared.[/dim]")
        return True, messages

    elif command == "/compact":
        messages = compact_messages(messages, config)
        return True, messages

    elif command == "/config":
        console.print(Panel(json.dumps(config, indent=2), title="Config", border_style="dim"))
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
        if arg == "list":
            ollama_url = config.get("ollama_url", "http://localhost:11434")
            try:
                data = requests.get(f"{ollama_url}/api/tags", timeout=10).json()
                models = data.get("models", [])
                current = config.get("model")
                console.print(f"\n[bold]Available models on {ollama_url}:[/bold]")
                for m in models:
                    name = m["name"]
                    size_gb = m["size"] / 1e9
                    params = m.get("details", {}).get("parameter_size", "")
                    quant  = m.get("details", {}).get("quantization_level", "")
                    marker = "[bold green] ◀ active[/bold green]" if name == current else ""
                    console.print(f"  [cyan]{name}[/cyan]  [dim]{params} {quant} {size_gb:.1f}GB[/dim]{marker}")
            except Exception as e:
                console.print(f"[red]Could not fetch models: {e}[/red]")
        elif arg:
            config["model"] = arg
            detect_context_window(config)
            # Rebuild system message so the model knows its own name
            for m in messages:
                if m.get("role") == "system":
                    m["content"] = build_system_prompt(config)
                    break
            console.print(f"[dim]Model switched to: {arg}  (context: {config['context_window']:,})[/dim]")
        else:
            console.print(f"[dim]Current model: {config.get('model')}[/dim]")
        return True, messages

    elif command == "/update":
        do_update()
        console.print("[dim]Restart clawcli to run the new version.[/dim]")
        return True, messages

    elif command == "/doctor":
        do_doctor(config)
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
            console.print(f"  [white]{first}[/white]\n")
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
    if pv >= (3, 10):
        console.print(f"[green]✓[/green]  Python {pv.major}.{pv.minor}.{pv.micro}")
    else:
        console.print(f"[red]✗[/red]  Python {pv.major}.{pv.minor}.{pv.micro} — 3.10+ required")
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
            console.print(f"[yellow]pip warning:[/yellow] {pip.stderr.strip()}")
    console.print("[green]Up to date.[/green]")


_SLASH_COMMANDS = [
    ("/help",         "show help"),
    ("/update",       "pull latest version (restart to apply)"),
    ("/doctor",       "check Ollama, SearXNG, dependencies"),
    ("/memory",       "show persistent memory"),
    ("/clear",        "clear conversation history"),
    ("/compact",      "summarize history to free context window"),
    ("/config",       "show current config"),
    ("/cwd <path>",   "change working directory"),
    ("/model list",   "list available Ollama models"),
    ("/model <name>", "switch Ollama model"),
    ("/exit",         "quit and save session"),
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
                console.print(f"[bold cyan]You:[/bold cyan] {content.strip()}")
            elif role == "assistant":
                console.print(Markdown(content.strip()))
            console.print()

    # Interactive REPL
    console.clear()
    print_welcome(config)
    _finish_update_check(update_thread)

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
            if handled:
                continue

        messages = run_agentic_loop(user_input, messages, config)


if __name__ == "__main__":
    main()
