#!/usr/bin/env python3
"""CLAWCLI — A Claude Code-like AI assistant powered by Ollama/gemma4:26b"""

import os
import sys
import json
import re
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
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style

# ── Paths ────────────────────────────────────────────────────────────────────
CLAWCLI_DIR = Path(__file__).parent.resolve()
CONFIG_FILE  = CLAWCLI_DIR / "config.json"
MEMORY_FILE  = CLAWCLI_DIR / "memory" / "MEMORY.md"
SYSPROMPT    = CLAWCLI_DIR / "system_prompt.txt"
HISTORY_FILE = Path.home() / ".clawcli_history"

# ── Console ──────────────────────────────────────────────────────────────────
console = Console()

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


def load_memory() -> str:
    if MEMORY_FILE.exists():
        return MEMORY_FILE.read_text()
    return ""


def save_memory(section: str, content: str) -> str:
    text = MEMORY_FILE.read_text() if MEMORY_FILE.exists() else "# CLAWCLI Memory\n\n"
    marker = f"## {section}"
    if marker in text:
        idx = text.index(marker) + len(marker)
        next_section = text.find("\n## ", idx)
        if next_section == -1:
            text = text[:idx] + "\n" + f"- {content}\n" + text[idx:]
        else:
            text = text[:idx] + "\n" + f"- {content}\n" + text[idx:]
    else:
        text += f"\n## {section}\n- {content}\n"
    MEMORY_FILE.write_text(text)
    return f"Memory saved to section '{section}'"


def build_system_prompt(config: dict) -> str:
    base = SYSPROMPT.read_text() if SYSPROMPT.exists() else "You are CLAWCLI, an AI coding assistant."
    base = base.replace("{date}", datetime.now().strftime("%Y-%m-%d"))
    memory = load_memory()
    if memory.strip():
        base += f"\n\n## Your Persistent Memory\n{memory}"
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
    ollama_url  = config.get("ollama_url", "http://192.168.1.62:11434")
    searxng_url = config.get("searxng_url", "http://192.168.1.140:8888")

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
                args.get("timeout", 60),
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


def render_tool_call(name: str, args: dict):
    console.print(f"\n[bold cyan]⚙ {name}[/bold cyan] ", end="")
    key_args = {k: v for k, v in args.items() if k not in ("content",)}
    if key_args:
        parts = [f"[dim]{k}=[/dim][white]{str(v)[:60]}[/white]" for k, v in key_args.items()]
        console.print(" ".join(parts))
    else:
        console.print()


def chat(messages: list, config: dict, stream: bool = True) -> dict:
    url   = config.get("ollama_url", "http://192.168.1.62:11434") + "/api/chat"
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
    resp = requests.post(url, json=payload, stream=stream, timeout=300)
    resp.raise_for_status()

    if not stream:
        return resp.json()

    # Streaming: accumulate content and tool_calls
    full_content   = ""
    tool_calls     = []
    current_tc     = None
    printed_anything = False

    for line in resp.iter_lines():
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg = chunk.get("message", {})
        delta_content = msg.get("content", "")
        delta_tools   = msg.get("tool_calls", [])

        if delta_content:
            if not printed_anything:
                console.print()
                printed_anything = True
            console.print(delta_content, end="", markup=False)
            full_content += delta_content

        if delta_tools:
            tool_calls.extend(delta_tools)

        if chunk.get("done"):
            break

    if full_content:
        console.print()

    return {
        "message": {
            "role": "assistant",
            "content": full_content,
            "tool_calls": tool_calls,
        }
    }


def is_research_prompt(text: str) -> bool:
    return bool(re.match(r"^research\s+.+", text.strip(), re.IGNORECASE))


def extract_research_query(text: str) -> str:
    m = re.match(r"^research\s+(.+)", text.strip(), re.IGNORECASE)
    return m.group(1) if m else text


