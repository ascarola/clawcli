"""Bash execution tool with allow/deny list enforcement."""

import os
import subprocess  # nosec B404
import shlex
import logging
from datetime import datetime, timezone
from pathlib import Path

_audit_logger = logging.getLogger("clawcli.audit")


def load_list(file_path: str) -> list[str]:
    p = Path(file_path).expanduser()
    if not p.exists():
        return []
    lines = p.read_text().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def is_denied(command: str, denied: list[str]) -> bool:
    cmd = command.strip()
    tokens = set(cmd.split())
    for pattern in denied:
        if cmd.startswith(pattern):
            return True
        # Multi-word patterns: substring match (e.g. "rm -rf /")
        # Single-word patterns: whole-token match to avoid false positives
        # on filenames containing the pattern (e.g. "reboot" in "reboot-required")
        if " " in pattern:
            if pattern in cmd:
                return True
        else:
            if pattern in tokens:
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
    timeout: int = 300,
    description: str = None,
    config_dir: str = None,
    confirm_callback=None,
) -> str:
    base = Path(config_dir) if config_dir else Path(__file__).parent.parent
    allowed = load_list(base / "allowed_commands.txt")
    denied = load_list(base / "denied_commands.txt")

    if not _audit_logger.handlers:
        handler = logging.FileHandler(base / "audit.log")
        handler.setFormatter(logging.Formatter("%(message)s"))
        _audit_logger.addHandler(handler)
        _audit_logger.setLevel(logging.INFO)

    timeout = min(timeout, 600)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if is_denied(command, denied):
        _audit_logger.info("%s BLOCKED %s", ts, command)
        return f"Error: Command blocked by denied_commands.txt: {command[:80]}"

    if not is_allowed(command, allowed) and confirm_callback:
        desc = description or command[:80]
        approved = confirm_callback(command, desc)
        if not approved:
            _audit_logger.info("%s DENIED_BY_USER %s", ts, command)
            return "Command denied by user."

    try:
        result = subprocess.run(  # nosec B602 — shell=True is intentional; commands are gated by allow/deny lists and optional user confirmation
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.getcwd(),
        )
        _audit_logger.info("%s EXIT=%d %s", ts, result.returncode, command)
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
        _audit_logger.info("%s TIMEOUT %s", ts, command)
        return f"Error: Command timed out after {timeout}s"
    except Exception as e:
        _audit_logger.info("%s ERROR %s | %s", ts, command, e)
        return f"Error executing command: {e}"
