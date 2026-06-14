#!/usr/bin/env bash
#
# Render the launchd plist with current paths and install it as a
# LaunchAgent. The service starts on user login, restarts on crash,
# and writes logs to ~/Library/Logs/aetherscope/.
#
# Override the label with AETHERSCOPE_LABEL if you want.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_DIR/deploy/com.aetherscope.plist.template"

[[ -f "$TEMPLATE" ]] || { echo "ERROR: template not found at $TEMPLATE" >&2; exit 1; }
[[ "$(uname -s)" == "Darwin" ]] || { echo "ERROR: launchd is macOS-only." >&2; exit 1; }

LABEL="${AETHERSCOPE_LABEL:-local.aetherscope}"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

# Find uv — prefer one on PATH, fall back to ~/.local/bin
UV_PATH="$(command -v uv || true)"
[[ -z "$UV_PATH" && -x "$HOME/.local/bin/uv" ]] && UV_PATH="$HOME/.local/bin/uv"
[[ -n "$UV_PATH" ]] || { echo "ERROR: uv not found. Run ./deploy/install.sh first." >&2; exit 1; }

# Build a PATH the launchd-spawned process can use to find SDR binaries
SERVICE_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"

mkdir -p "$HOME/Library/Logs/aetherscope" "$HOME/Library/LaunchAgents"

# Render plist
sed \
    -e "s|@@LABEL@@|$LABEL|g" \
    -e "s|@@REPO_DIR@@|$REPO_DIR|g" \
    -e "s|@@UV_PATH@@|$UV_PATH|g" \
    -e "s|@@HOME@@|$HOME|g" \
    -e "s|@@PATH@@|$SERVICE_PATH|g" \
    "$TEMPLATE" > "$DEST"

echo "==> Rendered plist: $DEST"

# Tear down any previous instance (this label OR the legacy clorth0 one)
for old in "$LABEL" "local.hackrf-web" "com.clorth0.hackrf-web"; do
    if launchctl print "gui/$(id -u)/$old" >/dev/null 2>&1; then
        echo "==> Unloading existing service: $old"
        launchctl bootout "gui/$(id -u)/$old" 2>/dev/null || true
    fi
done

# Also stop any manually-running instance bound to port 8765
PIDS="$(lsof -ti :8765 2>/dev/null || true)"
if [[ -n "$PIDS" ]]; then
    echo "==> Killing existing process on :8765 (pids: $PIDS)"
    kill $PIDS 2>/dev/null || true
    sleep 1
fi

echo "==> Bootstrapping $LABEL"
launchctl bootstrap "gui/$(id -u)" "$DEST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

sleep 2

echo "==> Status:"
launchctl print "gui/$(id -u)/$LABEL" 2>&1 | grep -E "state|pid|last exit" | head -5

echo ""
echo "==> Verify:"
if curl -sf -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/ | grep -q 200; then
    echo "    http://127.0.0.1:8765/  -> 200 OK"
else
    echo "    http://127.0.0.1:8765/  -> NOT responding yet; check logs:"
    echo "    tail -f ~/Library/Logs/aetherscope/stderr.log"
fi

cat <<EOF

Useful commands:
    launchctl print     gui/\$(id -u)/$LABEL     # status, pid, last exit
    launchctl kickstart -k gui/\$(id -u)/$LABEL  # restart
    launchctl kill SIGTERM gui/\$(id -u)/$LABEL  # stop (auto-respawns)
    launchctl bootout   gui/\$(id -u)/$LABEL     # disable and unload
    tail -f ~/Library/Logs/aetherscope/stderr.log
EOF
