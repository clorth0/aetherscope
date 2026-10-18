// Aetherscope core: the one shared seam every mode module sits behind.
// Owns the socket, shared state, status/toast/device/GPS plumbing, the
// html`` escaping helpers, and mode switching. Mode modules register
// config/restore/enter/leave/GPS hooks here instead of reaching into
// each other's globals. Loaded first; see index.html for load order.
"use strict";
window.Aether = (function () {
  const socket = io({ withCredentials: true });
  // Auth (when AETHERSCOPE_TOKEN is set): the HttpOnly gate cookie rides
  // this connection. If it is gone/rejected, / answers 401 and the gate
  // page at / is where the token is re-entered.
  socket.io.on("error", () => {
    fetch("/api/captures", { credentials: "same-origin" })
      .then((r) => { if (r.status === 401) window.location.assign("/"); })
      .catch(() => {});
  });

  const statusDot = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");
  const devicePill = document.getElementById("device-pill");
  const deviceDot = document.getElementById("device-dot");
  const deviceText = document.getElementById("device-text");
  const deviceMeta = document.getElementById("device-meta");
  const toastHost = document.getElementById("toast-host");
  const sweepRateEl = document.getElementById("sweep-rate");

  const viewSweep = document.getElementById("view-sweep");
  const viewDecode = document.getElementById("view-decode");
  const viewCapture = document.getElementById("view-capture");
  const viewAdsb = document.getElementById("view-adsb");
  const viewScan = document.getElementById("view-scan");
  const paneSweep = document.getElementById("pane-sweep");
  const paneDecode = document.getElementById("pane-decode");
  const paneCapture = document.getElementById("pane-capture");
  const paneAdsb = document.getElementById("pane-adsb");
  const paneScan = document.getElementById("pane-scan");
  const viewRadio = document.getElementById("view-radio");
  const paneRadio = document.getElementById("pane-radio");
  const viewInventory = document.getElementById("view-inventory");

  // The shared state behind the seam. Modes read/write these fields
  // through core.state; no mode keeps its own copy of them.
  const state = {
    currentMode: "sweep",
    serverMode: "idle",
    serverStatusLabel: "Idle",
    prevServerMode: "idle",
    gps: { enabled: false },
  };

  // Handler registration: each mode module registers what the shared
  // status/GPS/mode plumbing must call into it, in load order.
  const applyConfigFns = [];
  const restoreSettingsFns = [];
  const enterHooks = {};
  const leftHooks = {};
  const gpsHooks = [];
  function registerApplyConfig(fn) { applyConfigFns.push(fn); }
  function registerRestoreSettings(fn) { restoreSettingsFns.push(fn); }
  function registerEnterMode(mode, fn) { enterHooks[mode] = fn; }
  function registerLeftMode(mode, fn) { (leftHooks[mode] = leftHooks[mode] || []).push(fn); }
  function registerGpsHook(fn) { gpsHooks.push(fn); }

// ------------------------------------------------------------------
// Mode switching
// ------------------------------------------------------------------
function setMode(mode) {
  state.currentMode = mode;
  viewSweep.hidden   = mode !== "sweep";
  viewDecode.hidden  = mode !== "decode";
  viewCapture.hidden = mode !== "capture";
  viewAdsb.hidden    = mode !== "adsb";
  viewScan.hidden    = mode !== "scan";
  viewRadio.hidden   = mode !== "radio";
  viewInventory.hidden = mode !== "inventory";
  paneSweep.hidden   = mode !== "sweep";
  paneDecode.hidden  = mode !== "decode";
  paneCapture.hidden = mode !== "capture";
  paneAdsb.hidden    = mode !== "adsb";
  paneScan.hidden    = mode !== "scan";
  paneRadio.hidden   = mode !== "radio";
  document.querySelectorAll(".mode-tab").forEach(t => {
    t.classList.toggle("active", t.dataset.mode === mode);
  });
  if (enterHooks[mode]) enterHooks[mode]();
}

document.querySelectorAll(".mode-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    setMode(tab.dataset.mode);
    socket.emit("set_setting", { key: "last_mode", value: tab.dataset.mode });
  });
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Markup the caller already built. The html tag inserts this as-is.
function raw(markup) {
  return { __html: String(markup) };
}

// Escapes every value. Pass raw() only for markup built by html``.
function html(strings, ...values) {
  let out = "";
  strings.forEach((part, i) => {
    out += part;
    if (i >= values.length) return;
    const value = values[i];
    if (value && typeof value === "object" && Object.hasOwn(value, "__html")) {
      out += value.__html;
      return;
    }
    out += escapeHtml(value ?? "");
  });
  return out;
}

function fmtAgo(ts) {
  const dt = Date.now() / 1000 - ts;
  if (dt < 60) return `${Math.round(dt)}s ago`;
  if (dt < 3600) return `${Math.round(dt / 60)}m ago`;
  if (dt < 86400) return `${Math.round(dt / 3600)}h ago`;
  return `${Math.round(dt / 86400)}d ago`;
}

function showToast(level, message) {
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.textContent = message;
  toastHost.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 220);
  }, 5000);
  el.addEventListener("click", () => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 220);
  });
}

