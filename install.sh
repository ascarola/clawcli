#!/bin/bash
# CLAWCLI install script — run on any machine to install clawcli
set -e

REPO_OWNER="ascarola"
REPO_NAME="clawcli"
INSTALL_DIR="${CLAWCLI_DIR:-$HOME/clawcli}"
BIN_LINK="/usr/local/bin/clawcli"

# Configurable endpoints (override via env vars)
OLLAMA_URL="${OLLAMA_URL:-http://192.168.1.62:11434}"
SEARXNG_URL="${SEARXNG_URL:-http://192.168.1.140:8888}"

# ── GitHub token (required for private repo) ──────────────────────────────────
if [ -z "$GITHUB_TOKEN" ]; then
    echo "ERROR: GITHUB_TOKEN is not set."
    echo "  export GITHUB_TOKEN=github_pat_xxx"
    echo "  Then re-run: bash <(curl -s -H \"Authorization: Bearer \$GITHUB_TOKEN\" https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/install.sh)"
    exit 1
fi

REPO_URL="https://${REPO_OWNER}:${GITHUB_TOKEN}@github.com/${REPO_OWNER}/${REPO_NAME}.git"

echo "==> Installing CLAWCLI"
echo "    Ollama:  $OLLAMA_URL"
echo "    SearXNG: $SEARXNG_URL"
echo "    Target:  $INSTALL_DIR"
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found. Install it and retry."; exit 1; }
command -v pip3    >/dev/null 2>&1 || { echo "ERROR: pip3 not found. Install python3-pip and retry."; exit 1; }
command -v git     >/dev/null 2>&1 || { echo "ERROR: git not found. Install it and retry."; exit 1; }

# ── Clone or update ───────────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "==> Updating existing installation..."
    git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
    git -C "$INSTALL_DIR" pull --ff-only
else
    if [ -d "$INSTALL_DIR" ]; then
        echo "WARNING: $INSTALL_DIR exists but is not a git repo. Remove it and re-run, or set CLAWCLI_DIR to a different path."
        exit 1
    fi
    echo "==> Cloning repository..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ── Python dependencies ───────────────────────────────────────────────────────
echo "==> Installing Python dependencies..."
pip3 install -q -r "$INSTALL_DIR/requirements.txt"

# ── Generate machine-local config.json from defaults template ─────────────────
DEFAULTS="$INSTALL_DIR/config.defaults.json"
CONFIG="$INSTALL_DIR/config.json"
if [ ! -f "$CONFIG" ]; then
    python3 - "$DEFAULTS" "$CONFIG" "$OLLAMA_URL" "$SEARXNG_URL" <<'PYEOF'
import sys, json
defaults_path, out_path, ollama_url, searxng_url = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(defaults_path) as f:
    cfg = json.load(f)
cfg["ollama_url"]  = ollama_url
cfg["searxng_url"] = searxng_url
with open(out_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print("    config.json created from defaults")
PYEOF
else
    echo "==> config.json already exists, skipping (edit manually or delete to regenerate)"
fi

# ── Ensure memory file exists ─────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR/memory"
if [ ! -f "$INSTALL_DIR/memory/MEMORY.md" ]; then
    cat > "$INSTALL_DIR/memory/MEMORY.md" <<'EOF'
# CLAWCLI Memory

## User Preferences

## Project Context

## Important Facts
EOF
fi

# ── Symlink to PATH ───────────────────────────────────────────────────────────
chmod +x "$INSTALL_DIR/clawcli.py"

LINK_OK=0
if [ -w "$(dirname "$BIN_LINK")" ] || sudo -n true 2>/dev/null; then
    sudo ln -sf "$INSTALL_DIR/clawcli.py" "$BIN_LINK" && LINK_OK=1
fi

if [ "$LINK_OK" -eq 0 ]; then
    LOCAL_BIN="$HOME/.local/bin"
    mkdir -p "$LOCAL_BIN"
    ln -sf "$INSTALL_DIR/clawcli.py" "$LOCAL_BIN/clawcli"
    echo "==> Installed to $LOCAL_BIN/clawcli"
    echo "    Make sure $LOCAL_BIN is in your PATH:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
else
    echo "==> Installed to $BIN_LINK"
fi

# ── Verify ────────────────────────────────────────────────────────────────────
echo ""
echo "==> Testing Ollama connectivity..."
if curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    echo "    OK — Ollama reachable at $OLLAMA_URL"
else
    echo "    WARNING: Cannot reach Ollama at $OLLAMA_URL"
    echo "    Edit $CONFIG to update ollama_url after installation."
fi

echo ""
echo "==> CLAWCLI installed successfully!"
echo "    Run: clawcli"
echo "    Help: clawcli --help"
