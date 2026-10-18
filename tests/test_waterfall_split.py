"""Static guard for the waterfall.js split at its mode seams.

No browser test runner exists in this repo, so this guard is the
test surface for the split: it proves socket-event and DOM-id parity
with the monolith, script load order in index.html, and that no mode
module reaches into another mode's private globals. Cross-mode use
must go through window.Aether (core state/hooks or a named mode
interface such as Aether.radio.tuneToMark).
"""

import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "frontend" / "static"
INDEX = ROOT / "frontend" / "templates" / "index.html"

MODULES = [
    "aether-core.js",
    "aether-sweep.js",
    "aether-decode.js",
    "aether-capture.js",
    "aether-adsb.js",
    "aether-scan.js",
    "aether-radio.js",
    "aether-inventory.js",
    "aether-boot.js",
]

EXPECTED_ON = ['adsb', 'audio_record_status', 'bookmarks', 'capture_done', 'capture_progress', 'capture_started', 'captures', 'contacts', 'decoded', 'device_status', 'gps_status', 'iq_play_done', 'iq_play_started', 'phase_completed', 'phase_progress', 'phase_started', 'radio_audio', 'radio_signal', 'radio_started', 'scan_completed', 'scan_failed', 'scan_hold', 'scan_pos', 'scan_radio_started', 'scan_resume', 'scan_started', 'scan_stopped', 'status', 'sweep', 'telemetry', 'toast']
EXPECTED_EMIT = ['add_bookmark', 'bump_bookmark', 'cancel_capture', 'clear_inventory', 'decode_capture', 'delete_bookmark', 'delete_capture', 'list_captures', 'list_inventory', 'play_capture', 'redact_capture', 'refresh_device', 'set_setting', 'snap_radio', 'start_adsb', 'start_audio_record', 'start_capture', 'start_decode', 'start_radio', 'start_replay', 'start_scan', 'start_scan_radio', 'start_sweep', 'stop', 'stop_audio_record', 'update_bookmark', 'update_capture']
# gps-broadcast-precision added intentionally with the live-broadcast
# precision control (geotag #4); socket.emit count 47->48 is that control.
EXPECTED_JS_IDS = ['adsb-filter', 'adsb-stats', 'adsb_gain', 'adsb_gain_val', 'adsb_lat', 'adsb_lon', 'aircraft-list', 'amp', 'bin_width', 'bookmark-filter', 'bookmark-label', 'bookmark-list', 'btn-add-bookmark', 'btn-adsb-use-gps', 'btn-avg', 'btn-cancel-capture', 'btn-clear-events', 'btn-clear-inventory', 'btn-mark-peak', 'btn-maxhold', 'btn-record-audio', 'btn-scan-radio', 'btn-snap-radio', 'btn-start-adsb', 'btn-start-capture', 'btn-start-decode', 'btn-start-radio', 'btn-start-scan', 'btn-start-sweep', 'btn-stop-adsb', 'btn-stop-decode', 'btn-stop-radio', 'btn-stop-scan', 'btn-stop-sweep', 'btn-trace-clear', 'cal_offset', 'cap-active', 'cap-active-detail', 'cap-active-name', 'cap-bytes', 'cap-eta', 'cap-filter', 'cap-pct', 'cap-progress-bar', 'cap_amp', 'cap_duration', 'cap_freq', 'cap_label', 'cap_lna', 'cap_lna_val', 'cap_rate', 'cap_vga', 'cap_vga_val', 'captures-list', 'dec_freq', 'dec_gain', 'dec_gain_val', 'dec_rate', 'device-dot', 'device-meta', 'device-pill', 'device-text', 'diag-log', 'event-count', 'event-filter', 'event-list', 'f_start', 'f_stop', 'fft', 'gps-broadcast-precision', 'gps-dot', 'gps-pill', 'gps-precision', 'gps-text', 'hover-freq', 'hover-power', 'inv-filter', 'inventory-list', 'iq-play-bar', 'iq-play-demod', 'iq-play-name', 'iq-play-offset', 'iq-play-offset-val', 'iq-play-stop', 'lna', 'lna_val', 'marks-delta', 'marks-list', 'pane-adsb', 'pane-capture', 'pane-decode', 'pane-radio', 'pane-scan', 'pane-sweep', 'peaks-list', 'radio-down', 'radio-meter-fill', 'radio-meter-label', 'radio-now-demod', 'radio-now-freq', 'radio-now-state', 'radio-up', 'radio_freq', 'radio_squelch', 'radio_squelch_val', 'radio_vol', 'radio_vol_val', 'scan-results', 'scan_adsb_s', 'scan_lat', 'scan_lon', 'scan_rtl_s', 'scan_sweep_s', 'scan_thresh', 'status-dot', 'status-text', 'sweep-rate', 'toast-host', 'vga', 'vga_val', 'view-adsb', 'view-capture', 'view-decode', 'view-inventory', 'view-radio', 'view-scan', 'view-sweep', 'waterfall']

