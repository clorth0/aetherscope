#!/usr/bin/env bash
#
# Update and restart the Aetherscope launchd service.
#
# Run this after `git pull`: it syncs Python deps and restarts the service
# so it picks up the new code. If the service isn't installed yet, it hands
# off to install-launchd.sh.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

LABEL="${AETHERSCOPE_LABEL:-local.aetherscope}"
export PATH="$HOME/.local/bin:$PATH"

say() { printf "\033[1;36m==>\033[0m %s\n" "$*"; }

say "Syncing Python dependencies"
uv sync

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    say "Restarting $LABEL"
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
else
    say "Service $LABEL not installed yet; installing"
    exec ./deploy/install-launchd.sh
fi

sleep 2
PORT="${AETHERSCOPE_PORT:-8765}"
if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/"; then
    say "http://127.0.0.1:${PORT}/ -> OK (running latest code)"
else
    printf "\033[1;31mERROR:\033[0m not responding; check logs:\n" >&2
    printf "    tail -f ~/Library/Logs/aetherscope/stderr.log\n" >&2
    exit 1
fi
