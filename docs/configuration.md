# Configuration

All environment variables are optional.

| Variable | Purpose | Default |
|---|---|---|
| `AETHERSCOPE_CAPTURES_DIR` | Where IQ recordings land | `<repo>/captures/` |
| `AETHERSCOPE_DATA_DIR` | SQLite data dir (bookmarks, settings, inventory) | `~/.local/share/aetherscope/` |
| `AETHERSCOPE_HOST` | Bind address (containers set `0.0.0.0`) | `127.0.0.1` |
| `AETHERSCOPE_PORT` | HTTP port to listen on (invalid/out-of-range falls back to the default) | `8765` |
| `AETHERSCOPE_ALLOWED_ORIGINS` | Comma-separated extra origins allowed to connect; set to your proxy domain when behind a reverse proxy | same-origin only |
| `AETHERSCOPE_SECRET_KEY` | Flask session signing key | random per process |
| `AETHERSCOPE_LABEL` | launchd service label | `local.aetherscope` |
| `AETHERSCOPE_GPS` | Set to `0` to hard-disable GPS geotagging regardless of the in-app toggle | toggle in app |
| `AETHERSCOPE_GPSD_HOST` / `AETHERSCOPE_GPSD_PORT` | gpsd location for geotagging | `127.0.0.1` / `2947` |

For the launchd service, set these in the plist `EnvironmentVariables` block (or export them before `uv run aetherscope` when running manually).
