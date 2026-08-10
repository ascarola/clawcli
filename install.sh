#!/bin/bash
# CLAWCLI install script
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/ascarola/clawcli/main/install.sh)
#
# Non-interactive (env var overrides):
#   OLLAMA_URL=http://myserver:11434 OLLAMA_MODEL=llama3.2:3b bash install.sh
#
# Against an OpenAI-compatible gateway instead of a bare Ollama host:
#   OLLAMA_URL=http://gateway:5010/v1 API_KEY=sk-... bash install.sh
set -e

REPO_OWNER="${REPO_OWNER:-ascarola}"
REPO_NAME="${REPO_NAME:-clawcli}"
INSTALL_DIR="${CLAWCLI_DIR:-$HOME/clawcli}"
BIN_LINK="/usr/local/bin/clawcli"

# ── Detect if running interactively ──────────────────────────────────────────
IS_INTERACTIVE=0
[ -t 0 ] && IS_INTERACTIVE=1

# ── Helper ────────────────────────────────────────────────────────────────────
ask() {
    local prompt="$1"
    local default="$2"
    local result
    if [ -n "$default" ]; then
        printf "%s [%s]: " "$prompt" "$default" >&2
    else
        printf "%s: " "$prompt" >&2
    fi
    read -r result
    echo "${result:-$default}"
}

ask_yn() {
    local prompt="$1"
    local default="${2:-y}"
    local result
    if [ "$default" = "y" ]; then
        printf "%s [Y/n]: " "$prompt"
    else
        printf "%s [y/N]: " "$prompt"
    fi
    read -r result
    result="${result:-$default}"
    case "$result" in
        [Yy]*) return 0 ;;
        *)     return 1 ;;
    esac
}

# ── Build repo URL ─────────────────────────────────────────────────────────────
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo "🦞 CLAWCLI Installer"
echo "──────────────────────────────────────"
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found. Install it and retry."; exit 1; }
command -v git     >/dev/null 2>&1 || { echo "ERROR: git not found. Install it and retry."; exit 1; }

# On Debian/Ubuntu, proactively ensure venv and pip packages are present
if command -v apt-get >/dev/null 2>&1; then
    MISSING=""
    python3 -m venv --help >/dev/null 2>&1 || MISSING="$MISSING python3-venv"
    python3 -m pip --version >/dev/null 2>&1  || MISSING="$MISSING python3-pip"
    if [ -n "$MISSING" ]; then
        echo "==> Installing missing system packages:$MISSING"
        sudo apt-get install -y $MISSING  # shellcheck disable=SC2086 — intentional word-split
    fi
fi

# ── Clone or update ───────────────────────────────────────────────────────────
IS_UPDATE=0
if [ -d "$INSTALL_DIR/.git" ]; then
    IS_UPDATE=1
    echo "==> Updating existing installation at $INSTALL_DIR..."
    git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
    if ! git -C "$INSTALL_DIR" pull --ff-only; then
        echo "==> Fast-forward failed (remote history may have changed); resetting to origin/main..."
        git -C "$INSTALL_DIR" fetch origin
        git -C "$INSTALL_DIR" reset --hard origin/main
    fi
else
    if [ -d "$INSTALL_DIR" ]; then
        echo "WARNING: $INSTALL_DIR exists but is not a git repo."
        echo "  Remove it and re-run, or set CLAWCLI_DIR to a different path."
        exit 1
    fi
    echo "==> Cloning repository to $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ── Virtualenv + Python dependencies ─────────────────────────────────────────
VENV="$INSTALL_DIR/.venv"
if [ ! -d "$VENV" ]; then
    echo "==> Creating virtualenv..."
    if ! python3 -m venv "$VENV" 2>/dev/null; then
        if command -v apt-get >/dev/null 2>&1; then
            sudo apt-get install -y python3-venv python3-pip
        elif command -v dnf >/dev/null 2>&1; then
            sudo dnf install -y python3-pip
        elif command -v pacman >/dev/null 2>&1; then
            sudo pacman -Sy --noconfirm python-pip
        fi
        python3 -m venv "$VENV"
    fi
fi

echo "==> Installing Python dependencies..."
"$VENV/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# ── Config — only prompt/generate on fresh install ────────────────────────────
DEFAULTS="$INSTALL_DIR/config.defaults.json"
CONFIG="$INSTALL_DIR/config.json"

if [ "$IS_UPDATE" -eq 1 ] || [ -f "$CONFIG" ]; then
    echo "==> config.json already exists — skipping (edit manually to change settings)"