function updateSliderFills(ids) {
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    const pct = ((el.value - el.min) / (el.max - el.min)) * 100;
    el.style.setProperty("--fill", `${pct}%`);
  }
}

function flashClick(el, message) {
  console.log("[aetherscope] click:", message, "at", new Date().toISOString());
  const orig = {
    background: el.style.background,
    boxShadow: el.style.boxShadow,
    transform: el.style.transform,
  };
  el.style.background = "#4dd0e1";
  el.style.boxShadow = "0 0 16px #4dd0e1";
  el.style.transform = "scale(0.94)";
  setTimeout(() => {
    el.style.background = orig.background;
    el.style.boxShadow = orig.boxShadow;
    el.style.transform = orig.transform;
  }, 200);
}

// ------------------------------------------------------------------
// Status
// ------------------------------------------------------------------
function refreshStatusUI() {
  const running = state.serverMode !== "idle";
  statusDot.classList.toggle("running", running);
  statusDot.classList.toggle("stopped", !running);
  statusText.textContent = state.serverStatusLabel;
  if (state.serverMode !== "sweep") sweepRateEl.textContent = "0.0 Hz";
  const scanBtn = document.getElementById("btn-scan-radio");
  if (scanBtn) {
    scanBtn.textContent = state.serverMode === "scan_radio" ? "■ Stop scan" : "⤬ Scan marks";
    scanBtn.classList.toggle("active", state.serverMode === "scan_radio");
  }

  // Light up the tab whose mode is actually running, even when the user
  // is viewing a different tab.
  document.querySelectorAll(".mode-tab").forEach(t => {
    t.classList.toggle("is-running", t.dataset.mode === state.serverMode);
  });

  updateActionButtons();
}

// Drive Start/Stop styling from the actual running state so the lit button
// always signals on/off: idle -> Start lit (cyan), running -> Stop lit (amber).
const ACTION_PAIRS = [
  ["sweep",   "btn-start-sweep",   "btn-stop-sweep"],
  ["decode",  "btn-start-decode",  "btn-stop-decode"],
  ["scan",    "btn-start-scan",    "btn-stop-scan"],
  ["adsb",    "btn-start-adsb",    "btn-stop-adsb"],
  ["radio",   "btn-start-radio",   "btn-stop-radio"],
  ["capture", "btn-start-capture", "btn-cancel-capture"],
];
function updateActionButtons() {
  for (const [mode, startId, stopId] of ACTION_PAIRS) {
    const start = document.getElementById(startId);
    const stop = document.getElementById(stopId);
    if (!start || !stop) continue;
    const live = state.serverMode === mode;
    start.disabled = live;       // can't start what's already running
    stop.disabled = !live;       // nothing to stop when idle
    stop.classList.toggle("live", live);
  }
}

socket.on("device_status", (s) => {
  const info = s.info;
  devicePill.classList.remove("connected", "disconnected", "probing");
  deviceDot.classList.remove("running", "stopped", "warn");
  if (info && info.serial) {
    devicePill.classList.add("connected");
    deviceDot.classList.add("running");
    deviceText.textContent = "HackRF";
    const tail = info.serial.slice(-6).toUpperCase();
    const fw = info.firmware ? ` · fw ${info.firmware}` : "";
    deviceMeta.textContent = `${tail}${fw}`;
    devicePill.title = `Serial: ${info.serial}\nBoard: ${info.board || "?"}\nFirmware: ${info.firmware || "?"}\nClick to re-probe`;
  } else {
    devicePill.classList.add("disconnected");
    deviceDot.classList.add("stopped");
    deviceText.textContent = "No HackRF";
    deviceMeta.textContent = "";
    devicePill.title = "HackRF not detected. Plug it in directly to the Mac mini, not through a hub. Click to re-probe.";
  }
});

socket.on("toast", (t) => {
  showToast(t.level || "info", t.message || "");
});

devicePill.addEventListener("click", () => socket.emit("refresh_device"));

const gpsPill = document.getElementById("gps-pill");
const gpsDot = document.getElementById("gps-dot");
const gpsText = document.getElementById("gps-text");

