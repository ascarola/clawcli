# CLAWCLI

A Claude Code-like AI assistant for the terminal, powered by a local Ollama model.

## Features

- Interactive REPL with conversation history
- Tool use: read/write/edit files, run bash commands, grep/glob filesystem
- Web research via SearXNG (`research <topic>`)
- Persistent memory across sessions
- Configurable allow/deny lists for bash commands
- One-shot mode for scripting
- Streaming output

## Requirements

- Python 3.10+
- Ollama server accessible on your network
- SearXNG instance (optional, for research prompts)

## Installation

Run the install script on any machine in your network:

```bash
bash <(curl -s http://YOUR_SERVER/install.sh)
```

Or clone and install manually:

```bash
git clone https://github.com/ascarola/clawcli.git ~/clawcli
cd ~/clawcli
pip3 install -r requirements.txt
chmod +x clawcli.py
ln -sf ~/clawcli/clawcli.py /usr/local/bin/clawcli
```

Edit `config.json` to point at your Ollama and SearXNG servers.

## Configuration

`config.json` — main settings:

| Key | Default | Description |
|-----|---------|-------------|
| `model` | `gemma4:26b` | Ollama model to use |
| `ollama_url` | `http://192.168.1.62:11434` | Ollama server URL |
| `searxng_url` | `http://192.168.1.140:8888` | SearXNG instance URL |
| `temperature` | `0.1` | Model temperature |
| `context_window` | `8192` | Token context window |
| `stream` | `true` | Enable streaming output |
| `confirm_bash` | `true` | Ask before unapproved bash commands |
| `max_tool_iterations` | `20` | Max agentic loop iterations |

`allowed_commands.txt` — bash command prefixes that run without confirmation (one per line).

`denied_commands.txt` — bash command patterns that are always blocked (one per line).

`memory/MEMORY.md` — persistent memory across sessions. The model reads this at startup and can write to it using the `save_memory` tool.

## Usage

```bash
# Interactive mode
clawcli

# One-shot mode
clawcli "explain this repo's structure"

# Research mode (auto-triggers SearXNG)
clawcli "research qwen2.5 model benchmarks"

# Override model
clawcli --model gemma4:e4b
```

## Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/memory` | Show current memory |
| `/clear` | Clear conversation history |
| `/config` | Show current config |
| `/cwd <path>` | Change working directory |
| `/model <name>` | Switch Ollama model |
| `/exit` | Quit |

## Directory Structure

```
~/clawcli/
├── clawcli.py              # Main entry point
├── config.json             # Configuration
├── system_prompt.txt       # System prompt template
├── allowed_commands.txt    # Pre-approved bash prefixes
├── denied_commands.txt     # Blocked bash patterns
├── memory/
│   └── MEMORY.md           # Persistent memory
├── tools/
│   ├── __init__.py         # Tool definitions (Ollama schema)
│   ├── file_tools.py       # File operations
│   ├── bash_tool.py        # Bash execution
│   └── search_tool.py      # SearXNG + web fetch
├── requirements.txt
├── install.sh
└── README.md
```

## Model

Default model is `gemma4:26b` — Google Gemma 4 at 25.8B parameters (Q4_K_M), the most capable model available on the configured Ollama server. Switch via `--model` or `/model`.
