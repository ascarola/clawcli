"""Bash execution tool with allow/deny list enforcement."""

import os
import subprocess
import shlex
from pathlib import Path


def load_list(file_path: str) -> list[str]:
    p = Path(file_path).expanduser()
    if not p.exists():
        return []
    lines = p.read_text().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def is_denied(command: str, denied: list[str]) -> bool:
    cmd = command.strip()
    for pattern in denied:
        if cmd.startswith(pattern) or pattern in cmd:
            return True
    return False


def is_allowed(command: str, allowed: list[str]) -> bool:
    cmd = command.strip()
    for pattern in allowed:
        if cmd == pattern or cmd.startswith(pattern + " ") or cmd.startswith(pattern + "\n"):
            return True
    return False


def execute_bash(
    command: str,
    timeout: int = 60,
    description: str = None,
    config_dir: str = None,
    confirm_callback=None,
) -> str:
    base = Path(config_dir) if config_dir else Path(__file__).parent.parent
    allowed = load_list(base / "allowed_commands.txt")
    denied = load_list(base / "denied_commands.txt")

    if is_denied(command, denied):
        return f"Error: Command blocked by denied_commands.txt: {command[:80]}"

    if not is_allowed(command, allowed) and confirm_callback:
        desc = description or command[:80]
        approved = confirm_callback(command, desc)
        if not approved:
            return "Command denied by user."

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.getcwd(),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += result.stderr
        if result.returncode != 0 and not output:
            output = f"(exit code {result.returncode})"
        elif result.returncode != 0:
            output += f"\n(exit code {result.returncode})"
        return output.strip() if output.strip() else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"
    except Exception as e:
        return f"Error executing command: {e}"
