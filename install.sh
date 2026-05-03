#!/bin/bash
# CLAWCLI install script
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/ascarola/clawcli/main/install.sh)
#
# Non-interactive (env var overrides):
#   OLLAMA_URL=http://myserver:11434 OLLAMA_MODEL=llama3.2:3b bash install.sh
#
# Private repo (token auth):
#   GITHUB_TOKEN=github_pat_xxx bash install.sh
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
if [ -n "$GITHUB_TOKEN" ]; then
    REPO_URL="https://${REPO_OWNER}:${GITHUB_TOKEN}@github.com/${REPO_OWNER}/${REPO_NAME}.git"
else
    REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"
fi

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
        sudo apt-get install -y $MISSING
    fi
fi

# ── Clone or update ───────────────────────────────────────────────────────────
IS_UPDATE=0
if [ -d "$INSTALL_DIR/.git" ]; then
    IS_UPDATE=1
    echo "==> Updating existing installation at $INSTALL_DIR..."
    git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
    git -C "$INSTALL_DIR" pull --ff-only
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

    # Ollama URL
    if [ -z "$OLLAMA_URL" ]; then
        if [ "$IS_INTERACTIVE" -eq 1 ]; then
            OLLAMA_URL=$(ask "  Ollama server URL" "http://localhost:11434")
        else
            OLLAMA_URL="http://localhost:11434"
        fi
    fi

    # Ollama model
    if [ -z "$OLLAMA_MODEL" ]; then
        if [ "$IS_INTERACTIVE" -eq 1 ]; then
            echo ""
            OLLAMA_DEFAULT="gemma4:27b"
            FETCHED_MODELS=$(curl -sf --max-time 5 "$OLLAMA_URL/api/tags" 2>/dev/null \
                | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    models = data.get('models', [])
    if models:
        for m in models:
            print('   ', m['name'])
        # suggest first model as default
        print('__DEFAULT__', models[0]['name'])
except Exception:
    pass
" 2>/dev/null)
            if [ -n "$FETCHED_MODELS" ]; then
                FETCHED_DEFAULT=$(echo "$FETCHED_MODELS" | grep "^__DEFAULT__" | awk '{print $2}')
                [ -n "$FETCHED_DEFAULT" ] && OLLAMA_DEFAULT="$FETCHED_DEFAULT"
                echo "  Models available on $OLLAMA_URL:"
                echo "$FETCHED_MODELS" | grep -v "^__DEFAULT__"
            else
                echo "  Suggested models (you must have these pulled in Ollama):"
                echo "    gemma4:27b   — best quality, needs ~20GB VRAM"
                echo "    llama3.1:8b  — good balance, needs ~6GB VRAM"
                echo "    llama3.2:3b  — lightweight, runs on CPU"
            fi
            echo ""
            OLLAMA_MODEL=$(ask "  Ollama model" "$OLLAMA_DEFAULT")
        else
            OLLAMA_MODEL="gemma4:27b"
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
    echo "    Ollama URL:       $OLLAMA_URL"
    echo "    Model:            $OLLAMA_MODEL"
    echo "    SearXNG:          ${SEARXNG_URL:-not configured}"
    echo "    Assistant name:   $ASSISTANT_NAME"
    echo "    Your name:        ${USER_NAME:-not set}"
    echo ""

    "$VENV/bin/python3" - "$DEFAULTS" "$CONFIG" "$OLLAMA_URL" "$OLLAMA_MODEL" "$SEARXNG_URL" "$ASSISTANT_NAME" "$USER_NAME" <<'PYEOF'
import sys, json
defaults_path, out_path, ollama_url, model, searxng_url, assistant_name, user_name = sys.argv[1:]
with open(defaults_path) as f:
    cfg = json.load(f)
cfg["ollama_url"]      = ollama_url
cfg["model"]           = model
cfg["searxng_url"]     = searxng_url
cfg["assistant_name"]  = assistant_name
cfg["user_name"]       = user_name
with open(out_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print("    config.json created")
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

# ── Verify Ollama connectivity ────────────────────────────────────────────────
OLLAMA_CHECK_URL="${OLLAMA_URL:-$(python3 -c "import json; print(json.load(open('$CONFIG'))['ollama_url'])" 2>/dev/null || echo "http://localhost:11434")}"
echo ""
echo "==> Testing Ollama connectivity..."
if curl -sf "$OLLAMA_CHECK_URL/api/tags" >/dev/null 2>&1; then
    echo "    OK — Ollama reachable at $OLLAMA_CHECK_URL"
else
    echo "    WARNING: Cannot reach Ollama at $OLLAMA_CHECK_URL"
    echo "    Edit $CONFIG to update ollama_url, or run: clawcli doctor"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "==> CLAWCLI installed successfully!"
echo ""
echo "    Run:        clawcli"
echo "    Check:      clawcli doctor"
echo "    Help:       clawcli --help"
echo ""