else
    echo ""
    echo "==> Configuration"
    echo "    (Press Enter to accept the default shown in brackets)"
    echo ""

    # LLM endpoint — a bare Ollama host, or an OpenAI-compatible gateway
    if [ -z "$OLLAMA_URL" ]; then
        if [ "$IS_INTERACTIVE" -eq 1 ]; then
            echo "  CLAWCLI talks to either an Ollama host directly, or an"
            echo "  OpenAI-compatible gateway (LiteLLM, vLLM, a custom gateway…)."
            echo "  For a gateway, give the full base URL including /v1."
            OLLAMA_URL=$(ask "  LLM endpoint URL" "http://localhost:11434")
        else
            OLLAMA_URL="http://localhost:11434"
        fi
    fi

    # API key — required by gateways, never by a bare Ollama host.
    # Detect the gateway case from the /v1 suffix so we only nag when it matters.
    case "$OLLAMA_URL" in
        */v1|*/v1/) IS_GATEWAY=1 ;;
        *)          IS_GATEWAY=0 ;;
    esac
    if [ -z "$API_KEY" ] && [ "$IS_INTERACTIVE" -eq 1 ]; then
        echo ""
        if [ "$IS_GATEWAY" -eq 1 ]; then
            echo "  That looks like an OpenAI-compatible gateway, which normally needs a key."
            API_KEY=$(ask "  API key" "")
        else
            echo "  A bare Ollama host needs no API key — leave blank unless yours is"
            echo "  behind an authenticating proxy."
            API_KEY=$(ask "  API key (optional)" "")
        fi
    fi

    # Model list — endpoint-shaped, and authenticated when a key was given
    if [ -z "$OLLAMA_MODEL" ]; then
        if [ "$IS_INTERACTIVE" -eq 1 ]; then
            echo ""
            OLLAMA_DEFAULT="gemma4:26b"
            if [ "$IS_GATEWAY" -eq 1 ]; then
                MODELS_URL="$OLLAMA_URL/models"
            else
                MODELS_URL="$OLLAMA_URL/api/tags"
            fi
            if [ -n "$API_KEY" ]; then
                RAW_MODELS=$(curl -sf --max-time 5 -H "Authorization: Bearer $API_KEY" "$MODELS_URL" 2>/dev/null)
            else
                RAW_MODELS=$(curl -sf --max-time 5 "$MODELS_URL" 2>/dev/null)
            fi
            FETCHED_MODELS=$(printf '%s' "$RAW_MODELS" \
                | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    # Ollama: {'models': [{'name': ...}]}  |  OpenAI: {'data': [{'id': ...}]}
    models = [m['name'] for m in data.get('models', [])] or \
             [m['id'] for m in data.get('data', [])]
    if models:
        for name in models:
            print('   ', name)
        # suggest first model as default
        print('__DEFAULT__', models[0])
except Exception:
    pass
" 2>/dev/null)
            if [ -n "$FETCHED_MODELS" ]; then
                FETCHED_DEFAULT=$(echo "$FETCHED_MODELS" | grep "^__DEFAULT__" | awk '{print $2}')
                [ -n "$FETCHED_DEFAULT" ] && OLLAMA_DEFAULT="$FETCHED_DEFAULT"
                echo "  Models available on $OLLAMA_URL:"
                echo "$FETCHED_MODELS" | grep -v "^__DEFAULT__"
            elif [ "$IS_GATEWAY" -eq 1 ]; then
                echo "  Could not list models from $MODELS_URL"
                echo "  (check the URL and API key — you can fix this later with /key)"
            else
                echo "  Suggested models (you must have these pulled in Ollama):"
                echo "    gemma4:26b   — best quality, needs ~20GB VRAM"
                echo "    llama3.1:8b  — good balance, needs ~6GB VRAM"
                echo "    llama3.2:3b  — lightweight, runs on CPU"
            fi
            echo ""
            OLLAMA_MODEL=$(ask "  Ollama model" "$OLLAMA_DEFAULT")
        else
            OLLAMA_MODEL="gemma4:26b"
        fi
    fi

    # Vision model (optional)
    if [ -z "$VISION_MODEL" ]; then
        VISION_MODEL=""
        if [ "$IS_INTERACTIVE" -eq 1 ]; then
            echo ""
            echo "  Vision model (optional) — used for read_image, OCR, and scanned PDF reading."
            echo "  If your active model supports vision (e.g. gemma4, llava, minicpm-v), leave blank."
            VISION_MODEL=$(ask "  Vision model (blank to use active model)" "")
        fi
    fi

    # SearXNG (optional)
    if [ -z "$SEARXNG_URL" ]; then
        SEARXNG_URL=""
        if [ "$IS_INTERACTIVE" -eq 1 ]; then
            echo ""
            if ask_yn "  Do you have a SearXNG instance? (enables web research)" "n"; then
                SEARXNG_URL=$(ask "  SearXNG URL" "http://localhost:8888")
            fi
        fi
    fi

    # Kali security scanning (optional)
    if [ -z "$KALI_SERVER_URL" ]; then
        KALI_SERVER_URL=""
        if [ "$IS_INTERACTIVE" -eq 1 ]; then
            echo ""
            if ask_yn "  Do you have an mcp-kali-server instance? (enables security scanning)" "n"; then
                KALI_SERVER_URL=$(ask "  mcp-kali-server URL" "http://10.0.0.5:5050")
            fi
        fi
    fi

    # MCP server (optional)
    if [ -z "$MCP_SERVER_URL" ]; then
        MCP_SERVER_URL=""
        MCP_BEARER_TOKEN=""
        if [ "$IS_INTERACTIVE" -eq 1 ]; then
            echo ""
            if ask_yn "  Do you have an MCP server? (connects external tools via Model Context Protocol)" "n"; then
                MCP_SERVER_URL=$(ask "  MCP server URL" "http://localhost:8000/mcp")
                MCP_BEARER_TOKEN=$(ask "  MCP bearer token (leave blank if not required)" "")
            fi
        fi
    fi

    # Personalization (optional)
    if [ -z "$ASSISTANT_NAME" ]; then
        ASSISTANT_NAME="CLAWCLI"
        if [ "$IS_INTERACTIVE" -eq 1 ]; then
            echo ""
            ASSISTANT_NAME=$(ask "  What would you like to name your assistant?" "CLAWCLI")
        fi
    fi
    if [ -z "$USER_NAME" ]; then
        USER_NAME=""
        if [ "$IS_INTERACTIVE" -eq 1 ]; then
            USER_NAME=$(ask "  How should the assistant address you? (leave blank to skip)" "")
        fi
    fi

    echo ""
    echo "  Settings:"
    echo "    LLM endpoint:     $OLLAMA_URL"
    if [ "$IS_GATEWAY" -eq 1 ]; then
        echo "    API format:       openai (gateway)"
    else
        echo "    API format:       ollama (native)"
    fi
    echo "    API key:          ${API_KEY:+set}${API_KEY:-not set}"
    echo "    Model:            $OLLAMA_MODEL"
    echo "    Vision model:     ${VISION_MODEL:-same as active model}"
    echo "    SearXNG:          ${SEARXNG_URL:-not configured}"
    echo "    Kali server:      ${KALI_SERVER_URL:-not configured}"
    echo "    MCP server:       ${MCP_SERVER_URL:-not configured}"
    echo "    MCP token:        ${MCP_BEARER_TOKEN:+set}${MCP_BEARER_TOKEN:-not set}"
    echo "    Assistant name:   $ASSISTANT_NAME"
    echo "    Your name:        ${USER_NAME:-not set}"
    echo ""

    "$VENV/bin/python3" - "$DEFAULTS" "$CONFIG" "$OLLAMA_URL" "$OLLAMA_MODEL" "$VISION_MODEL" "$SEARXNG_URL" "$KALI_SERVER_URL" "$MCP_SERVER_URL" "$MCP_BEARER_TOKEN" "$ASSISTANT_NAME" "$USER_NAME" "$API_KEY" <<'PYEOF'
import sys, json, os
defaults_path, out_path, ollama_url, model, vision_model, searxng_url, kali_server_url, mcp_server_url, mcp_bearer_token, assistant_name, user_name, api_key = sys.argv[1:]
with open(defaults_path) as f:
    cfg = json.load(f)
cfg["ollama_url"]        = ollama_url
cfg["api_key"]           = api_key
# api_format stays "auto": it infers openai from a /v1 URL and ollama otherwise,
# so an existing Ollama setup keeps its exact previous behaviour.
cfg["api_format"]        = "auto"
cfg["model"]             = model
cfg["vision_model"]      = vision_model
cfg["searxng_url"]       = searxng_url
cfg["kali_server_url"]   = kali_server_url
cfg["mcp_server_url"]    = mcp_server_url
cfg["mcp_bearer_token"]  = mcp_bearer_token
cfg["assistant_name"]    = assistant_name
cfg["user_name"]         = user_name
with open(out_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
# config.json can hold bearer tokens — keep it owner-readable only.
os.chmod(out_path, 0o600)
print("    config.json created (mode 0600)")
PYEOF
fi

# Existing installs predate api_key/api_format and predate the 0600 tightening.
if [ -f "$CONFIG" ]; then
    "$VENV/bin/python3" - "$CONFIG" "$DEFAULTS" <<'PYEOF'
import sys, json, os
cfg_path, defaults_path = sys.argv[1:]
try:
    with open(cfg_path) as f:
        cfg = json.load(f)
except Exception:
    raise SystemExit(0)  # malformed config — leave it alone, doctor will flag it
with open(defaults_path) as f:
    defaults = json.load(f)
added = [k for k in ("api_base", "api_key", "api_format") if k not in cfg]
for k in added:
    cfg[k] = defaults.get(k, "")
if added:
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"    config.json: added {', '.join(added)} (defaults preserve existing behaviour)")
if (os.stat(cfg_path).st_mode & 0o077) and (cfg.get("api_key") or cfg.get("mcp_bearer_token")):
    os.chmod(cfg_path, 0o600)
    print("    config.json: tightened permissions to 0600 (contains a token)")
PYEOF
fi

# ── Ensure memory file exists ─────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR/memory"
MEMORY_FILE="$INSTALL_DIR/memory/MEMORY.md"
if [ ! -f "$MEMORY_FILE" ]; then
    cat > "$MEMORY_FILE" <<'EOF'
# CLAWCLI Memory

## User Preferences

## Project Context

## Important Facts
EOF
fi

# Write user name into memory if provided
if [ -n "$USER_NAME" ]; then
    if ! grep -q "User's name:" "$MEMORY_FILE" 2>/dev/null; then
        printf "\n## About the User\n- User's name: %s\n" "$USER_NAME" >> "$MEMORY_FILE"
    fi
fi

# ── Write launcher wrapper ────────────────────────────────────────────────────
LAUNCHER="$INSTALL_DIR/clawcli"
cat > "$LAUNCHER" <<WRAPPER
#!/bin/bash
exec "$VENV/bin/python3" "$INSTALL_DIR/clawcli.py" "\$@"
WRAPPER
chmod +x "$LAUNCHER"

# ── Symlink launcher to PATH ──────────────────────────────────────────────────
if [ "$(uname -s)" = "Darwin" ] && [ -d "/opt/homebrew/bin" ]; then
    BIN_LINK="/opt/homebrew/bin/clawcli"
fi

LINK_OK=0
if [ -w "$(dirname "$BIN_LINK")" ]; then
    ln -sf "$LAUNCHER" "$BIN_LINK" && LINK_OK=1
elif sudo -n true 2>/dev/null; then
    sudo ln -sf "$LAUNCHER" "$BIN_LINK" && LINK_OK=1
fi

if [ "$LINK_OK" -eq 0 ]; then
    LOCAL_BIN="$HOME/.local/bin"
    mkdir -p "$LOCAL_BIN"
    ln -sf "$LAUNCHER" "$LOCAL_BIN/clawcli"
    echo "==> Installed to $LOCAL_BIN/clawcli"
    echo "    Make sure $LOCAL_BIN is in your PATH:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
else
    echo "==> Installed to $BIN_LINK"
fi

# ── Verify LLM endpoint connectivity ──────────────────────────────────────────
# Read back whatever ended up in config.json so this also covers upgrades.
CHECK=$(python3 -c "
import json
try:
    cfg = json.load(open('$CONFIG'))
except Exception:
    cfg = {}
base = (cfg.get('api_base') or cfg.get('ollama_url') or 'http://localhost:11434').rstrip('/')
key  = cfg.get('api_key') or ''
tail = base.rsplit('/', 1)[-1].lower()
fmt  = cfg.get('api_format') or 'auto'
if fmt == 'auto':
    fmt = 'openai' if tail.startswith('v') and tail[1:].isdigit() else 'ollama'
print(base)
print('/models' if fmt == 'openai' else '/api/tags')
print(key)
" 2>/dev/null)
CHECK_URL=$(echo "$CHECK" | sed -n 1p)
CHECK_PATH=$(echo "$CHECK" | sed -n 2p)
CHECK_KEY=$(echo "$CHECK" | sed -n 3p)
[ -n "$API_KEY" ] && CHECK_KEY="$API_KEY"
[ -z "$CHECK_URL" ] && CHECK_URL="${OLLAMA_URL:-http://localhost:11434}"
[ -z "$CHECK_PATH" ] && CHECK_PATH="/api/tags"

echo ""
echo "==> Testing LLM endpoint connectivity..."
if [ -n "$CHECK_KEY" ]; then
    CHECK_OK=$(curl -sf --max-time 10 -H "Authorization: Bearer $CHECK_KEY" "$CHECK_URL$CHECK_PATH" >/dev/null 2>&1 && echo yes || echo no)
else
    CHECK_OK=$(curl -sf --max-time 10 "$CHECK_URL$CHECK_PATH" >/dev/null 2>&1 && echo yes || echo no)
fi
if [ "$CHECK_OK" = "yes" ]; then
    echo "    OK — reachable at $CHECK_URL"
else
    echo "    WARNING: Cannot reach $CHECK_URL$CHECK_PATH"
    echo "    Edit $CONFIG to update ollama_url/api_base/api_key, or run: clawcli doctor"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "==> CLAWCLI installed successfully!"
echo ""
echo "    Run:        clawcli"
echo "    Check:      clawcli doctor"
echo "    Help:       clawcli --help"
echo ""
