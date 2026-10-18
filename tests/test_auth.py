"""Auth boundary: startup guard, HTTP gate, and socket connect gate.

Hardware-free. Exercises a real request cycle through the Flask test
client and the Flask-SocketIO test client, the same clients a browser
request travels through.
"""

import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERSCOPE_DATA_DIR", tempfile.mkdtemp(prefix="aether-auth-"))

from backend import app as appmod  # noqa: E402
from backend.auth import (  # noqa: E402
    AuthConfigError,
    AuthPolicy,
    is_loopback_host,
)
from backend.capture import CAPTURES_DIR  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token-not-a-real-secret"

# Characterization: every socket handler that exists today. A handler
# added to app.py without being added here fails this file, so no
# handler can be left ungated by accident: gating lives at connect,
# and connect is the one seam every handler sits behind.
EXPECTED_SOCKET_EVENTS = {
    "connect",
    "start_sweep", "start_decode", "start_capture", "start_adsb",
    "start_radio", "start_scan_radio", "start_replay", "play_capture",
    "decode_capture", "start_scan", "cancel_capture", "stop",
    "snap_radio", "refresh_device", "list_captures", "delete_capture",
    "update_capture", "list_bookmarks", "add_bookmark", "update_bookmark",
    "delete_bookmark", "bump_bookmark", "set_setting", "redact_capture",
    "start_audio_record", "stop_audio_record", "list_inventory",
    "clear_inventory",
}


class _policy:
    """Swap the app-global auth policy for one test, restoring it after."""

    def __init__(self, policy):
        self.policy = policy

    def __enter__(self):
        self._old = appmod._auth_policy
        appmod._auth_policy = self.policy
        return self.policy

    def __exit__(self, *exc):
        appmod._auth_policy = self._old
        return False


def _required_policy():
    # Loopback host but a token is set: auth is required (decision 2).
    return AuthPolicy(host="127.0.0.1", token=TOKEN)


# ---------------------------------------------------------------- guard

def test_loopback_hosts_are_recognised():
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("127.0.0.2")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("::")
    assert not is_loopback_host("192.168.1.10")
    assert not is_loopback_host("")


def test_non_loopback_without_token_refuses_to_start():
    for host in ("0.0.0.0", "192.168.1.10", "::"):
        try:
            AuthPolicy(host=host, token=None).ensure_safe_startup()
        except AuthConfigError as exc:
            assert "AETHERSCOPE_TOKEN" in str(exc)
        else:
            raise AssertionError(f"{host} without token was allowed")


def test_loopback_or_token_may_start():
    AuthPolicy(host="127.0.0.1", token=None).ensure_safe_startup()
    AuthPolicy(host="0.0.0.0", token=TOKEN).ensure_safe_startup()
    AuthPolicy(host="127.0.0.1", token=TOKEN).ensure_safe_startup()


def test_auth_required_only_when_non_loopback_or_token_set():
    assert AuthPolicy(host="127.0.0.1", token=None).requires_auth is False
    assert AuthPolicy(host="127.0.0.1", token=TOKEN).requires_auth is True
    assert AuthPolicy(host="0.0.0.0", token=TOKEN).requires_auth is True


def test_token_compare_and_cookie_round_trip():
    policy = _required_policy()
    assert policy.check_token(TOKEN)
    assert not policy.check_token("wrong")
    assert not policy.check_token(None)
    assert not policy.check_token("")
    cookie = policy.make_cookie_value()
    assert TOKEN not in cookie  # the cookie never carries the raw token
    assert policy.verify_cookie(cookie)
    assert not policy.verify_cookie(cookie + "x")
    assert not policy.verify_cookie("")
    assert not policy.verify_cookie(None)
    assert not AuthPolicy(host="127.0.0.1", token="other").verify_cookie(cookie)


# ------------------------------------------------------- characterization

def test_socket_handler_inventory_is_complete():
    source = (ROOT / "backend" / "app.py").read_text()
    found = set(re.findall(r'@socketio\.on\("([^"]+)"\)', source))
    assert found == EXPECTED_SOCKET_EVENTS, (
        f"socket handlers changed: new={sorted(found - EXPECTED_SOCKET_EVENTS)} "
        f"gone={sorted(EXPECTED_SOCKET_EVENTS - found)}; update the auth "
        "characterization in tests/test_auth.py"
    )


# ------------------------------------------------------------- HTTP cycle

def test_http_capture_routes_require_token_when_configured():
    with _policy(_required_policy()):
        client = appmod.app.test_client()
        assert client.get("/api/captures").status_code == 401
        assert client.get("/captures/whatever.iq").status_code == 401
        bad = {"Authorization": "Bearer wrong"}
        assert client.get("/api/captures", headers=bad).status_code == 401
        good = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/api/captures", headers=good).status_code == 200


def test_gate_page_then_cookie_unlocks_the_app():
    with _policy(_required_policy()):
        client = appmod.app.test_client()
        gate = client.get("/")
        assert gate.status_code == 200
        assert b"Access token" in gate.data
        assert b'id="view-sweep"' not in gate.data  # the app shell stays shut

        wrong = client.post("/auth/token", data={"token": "wrong"})
        assert wrong.status_code == 401
        assert client.get("/api/captures").status_code == 401

        ok = client.post("/auth/token", data={"token": TOKEN})
        assert ok.status_code in (302, 303)
        cookie = ok.headers.get("Set-Cookie", "")
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert TOKEN not in cookie
        # The cookie the browser now holds unlocks shell + captures.
        assert b'id="view-sweep"' in client.get("/").data
        assert client.get("/api/captures").status_code == 200


def test_capture_download_with_token_reads_the_file():
    with _policy(_required_policy()), tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "clip.iq").write_bytes(b"\x00\x01")
        old = appmod.CAPTURES_DIR
        appmod.CAPTURES_DIR = Path(tmp)
        try:
            client = appmod.app.test_client()
            good = {"Authorization": f"Bearer {TOKEN}"}
            resp = client.get("/captures/clip.iq", headers=good)
            assert resp.status_code == 200
            assert resp.data == b"\x00\x01"
        finally:
            appmod.CAPTURES_DIR = old


# ----------------------------------------------------------- socket cycle

def test_socket_connect_requires_token_when_configured():
    with _policy(_required_policy()):
        anon = appmod.socketio.test_client(appmod.app)
        assert not anon.is_connected()
        wrong = appmod.socketio.test_client(appmod.app, auth={"token": "wrong"})
        assert not wrong.is_connected()
        good = appmod.socketio.test_client(appmod.app, auth={"token": TOKEN})
        assert good.is_connected()
        # A gated handler answers only behind the authenticated connect.
        good.emit("list_captures")
        received = good.get_received()
        assert any(item["name"] == "captures" for item in received)
        good.disconnect()


def test_socket_connect_accepts_the_gate_cookie():
    with _policy(_required_policy()):
        http = appmod.app.test_client()
        http.post("/auth/token", data={"token": TOKEN})
        client = appmod.socketio.test_client(appmod.app, flask_test_client=http)
        assert client.is_connected()
        client.disconnect()


# -------------------------------------------------------- loopback parity

def test_loopback_without_token_behaves_as_before():
    with _policy(AuthPolicy(host="127.0.0.1", token=None)):
        client = appmod.app.test_client()
        assert b'id="view-sweep"' in client.get("/").data
        assert client.get("/api/captures").status_code == 200
        socket_client = appmod.socketio.test_client(appmod.app)
        assert socket_client.is_connected()
        socket_client.emit("list_captures")
        received = socket_client.get_received()
        assert any(item["name"] == "captures" for item in received)
        socket_client.disconnect()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
