# Security Policy

## Supported Versions

Only the current `main` branch receives security updates. There are no versioned release branches.

| Version | Supported |
|---------|-----------|
| latest (`main`) | ✅ |
| older commits | ❌ |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Use GitHub's private vulnerability reporting instead:
**[Report a vulnerability](https://github.com/ascarola/clawcli/security/advisories/new)**

Include:
- A description of the vulnerability and its potential impact
- Steps to reproduce
- Any suggested fix, if you have one

You can expect an acknowledgement within **3 days** and a status update within **7 days**. If the vulnerability is confirmed, a fix will be prioritized and you will be credited in the release notes (unless you prefer to remain anonymous).

## Scope

CLAWCLI is a locally-run personal AI assistant. Please keep the following in mind when assessing findings:

**In scope:**
- Vulnerabilities that allow the AI model to escape intended sandboxing (e.g., bypassing the bash allow/deny lists or file sensitivity checks to access paths the user did not intend to expose)
- SSRF or DNS rebinding issues in `web_fetch`
- Secrets leaking to the audit log, session files, or model context in ways the user would not expect
- Prompt injection via external content (web fetch results, file contents) that causes the model to take unintended destructive actions

**Out of scope:**
- Attacks that require an adversary to already have local filesystem access on the user's machine (if they have that, they don't need clawcli)
- The AI model producing harmful or incorrect output — this is a model behavior issue, not a clawcli security issue
- Vulnerabilities in Ollama, SearXNG, or mcp-kali-server themselves — report those to their respective projects
- The `shell=True` subprocess call in `bash_tool.py` — this is intentional and gated by allow/deny lists plus user confirmation; it is the core function of the bash tool
- Social engineering or prompt injection via user-supplied input (the user controls their own prompts)
