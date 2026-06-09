# hackrf-web

Self-hosted browser UI for a [HackRF One](https://greatscottgadgets.com/hackrf/) SDR. Live spectrum sweep + scrolling waterfall, designed to be the starting point for a homelab security-RX workflow.

Runs on `127.0.0.1` only — intended to be reached over Tailscale or `ssh -L`.

## Status

MVP. Live wideband sweep + waterfall + tuning + gain controls. Decoders (`rtl_433`, `dump1090`, replay tooling) planned.

## Requirements

- macOS (Apple Silicon tested) or Linux
- Homebrew packages: `hackrf` (sweep/transfer/info), `rtl_433` for Decode mode (rebuild from source with SoapySDR — see below)
- `readsb-hackrf` for ADS-B mode — built from source with `HACKRF=yes` (see below)
- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pip`

### Decoder/ADS-B prerequisites (one-time)

```sh
# rtl_433 must be compiled against SoapySDR (Homebrew bottle is RTL-SDR only)
brew install --build-from-source rtl_433

# readsb-hackrf for ADS-B
git clone --depth 1 https://github.com/wiedehopf/readsb /tmp/readsb-build
cd /tmp/readsb-build
sed -i.bak -E 's/( -Werror)([^=])/\2/g; s/ -Werror$//' Makefile  # strict warnings on macOS
make HACKRF=yes -j8
mkdir -p ~/.local/bin && cp readsb ~/.local/bin/readsb-hackrf
```

## Install

```sh
brew install hackrf
git clone https://github.com/clorth0/hackrf-web.git
cd hackrf-web
uv sync
```

## Run

```sh
uv run hackrf-web
```

Then open <http://127.0.0.1:8765/>.

## Architecture

```
┌──────────────┐  CSV stdout   ┌──────────────┐  WebSocket   ┌──────────────┐
│ hackrf_sweep ├──────────────▶│ backend/sdr  ├─────────────▶│ canvas UI    │
└──────────────┘               │  (Flask app) │              │  (vanilla JS)│
                               └──────────────┘              └──────────────┘
```

- `backend/sdr.py` spawns `hackrf_sweep`, parses chunked CSV output, accumulates one row per full sweep cycle.
- `backend/app.py` exposes a Socket.IO server. Clients send `start`/`stop` with a `SweepConfig`; server streams `sweep` events.
- `frontend/static/waterfall.js` renders the FFT line plot and scrolling waterfall on `<canvas>`.

## Running as a service (macOS launchd)

`hackrf-web` ships with a launchd plist that keeps it running, restarts
it on crash, and starts it automatically at login. Install:

```sh
cp deploy/com.clorth0.hackrf-web.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.clorth0.hackrf-web.plist
launchctl kickstart -k gui/$(id -u)/com.clorth0.hackrf-web
```

Edit the absolute paths in the plist if you cloned the repo somewhere
other than `~/hackrf-web`.

Useful commands:

```sh
launchctl print     gui/$(id -u)/com.clorth0.hackrf-web   # status, last exit, pid
launchctl kickstart -k gui/$(id -u)/com.clorth0.hackrf-web   # restart
launchctl kill SIGTERM gui/$(id -u)/com.clorth0.hackrf-web   # stop (launchd will respawn)
launchctl bootout      gui/$(id -u)/com.clorth0.hackrf-web   # disable and unload
tail -f ~/Library/Logs/hackrf-web/stderr.log               # follow logs
```

Not Dockerized intentionally — macOS Docker Desktop cannot pass USB
devices to containers, so the HackRF can only be reached by host-native
processes. On Linux, a Dockerfile would be straightforward and is on
the roadmap.

## Roadmap

- `rtl_433` decoder integration for ISM-band IoT chatter
- `dump1090` integration for ADS-B
- IQ capture-to-disk endpoint
- Per-band presets (FM broadcast / aircraft / NOAA / ham 2m / ISM)
- Auth (basic, behind Tailscale fine for now)
