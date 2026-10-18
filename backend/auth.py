"""Auth boundary: who may drive the SDR, decided in one module.

Aetherscope is a single-user local app. Today that posture is implicit
(localhost bind + same-origin sockets). This module makes it explicit:

- Loopback bind and no token: no auth, exactly the historical behaviour.
- A token is set (``AETHERSCOPE_TOKEN``) or the bind leaves loopback:
  auth is required on the socket connect (which gates every socket
  handler behind it) and on the capture HTTP routes.
- Non-loopback bind without a token is unrepresentable at startup:
  ``ensure_safe_startup`` refuses, so widening the bind later is one
  deliberate config flip (set the token), never an accident.

Interface (the whole surface a caller must learn):

- ``AuthPolicy(host, token)`` with ``requires_auth``,
  ``ensure_safe_startup()``, ``check_token()``, ``make_cookie_value()``
  and ``verify_cookie()``.
- ``policy.is_authenticated(headers, cookies, auth)`` -- one verdict
  for an HTTP request or a Socket.IO connect, from a Bearer /
  X-Aetherscope-Token header, the signed cookie, or the socket auth
  payload.
- ``render_gate()`` -- the token-entry page served in place of the app
  shell while unauthenticated.

The cookie never carries the raw token: it is ``<issued>.<hmac>`` where
the HMAC key is the token itself, so rotating the token invalidates
every outstanding cookie. Comparisons are constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time
from dataclasses import dataclass

COOKIE_NAME = "aetherscope_auth"
COOKIE_MAX_AGE_S = 30 * 24 * 3600  # a local single-user session: 30 days
_COOKIE_MESSAGE = "aetherscope-auth-v1"


class AuthConfigError(RuntimeError):
    """The configured bind/token combination must not be served."""


def is_loopback_host(host: str | None) -> bool:
    """True only for hosts that cannot be reached from another machine."""
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _sign(token: str, issued: int) -> str:
    return hmac.new(
        token.encode(), f"{_COOKIE_MESSAGE}:{issued}".encode(), hashlib.sha256
    ).hexdigest()


@dataclass(frozen=True)
class AuthPolicy:
    host: str = "127.0.0.1"
    token: str | None = None

    def __post_init__(self) -> None:
        token = (self.token or "").strip() or None
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "host", (self.host or "").strip() or "127.0.0.1")

    @classmethod
    def from_env(cls, env, host: str | None = None) -> "AuthPolicy":
        return cls(
            host=host if host is not None else env.get("AETHERSCOPE_HOST", "127.0.0.1"),
            token=env.get("AETHERSCOPE_TOKEN"),
        )

    @property
    def requires_auth(self) -> bool:
        return self.token is not None or not is_loopback_host(self.host)

    def ensure_safe_startup(self) -> None:
        """Fail closed: never serve a non-loopback bind without a token."""
        if not is_loopback_host(self.host) and self.token is None:
            raise AuthConfigError(
                f"Refusing to start: AETHERSCOPE_HOST={self.host} is not a "
                "loopback address and no AETHERSCOPE_TOKEN is set. Anyone who "
                "can reach this port could drive the SDR. Set AETHERSCOPE_TOKEN "
                "to a long random value, or bind 127.0.0.1 (the default) and "
                "reach the app over Tailscale / an SSH tunnel / a reverse proxy."
            )

    def check_token(self, candidate) -> bool:
        if self.token is None or not isinstance(candidate, str) or not candidate:
            return False
        return hmac.compare_digest(candidate, self.token)

    def make_cookie_value(self, now: float | None = None) -> str:
        issued = int(time.time() if now is None else now)
        return f"{issued}.{_sign(self.token or '', issued)}"

    def verify_cookie(self, value, now: float | None = None) -> bool:
        if self.token is None or not isinstance(value, str) or "." not in value:
            return False
        issued_raw, _, signature = value.partition(".")
        try:
            issued = int(issued_raw)
        except ValueError:
            return False
        age = int(time.time() if now is None else now) - issued
        if age < 0 or age > COOKIE_MAX_AGE_S:
            return False
        return hmac.compare_digest(signature, _sign(self.token, issued))

    def is_authenticated(self, headers=None, cookies=None, auth=None) -> bool:
        """One verdict for HTTP requests and socket connects alike."""
        if not self.requires_auth:
            return True
        headers = headers or {}
        cookies = cookies or {}
        if isinstance(auth, dict) and self.check_token(auth.get("token")):
            return True
        bearer = headers.get("Authorization", "")
        if bearer.startswith("Bearer ") and self.check_token(bearer[7:].strip()):
            return True
        if self.check_token(headers.get("X-Aetherscope-Token")):
            return True
        return self.verify_cookie(cookies.get(COOKIE_NAME))


_GATE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<title>Aetherscope — access token required</title></head>
<body>
<h1>Aetherscope</h1>
<p>Access token required. Enter the value of <code>AETHERSCOPE_TOKEN</code>
for this server.</p>
{error}
<form method="post" action="/auth/token">
<label>Access token <input type="password" name="token" required autofocus /></label>
<button type="submit">Unlock</button>
</form>
</body></html>
"""


def render_gate(error: bool = False) -> str:
    note = "<p><strong>That token was not accepted.</strong></p>" if error else ""
    return _GATE_PAGE.format(error=note)
