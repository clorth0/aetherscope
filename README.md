# Aetherscope

Aetherscope is a self-hosted browser UI for a [HackRF One](https://greatscottgadgets.com/hackrf/) SDR. Live spectrum + waterfall, single-frequency ISM device decoding via rtl_433, IQ capture-to-disk, ADS-B aircraft tracking on a map, and a multi-phase Auto-Scan that does all of the above sequentially and produces a single report. Designed for a homelab security-RX workflow.

Binds to `127.0.0.1` only — intended to be reached over Tailscale or `ssh -L`.

## Modes

| Mode | What it does | Underlying tool |
|---|---|---|
| **Sweep** | Live FFT + scrolling waterfall, 1 Hz to 6 GHz | `hackrf_sweep` |
| **Decode** | Live ISM-band device decoding (315/433/868/915 MHz) | `rtl_433` w/ SoapySDR |
| **Capture** | Record IQ to disk + JSON sidecar | `hackrf_transfer` |
| **ADS-B** | Aircraft tracking with Leaflet dark-tile map | `readsb-hackrf` |
| **Radio** | Listen to audio in the browser, AM/FM toggle (FM broadcast 88-108 MHz, AM airband 118-137 MHz) | `hackrf_transfer` + numpy/scipy demod |
| **Auto-Scan** | Sequential pipeline: sweep → ISM 433 → ISM 915 → ADS-B → report | all of the above |

## Requirements

- macOS (Apple Silicon tested) or Linux
- Homebrew on macOS
- A HackRF One plugged in directly to the host (USB passthrough doesn't work on macOS Docker)

## Install on macOS (one command)

```sh
git clone https://github.com/clorth0/aetherscope.git
cd aetherscope
./deploy/install.sh
```

The installer:

1. Installs the Homebrew SDR stack (`hackrf`, `librtlsdr`, `soapysdr`, `soapyhackrf`, `soapyrtlsdr`)
2. Rebuilds `rtl_433` from source if the bottle was missing SoapySDR (needed to use the HackRF)
3. Builds `readsb-hackrf` from `wiedehopf/readsb` with `HACKRF=yes`, installs to `~/.local/bin/`
4. Installs `uv` if missing
5. Runs `uv sync` to install Python deps

## Running

**Manual** (foreground, kill with Ctrl-C):

```sh
uv run aetherscope
```

**As a managed launchd service** (auto-start on login, restart on crash):

```sh
./deploy/install-launchd.sh
```

Then open <http://127.0.0.1:8765/>.

Useful launchd commands (also printed by the installer):

```sh
launchctl print     gui/$(id -u)/local.aetherscope   # status, pid, last exit
launchctl kickstart -k gui/$(id -u)/local.aetherscope   # restart
launchctl kill SIGTERM gui/$(id -u)/local.aetherscope   # stop (auto-respawns)
launchctl bootout      gui/$(id -u)/local.aetherscope   # disable and unload
tail -f ~/Library/Logs/aetherscope/stderr.log
```

Override the service label with `AETHERSCOPE_LABEL=…` if you have a naming convention.

## Configuration

Environment variables, all optional:

- `AETHERSCOPE_CAPTURES_DIR` — where IQ recordings land (defaults to `<repo>/captures/`)

## Why not Docker?

macOS Docker Desktop cannot pass USB devices through to containers, and the HackRF needs direct USB access. On Linux, USB passthrough works and Docker is straightforward — a Dockerfile is on the roadmap.

## Architecture

```
┌──────────────┐   stdout/JSON/files   ┌──────────────┐   Socket.IO    ┌──────────────┐
│ hackrf_sweep │                       │ Flask + JS   │                │ Browser      │
│ hackrf_xfer  ├──────────────────────▶│ orchestration ├──────────────▶│ canvas + map │
│ rtl_433      │                       │ (one slot,   │                │ + event log  │
│ readsb       │                       │  mutex)      │                │              │
└──────────────┘                       └──────────────┘                └──────────────┘
```

All HackRF-claiming jobs (sweep, decode, capture, ADS-B, auto-scan) are mutually exclusive — the backend keeps a single "current job" slot. Background poller probes `hackrf_info` every 2.5 s so the UI shows live device status.

## Roadmap

- `rtl_433` decoder integration ✓
- IQ capture-to-disk ✓
- ADS-B (readsb-hackrf) with live aircraft map ✓
- Auto-scan: sweep + ISM 433 + ISM 915 + ADS-B sequential report ✓
- Per-band gain presets ✓
- launchd service ✓
- Tailscale-serve docs
- Basic auth (for when you want to expose beyond Tailscale)
- Linux Dockerfile + compose
