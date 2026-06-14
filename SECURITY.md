# Security Policy

## Reporting a vulnerability

Please report security issues privately using GitHub's **"Report a vulnerability"**
button on the [Security tab](https://github.com/clorth0/aetherscope/security/advisories),
rather than opening a public issue. Reports are acknowledged as soon as possible.

## Scope and threat model

Aetherscope is a self-hosted tool that drives a HackRF One. By design it:

- binds to `127.0.0.1` only, and is meant to be reached over Tailscale or an
  SSH tunnel, not exposed directly to the internet;
- has no authentication of its own, so it should not be placed on a public
  interface without your own access controls in front of it;
- invokes local SDR binaries (`hackrf_*`, `rtl_433`, `readsb-hackrf`) with
  fixed argument lists (no shell interpolation), and validates socket inputs
  (demod type and frequency range) before use.

If you expose it beyond localhost, put it behind your own authentication and
network controls.
