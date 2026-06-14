# Aetherscope

![Aetherscope spectrum and waterfall](docs/img/spectrum.png)

[![CI](https://github.com/clorth0/aetherscope/actions/workflows/ci.yml/badge.svg)](https://github.com/clorth0/aetherscope/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/clorth0/aetherscope)](https://github.com/clorth0/aetherscope/releases)
[![License: MIT](https://img.shields.io/github/license/clorth0/aetherscope)](LICENSE)

A self-hosted browser UI for a [HackRF One](https://greatscottgadgets.com/hackrf/)
SDR: a live spectrum analyzer, AM/FM/NBFM you can listen to (with snap-to-peak
tuning and saved bookmarks), ISM device decoding, ADS-B aircraft tracking with
registration and range rings, IQ captures you can replay/listen-to/decode, WAV
audio recording, optional GPS geotagging and SigMF export, a persistent device
inventory, and a one-click survey scan. Built for a homelab security-RX workflow.

Binds to `127.0.0.1` only, meant to be reached over Tailscale or `ssh -L`.

## Highlights

- **Spectrum analyzer:** live FFT + scrolling waterfall (1 Hz to 6 GHz), max-hold
  and average traces, a live peak table with SNR, click-drag zoom, hover and
  click-to-mark, and dB-offset calibration.
- **Radio:** listen in the browser (FM, narrowband FM, AM), one-click
  **snap-to-peak** to center a station, a scanner that stops on activity, and
  **WAV recording** of what you hear. Frequencies you care about become
  **bookmarks**.
- **Captures you can use:** record IQ, then **replay** it as a spectrogram,
  **listen** to it with an offset tuner, or **decode** it with rtl_433. Each
  capture also gets a **SigMF** sidecar for portability to other SDR tools.
- **Track and enrich:** ADS-B aircraft on a map with **US registration**,
  country, **range rings** around your receiver, and live stats; rtl_433 ISM
  devices.
- **Situational awareness:** a persistent **device inventory** ("what's around
  me") aggregating ADS-B + ISM contacts, and optional **GPS geotagging** of
  captures (opt-in, with per-capture redaction and precision coarsening).
- **Auto-Scan:** sequential survey (sweep, ISM, ADS-B) with a band-classified
  report.
- **Built to trust:** a diagnostics/telemetry panel, a strict CSP with vendored
  dependencies (works offline), same-origin websockets, input validation, and CI.

## Modes

| Mode | What it does | Tool |
|---|---|---|
| **Sweep** | FFT + waterfall, max-hold, peak table, zoom, marks | `hackrf_sweep` |
| **Radio** | AM / FM / NBFM audio, snap-to-peak, scanner, WAV recording, bookmarks | `hackrf_transfer` + numpy/scipy |
| **Capture** | Record IQ (+ JSON sidecar, SigMF, optional geotag); replay / listen / decode a saved capture | `hackrf_transfer` |
| **Decode** | ISM device decoding (315 / 433 / 868 / 915 MHz) | `rtl_433` + SoapySDR |
| **ADS-B** | Aircraft on a Leaflet map with registration, country, range rings, stats | `readsb-hackrf` |
| **Inventory** | Persistent catalog of seen ADS-B + ISM contacts | SQLite |
| **Auto-Scan** | Sweep, ISM, ADS-B, report | all of the above |

## Quickstart (macOS)

```sh
git clone https://github.com/clorth0/aetherscope.git
cd aetherscope
./deploy/install.sh
uv run aetherscope        # then open http://127.0.0.1:8765/
```

Set `AETHERSCOPE_PORT` to listen on a different port. Requirements, the launchd
service, and updating are in [docs/install.md](docs/install.md).

## Reach it remotely

Over Tailscale or an `ssh -L` tunnel it needs no extra setup. To expose it, put
TLS + auth in front (Caddy recipe) or run the Linux container. See
[docs/deployment.md](docs/deployment.md).

## Docs

- [Install and run](docs/install.md)
- [Configuration](docs/configuration.md)
- [Deployment (Caddy, Docker)](docs/deployment.md)
- [Architecture](docs/architecture.md)
- [Security policy](SECURITY.md)

## Security

Localhost-only by design, with no built-in authentication; websockets are
restricted to the same origin. Do not put it on a public interface without your
own access controls (TLS + auth in front). GPS geotagging is **opt-in and off by
default** — when enabled, captures are stamped with your location, with
per-capture redaction and precision coarsening. Report issues privately via the
repository's Security tab. See [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
