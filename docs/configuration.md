# Configuration

All environment variables are optional.

| Variable | Purpose | Default |
|---|---|---|
| `AETHERSCOPE_CAPTURES_DIR` | Where IQ recordings land | `<repo>/captures/` |
| `AETHERSCOPE_DATA_DIR` | SQLite data dir (bookmarks, settings, inventory) | `~/.local/share/aetherscope/` |
| `AETHERSCOPE_HOST` | Bind address (containers set `0.0.0.0`; a non-loopback bind requires `AETHERSCOPE_TOKEN`) | `127.0.0.1` |
| `AETHERSCOPE_TOKEN` | Shared access token. When set, the browser must exchange it once for an HttpOnly cookie, and socket/HTTP clients must present it. When unset, allowed only on loopback binds | unset (loopback only) |
| `AETHERSCOPE_PORT` | HTTP port to listen on (invalid/out-of-range falls back to the default) | `8765` |
| `AETHERSCOPE_ALLOWED_ORIGINS` | Comma-separated extra origins allowed to connect; set to your proxy domain when behind a reverse proxy | same-origin only |
| `AETHERSCOPE_SECRET_KEY` | Flask session signing key | random per process |
| `AETHERSCOPE_LABEL` | launchd service label | `local.aetherscope` |
| `AETHERSCOPE_GPS` | Set to `0` to hard-disable GPS geotagging regardless of the in-app toggle | toggle in app |
| `AETHERSCOPE_GPSD_HOST` / `AETHERSCOPE_GPSD_PORT` | gpsd location for geotagging | `127.0.0.1` / `2947` |

For the launchd service, set these in the plist `EnvironmentVariables` block (or export them before `uv run aetherscope` when running manually).

## Auth boundary

Loopback bind (`127.0.0.1`, the default) with no `AETHERSCOPE_TOKEN` behaves
exactly as before: no login, no friction. Setting `AETHERSCOPE_TOKEN` turns
auth on even on loopback. Binding beyond loopback (`AETHERSCOPE_HOST=0.0.0.0`)
without a token is refused at startup with a clear error — widening the bind
is a deliberate flip (set a long random token), not an accident. In the
browser, enter the token once on the gate page; the server sets an HttpOnly,
`SameSite=Strict` signed cookie (the raw token is never stored in the cookie),
and that cookie authenticates both HTTP capture routes and the Socket.IO
connection. API clients may instead send `Authorization: Bearer <token>` or
`X-Aetherscope-Token: <token>`; Socket.IO clients may send an `auth` payload
of `{"token": "..."}`.