function renderGps(s) {
  state.gps = s || { enabled: false };
  gpsPill.classList.remove("connected", "disconnected", "probing");
  gpsDot.classList.remove("running", "stopped", "warn");
  if (!state.gps.enabled) {
    gpsDot.classList.add("stopped");
    gpsText.textContent = "GPS off";
    gpsPill.title = "GPS geotagging is off. Captures are not stamped with your location. Click to enable.";
  } else if (state.gps.lat != null && !state.gps.stale) {
    const fix = state.gps.mode >= 3 ? "3D" : "2D";
    const sat = state.gps.sats != null ? ` · ${state.gps.sats} sat` : "";
    gpsPill.classList.add("connected");
    gpsDot.classList.add("running");
    gpsText.textContent = `GPS ${fix}${sat}`;
    // Coords masked on screen; revealed only on hover, leading with "ON".
    const alt = state.gps.alt != null ? ` · ${state.gps.alt.toFixed(0)} m` : "";
    const hd = state.gps.hdop != null ? `\nhdop ${state.gps.hdop}` : "";
    gpsPill.title = `Geotagging ON — captures are stamped with your location.\n` +
      `${state.gps.lat.toFixed(6)}, ${state.gps.lon.toFixed(6)}${alt}${hd}\nClick to disable.`;
  } else {
    gpsDot.classList.add("warn");
    gpsText.textContent = "GPS no fix";
    gpsPill.title = "Geotagging ON but no GPS fix yet. Click to disable.";
  }
  gpsHooks.forEach((fn) => fn());
}

socket.on("gps_status", renderGps);
gpsPill.addEventListener("click", () => {
  socket.emit("set_setting", { key: "gps_enabled", value: !state.gps.enabled });
});

const gpsPrecisionEl = document.getElementById("gps-precision");
if (gpsPrecisionEl) {
  gpsPrecisionEl.addEventListener("change", () => {
    socket.emit("set_setting", { key: "gps_precision", value: gpsPrecisionEl.value });
  });
}

let _settingsRestored = false;

socket.on("status", (s) => {
  state.serverMode = s.mode || "idle";
  state.serverStatusLabel = s.status_label || (state.serverMode === "idle" ? "Idle" : state.serverMode);
  applyConfigFns.forEach((fn) => fn(s));

  // Restore persisted UI settings once on the first status event.
  if (!_settingsRestored && s.settings) {
    _settingsRestored = true;
    const st = s.settings;
    const KNOWN_MODES = ["sweep", "decode", "capture", "adsb", "scan", "radio"];
    if (KNOWN_MODES.includes(st.last_mode)) setMode(st.last_mode);
    restoreSettingsFns.forEach((fn) => fn(st));
    if (st.gps_precision) {
      const gpe = document.getElementById("gps-precision");
      if (gpe) gpe.value = st.gps_precision;
    }
  }

  // Visible-feedback when the backend transitions out of a running mode:
  // wipe whatever the previous mode was painting so the user can SEE
  // that Stop (or mode switch) actually took effect.
  if (state.prevServerMode !== "idle" && state.serverMode !== state.prevServerMode) {
    onServerLeftMode(state.prevServerMode);
  }
  state.prevServerMode = state.serverMode;

  refreshStatusUI();
});

function onServerLeftMode(prevMode) {
  (leftHooks[prevMode] || []).forEach((fn) => fn());
  // capture/scan have their own dedicated "done" / "stopped" events
}

socket.on("telemetry", (t) => {
  const c = (t && t.counters) || {};
  const computed = c.sweeps_computed || 0;
  const emitted = c.sweeps_emitted || 0;
  const setT = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  setT("diag-sweeps", emitted.toLocaleString());
  setT("diag-dropped", computed > emitted ? (computed - emitted).toLocaleString() : "0");
  setT("diag-usb", c.usb_warnings || 0);
  setT("diag-deaths", c.subprocess_deaths || 0);
  const log = document.getElementById("diag-log");
  if (!log) return;
  const recent = (t && t.recent) || [];
  log.replaceChildren();
  if (!recent.length) {
    const e = document.createElement("div");
    e.className = "event-empty";
    e.textContent = "No warnings.";
    log.appendChild(e);
    return;
  }
  recent.slice(-30).reverse().forEach((line) => {
    const d = document.createElement("div");
    d.className = "diag-log-line";
    d.textContent = line;
    log.appendChild(d);
  });
});

  return {
    core: {
      socket, state, escapeHtml, raw, html, fmtAgo, showToast,
      updateSliderFills, flashClick, setMode,
      registerApplyConfig, registerRestoreSettings,
      registerEnterMode, registerLeftMode, registerGpsHook,
    },
  };
})();
