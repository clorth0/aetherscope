#!/usr/bin/env bash
#
# Aetherscope installer — bootstraps everything you need from a fresh clone.
#
# What it does:
#   1. Verifies Homebrew is present
#   2. Installs hackrf, librtlsdr, soapysdr, soapyhackrf, soapyrtlsdr
#   3. Rebuilds rtl_433 from source if the bottle is RTL-SDR-only (for HackRF support)
#   4. Builds readsb-hackrf from wiedehopf/readsb sources with HACKRF=yes
#   5. Installs uv (Astral Python package manager) if missing
#   6. Runs `uv sync` to install Python deps
#
# After this, start with: uv run aetherscope
# Or install as a launchd service: ./deploy/install-launchd.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

say() { printf "\033[1;36m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!!\033[0m %s\n" "$*" >&2; }
die() { printf "\033[1;31mERROR:\033[0m %s\n" "$*" >&2; exit 1; }

# --- macOS check ---------------------------------------------------
[[ "$(uname -s)" == "Darwin" ]] || die "This installer targets macOS. On Linux, install hackrf/rtl_433/readsb via your distro and run \`uv sync\`."

# --- Homebrew ------------------------------------------------------
command -v brew >/dev/null 2>&1 || die "Homebrew is required. Install from https://brew.sh and re-run."

say "Installing Homebrew SDR dependencies (hackrf, soapysdr, rtl_433, ...)"
brew install hackrf librtlsdr soapysdr soapyhackrf soapyrtlsdr

# rtl_433 bottle is compiled without SoapySDR — needed to talk to HackRF.
if ! rtl_433 -h 2>&1 | head -1 | grep -q SoapySDR; then
    say "rtl_433 bottle lacks SoapySDR — rebuilding from source"
    brew uninstall rtl_433 >/dev/null 2>&1 || true
    brew install --build-from-source rtl_433
fi

# --- readsb-hackrf -------------------------------------------------
if [[ ! -x "$HOME/.local/bin/readsb-hackrf" ]]; then
    say "Building readsb-hackrf from wiedehopf/readsb (HACKRF=yes)"
    TMP="$(mktemp -d)"
    trap "rm -rf '$TMP'" EXIT
    git clone --depth 1 https://github.com/wiedehopf/readsb "$TMP/readsb"
    pushd "$TMP/readsb" >/dev/null

    # macOS clang treats -Werror=unused-parameter as fatal in readsb's source.
    # Strip plain -Werror (keep -Werror=format-security which is fine).
    sed -i.bak -E 's/( -Werror)([^=])/\2/g; s/ -Werror$//' Makefile

    make HACKRF=yes -j"$(sysctl -n hw.ncpu)"
    mkdir -p "$HOME/.local/bin"
    cp readsb "$HOME/.local/bin/readsb-hackrf"
    popd >/dev/null
fi

# --- uv ------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/uv" ]]; then
    say "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Make sure uv is on PATH for the rest of this script
export PATH="$HOME/.local/bin:$PATH"

# --- Python deps ---------------------------------------------------
say "Installing Python dependencies"
uv sync

# --- Smoke test ----------------------------------------------------
say "Verifying tooling"
printf "  hackrf_info     : "; command -v hackrf_info       || die "missing"
printf "  hackrf_sweep    : "; command -v hackrf_sweep      || die "missing"
printf "  hackrf_transfer : "; command -v hackrf_transfer   || die "missing"
printf "  rtl_433         : "; command -v rtl_433           || die "missing"
printf "  readsb-hackrf   : "; command -v readsb-hackrf || ls "$HOME/.local/bin/readsb-hackrf"
printf "  uv              : "; command -v uv               || die "missing"

cat <<EOF

\033[1;32m==> Aetherscope installed.\033[0m

Start manually:
    cd $REPO_DIR && uv run aetherscope

Install as launchd service (auto-start, restart on crash):
    ./deploy/install-launchd.sh

Open in browser:
    http://127.0.0.1:8765/
EOF
