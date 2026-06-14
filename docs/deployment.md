# Deployment

Aetherscope binds `127.0.0.1` and has no built-in authentication. Reach it over
Tailscale or an `ssh -L` tunnel with no extra setup, or front it with a reverse
proxy for TLS and auth. Do not put it on a public interface directly.

## Reverse proxy + auth (Caddy)

`deploy/Caddyfile.example` is a ready-to-edit recipe:

1. Generate a password hash: `caddy hash-password --plaintext 'your-password'`
2. Paste it into the `basic_auth` block, set your domain, and `reverse_proxy 127.0.0.1:8765`.
3. Set `AETHERSCOPE_ALLOWED_ORIGINS=https://your-domain` so the proxied Socket.IO
   connection is accepted (this is almost always the cause if it fails to connect).

The app stays single-process on localhost; Caddy handles TLS and auth in front.
WebSocket upgrades are handled automatically by Caddy's `reverse_proxy`.

## Docker (Linux)

macOS Docker Desktop cannot pass USB through to containers, so on a Mac use the
native install ([install.md](install.md)). On Linux, USB passthrough works:

```sh
docker compose up --build
# or:
docker build -t aetherscope .
docker run --rm --device=/dev/bus/usb -p 127.0.0.1:8765:8765 aetherscope
```

Notes:

- The container binds `0.0.0.0` internally (via `AETHERSCOPE_HOST`) but publishes
  only to `127.0.0.1:8765`. Reach it over Tailscale/SSH or front it with Caddy.
- It runs as a non-root user, so USB access may need a host udev rule plus
  `--group-add plugdev`, or `--privileged` as a fallback.
- Captures persist in `./captures`.
- Provided as a starting point; not yet verified on a real Linux + HackRF host.