# Private names owned by each mode. A bare reference in another
# module's file is a cross-mode global leak; the Aether.* interface
# (e.g. Aether.radio.startIqPlay) is the only permitted crossing.
PRIVATE_NAMES = {
    "aether-sweep.js": ["lastSweep", "drawFFT", "pushWaterfallRow", "maxHoldPowers", "avgPowers", "renderMarks", "addMark", "clearSweepVisuals", "readSweepConfig", "applySweepConfigToInputs"],
    "aether-decode.js": ["renderEvents", "summarizeFields", "readDecodeConfig", "applyDecodeConfigToInputs"],
    "aether-capture.js": ["renderCaptures", "capturesListEl", "readCaptureConfig", "applyCaptureConfigToInputs", "playCapture"],
    "aether-adsb.js": ["adsbMap", "adsbMarkers", "adsbTracks", "adsbTrails", "adsbAircraft", "renderAircraftList", "updateAdsbMap", "clearAdsbVisuals", "initAdsbMap", "readAdsbConfig", "applyAdsbConfigToInputs"],
    "aether-scan.js": ["scanFindings", "renderScanResults", "readScanConfig", "applyScanConfigToInputs", "resetPhases"],
    "aether-radio.js": ["radioCtx", "radioNode", "radioGain", "radioPlaying", "radioDemod", "stopRadioPlayback", "ensureRadioAudio", "setRadioDemod", "setRadioNow", "renderBookmarks", "startIqPlay", "tuneToMark"],
    "aether-inventory.js": ["renderInventory", "inventoryListEl"],
}


def _sources():
    return {name: (STATIC / name).read_text() for name in MODULES if (STATIC / name).exists()}


def test_monolith_is_gone_and_modules_exist():
    assert not (STATIC / "waterfall.js").exists(), "waterfall.js monolith still exists"
    missing = [m for m in MODULES if not (STATIC / m).exists()]
    assert not missing, f"missing modules: {missing}"


def test_script_load_order_in_index():
    html = INDEX.read_text()
    scripts = re.findall(r"filename='([^']+\.js)'", html)
    app_scripts = [s for s in scripts if s.startswith("aether-")]
    assert app_scripts == MODULES, f"load order {app_scripts} != {MODULES}"
    assert "waterfall.js" not in scripts


def test_socket_event_sets_are_unchanged():
    blob = "\n".join(_sources().values())
    on = re.findall(r'socket\.on\("([^"]+)"', blob)
    assert sorted(set(on)) == EXPECTED_ON, f"socket.on set drift: {sorted(set(on))}"
    assert len(on) == 31, f"socket.on registrations {len(on)} != 31"
    emit = set(re.findall(r'socket\.emit\("([^"]+)"', blob))
    # the audio-record toggle emits via a ternary over two literals
    emit |= set(re.findall(r'socket\.emit\([^)]*?\?\s*"([^"]+)"\s*:\s*"([^"]+)"', blob) and
                [x for pair in re.findall(r'socket\.emit\([^)]*?\?\s*"([^"]+)"\s*:\s*"([^"]+)"', blob) for x in pair])
    assert sorted(emit) == EXPECTED_EMIT, f"socket.emit set drift: {sorted(emit)}"
    assert len(re.findall(r"socket\.emit\(", blob)) == 48, "socket.emit call sites != 48"


def test_dom_ids_referenced_are_unchanged():
    blob = "\n".join(_sources().values())
    ids = sorted(set(re.findall(r'getElementById\("([^"]+)"\)', blob)))
    assert ids == EXPECTED_JS_IDS, f"JS DOM id set drift: {ids}"
    index_ids = set(re.findall(r'id="([^"]+)"', INDEX.read_text()))
    assert set(ids) <= index_ids, "JS references an id index.html does not define"


def test_no_mode_module_references_another_modes_globals():
    sources = _sources()
    for owner, names in PRIVATE_NAMES.items():
        for other, src in sources.items():
            if other in (owner, "aether-core.js", "aether-boot.js"):
                continue
            for name in names:
                # Aether.<mode>.<name> is the named seam, not a bare global.
                stripped = re.sub(r"Aether\.\w+\." + re.escape(name) + r"\b", "", src)
                stripped = re.sub(r"A\.\w+\." + re.escape(name) + r"\b", "", stripped)
                assert not re.search(r"\b" + re.escape(name) + r"\b", stripped), \
                    f"{other} references {owner}'s private {name}; use the Aether interface"


def test_each_module_is_an_iife_behind_the_aether_namespace():
    for name, src in _sources().items():
        if name == "aether-core.js":
            assert "window.Aether" in src
            continue
        assert "(function (A)" in src, f"{name} is not an IIFE module"
        assert '"use strict"' in src, f"{name} lacks strict mode"


def test_all_frontend_js_parses():
    for name in MODULES:
        path = STATIC / name
        assert path.exists(), name
        result = subprocess.run(["node", "--check", str(path)], text=True, capture_output=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


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
