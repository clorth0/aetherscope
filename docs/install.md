# Install and run

## Requirements

- macOS (Apple Silicon tested) or Linux
- Homebrew on macOS
- A HackRF One plugged directly into the host (USB passthrough does not work in macOS Docker)

## macOS (one command)

```sh
git clone https://github.com/clorth0/aetherscope.git
cd aetherscope
./deploy/install.sh
```

The installer:

1. Installs the Homebrew SDR stack (`hackrf`, `librtlsdr`, `soapysdr`, `soapyhackrf`, `soapyrtlsdr`)
2. Rebuilds `rtl_433` from source if the bottle lacked SoapySDR (needed to drive the HackRF)
3. Builds `readsb-hackrf` from `wiedehopf/readsb` with `HACKRF=yes` into `~/.local/bin/`
4. Installs `uv` if missing
5. Runs `uv sync` to install the Python dependencies

## Run

Foreground (Ctrl-C to stop):

```sh
uv run aetherscope
```

Then open <http://127.0.0.1:8765/>.

## Run as a managed service (launchd, macOS)

Auto-start on login, restart on crash:

```sh
./deploy/install-launchd.sh
```

Useful commands:

```sh
launchctl print        gui/$(id -u)/local.aetherscope   # status, pid, last exit
launchctl kickstart -k gui/$(id -u)/local.aetherscope   # restart
launchctl kill SIGTERM gui/$(id -u)/local.aetherscope   # stop (auto-respawns)
launchctl bootout      gui/$(id -u)/local.aetherscope   # disable / unload
tail -f ~/Library/Logs/aetherscope/stderr.log
```

Override the service label with `AETHERSCOPE_LABEL` if you have a naming convention.

## Update after pulling new code

```sh
./deploy/restart.sh      # uv sync + restart the launchd service, then verify
```

## Linux

USB passthrough works on Linux, so a container is an option. See [deployment.md](deployment.md).
