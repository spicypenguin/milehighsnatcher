#!/usr/bin/env bash
# setup.sh — One-shot setup for MileHighSnatcher on macOS.
#
# What it does:
#   1. Creates and activates a Python virtual environment
#   2. Installs dependencies
#   3. Copies .env.example → .env (if .env doesn't exist yet)
#   4. Installs the LaunchAgent (runs monitor.py at 07:00 and 19:00 daily)
#
# Usage:
#   chmod +x setup.sh && ./setup.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
SCRIPT="$REPO_DIR/monitor.py"
PLIST_TEMPLATE="$REPO_DIR/com.milessnatcher.jal-monitor.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.milessnatcher.jal-monitor.plist"
LOG_FILE="$REPO_DIR/logs/monitor.log"
LABEL="com.milessnatcher.jal-monitor"

echo "=== MileHighSnatcher setup ==="
echo "Repo: $REPO_DIR"

# ── 1. Virtual environment ────────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "[1/4] Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
else
    echo "[1/4] Virtual environment already exists — skipping."
fi

echo "[2/4] Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

# ── 2. .env ───────────────────────────────────────────────────────────────────
if [ ! -f "$REPO_DIR/.env" ]; then
    echo "[3/4] Creating .env from template — edit it to add your API key."
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
else
    echo "[3/4] .env already exists — skipping."
fi

# ── 3. LaunchAgent ────────────────────────────────────────────────────────────
echo "[4/4] Installing LaunchAgent..."

mkdir -p "$REPO_DIR/logs"
mkdir -p "$HOME/Library/LaunchAgents"

PYTHON_PATH="$VENV_DIR/bin/python3"

# Substitute placeholders in the plist template.
sed \
    -e "s|PYTHON_PATH_PLACEHOLDER|$PYTHON_PATH|g" \
    -e "s|SCRIPT_PATH_PLACEHOLDER|$SCRIPT|g" \
    -e "s|LOG_PATH_PLACEHOLDER|$LOG_FILE|g" \
    "$PLIST_TEMPLATE" > "$PLIST_DEST"

# Unload first in case it was previously loaded.
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"

echo ""
echo "✓ Setup complete."
echo ""
echo "  Next steps:"
echo "  1. Edit .env and paste in your seats.aero API key:"
echo "       open '$REPO_DIR/.env'"
echo ""
echo "  2. Test the monitor manually:"
echo "       '$VENV_DIR/bin/python3' '$SCRIPT'"
echo ""
echo "  3. The LaunchAgent will run automatically at 07:00 and 19:00."
echo "     Logs: $LOG_FILE"
echo ""
echo "  To uninstall the LaunchAgent:"
echo "     launchctl unload '$PLIST_DEST' && rm '$PLIST_DEST'"
echo ""
echo "  To run on demand via launchctl:"
echo "     launchctl start $LABEL"