def run_agentic_loop(user_input: str, messages: list, config: dict) -> list:
    searxng_url = config.get("searxng_url", "http://192.168.1.140:8888")
    max_iters   = config.get("max_tool_iterations", 20)

    if is_research_prompt(user_input):
        query = extract_research_query(user_input)
        console.print(f"[cyan]Searching:[/cyan] {query}")
        results = web_search(query, searxng_url)
        user_input = f"Research: {query}\n\nSearch results:\n{results}\n\nPlease analyze and summarize these results."

    messages.append({"role": "user", "content": user_input})

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

        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls if tool_calls else None,
        })

        if not tool_calls:
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

            render_tool_call(name, args)
            result = dispatch_tool(name, args, config, confirm=config.get("confirm_bash", False))

            # Show brief result preview
            preview = result[:200] + "…" if len(result) > 200 else result
            console.print(f"[dim]  → {preview}[/dim]")

            tool_results.append({
                "role": "tool",
                "content": result,
                "name": name,
            })

        messages.extend(tool_results)

    return messages


def print_welcome(config: dict):
    model = config.get("model", "gemma4:26b")
    cwd   = os.getcwd()
    console.print(Panel(
        f"[bold white]CLAWCLI[/bold white]  [dim]powered by {model}[/dim]\n"
        f"[dim]cwd: {cwd}[/dim]\n"
        f"[dim]Type your task, 'research <topic>' to search, /help for commands, Ctrl+C to exit[/dim]",
        border_style="blue",
        padding=(0, 1),
    ))


def show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          — show this help\n"
        "  /memory        — show current memory\n"
        "  /clear         — clear conversation history\n"
        "  /config        — show current config\n"
        "  /cwd <path>    — change working directory\n"
        "  /model <name>  — switch Ollama model\n"
        "  /exit          — quit CLAWCLI\n\n"
        "[bold]Special prompts:[/bold]\n"
        "  research <topic>  — search SearXNG then summarize\n\n"
        "[bold]Key bindings:[/bold]\n"
        "  Enter       — submit (single line)\n"
        "  Ctrl+C      — cancel / exit\n"
        "  Up/Down     — history navigation",
        title="CLAWCLI Help",
        border_style="blue",
    ))


def handle_slash_command(cmd: str, config: dict, messages: list) -> tuple[bool, list]:
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
        if arg:
            config["model"] = arg
            console.print(f"[dim]Model switched to: {arg}[/dim]")
        else:
            console.print(f"[dim]Current model: {config.get('model')}[/dim]")
        return True, messages

    elif command in ("/exit", "/quit", "/q"):
        console.print("[dim]Goodbye.[/dim]")
        sys.exit(0)

    return False, messages


def main():
    parser = argparse.ArgumentParser(
        prog="clawcli",
        description="CLAWCLI — AI coding assistant powered by Ollama",
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt (non-interactive)")
    parser.add_argument("--model", "-m", help="Override Ollama model")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming output")
    parser.add_argument("--version", action="version", version="CLAWCLI 1.0.0")
    args = parser.parse_args()

    config = load_config()
    if args.model:
        config["model"] = args.model
    if args.no_stream:
        config["stream"] = False

    system_prompt = build_system_prompt(config)
    messages = [{"role": "system", "content": system_prompt}]

    # One-shot mode
    if args.prompt:
        user_input = " ".join(args.prompt)
        messages = run_agentic_loop(user_input, messages, config)
        return

    # Interactive REPL
    print_welcome(config)

    session = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        style=Style.from_dict({"prompt": "bold cyan"}),
        multiline=False,
    )

    while True:
        try:
            cwd_short = os.getcwd().replace(str(Path.home()), "~")
            user_input = session.prompt(f"\n[{cwd_short}] > ", default="")
        except KeyboardInterrupt:
            console.print("\n[dim]Use /exit to quit or Ctrl+C again to force exit.[/dim]")
            try:
                session.prompt("  > ")
            except KeyboardInterrupt:
                console.print("[dim]Goodbye.[/dim]")
                sys.exit(0)
            continue
        except EOFError:
            console.print("[dim]Goodbye.[/dim]")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input.startswith("/"):
            handled, messages = handle_slash_command(user_input, config, messages)
            if handled:
                continue

        messages = run_agentic_loop(user_input, messages, config)


if __name__ == "__main__":
    main()
