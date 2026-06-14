// Aetherscope — frontend SDR canvas: FFT trace, scrolling waterfall,
// and rtl_433 decoded event log. Two mutually exclusive modes: sweep / decode.

const socket = io();

const fftCanvas = document.getElementById("fft");
const waterfallCanvas = document.getElementById("waterfall");
const fftCtx = fftCanvas.getContext("2d");
const wfCtx = waterfallCanvas.getContext("2d", { willReadFrequently: true });

const statusDot  = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const devicePill = document.getElementById("device-pill");
const deviceDot  = document.getElementById("device-dot");
const deviceText = document.getElementById("device-text");
const deviceMeta = document.getElementById("device-meta");
const toastHost  = document.getElementById("toast-host");
const hoverFreqEl  = document.getElementById("hover-freq");
const hoverPowerEl = document.getElementById("hover-power");
const sweepRateEl  = document.getElementById("sweep-rate");
const eventCountEl = document.getElementById("event-count");

const viewSweep   = document.getElementById("view-sweep");
const viewDecode  = document.getElementById("view-decode");
const viewCapture = document.getElementById("view-capture");
const viewAdsb    = document.getElementById("view-adsb");
const viewScan    = document.getElementById("view-scan");
const paneSweep   = document.getElementById("pane-sweep");
const paneDecode  = document.getElementById("pane-decode");
const paneCapture = document.getElementById("pane-capture");
const paneAdsb    = document.getElementById("pane-adsb");
const paneScan    = document.getElementById("pane-scan");
const viewRadio   = document.getElementById("view-radio");
const paneRadio   = document.getElementById("pane-radio");

let currentMode    = "sweep";       // UI tab (sweep | decode | capture | adsb | scan)
let serverMode     = "idle";        // backend mode (idle | sweep | decode | capture | adsb | scan)
let prevServerMode = "idle";        // last seen serverMode — used to detect transitions
let lastSweep    = null;
let maxHoldOn    = false;
let avgOn        = false;
let maxHoldPowers = null;   // Float32Array, per-bin peak hold
let avgPowers     = null;   // Float32Array, per-bin exponential average
let lastPeaks     = [];     // most recent detected peaks
let lastPeakRender = 0;     // throttle timestamp
let dragStartX    = -1;     // spectrum drag-zoom selection (px), -1 = none
let dragCurX      = -1;
let prevRange     = null;   // range before the last zoom, for double-click reset
let suppressClick = false;  // swallow the click that ends a drag
let calOffset    = loadCalOffset();  // user dB offset so the axis reads approx dBm
let cursorX      = -1;
let sweepTimestamps = [];
let events       = [];
let eventFilter  = "";

// Saved frequency marks on the sweep spectrum: [{hz}], persisted in the browser.
function loadMarks() {
  try { return JSON.parse(localStorage.getItem("aetherscope.marks") || "[]"); }
  catch (e) { return []; }
}
function saveMarks() {
  try { localStorage.setItem("aetherscope.marks", JSON.stringify(marks)); }
  catch (e) { /* ignore storage errors */ }
}
let marks = loadMarks();

// Bookmarks: populated from status on connect, updated on "bookmarks" events.
let bookmarks = [];

// Cursor-following frequency label for the spectrum.
const hoverTag = document.createElement("div");
hoverTag.className = "hover-tag";
hoverTag.hidden = true;
document.body.appendChild(hoverTag);

const MAX_EVENTS    = 500;
const POWER_MIN     = -100;
const POWER_MAX     = -20;
const FFT_PADDING   = { top: 16, right: 14, bottom: 28, left: 64 };
const WF_PADDING    = { top: 0,  right: 14, bottom: 28, left: 64 };
const AXIS_FONT     = "12px ui-monospace, 'SF Mono', Menlo, monospace";
const DPR = Math.max(window.devicePixelRatio || 1, 1);

// ------------------------------------------------------------------
// Mode switching
// ------------------------------------------------------------------
function setMode(mode) {
  currentMode = mode;
  viewSweep.hidden   = mode !== "sweep";
  viewDecode.hidden  = mode !== "decode";
  viewCapture.hidden = mode !== "capture";
  viewAdsb.hidden    = mode !== "adsb";
  viewScan.hidden    = mode !== "scan";
  viewRadio.hidden   = mode !== "radio";
  paneSweep.hidden   = mode !== "sweep";
  paneDecode.hidden  = mode !== "decode";
  paneCapture.hidden = mode !== "capture";
  paneAdsb.hidden    = mode !== "adsb";
  paneScan.hidden    = mode !== "scan";
  paneRadio.hidden   = mode !== "radio";
  document.querySelectorAll(".mode-tab").forEach(t => {
    t.classList.toggle("active", t.dataset.mode === mode);
  });
  if (mode === "sweep") requestAnimationFrame(fitAll);
  if (mode === "capture") socket.emit("list_captures");
  if (mode === "adsb") initAdsbMap();
}

document.querySelectorAll(".mode-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    setMode(tab.dataset.mode);
    socket.emit("set_setting", { key: "last_mode", value: tab.dataset.mode });
  });
});

// ------------------------------------------------------------------
// Canvas sizing
// ------------------------------------------------------------------
function fitCanvas(canvas, ctx) {
  const r = canvas.getBoundingClientRect();
  const w = Math.floor(r.width * DPR);
  const h = Math.floor(r.height * DPR);
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }
}
function cssSize(canvas) {
  const r = canvas.getBoundingClientRect();
  return { w: r.width, h: r.height };
}
function fitAll() {
  fitCanvas(fftCanvas, fftCtx);
  fitCanvas(waterfallCanvas, wfCtx);
  if (lastSweep) drawFFT(lastSweep.powers);
}
window.addEventListener("resize", () => { if (currentMode === "sweep") fitAll(); });

// ------------------------------------------------------------------
// Colormap & helpers (sweep view)
// ------------------------------------------------------------------
const COLORMAP = (() => {
  const stops = [
    [0.00,  16,   8,  40],
    [0.15,  43,  20, 100],
    [0.30,  61,  62, 150],
    [0.45,  35, 130, 168],
    [0.60,  35, 178, 124],
    [0.75, 175, 215,  64],
    [0.90, 254, 235,  39],
    [1.00, 255, 100,  60],
  ];
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    for (let s = 0; s < stops.length - 1; s++) {
      const [a, ar, ag, ab] = stops[s];
      const [b, br, bg, bb] = stops[s + 1];
      if (t >= a && t <= b) {
        const k = (t - a) / (b - a || 1);
        lut[i * 3]     = ar + (br - ar) * k;
        lut[i * 3 + 1] = ag + (bg - ag) * k;
        lut[i * 3 + 2] = ab + (bb - ab) * k;
        break;
      }
    }
  }
  return lut;
})();
function powerToLut(dB) {
  const t = Math.max(0, Math.min(1, (dB - POWER_MIN) / (POWER_MAX - POWER_MIN)));
  return Math.floor(t * 255);
}
function fmtFreq(hz) {
  if (hz >= 1e9) return (hz / 1e9).toFixed(3) + " GHz";
  if (hz >= 1e6) return (hz / 1e6).toFixed(3) + " MHz";
  if (hz >= 1e3) return (hz / 1e3).toFixed(1) + " kHz";
  return hz.toFixed(0) + " Hz";
}
function niceTicks(min, max, target = 8) {
  const range = max - min;
  const rawStep = range / target;
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const norm = rawStep / mag;
  let step;
  if      (norm < 1.5) step = 1   * mag;
  else if (norm < 3.5) step = 2.5 * mag;
  else if (norm < 7.5) step = 5   * mag;
  else                 step = 10  * mag;
  const out = [];
  const start = Math.ceil(min / step) * step;
  for (let v = start; v <= max + 1e-9; v += step) out.push(v);
  return out;
}
function resampleMax(powers, width) {
  const out = new Float32Array(width);
  if (!powers.length) return out;
  const ratio = powers.length / width;
  for (let i = 0; i < width; i++) {
    const a = Math.floor(i * ratio);
    const b = Math.max(a + 1, Math.floor((i + 1) * ratio));
    let m = -Infinity;
    for (let j = a; j < b && j < powers.length; j++) if (powers[j] > m) m = powers[j];
    out[i] = m === -Infinity ? POWER_MIN : m;
  }
  return out;
}

// ------------------------------------------------------------------
// FFT + waterfall draw
// ------------------------------------------------------------------
function drawFFT(powers) {
  const { w, h } = cssSize(fftCanvas);
  fftCtx.clearRect(0, 0, w, h);
  const bg = fftCtx.createLinearGradient(0, 0, 0, h);
  bg.addColorStop(0, "#0a0c10");
  bg.addColorStop(1, "#06080c");
  fftCtx.fillStyle = bg;
  fftCtx.fillRect(0, 0, w, h);
  const plot = {
    x: FFT_PADDING.left,
    y: FFT_PADDING.top,
    w: w - FFT_PADDING.left - FFT_PADDING.right,
    h: h - FFT_PADDING.top - FFT_PADDING.bottom,
  };
  fftCtx.font = AXIS_FONT;
  fftCtx.fillStyle = "#8a93a6";
  fftCtx.textBaseline = "middle";
  fftCtx.textAlign = "right";
  for (let dB = POWER_MIN; dB <= POWER_MAX; dB += 20) {
    const y = plot.y + (1 - (dB - POWER_MIN) / (POWER_MAX - POWER_MIN)) * plot.h;
    fftCtx.strokeStyle = dB === -60 ? "#1f2530" : "#171c25";
    fftCtx.lineWidth = 1;
    fftCtx.beginPath();
    fftCtx.moveTo(plot.x, y + 0.5);
    fftCtx.lineTo(plot.x + plot.w, y + 0.5);
    fftCtx.stroke();
    fftCtx.fillText(`${dB + calOffset}`, plot.x - 6, y);
  }
  fftCtx.textAlign = "left";
  fftCtx.fillText(calOffset ? "dBm" : "dB", plot.x + plot.w + 4, plot.y + 4);
  if (lastSweep) {
    fftCtx.textAlign = "center";
    fftCtx.textBaseline = "top";
    const ticks = niceTicks(lastSweep.f0, lastSweep.f1, Math.max(4, Math.floor(plot.w / 130)));
    for (const f of ticks) {
      const x = plot.x + ((f - lastSweep.f0) / (lastSweep.f1 - lastSweep.f0)) * plot.w;
      fftCtx.strokeStyle = "#171c25";
      fftCtx.beginPath();
      fftCtx.moveTo(x + 0.5, plot.y);
      fftCtx.lineTo(x + 0.5, plot.y + plot.h);
      fftCtx.stroke();
      fftCtx.fillStyle = "#8a93a6";
      fftCtx.fillText(fmtFreq(f), x, plot.y + plot.h + 6);
    }
  }
  if (powers && powers.length) {
    const samples = resampleMax(powers, Math.floor(plot.w));
    const xScale = plot.w / samples.length;
    const fill = fftCtx.createLinearGradient(0, plot.y, 0, plot.y + plot.h);
    fill.addColorStop(0, "rgba(77, 208, 225, 0.35)");
    fill.addColorStop(1, "rgba(77, 208, 225, 0.02)");
    fftCtx.fillStyle = fill;
    fftCtx.beginPath();
    fftCtx.moveTo(plot.x, plot.y + plot.h);
    for (let i = 0; i < samples.length; i++) {
      const dB = samples[i];
      const y = plot.y + (1 - (dB - POWER_MIN) / (POWER_MAX - POWER_MIN)) * plot.h;
      fftCtx.lineTo(plot.x + i * xScale, y);
    }
    fftCtx.lineTo(plot.x + plot.w, plot.y + plot.h);
    fftCtx.closePath();
    fftCtx.fill();
    fftCtx.shadowBlur = 8;
    fftCtx.shadowColor = "rgba(77, 208, 225, 0.5)";
    fftCtx.strokeStyle = "#4dd0e1";
    fftCtx.lineWidth = 1.4;
    fftCtx.lineJoin = "round";
    fftCtx.beginPath();
    for (let i = 0; i < samples.length; i++) {
      const dB = samples[i];
      const y = plot.y + (1 - (dB - POWER_MIN) / (POWER_MAX - POWER_MIN)) * plot.h;
      const x = plot.x + i * xScale;
      if (i === 0) fftCtx.moveTo(x, y); else fftCtx.lineTo(x, y);
    }
    fftCtx.stroke();
    fftCtx.shadowBlur = 0;
  }
  // max-hold / average overlay traces (drawn over the live trace)
  if (lastSweep) {
    const drawOverlay = (arr, color) => {
      if (!arr) return;
      const s = resampleMax(arr, Math.floor(plot.w));
      const xs = plot.w / s.length;
      fftCtx.strokeStyle = color;
      fftCtx.lineWidth = 1;
      fftCtx.beginPath();
      for (let i = 0; i < s.length; i++) {
        const y = plot.y + (1 - (s[i] - POWER_MIN) / (POWER_MAX - POWER_MIN)) * plot.h;
        const x = plot.x + i * xs;
        if (i === 0) fftCtx.moveTo(x, y); else fftCtx.lineTo(x, y);
      }
      fftCtx.stroke();
    };
    if (maxHoldOn) drawOverlay(maxHoldPowers, "rgba(224, 149, 77, 0.85)"); // amber
    if (avgOn) drawOverlay(avgPowers, "rgba(160, 120, 255, 0.85)");        // violet
  }
  if (cursorX >= 0 && cursorX <= w) {
    fftCtx.strokeStyle = "rgba(255, 255, 255, 0.4)";
    fftCtx.lineWidth = 1;
    fftCtx.setLineDash([3, 3]);
    fftCtx.beginPath();
    fftCtx.moveTo(cursorX + 0.5, plot.y);
    fftCtx.lineTo(cursorX + 0.5, plot.y + plot.h);
    fftCtx.stroke();
    fftCtx.setLineDash([]);
  }
  // saved frequency marks (gold vertical lines)
  if (lastSweep) {
    for (const m of marks) {
      if (m.hz < lastSweep.f0 || m.hz > lastSweep.f1) continue;
      const mx = plot.x + ((m.hz - lastSweep.f0) / (lastSweep.f1 - lastSweep.f0)) * plot.w;
      fftCtx.strokeStyle = "rgba(224, 185, 77, 0.9)";
      fftCtx.lineWidth = 1;
      fftCtx.beginPath();
      fftCtx.moveTo(mx + 0.5, plot.y);
      fftCtx.lineTo(mx + 0.5, plot.y + plot.h);
      fftCtx.stroke();
    }
  }
  if (dragStartX >= 0 && dragCurX >= 0) {
    const zx0 = Math.min(dragStartX, dragCurX);
    const zx1 = Math.max(dragStartX, dragCurX);
    fftCtx.fillStyle = "rgba(77, 208, 225, 0.15)";
    fftCtx.fillRect(zx0, plot.y, zx1 - zx0, plot.h);
    fftCtx.strokeStyle = "rgba(77, 208, 225, 0.6)";
    fftCtx.lineWidth = 1;
    fftCtx.strokeRect(zx0 + 0.5, plot.y + 0.5, zx1 - zx0, plot.h);
  }
  fftCtx.strokeStyle = "#171c25";
  fftCtx.lineWidth = 1;
  fftCtx.strokeRect(plot.x + 0.5, plot.y + 0.5, plot.w, plot.h);
}

function pushWaterfallRow(powers) {
  const { w, h } = cssSize(waterfallCanvas);
  const plot = {
    x: WF_PADDING.left,
    y: WF_PADDING.top,
    w: w - WF_PADDING.left - WF_PADDING.right,
    h: h - WF_PADDING.top - WF_PADDING.bottom,
  };
  const cw = Math.floor(plot.w);
  const ch = Math.floor(plot.h);
  if (cw <= 0 || ch <= 0) return;
  const sx = Math.floor(plot.x * DPR);
  const sy = Math.floor(plot.y * DPR);
  const sw = Math.floor(cw * DPR);
  const sh = Math.floor(ch * DPR);
  const stride = Math.max(1, Math.floor(DPR));
  const img = wfCtx.getImageData(sx, sy, sw, sh - stride);
  wfCtx.putImageData(img, sx, sy + stride);
  const samples = resampleMax(powers, cw);
  const row = wfCtx.createImageData(cw, 1);
  for (let i = 0; i < cw; i++) {
    const lutIdx = powerToLut(samples[i]);
    row.data[i * 4 + 0] = COLORMAP[lutIdx * 3];
    row.data[i * 4 + 1] = COLORMAP[lutIdx * 3 + 1];
    row.data[i * 4 + 2] = COLORMAP[lutIdx * 3 + 2];
    row.data[i * 4 + 3] = 255;
  }
  const tmp = document.createElement("canvas");
  tmp.width = cw;
  tmp.height = 1;
  tmp.getContext("2d").putImageData(row, 0, 0);
  wfCtx.drawImage(tmp, plot.x, plot.y, cw, 1);
  if (lastSweep) {
    wfCtx.clearRect(0, plot.y + plot.h, w, h - plot.y - plot.h);
    wfCtx.font = AXIS_FONT;
    wfCtx.fillStyle = "#8a93a6";
    wfCtx.textAlign = "center";
    wfCtx.textBaseline = "top";
    const ticks = niceTicks(lastSweep.f0, lastSweep.f1, Math.max(4, Math.floor(plot.w / 130)));
    for (const f of ticks) {
      const x = plot.x + ((f - lastSweep.f0) / (lastSweep.f1 - lastSweep.f0)) * plot.w;
      wfCtx.fillText(fmtFreq(f), x, plot.y + plot.h + 6);
    }
  }
}

// ------------------------------------------------------------------
// Decoded events
// ------------------------------------------------------------------
const eventListEl = document.getElementById("event-list");
const eventFilterEl = document.getElementById("event-filter");
document.getElementById("btn-clear-events").addEventListener("click", () => {
  events = [];
  renderEvents();
});
eventFilterEl.addEventListener("input", () => {
  eventFilter = eventFilterEl.value.trim().toLowerCase();
  renderEvents();
});

function eventMatchesFilter(ev) {
  if (!eventFilter) return true;
  const blob = JSON.stringify(ev).toLowerCase();
  return blob.includes(eventFilter);
}

function fmtTime(iso) {
  if (!iso) return "—";
  const t = String(iso).match(/T(\d\d:\d\d:\d\d)/);
  return t ? t[1] : String(iso).slice(-8);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function summarizeFields(ev) {
  // Skip noisy metadata, surface useful KV pairs
  const skip = new Set(["time", "model", "freq", "freq1", "freq2", "rssi", "snr", "noise", "mod"]);
  const parts = [];
  for (const [k, v] of Object.entries(ev)) {
    if (skip.has(k)) continue;
    if (v === null || v === undefined) continue;
    parts.push(`<span class="k">${escapeHtml(k)}=</span><span class="v">${escapeHtml(v)}</span>`);
    if (parts.length >= 6) break;
  }
  return parts.join("  ");
}

function renderEvents() {
  if (events.length === 0) {
    eventListEl.innerHTML = `<div class="event-empty">No events yet. Hit <strong>Start Decoder</strong> in the sidebar to begin.</div>`;
    return;
  }
  const filtered = events.filter(eventMatchesFilter);
  const rows = filtered.map(ev => {
    const time = fmtTime(ev.time);
    const model = ev.model || "unknown";
    const fields = summarizeFields(ev);
    const rssi = ev.rssi_db != null ? `${ev.rssi_db.toFixed(0)} dB` : (ev.rssi != null ? `${ev.rssi}` : "");
    return `<div class="event-row">
      <span class="event-time">${time}</span>
      <span><span class="event-model">${escapeHtml(model)}</span> <span class="event-fields">${fields}</span></span>
      <span class="event-rssi">${rssi}</span>
    </div>`;
  });
  eventListEl.innerHTML = rows.join("");
}

// ------------------------------------------------------------------
// Status / sweep rate
// ------------------------------------------------------------------
function updateSweepRate() {
  const now = performance.now();
  sweepTimestamps.push(now);
  sweepTimestamps = sweepTimestamps.filter(t => now - t < 3000);
  const rate = sweepTimestamps.length / 3.0;
  sweepRateEl.textContent = `${rate.toFixed(1)} Hz`;
}

function refreshStatusUI() {
  const running = serverMode !== "idle";
  statusDot.classList.toggle("running", running);
  statusDot.classList.toggle("stopped", !running);
  if (serverMode === "sweep") statusText.textContent = "Sweeping";
  else if (serverMode === "decode") statusText.textContent = "Decoding";
  else if (serverMode === "capture") statusText.textContent = "Recording";
  else if (serverMode === "adsb") statusText.textContent = "Tracking";
  else if (serverMode === "scan") statusText.textContent = "Auto-Scanning";
  else if (serverMode === "scan_radio") statusText.textContent = "Scanning";
  else if (serverMode === "replay") statusText.textContent = "Replaying";
  else statusText.textContent = "Idle";
  if (serverMode !== "sweep") sweepRateEl.textContent = "0.0 Hz";
  const scanBtn = document.getElementById("btn-scan-radio");
  if (scanBtn) {
    scanBtn.textContent = serverMode === "scan_radio" ? "■ Stop scan" : "⤬ Scan marks";
    scanBtn.classList.toggle("active", serverMode === "scan_radio");
  }

  // Light up the tab whose mode is actually running, even when the user
  // is viewing a different tab.
  document.querySelectorAll(".mode-tab").forEach(t => {
    t.classList.toggle("is-running", t.dataset.mode === serverMode);
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
    const live = serverMode === mode;
    start.disabled = live;       // can't start what's already running
    stop.disabled = !live;       // nothing to stop when idle
    stop.classList.toggle("live", live);
  }
}

// ------------------------------------------------------------------
// Socket events
// ------------------------------------------------------------------
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

// ---- GPS geotagging pill (opt-in, default off) -------------------------
const gpsPill = document.getElementById("gps-pill");
const gpsDot = document.getElementById("gps-dot");
const gpsText = document.getElementById("gps-text");
let gpsState = { enabled: false };

function renderGps(s) {
  gpsState = s || { enabled: false };
  gpsPill.classList.remove("connected", "disconnected", "probing");
  gpsDot.classList.remove("running", "stopped", "warn");
  if (!gpsState.enabled) {
    gpsDot.classList.add("stopped");
    gpsText.textContent = "GPS off";
    gpsPill.title = "GPS geotagging is off. Captures are not stamped with your location. Click to enable.";
  } else if (gpsState.lat != null && !gpsState.stale) {
    const fix = gpsState.mode >= 3 ? "3D" : "2D";
    const sat = gpsState.sats != null ? ` · ${gpsState.sats} sat` : "";
    gpsPill.classList.add("connected");
    gpsDot.classList.add("running");
    gpsText.textContent = `GPS ${fix}${sat}`;
    // Coords masked on screen; revealed only on hover, leading with "ON".
    const alt = gpsState.alt != null ? ` · ${gpsState.alt.toFixed(0)} m` : "";
    const hd = gpsState.hdop != null ? `\nhdop ${gpsState.hdop}` : "";
    gpsPill.title = `Geotagging ON — captures are stamped with your location.\n` +
      `${gpsState.lat.toFixed(6)}, ${gpsState.lon.toFixed(6)}${alt}${hd}\nClick to disable.`;
  } else {
    gpsDot.classList.add("warn");
    gpsText.textContent = "GPS no fix";
    gpsPill.title = "Geotagging ON but no GPS fix yet. Click to disable.";
  }
  updateGpsMarker();
}

socket.on("gps_status", renderGps);
gpsPill.addEventListener("click", () => {
  socket.emit("set_setting", { key: "gps_enabled", value: !gpsState.enabled });
});

const gpsPrecisionEl = document.getElementById("gps-precision");
if (gpsPrecisionEl) {
  gpsPrecisionEl.addEventListener("change", () => {
    socket.emit("set_setting", { key: "gps_precision", value: gpsPrecisionEl.value });
  });
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

// Persisted UI settings are applied once, on the first status event that
// carries them. Reconnects re-send status, so this flag keeps the restore from
// yanking the user's current pane (setMode) or re-applying values mid-session.
let _settingsRestored = false;

socket.on("status", (s) => {
  serverMode = s.mode || "idle";
  if (s.sweep_config) applySweepConfigToInputs(s.sweep_config);
  if (s.decode_config) applyDecodeConfigToInputs(s.decode_config);
  if (s.capture_config) applyCaptureConfigToInputs(s.capture_config);
  if (s.adsb_config) applyAdsbConfigToInputs(s.adsb_config);
  if (s.scan_config) applyScanConfigToInputs(s.scan_config);
  if (s.radio_config) applyRadioConfigToInputs(s.radio_config);
  if (Array.isArray(s.bookmarks)) { bookmarks = s.bookmarks; renderBookmarks(); }

  // Restore persisted UI settings once on the first status event.
  if (!_settingsRestored && s.settings) {
    _settingsRestored = true;
    const st = s.settings;
    const KNOWN_MODES = ["sweep", "decode", "capture", "adsb", "scan", "radio"];
    if (KNOWN_MODES.includes(st.last_mode)) setMode(st.last_mode);
    if (Number.isFinite(st.last_radio_freq)) radioFreqEl.value = st.last_radio_freq;
    if (st.last_demod) setRadioDemod(st.last_demod);
    if (typeof st.radio_volume === "number") {
      radioVolEl.value = st.radio_volume;
      radioVolVal.textContent = `${st.radio_volume}%`;
      updateSliderFills(["radio_vol"]);
      if (radioGain) radioGain.gain.value = radioVolume();
    }
    if (st.gps_precision) {
      const gpe = document.getElementById("gps-precision");
      if (gpe) gpe.value = st.gps_precision;
    }
  }

  // Visible-feedback when the backend transitions out of a running mode:
  // wipe whatever the previous mode was painting so the user can SEE
  // that Stop (or mode switch) actually took effect.
  if (prevServerMode !== "idle" && serverMode !== prevServerMode) {
    onServerLeftMode(prevServerMode);
  }
  prevServerMode = serverMode;

  refreshStatusUI();
});

function onServerLeftMode(prevMode) {
  if (prevMode === "sweep")   clearSweepVisuals();
  if (prevMode === "adsb")    clearAdsbVisuals();
  if (prevMode === "radio")   stopRadioPlayback();
  if (prevMode === "scan_radio") stopRadioPlayback();
  // capture/scan have their own dedicated "done" / "stopped" events
}

function clearAdsbVisuals() {
  if (adsbMap) {
    for (const m of adsbMarkers.values()) adsbMap.removeLayer(m);
    adsbMarkers.clear();
    for (const tr of adsbTrails.values()) adsbMap.removeLayer(tr);
    adsbTrails.clear();
    adsbTracks.clear();
  }
  adsbAircraft = [];
  const statsEl = document.getElementById("adsb-stats");
  if (statsEl) statsEl.textContent = "0 tracked · 0 with position";
  if (currentMode === "adsb") renderAircraftList();
}

// Peak detection mirrors the Auto-Scan approach: median noise floor, a
// threshold above it, then group adjacent above-threshold bins into peaks.
function findPeaks(powers, f0, f1, thresholdDb = 10, maxPeaks = 12) {
  const n = powers.length;
  if (!n) return [];
  const floor = Array.prototype.slice.call(powers).sort((a, b) => a - b)[Math.floor(n / 2)];
  const thr = floor + thresholdDb;
  const groups = [];
  for (let i = 0; i < n; i++) {
    if (powers[i] <= thr) continue;
    const f = f0 + (i / (n - 1)) * (f1 - f0);
    const last = groups[groups.length - 1];
    if (last && i - last._i <= 2) {
      if (powers[i] > last.power) { last.power = powers[i]; last.hz = f; }
      last._i = i;
    } else {
      groups.push({ hz: f, power: powers[i], _i: i });
    }
  }
  for (const g of groups) g.snr = g.power - floor;
  groups.sort((a, b) => b.power - a.power);
  return groups.slice(0, maxPeaks);
}

function updatePeaks() {
  if (!lastSweep) return;
  lastPeaks = findPeaks(lastSweep.powers, lastSweep.f0, lastSweep.f1);
  renderPeakTable(lastPeaks);
}

function renderPeakTable(peaks) {
  const el = document.getElementById("peaks-list");
  if (!el) return;
  el.replaceChildren();
  if (!peaks.length) {
    const empty = document.createElement("div");
    empty.className = "event-empty";
    empty.textContent = "No peaks above the noise floor.";
    el.appendChild(empty);
    return;
  }
  peaks.forEach((p) => {
    const row = document.createElement("div");
    row.className = "peak-row";
    const f = document.createElement("span");
    f.className = "peak-freq";
    f.textContent = fmtFreq(p.hz);
    const lvl = document.createElement("span");
    lvl.className = "peak-lvl";
    lvl.textContent = `${displayPow(p.power).toFixed(0)} ${powUnit()} · +${p.snr.toFixed(0)}`;
    const mk = document.createElement("button");
    mk.className = "btn ghost small";
    mk.textContent = "Mark";
    mk.addEventListener("click", () => addMark(p.hz));
    row.append(f, lvl, mk);
    el.appendChild(row);
  });
}

document.getElementById("btn-mark-peak").addEventListener("click", () => {
  if (lastPeaks.length) addMark(lastPeaks[0].hz);
});

function updateHoldTraces(p) {
  if (maxHoldOn) {
    if (!maxHoldPowers || maxHoldPowers.length !== p.length) maxHoldPowers = Float32Array.from(p);
    else for (let i = 0; i < p.length; i++) { if (p[i] > maxHoldPowers[i]) maxHoldPowers[i] = p[i]; }
  }
  if (avgOn) {
    if (!avgPowers || avgPowers.length !== p.length) avgPowers = Float32Array.from(p);
    else { const a = 0.2; for (let i = 0; i < p.length; i++) avgPowers[i] = avgPowers[i] * (1 - a) + p[i] * a; }
  }
}

document.getElementById("btn-maxhold").addEventListener("click", (e) => {
  maxHoldOn = !maxHoldOn;
  maxHoldPowers = null;
  e.currentTarget.classList.toggle("active", maxHoldOn);
  if (lastSweep) drawFFT(lastSweep.powers);
});
document.getElementById("btn-avg").addEventListener("click", (e) => {
  avgOn = !avgOn;
  avgPowers = null;
  e.currentTarget.classList.toggle("active", avgOn);
  if (lastSweep) drawFFT(lastSweep.powers);
});
document.getElementById("btn-trace-clear").addEventListener("click", () => {
  maxHoldPowers = null;
  avgPowers = null;
  if (lastSweep) drawFFT(lastSweep.powers);
});

// Power calibration: a user dB offset so the axis can read approximate dBm
// (the HackRF is uncalibrated; this is a relative reference shift).
function loadCalOffset() {
  const v = parseFloat(localStorage.getItem("aetherscope.calOffset"));
  return Number.isFinite(v) ? v : 0;
}
function displayPow(db) { return db + calOffset; }
function powUnit() { return calOffset ? "dBm" : "dBFS"; }
const calOffsetEl = document.getElementById("cal_offset");
if (calOffsetEl) {
  calOffsetEl.value = calOffset;
  calOffsetEl.addEventListener("input", () => {
    const v = parseFloat(calOffsetEl.value);
    calOffset = Number.isFinite(v) ? v : 0;
    try { localStorage.setItem("aetherscope.calOffset", String(calOffset)); } catch (e) { /* ignore */ }
    if (lastSweep) drawFFT(lastSweep.powers);
    renderPeakTable(lastPeaks);
    renderMarks();
  });
}

socket.on("sweep", (msg) => {
  const rangeChanged = !lastSweep
    || Math.abs(lastSweep.f0 - msg.f0) > 1
    || Math.abs(lastSweep.f1 - msg.f1) > 1;
  if (rangeChanged && currentMode === "sweep") {
    const wr = waterfallCanvas.getBoundingClientRect();
    wfCtx.clearRect(0, 0, wr.width, wr.height);
  }
  lastSweep = { f0: msg.f0, f1: msg.f1, powers: msg.powers };
  if (rangeChanged) { maxHoldPowers = null; avgPowers = null; }
  updateHoldTraces(msg.powers);
  if (currentMode === "sweep") {
    drawFFT(msg.powers);
    pushWaterfallRow(msg.powers);
    const nowp = performance.now();
    if (nowp - lastPeakRender > 400) { lastPeakRender = nowp; updatePeaks(); }
  }
  // Prefer the backend's true rate (it counts every sweep before
  // rate-limiting); fall back to local timestamps if absent.
  if (typeof msg.rate_hz === "number") {
    sweepRateEl.textContent = `${msg.rate_hz.toFixed(1)} Hz`;
  } else {
    updateSweepRate();
  }
});

socket.on("decoded", (ev) => {
  events.unshift(ev);
  if (events.length > MAX_EVENTS) events.length = MAX_EVENTS;
  eventCountEl.textContent = String(events.length);
  if (currentMode === "decode") renderEvents();
});

// ------------------------------------------------------------------
// Config <-> input wiring
// ------------------------------------------------------------------
function applySweepConfigToInputs(c) {
  document.getElementById("f_start").value = c.f_start_mhz;
  document.getElementById("f_stop").value  = c.f_stop_mhz;
  document.getElementById("bin_width").value = c.bin_width_hz;
  document.getElementById("lna").value  = c.lna_gain;
  document.getElementById("vga").value  = c.vga_gain;
  document.getElementById("amp").checked = !!c.amp_enable;
  updateSliderFills(["lna", "vga"]);
  document.getElementById("lna_val").textContent = `${c.lna_gain} dB`;
  document.getElementById("vga_val").textContent = `${c.vga_gain} dB`;
  highlightPreset(c);
}
function readSweepConfig() {
  return {
    f_start_mhz: parseInt(document.getElementById("f_start").value, 10),
    f_stop_mhz:  parseInt(document.getElementById("f_stop").value, 10),
    bin_width_hz: parseInt(document.getElementById("bin_width").value, 10),
    lna_gain: parseInt(document.getElementById("lna").value, 10),
    vga_gain: parseInt(document.getElementById("vga").value, 10),
    amp_enable: document.getElementById("amp").checked,
  };
}

function applyDecodeConfigToInputs(c) {
  document.getElementById("dec_freq").value = (c.freq_hz / 1e6).toFixed(2);
  document.getElementById("dec_rate").value = (c.sample_rate / 1e6).toFixed(1);
  document.getElementById("dec_gain").value = c.gain_db;
  document.getElementById("dec_gain_val").textContent = `${c.gain_db} dB`;
  updateSliderFills(["dec_gain"]);
  highlightBandButton(c.freq_hz / 1e6);
}
function readDecodeConfig() {
  const freqMhz = parseFloat(document.getElementById("dec_freq").value);
  const rateMhz = parseFloat(document.getElementById("dec_rate").value);
  return {
    freq_hz: Math.round(freqMhz * 1e6),
    sample_rate: Math.round(rateMhz * 1e6),
    gain_db: parseInt(document.getElementById("dec_gain").value, 10),
  };
}

function updateSliderFills(ids) {
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    const pct = ((el.value - el.min) / (el.max - el.min)) * 100;
    el.style.setProperty("--fill", `${pct}%`);
  }
}

function highlightPreset(c) {
  document.querySelectorAll(".chip").forEach(b => {
    const match =
      parseInt(b.dataset.f0, 10) === c.f_start_mhz &&
      parseInt(b.dataset.f1, 10) === c.f_stop_mhz;
    b.classList.toggle("active", match);
  });
}
function highlightBandButton(freqMhz) {
  document.querySelectorAll(".band-btn").forEach(b => {
    b.classList.toggle("active", Math.abs(parseFloat(b.dataset.freq) - freqMhz) < 0.01);
  });
}

function applyGainsToSweepUI(lna, vga, amp) {
  document.getElementById("lna").value = lna;
  document.getElementById("vga").value = vga;
  document.getElementById("amp").checked = !!amp;
  document.getElementById("lna_val").textContent = `${lna} dB`;
  document.getElementById("vga_val").textContent = `${vga} dB`;
  updateSliderFills(["lna", "vga"]);
}

function applyGainsToCaptureUI(lna, vga, amp) {
  document.getElementById("cap_lna").value = lna;
  document.getElementById("cap_vga").value = vga;
  document.getElementById("cap_amp").checked = !!amp;
  document.getElementById("cap_lna_val").textContent = `${lna} dB`;
  document.getElementById("cap_vga_val").textContent = `${vga} dB`;
  updateSliderFills(["cap_lna", "cap_vga"]);
}

// preset chips (sweep mode) — no debounce: the backend state lock
// serializes rapid clicks safely, and any frontend delay just feels
// sluggish for a single click.
document.querySelectorAll(".chip").forEach(b => {
  b.addEventListener("click", () => {
    document.getElementById("f_start").value = b.dataset.f0;
    document.getElementById("f_stop").value  = b.dataset.f1;
    document.getElementById("bin_width").value = b.dataset.bin;
    if (b.dataset.lna != null) {
      applyGainsToSweepUI(
        parseInt(b.dataset.lna, 10),
        parseInt(b.dataset.vga, 10),
        parseInt(b.dataset.amp || "0", 10) === 1,
      );
    }
    highlightPreset(readSweepConfig());
    // Clear the spectrum/waterfall immediately so the user can SEE the
    // switch happened instead of staring at old data for ~50ms.
    clearSweepVisuals();
    socket.emit("start_sweep", readSweepConfig());
  });
});

function clearSweepVisuals() {
  lastSweep = null;
  maxHoldPowers = null;
  avgPowers = null;
  lastPeaks = [];
  renderPeakTable([]);
  const fr = fftCanvas.getBoundingClientRect();
  fftCtx.clearRect(0, 0, fr.width, fr.height);
  const wr = waterfallCanvas.getBoundingClientRect();
  wfCtx.clearRect(0, 0, wr.width, wr.height);
  hoverFreqEl.textContent  = "— MHz";
  hoverPowerEl.textContent = "— dB";
}

// band buttons (decode mode)
document.querySelectorAll(".band-btn").forEach(b => {
  b.addEventListener("click", () => {
    document.getElementById("dec_freq").value = b.dataset.freq;
    if (b.dataset.gain != null) {
      const g = parseInt(b.dataset.gain, 10);
      document.getElementById("dec_gain").value = g;
      document.getElementById("dec_gain_val").textContent = `${g} dB`;
      updateSliderFills(["dec_gain"]);
    }
    highlightBandButton(parseFloat(b.dataset.freq));
  });
});

// buttons
// Unmistakable click flash — proves the click reached JS even if the
// network or backend is slow. If you click and DON'T see this flash,
// the click never reached the page at all (browser extension blocking
// it, missed the button, etc).
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

document.getElementById("btn-start-sweep").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "start_sweep");
  socket.emit("start_sweep", readSweepConfig());
});
document.getElementById("btn-stop-sweep").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "stop (sweep)");
  socket.emit("stop");
});
document.getElementById("btn-start-decode").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "start_decode");
  socket.emit("start_decode", readDecodeConfig());
});
document.getElementById("btn-stop-decode").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "stop (decode)");
  socket.emit("stop");
});

// slider live readouts
document.getElementById("lna").addEventListener("input", e => {
  document.getElementById("lna_val").textContent = `${e.target.value} dB`;
  updateSliderFills(["lna"]);
});
document.getElementById("vga").addEventListener("input", e => {
  document.getElementById("vga_val").textContent = `${e.target.value} dB`;
  updateSliderFills(["vga"]);
});
document.getElementById("dec_gain").addEventListener("input", e => {
  document.getElementById("dec_gain_val").textContent = `${e.target.value} dB`;
  updateSliderFills(["dec_gain"]);
});
updateSliderFills(["lna", "vga", "dec_gain"]);

// hover
function handleHover(e) {
  if (!lastSweep || currentMode !== "sweep") return;
  const rect = fftCanvas.getBoundingClientRect();
  cursorX = e.clientX - rect.left;
  const plotLeft = FFT_PADDING.left;
  const plotW = rect.width - FFT_PADDING.left - FFT_PADDING.right;
  const t = Math.max(0, Math.min(1, (cursorX - plotLeft) / plotW));
  const freqHz = lastSweep.f0 + t * (lastSweep.f1 - lastSweep.f0);
  const idx = Math.floor(t * lastSweep.powers.length);
  const dB = lastSweep.powers[Math.max(0, Math.min(lastSweep.powers.length - 1, idx))];
  hoverFreqEl.textContent  = fmtFreq(freqHz);
  hoverPowerEl.textContent = `${displayPow(dB).toFixed(1)} ${powUnit()}`;
  hoverTag.textContent = fmtFreq(freqHz);
  hoverTag.style.left = `${e.clientX + 12}px`;
  hoverTag.style.top  = `${e.clientY + 14}px`;
  hoverTag.hidden = false;
  drawFFT(lastSweep.powers);
}
function endHover(redraw) {
  cursorX = -1;
  hoverFreqEl.textContent  = "— MHz";
  hoverPowerEl.textContent = "— dB";
  hoverTag.hidden = true;
  if (redraw && lastSweep) drawFFT(lastSweep.powers);
}
fftCanvas.addEventListener("mousemove", handleHover);
fftCanvas.addEventListener("mouseleave", () => endHover(true));
waterfallCanvas.addEventListener("mousemove", handleHover);
waterfallCanvas.addEventListener("mouseleave", () => endHover(false));

// ------------------------------------------------------------------
// Marked frequencies: click the spectrum to save, then Listen
// ------------------------------------------------------------------
function freqAtCursor(e) {
  const rect = e.currentTarget.getBoundingClientRect();
  const plotW = rect.width - FFT_PADDING.left - FFT_PADDING.right;
  const t = Math.max(0, Math.min(1, (e.clientX - rect.left - FFT_PADDING.left) / plotW));
  return lastSweep.f0 + t * (lastSweep.f1 - lastSweep.f0);
}
function addMark(hz) {
  if (marks.some(m => Math.abs(m.hz - hz) < 50_000)) return; // dedupe within 50 kHz
  let power = null;
  if (lastSweep) {
    const n = lastSweep.powers.length;
    const idx = Math.max(0, Math.min(n - 1,
      Math.round((hz - lastSweep.f0) / (lastSweep.f1 - lastSweep.f0) * (n - 1))));
    power = lastSweep.powers[idx];
  }
  marks.push({ hz, power });
  marks.sort((a, b) => a.hz - b.hz);
  saveMarks();
  renderMarks();
  if (lastSweep) drawFFT(lastSweep.powers);
}
function onSpectrumClick(e) {
  if (suppressClick) { suppressClick = false; return; }
  if (!lastSweep || currentMode !== "sweep") return;
  addMark(freqAtCursor(e));
}
fftCanvas.addEventListener("click", onSpectrumClick);
waterfallCanvas.addEventListener("click", onSpectrumClick);

// Click-drag on the spectrum to zoom into a span; double-click resets.
function fftXToFreq(xPx) {
  const rect = fftCanvas.getBoundingClientRect();
  const plotW = rect.width - FFT_PADDING.left - FFT_PADDING.right;
  const t = Math.max(0, Math.min(1, (xPx - FFT_PADDING.left) / plotW));
  return lastSweep.f0 + t * (lastSweep.f1 - lastSweep.f0);
}
fftCanvas.addEventListener("mousedown", (e) => {
  if (!lastSweep || currentMode !== "sweep") return;
  const rect = fftCanvas.getBoundingClientRect();
  dragStartX = e.clientX - rect.left;
  dragCurX = dragStartX;
});
fftCanvas.addEventListener("mousemove", (e) => {
  if (dragStartX < 0) return;
  const rect = fftCanvas.getBoundingClientRect();
  dragCurX = e.clientX - rect.left;
  if (lastSweep) drawFFT(lastSweep.powers);
});
window.addEventListener("mouseup", () => {
  if (dragStartX < 0) return;
  const x0 = Math.min(dragStartX, dragCurX);
  const x1 = Math.max(dragStartX, dragCurX);
  const dragged = (x1 - x0) > 8;
  if (dragged && lastSweep) {
    const f0 = fftXToFreq(x0) / 1e6;
    const f1 = fftXToFreq(x1) / 1e6;
    dragStartX = -1; dragCurX = -1;
    if (f1 - f0 >= 0.05) {
      // hackrf_sweep tunes in ~20 MHz steps, so a narrower span comes back as
      // a 20 MHz window. Floor the zoom to 20 MHz centered on the selection so
      // the requested range matches what the hardware actually returns.
      let z0 = f0, z1 = f1;
      if (z1 - z0 < 20) { const mid = (z0 + z1) / 2; z0 = Math.max(1, mid - 10); z1 = z0 + 20; }
      prevRange = { f0: lastSweep.f0 / 1e6, f1: lastSweep.f1 / 1e6 };
      suppressClick = true;
      document.getElementById("f_start").value = z0.toFixed(3);
      document.getElementById("f_stop").value = z1.toFixed(3);
      socket.emit("start_sweep", readSweepConfig());
      return;
    }
  }
  dragStartX = -1; dragCurX = -1;
  if (lastSweep) drawFFT(lastSweep.powers);
});
fftCanvas.addEventListener("dblclick", () => {
  if (!prevRange) return;
  document.getElementById("f_start").value = prevRange.f0.toFixed(3);
  document.getElementById("f_stop").value = prevRange.f1.toFixed(3);
  prevRange = null;
  socket.emit("start_sweep", readSweepConfig());
});

function renderMarks() {
  const el = document.getElementById("marks-list");
  const deltaEl = document.getElementById("marks-delta");
  if (!el) return;
  el.replaceChildren();
  if (deltaEl) {
    if (marks.length >= 2) {
      const hzs = marks.map(m => m.hz);
      const span = (Math.max(...hzs) - Math.min(...hzs)) / 1e6;
      const pows = marks.map(m => m.power).filter(p => p != null);
      let txt = `Span Δf ${span.toFixed(3)} MHz`;
      if (pows.length >= 2) txt += `  ·  ΔdB ${(Math.max(...pows) - Math.min(...pows)).toFixed(0)}`;
      deltaEl.textContent = txt;
      deltaEl.hidden = false;
    } else {
      deltaEl.hidden = true;
    }
  }
  if (!marks.length) {
    const empty = document.createElement("div");
    empty.className = "event-empty";
    empty.textContent = "Click the spectrum to mark a frequency.";
    el.appendChild(empty);
    return;
  }
  marks.forEach((m, i) => {
    const row = document.createElement("div");
    row.className = "mark-row";

    const freq = document.createElement("span");
    freq.className = "mark-freq";
    freq.textContent = (m.power != null) ? `${fmtFreq(m.hz)}  ${displayPow(m.power).toFixed(0)} ${powUnit()}` : fmtFreq(m.hz);

    const tune = document.createElement("button");
    tune.className = "btn ghost small";
    tune.textContent = "Listen";
    tune.addEventListener("click", () => tuneToMark(m.hz));

    const save = document.createElement("button");
    save.className = "btn ghost small mark-save";
    save.title = "Save as bookmark";
    save.textContent = "★";
    save.addEventListener("click", () => {
      const mhz = m.hz / 1e6;
      const label = `${mhz.toFixed(3)} MHz`;
      socket.emit("add_bookmark", {
        freq_hz: Math.round(m.hz),
        demod: null,
        label,
        source: "mark",
      });
    });

    const del = document.createElement("button");
    del.className = "btn ghost small mark-del";
    del.title = "Remove";
    del.textContent = "×";
    del.addEventListener("click", () => {
      marks.splice(i, 1);
      saveMarks();
      renderMarks();
      if (lastSweep) drawFFT(lastSweep.powers);
    });

    row.append(freq, tune, save, del);
    el.appendChild(row);
  });
}

async function tuneToMark(hz) {
  const mhz = hz / 1e6;
  setMode("radio");
  radioFreqEl.value = mhz.toFixed(1);
  // FM broadcast -> wideband; airband -> AM; everything else (land mobile,
  // ham, GMRS, business) is most likely narrowband FM.
  let demod = "nfm";
  if (mhz >= 88 && mhz <= 108) demod = "fm";
  else if (mhz >= 108 && mhz < 137) demod = "am";
  setRadioDemod(demod);
  try {
    await ensureRadioAudio();
  } catch (err) {
    console.error("[aetherscope] audio init failed", err);
    return;
  }
  radioNode.port.postMessage({ type: "flush" });
  radioPlaying = true;
  const freq = parseFloat(radioFreqEl.value);
  socket.emit("start_radio", { demod: radioDemod, freq_mhz: freq });
  setRadioNow("Tuning…", freq, radioDemod);
}

// ------------------------------------------------------------------
// Capture mode
// ------------------------------------------------------------------
const capActiveEl   = document.getElementById("cap-active");
const capNameEl     = document.getElementById("cap-active-name");
const capDetailEl   = document.getElementById("cap-active-detail");
const capBarEl      = document.getElementById("cap-progress-bar");
const capBytesEl    = document.getElementById("cap-bytes");
const capPctEl      = document.getElementById("cap-pct");
const capEtaEl      = document.getElementById("cap-eta");
const capturesListEl = document.getElementById("captures-list");
const capFilterEl   = document.getElementById("cap-filter");

let captures = [];
let capFilter = "";
let capAudio = null;

function playCapture(name) {
  if (capAudio) { try { capAudio.pause(); } catch (e) { /* ignore */ } }
  capAudio = new Audio("/captures/" + encodeURIComponent(name));
  capAudio.play().catch(() => showToast("error", "Could not play audio."));
}

capFilterEl.addEventListener("input", () => {
  capFilter = capFilterEl.value.trim().toLowerCase();
  renderCaptures();
});

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
function fmtMHz(hz) { return `${(hz / 1e6).toFixed(3)} MHz`; }
function fmtMSps(hz) { return `${(hz / 1e6).toFixed(1)} MSPS`; }
function fmtAgo(ts) {
  const dt = Date.now() / 1000 - ts;
  if (dt < 60) return `${Math.round(dt)}s ago`;
  if (dt < 3600) return `${Math.round(dt / 60)}m ago`;
  if (dt < 86400) return `${Math.round(dt / 3600)}h ago`;
  return `${Math.round(dt / 86400)}d ago`;
}

function renderCaptures() {
  if (!captures.length) {
    capturesListEl.innerHTML = `<div class="event-empty">No captures yet. Configure in the sidebar and hit Record.</div>`;
    return;
  }
  const filtered = captures.filter(c => {
    if (!capFilter) return true;
    const blob = `${c.name} ${c.label} ${c.user_label || ""} ${c.freq_hz}`.toLowerCase();
    return blob.includes(capFilter);
  });
  capturesListEl.innerHTML = filtered.map(c => {
    const displayName = escapeHtml(c.user_label || c.label || c.name);
    const tags = Array.isArray(c.tags) ? c.tags : [];
    const tagsHtml = tags.length
      ? tags.map(t => `<span class="bm-tag">${escapeHtml(t)}</span>`).join(" ")
      : "";
    const missingBadge = c.missing ? `<span class="cap-missing-badge">missing</span>` : "";
    const geo = c.geolocation;
    const hasGeo = geo && typeof geo.lat === "number" && typeof geo.lon === "number";
    const geoBadge = hasGeo
      ? `<span class="cap-geo-badge" title="lat ${geo.lat.toFixed(6)}, lon ${geo.lon.toFixed(6)}">located</span>`
      : (c.geolocation_redacted ? `<span class="cap-geo-badge redacted">location removed</span>` : "");
    const redactBtn = hasGeo ? `<button data-redact="${escapeHtml(c.name)}">Remove location</button>` : "";
    const isAudio = c.kind === "audio";
    const kindBadge = isAudio ? `<span class="cap-kind-badge">audio</span>` : "";
    const sigmfName = (!isAudio && c.name.endsWith(".iq")) ? c.name.slice(0, -3) + ".sigmf-meta" : "";
    const sigmfLink = (c.sigmf && sigmfName)
      ? `<a href="/captures/${encodeURIComponent(sigmfName)}" download title="SigMF metadata (portable to other SDR tools)">SigMF</a>`
      : "";
    const dur = (c.duration_s || 0).toFixed(1);
    const detail = isAudio
      ? `${fmtMHz(c.freq_hz)}<span class="sep">·</span>${escapeHtml((c.demod || "").toUpperCase())}<span class="sep">·</span>${dur}s<span class="sep">·</span>${fmtBytes(c.file_size)}`
      : `${fmtMHz(c.freq_hz)}<span class="sep">·</span>${fmtMSps(c.sample_rate)}<span class="sep">·</span>${dur}s<span class="sep">·</span>${fmtBytes(c.file_size)}`;
    const metaTail = isAudio
      ? (c.audio_rate ? `audio ${Math.round(c.audio_rate / 1000)} kHz` : "audio")
      : escapeHtml(c.sample_format || "");
    const playOrReplay = isAudio
      ? `<button data-play="${escapeHtml(c.name)}">Play</button>`
      : `<button data-replay="${escapeHtml(c.name)}">Replay</button> <button data-listen="${escapeHtml(c.name)}">Listen</button>`;
    return `
    <div class="cap-row">
      <div class="cap-main">
        <div class="cap-name">${displayName}${kindBadge}${missingBadge}${geoBadge}</div>
        ${tagsHtml ? `<div class="cap-tags">${tagsHtml}</div>` : ""}
        <div class="cap-detail">${detail}</div>
        <div class="cap-meta">${escapeHtml(c.name)} · ${fmtAgo(c.started_at)} · ${metaTail}</div>
      </div>
      <div class="cap-actions">
        <a href="/captures/${encodeURIComponent(c.name)}" download>Download</a>
        ${sigmfLink}
        <button data-edit="${escapeHtml(c.name)}">Edit</button>
        ${playOrReplay}
        ${redactBtn}
        <button class="danger" data-delete="${escapeHtml(c.name)}">Delete</button>
      </div>
    </div>
  `;
  }).join("");

  capturesListEl.querySelectorAll("[data-edit]").forEach(btn => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.edit;
      const item = captures.find(c => c.name === name);
      const newLabel = prompt("Label:", item ? (item.user_label || item.label || "") : "");
      if (newLabel === null) return;
      const newNotes = prompt("Notes:", item ? (item.notes || "") : "");
      if (newNotes === null) return;
      const rawTags = prompt("Tags (comma-separated):", item && Array.isArray(item.tags) ? item.tags.join(", ") : "");
      if (rawTags === null) return;
      const tags = rawTags.split(",").map(t => t.trim()).filter(Boolean);
      socket.emit("update_capture", {
        filename: name,
        user_label: newLabel.trim(),
        notes: newNotes.trim(),
        tags,
      });
    });
  });

  capturesListEl.querySelectorAll("[data-delete]").forEach(btn => {
    btn.addEventListener("click", () => {
      if (confirm(`Delete ${btn.dataset.delete}?`)) {
        socket.emit("delete_capture", { name: btn.dataset.delete });
      }
    });
  });
  capturesListEl.querySelectorAll("[data-replay]").forEach(btn => {
    btn.addEventListener("click", () => {
      socket.emit("start_replay", { name: btn.dataset.replay });
      setMode("sweep");   // watch the recorded spectrogram in the sweep view
    });
  });
  capturesListEl.querySelectorAll("[data-play]").forEach(btn => {
    btn.addEventListener("click", () => playCapture(btn.dataset.play));
  });
  capturesListEl.querySelectorAll("[data-listen]").forEach(btn => {
    btn.addEventListener("click", () => startIqPlay(btn.dataset.listen, "fm", 0));
  });
  capturesListEl.querySelectorAll("[data-redact]").forEach(btn => {
    btn.addEventListener("click", () => {
      if (confirm(`Remove GPS location from ${btn.dataset.redact}?`)) {
        socket.emit("redact_capture", { name: btn.dataset.redact });
      }
    });
  });
}

function readCaptureConfig() {
  return {
    freq_hz: Math.round(parseFloat(document.getElementById("cap_freq").value) * 1e6),
    sample_rate: Math.round(parseFloat(document.getElementById("cap_rate").value) * 1e6),
    duration_s: parseFloat(document.getElementById("cap_duration").value),
    lna_gain: parseInt(document.getElementById("cap_lna").value, 10),
    vga_gain: parseInt(document.getElementById("cap_vga").value, 10),
    amp_enable: document.getElementById("cap_amp").checked,
    label: document.getElementById("cap_label").value,
  };
}

function applyCaptureConfigToInputs(c) {
  document.getElementById("cap_freq").value = (c.freq_hz / 1e6).toFixed(2);
  document.getElementById("cap_rate").value = (c.sample_rate / 1e6).toFixed(0);
  document.getElementById("cap_duration").value = c.duration_s;
  document.getElementById("cap_lna").value = c.lna_gain;
  document.getElementById("cap_vga").value = c.vga_gain;
  document.getElementById("cap_amp").checked = !!c.amp_enable;
  document.getElementById("cap_lna_val").textContent = `${c.lna_gain} dB`;
  document.getElementById("cap_vga_val").textContent = `${c.vga_gain} dB`;
  if (c.label) document.getElementById("cap_label").value = c.label;
  updateSliderFills(["cap_lna", "cap_vga"]);
}

document.querySelectorAll(".band-btn-cap").forEach(b => {
  b.addEventListener("click", () => {
    document.getElementById("cap_freq").value = b.dataset.freq;
    if (b.dataset.lna != null) {
      applyGainsToCaptureUI(
        parseInt(b.dataset.lna, 10),
        parseInt(b.dataset.vga, 10),
        parseInt(b.dataset.amp || "0", 10) === 1,
      );
    }
    document.querySelectorAll(".band-btn-cap").forEach(x => x.classList.toggle("active", x === b));
  });
});

document.getElementById("btn-start-capture").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "start_capture");
  socket.emit("start_capture", readCaptureConfig());
});
document.getElementById("btn-cancel-capture").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "cancel_capture");
  socket.emit("cancel_capture");
});

document.getElementById("cap_lna").addEventListener("input", e => {
  document.getElementById("cap_lna_val").textContent = `${e.target.value} dB`;
  updateSliderFills(["cap_lna"]);
});
document.getElementById("cap_vga").addEventListener("input", e => {
  document.getElementById("cap_vga_val").textContent = `${e.target.value} dB`;
  updateSliderFills(["cap_vga"]);
});
updateSliderFills(["cap_lna", "cap_vga"]);

socket.on("capture_started", (msg) => {
  capActiveEl.hidden = false;
  capNameEl.textContent = `Recording: ${msg.record.label || msg.record.name}`;
  capDetailEl.textContent = `${fmtMHz(msg.record.freq_hz)} @ ${fmtMSps(msg.record.sample_rate)} · ${msg.record.duration_s}s`;
  capBarEl.style.width = "0%";
  capBytesEl.textContent = "0 B";
  capPctEl.textContent = "0%";
});

socket.on("capture_progress", (p) => {
  capBarEl.style.width = `${Math.min(100, p.pct).toFixed(1)}%`;
  capBytesEl.textContent = `${fmtBytes(p.bytes_written)} / ${fmtBytes(p.expected)}`;
  capPctEl.textContent = `${Math.min(100, p.pct).toFixed(0)}%`;
  const remainingBytes = Math.max(0, p.expected - p.bytes_written);
  const rateBps = p.bytes_written > 0 ? (p.bytes_written / Math.max(0.001, p.pct / 100)) : 0;
  const remainingPctSec = p.pct > 0 ? ((100 - p.pct) / (p.pct / Math.max(0.5, p.bytes_written / Math.max(1, rateBps)))) : 0;
  capEtaEl.textContent = remainingBytes > 0 ? `${(remainingBytes / (1024 * 1024)).toFixed(1)} MB remaining` : "almost done";
});

socket.on("capture_done", () => {
  capActiveEl.hidden = true;
  socket.emit("list_captures");
});

socket.on("captures", (msg) => {
  captures = msg.items || [];
  renderCaptures();
});

// ------------------------------------------------------------------
// ADS-B mode
// ------------------------------------------------------------------
let adsbMap = null;
let adsbMarkers = new Map();   // hex -> Leaflet marker
let adsbTracks = new Map();    // hex -> [[lat,lon], ...] recent positions
let adsbTrails = new Map();    // hex -> Leaflet LayerGroup of fading trail segments
const ADSB_TRACK_MAX = 40;     // points of history kept per aircraft
let adsbAircraft = [];
let adsbFilter = "";

function initAdsbMap() {
  if (adsbMap) {
    setTimeout(() => adsbMap.invalidateSize(), 50);
    return;
  }
  // Default to continental US until user provides a location or aircraft appear
  adsbMap = L.map("adsb-map", {
    zoomControl: true,
    attributionControl: true,
  }).setView([38.9, -77.0], 9);

  // Dark theme tile layer (CARTO Dark Matter — free, no API key)
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    maxZoom: 18,
    attribution: '&copy; OpenStreetMap, &copy; CARTO',
    subdomains: "abcd",
  }).addTo(adsbMap);

  setTimeout(() => adsbMap.invalidateSize(), 50);
  updateGpsMarker();   // show the receiver position if GPS already has a fix
}

// Live receiver position from GPS (distinct from aircraft markers).
let gpsMarker = null;
let gpsCentered = false;
function updateGpsMarker() {
  if (!adsbMap) return;
  const s = gpsState;
  const haveFix = s && s.enabled && s.lat != null && !s.stale;
  if (!haveFix) {
    if (gpsMarker) { adsbMap.removeLayer(gpsMarker); gpsMarker = null; }
    return;
  }
  const latlng = [s.lat, s.lon];
  if (!gpsMarker) {
    gpsMarker = L.circleMarker(latlng, {
      radius: 7, color: "#ffffff", weight: 2,
      fillColor: "#22d3ee", fillOpacity: 0.9,
    }).addTo(adsbMap);
    gpsMarker.bindTooltip("Receiver (GPS)");
    if (!gpsCentered) {
      adsbMap.setView(latlng, Math.max(adsbMap.getZoom(), 11));
      gpsCentered = true;
    }
  } else {
    gpsMarker.setLatLng(latlng);
  }
}

// Deterministic vivid color per aircraft, stable across updates (from the hex).
function acColor(hex) {
  let h = 0;
  for (let i = 0; i < hex.length; i++) h = (h * 31 + hex.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360}, 75%, 60%)`;
}

function planeIcon(track, color) {
  const angle = (track || 0).toFixed(0);
  const c = color || "#4dd0e1";
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24"
         style="transform: rotate(${angle}deg); filter: drop-shadow(0 0 3px ${c});">
      <path d="M12 2 L14.5 12 L22 14 L14 16 L12 22 L10 16 L2 14 L9.5 12 Z"
            fill="${c}" stroke="#0a0c10" stroke-width="0.6"/>
    </svg>`;
  return L.divIcon({
    html: svg,
    className: "ac-icon",
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
}

// Redraw an aircraft's trail: one segment per hop, fading from oldest (faint)
// to newest (bright), in the aircraft's color.
function drawTrail(hex) {
  const old = adsbTrails.get(hex);
  if (old) { adsbMap.removeLayer(old); adsbTrails.delete(hex); }
  const pts = adsbTracks.get(hex);
  if (!pts || pts.length < 2) return;
  const color = acColor(hex);
  const group = L.layerGroup();
  for (let i = 0; i < pts.length - 1; i++) {
    const t = (pts.length <= 2) ? 1 : i / (pts.length - 2);   // 0 oldest .. 1 newest
    L.polyline([pts[i], pts[i + 1]], { color, weight: 2, opacity: 0.12 + 0.7 * t }).addTo(group);
  }
  group.addTo(adsbMap);
  adsbTrails.set(hex, group);
}

function fmtAlt(ft) {
  if (ft == null || ft === "ground") return "—";
  return `${Number(ft).toLocaleString()}`;
}
function fmtKnots(gs) { return gs == null ? "—" : `${Math.round(gs)}`; }
function fmtTrack(t) { return t == null ? "—" : `${Math.round(t)}°`; }
function fmtPos(lat, lon) {
  if (lat == null || lon == null) return "no position";
  return `${lat.toFixed(3)}, ${lon.toFixed(3)}`;
}
function fmtRssi(r) { return r == null ? "—" : `${r.toFixed(0)}`; }

function aircraftMatchesFilter(a) {
  if (!adsbFilter) return true;
  const blob = `${a.hex || ""} ${a.flight || ""} ${a.alt_baro || ""}`.toLowerCase();
  return blob.includes(adsbFilter);
}

function renderAircraftList() {
  const list = document.getElementById("aircraft-list");
  const filtered = adsbAircraft.filter(aircraftMatchesFilter);
  if (!adsbAircraft.length) {
    list.innerHTML = `<div class="event-empty">No aircraft yet. Start ADS-B in the sidebar.</div>`;
    return;
  }
  const header = `<div class="aircraft-header">
    <span>Hex</span><span>Flight</span><span>Alt (ft)</span>
    <span>Speed</span><span>Track</span><span>Position</span><span>RSSI</span>
  </div>`;
  const rows = filtered
    .sort((a, b) => (a.seen || 0) - (b.seen || 0))
    .map(a => {
      const hasPos = a.lat != null && a.lon != null;
      return `<div class="ac-row ${hasPos ? "" : "no-pos"}">
        <span class="ac-hex">${escapeHtml((a.hex || "").toUpperCase())}</span>
        <span class="ac-flight">${escapeHtml((a.flight || "").trim() || "—")}</span>
        <span class="ac-alt">${fmtAlt(a.alt_baro)}</span>
        <span class="ac-spd">${fmtKnots(a.gs)}</span>
        <span class="ac-track">${fmtTrack(a.track)}</span>
        <span class="ac-pos">${fmtPos(a.lat, a.lon)}</span>
        <span class="ac-rssi">${fmtRssi(a.rssi)}</span>
      </div>`;
    }).join("");
  list.innerHTML = header + rows;
}

function updateAdsbMap(aircraft) {
  if (!adsbMap) return;
  const seenHex = new Set();
  let withPos = 0;
  for (const a of aircraft) {
    if (a.lat == null || a.lon == null) continue;
    withPos++;
    seenHex.add(a.hex);
    const flight = (a.flight || "").trim();
    const popup = `<b>${flight || a.hex.toUpperCase()}</b><br>
      ${flight ? a.hex.toUpperCase() + "<br>" : ""}
      Alt: ${fmtAlt(a.alt_baro)} ft<br>
      Speed: ${fmtKnots(a.gs)} kts<br>
      Track: ${fmtTrack(a.track)}<br>
      RSSI: ${fmtRssi(a.rssi)} dB`;
    const color = acColor(a.hex);
    // append to the position history (skip identical consecutive fixes)
    let track = adsbTracks.get(a.hex);
    if (!track) { track = []; adsbTracks.set(a.hex, track); }
    const last = track[track.length - 1];
    if (!last || last[0] !== a.lat || last[1] !== a.lon) {
      track.push([a.lat, a.lon]);
      if (track.length > ADSB_TRACK_MAX) track.shift();
      drawTrail(a.hex);
    }
    let m = adsbMarkers.get(a.hex);
    if (!m) {
      m = L.marker([a.lat, a.lon], { icon: planeIcon(a.track, color) });
      m.bindPopup(popup);
      m.addTo(adsbMap);
      adsbMarkers.set(a.hex, m);
    } else {
      m.setLatLng([a.lat, a.lon]);
      m.setIcon(planeIcon(a.track, color));
      m.setPopupContent(popup);
    }
  }
  // remove markers + trails for aircraft no longer present
  for (const [hex, m] of adsbMarkers.entries()) {
    if (!seenHex.has(hex)) {
      adsbMap.removeLayer(m);
      adsbMarkers.delete(hex);
      const tr = adsbTrails.get(hex);
      if (tr) { adsbMap.removeLayer(tr); adsbTrails.delete(hex); }
      adsbTracks.delete(hex);
    }
  }
  const statsEl = document.getElementById("adsb-stats");
  if (statsEl) statsEl.textContent = `${aircraft.length} tracked · ${withPos} with position`;
}

document.getElementById("adsb_gain").addEventListener("input", e => {
  document.getElementById("adsb_gain_val").textContent = `${e.target.value} dB`;
  updateSliderFills(["adsb_gain"]);
});
updateSliderFills(["adsb_gain"]);

document.getElementById("adsb-filter").addEventListener("input", e => {
  adsbFilter = e.target.value.trim().toLowerCase();
  renderAircraftList();
});

function readAdsbConfig() {
  const lat = parseFloat(document.getElementById("adsb_lat").value);
  const lon = parseFloat(document.getElementById("adsb_lon").value);
  return {
    gain_db: parseInt(document.getElementById("adsb_gain").value, 10),
    rx_lat: Number.isFinite(lat) ? lat : null,
    rx_lon: Number.isFinite(lon) ? lon : null,
  };
}
function applyAdsbConfigToInputs(c) {
  if (c.gain_db != null) {
    document.getElementById("adsb_gain").value = c.gain_db;
    document.getElementById("adsb_gain_val").textContent = `${c.gain_db} dB`;
    updateSliderFills(["adsb_gain"]);
  }
  if (c.rx_lat != null) document.getElementById("adsb_lat").value = c.rx_lat;
  if (c.rx_lon != null) document.getElementById("adsb_lon").value = c.rx_lon;
}

document.getElementById("btn-start-adsb").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "start_adsb");
  const cfg = readAdsbConfig();
  socket.emit("start_adsb", cfg);
  if (cfg.rx_lat != null && cfg.rx_lon != null && adsbMap) {
    adsbMap.setView([cfg.rx_lat, cfg.rx_lon], 9);
  }
});
document.getElementById("btn-adsb-use-gps").addEventListener("click", () => {
  // Fill the receiver location from the live GPS fix (full precision; this is a
  // local-only value used for map centering and range, not a shared artifact).
  if (!gpsState || !gpsState.enabled || gpsState.lat == null || gpsState.stale) {
    showToast("warn", "No GPS fix to use. Enable GPS and wait for a fix.");
    return;
  }
  document.getElementById("adsb_lat").value = gpsState.lat;
  document.getElementById("adsb_lon").value = gpsState.lon;
  showToast("info", "Receiver location set from GPS.");
});

document.getElementById("btn-stop-adsb").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "stop (adsb)");
  socket.emit("stop");
});

socket.on("adsb", (msg) => {
  adsbAircraft = msg.aircraft || [];
  if (currentMode === "adsb") {
    updateAdsbMap(adsbAircraft);
    renderAircraftList();
  }
});

// ------------------------------------------------------------------
// Auto-scan mode
// ------------------------------------------------------------------
const scanResultsEl = document.getElementById("scan-results");
let scanFindings = {};

function setPhaseState(phase, state) {
  const row = document.querySelector(`.scan-phase[data-phase="${phase}"]`);
  if (!row) return;
  row.classList.remove("active", "done");
  if (state) row.classList.add(state);
}
function setPhaseProgress(phase, pct, elapsed, duration) {
  const row = document.querySelector(`.scan-phase[data-phase="${phase}"]`);
  if (!row) return;
  row.querySelector(".scan-phase-fill").style.width = `${Math.min(100, pct).toFixed(1)}%`;
  row.querySelector(".scan-phase-stat").textContent = elapsed != null
    ? `${elapsed.toFixed(1)}s / ${duration.toFixed(0)}s`
    : "";
}
function resetPhases() {
  document.querySelectorAll(".scan-phase").forEach(r => {
    r.classList.remove("active", "done");
    r.querySelector(".scan-phase-fill").style.width = "0%";
    r.querySelector(".scan-phase-stat").textContent = "—";
  });
}

function readScanConfig() {
  const lat = parseFloat(document.getElementById("scan_lat").value);
  const lon = parseFloat(document.getElementById("scan_lon").value);
  return {
    sweep_seconds: parseFloat(document.getElementById("scan_sweep_s").value),
    rtl433_seconds: parseFloat(document.getElementById("scan_rtl_s").value),
    adsb_seconds: parseFloat(document.getElementById("scan_adsb_s").value),
    peak_threshold_db: parseFloat(document.getElementById("scan_thresh").value),
    rx_lat: Number.isFinite(lat) ? lat : null,
    rx_lon: Number.isFinite(lon) ? lon : null,
  };
}
function applyScanConfigToInputs(c) {
  document.getElementById("scan_sweep_s").value = c.sweep_seconds;
  document.getElementById("scan_rtl_s").value   = c.rtl433_seconds;
  document.getElementById("scan_adsb_s").value  = c.adsb_seconds;
  document.getElementById("scan_thresh").value  = c.peak_threshold_db;
  if (c.rx_lat != null) document.getElementById("scan_lat").value = c.rx_lat;
  if (c.rx_lon != null) document.getElementById("scan_lon").value = c.rx_lon;
}

document.getElementById("btn-start-scan").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "start_scan");
  resetPhases();
  scanResultsEl.innerHTML = `<div class="event-empty">Scanning… results will appear here phase by phase.</div>`;
  scanFindings = {};
  socket.emit("start_scan", readScanConfig());
});
document.getElementById("btn-stop-scan").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "stop (scan)");
  socket.emit("stop");
});

socket.on("scan_started", () => { resetPhases(); });
socket.on("phase_started", (p) => {
  setPhaseState(p.phase, "active");
  setPhaseProgress(p.phase, 0, 0, p.duration_s);
});
socket.on("phase_progress", (p) => {
  setPhaseProgress(p.phase, p.pct, p.elapsed_s, p.duration_s);
});
socket.on("phase_completed", (p) => {
  setPhaseProgress(p.phase, 100, p.findings?.duration_s, p.findings?.duration_s);
  setPhaseState(p.phase, "done");
  scanFindings[p.phase] = p.findings || {};
  renderScanResults(false, scanFindings, null);
});
socket.on("scan_completed", (s) => {
  renderScanResults(true, scanFindings, s);
});
socket.on("scan_failed", () => {
  scanResultsEl.innerHTML = `<div class="event-empty">Scan failed.</div>`;
});
socket.on("scan_stopped", () => {
  resetPhases();
  scanResultsEl.innerHTML = `<div class="event-empty">Scan stopped. Hit Start Scan to run again.</div>`;
});

function fmtFreqHz(hz) {
  if (hz >= 1e9) return `${(hz / 1e9).toFixed(3)} GHz`;
  if (hz >= 1e6) return `${(hz / 1e6).toFixed(3)} MHz`;
  return `${(hz / 1e3).toFixed(1)} kHz`;
}

function summarizeDeviceFields(d) {
  const skip = new Set([
    "_count", "_last_time", "_key", "time", "model", "freq", "freq1", "freq2",
    "rssi", "snr", "noise", "mod", "channel"
  ]);
  const parts = [];
  for (const [k, v] of Object.entries(d)) {
    if (skip.has(k)) continue;
    if (v == null) continue;
    parts.push(`${escapeHtml(k)}=${escapeHtml(v)}`);
    if (parts.length >= 5) break;
  }
  return parts.join("  ");
}

function renderScanResults(isFinal, findings, summary) {
  const sections = [];

  if (isFinal && summary?.summary) {
    const s = summary.summary;
    sections.push(`<div class="scan-summary">
      <div><div class="num">${s.peak_count}</div><span class="lbl">Spectrum peaks</span></div>
      <div><div class="num">${s.ism_433_devices}</div><span class="lbl">ISM 433 devices</span></div>
      <div><div class="num">${s.ism_915_devices}</div><span class="lbl">ISM 915 devices</span></div>
      <div><div class="num">${s.aircraft_count}</div><span class="lbl">Aircraft</span></div>
    </div>`);
  }

  // Peaks
  const peaks = findings.sweep?.peaks || [];
  sections.push(`<div class="scan-section">
    <h3>Spectrum Peaks (${peaks.length})</h3>
    ${peaks.length === 0
      ? `<div class="empty">No peaks above threshold yet.</div>`
      : peaks.map(p => `
        <div class="peak-row">
          <span class="freq">${fmtFreqHz(p.center_hz)}</span>
          <span class="band">${p.band}${p.decoder_hint ? ` <span class="hint-dec">→ ${p.decoder_hint}</span>` : ""}</span>
          <span class="snr">${p.snr_db.toFixed(1)} dB</span>
          <span class="snr">${(p.peak_db).toFixed(1)} dBFS</span>
        </div>`).join("")
    }
  </div>`);

  // ISM 433 devices
  for (const key of ["rtl433_433", "rtl433_915"]) {
    const phase = findings[key];
    const devices = phase?.devices || [];
    const label = key === "rtl433_433" ? "ISM 433 Devices" : "ISM 915 Devices";
    sections.push(`<div class="scan-section">
      <h3>${label} (${devices.length})</h3>
      ${devices.length === 0
        ? `<div class="empty">No devices decoded.</div>`
        : devices.map(d => `
          <div class="scan-device-row">
            <span class="model">${escapeHtml(d.model || "unknown")}</span>
            <span class="fields">${summarizeDeviceFields(d)}</span>
            <span class="count">×${d._count}</span>
          </div>`).join("")
      }
    </div>`);
  }

  // Aircraft
  const aircraft = findings.adsb?.aircraft || [];
  sections.push(`<div class="scan-section">
    <h3>Aircraft (${aircraft.length})</h3>
    ${aircraft.length === 0
      ? `<div class="empty">No aircraft tracked.</div>`
      : aircraft.map(a => `
        <div class="scan-ac-row">
          <span class="hex">${escapeHtml((a.hex || "").toUpperCase())}</span>
          <span class="flight">${escapeHtml((a.flight || "").trim() || "—")}</span>
          <span class="alt">${a.alt_baro != null ? a.alt_baro + " ft" : "—"}</span>
          <span class="spd">${a.gs != null ? Math.round(a.gs) + " kts" : "—"}</span>
          <span class="pos">${a.lat != null ? a.lat.toFixed(3) + ", " + a.lon.toFixed(3) : "no position"}</span>
        </div>`).join("")
    }
  </div>`);

  scanResultsEl.innerHTML = sections.join("");
}

// ------------------------------------------------------------------
// Radio (AM/FM) mode
// ------------------------------------------------------------------
let radioCtx = null;
let radioNode = null;
let radioGain = null;
let radioPlaying = false;
let radioDemod = "fm";
let radioRate = 50000;

const radioFreqEl = document.getElementById("radio_freq");
const radioVolEl  = document.getElementById("radio_vol");
const radioVolVal = document.getElementById("radio_vol_val");

function radioVolume() { return (parseInt(radioVolEl.value, 10) || 0) / 100; }

function setRadioDemod(demod) {
  radioDemod = demod;
  document.querySelectorAll("#radio-demod .seg-btn").forEach(x =>
    x.classList.toggle("active", x.dataset.demod === demod));
}

function setRadioNow(state, freq, demod) {
  const s = document.getElementById("radio-now-state");
  const f = document.getElementById("radio-now-freq");
  const d = document.getElementById("radio-now-demod");
  if (s) { s.textContent = state; s.classList.toggle("live", state === "Playing" || state === "Holding"); }
  if (f) f.textContent = (freq != null) ? `${parseFloat(freq).toFixed(3)} MHz` : "—";
  if (d) d.textContent = demod ? demod.toUpperCase() : "";
}

function applyRadioConfigToInputs(c) {
  if (!c) return;
  if (c.freq_mhz != null) radioFreqEl.value = parseFloat(c.freq_mhz).toFixed(1);
  if (c.demod) setRadioDemod(c.demod);
}

function stopRadioPlayback() {
  radioPlaying = false;
  if (radioNode) radioNode.port.postMessage({ type: "flush" });
  setRadioNow("Stopped", null, "");
  setRadioSignal(null);
}

// Map channel power (dBFS) to a 0-100% meter. Measured range at the antenna:
// ~-60 dBFS = dead/noise floor, ~-15 dBFS = strong station.
function setRadioSignal(dbfs) {
  const fill = document.getElementById("radio-meter-fill");
  const label = document.getElementById("radio-meter-label");
  if (dbfs == null) {
    if (fill) { fill.style.width = "0%"; fill.className = "radio-meter-fill"; }
    if (label) label.textContent = "Signal: —";
    return;
  }
  const pct = Math.max(0, Math.min(100, ((dbfs + 60) / 45) * 100));
  const cls = pct >= 55 ? "good" : pct >= 25 ? "ok" : "weak";
  if (fill) { fill.style.width = `${pct.toFixed(0)}%`; fill.className = `radio-meter-fill ${cls}`; }
  if (label) {
    label.textContent = `Signal: ${dbfs.toFixed(0)} dBFS${pct < 25 ? " (too weak)" : ""}`;
  }
}

document.querySelectorAll("#radio-demod .seg-btn").forEach(b => {
  b.addEventListener("click", () => {
    setRadioDemod(b.dataset.demod);
    socket.emit("set_setting", { key: "last_demod", value: b.dataset.demod });
  });
});

document.getElementById("radio-up").addEventListener("click", () => {
  radioFreqEl.value = (parseFloat(radioFreqEl.value || "0") + 0.1).toFixed(1);
  socket.emit("set_setting", { key: "last_radio_freq", value: parseFloat(radioFreqEl.value) });
});
document.getElementById("radio-down").addEventListener("click", () => {
  radioFreqEl.value = (parseFloat(radioFreqEl.value || "0") - 0.1).toFixed(1);
  socket.emit("set_setting", { key: "last_radio_freq", value: parseFloat(radioFreqEl.value) });
});
radioFreqEl.addEventListener("change", () => {
  const v = parseFloat(radioFreqEl.value);
  if (Number.isFinite(v)) socket.emit("set_setting", { key: "last_radio_freq", value: v });
});

let _volSaveTimer = null;
radioVolEl.addEventListener("input", () => {
  radioVolVal.textContent = `${radioVolEl.value}%`;
  if (radioGain) radioGain.gain.value = radioVolume();
  updateSliderFills(["radio_vol"]);
  // Audio/fill stay real-time; persist is debounced so a drag does not flood
  // the socket with set_setting messages.
  clearTimeout(_volSaveTimer);
  _volSaveTimer = setTimeout(() => {
    socket.emit("set_setting", { key: "radio_volume", value: parseInt(radioVolEl.value, 10) });
  }, 400);
});
updateSliderFills(["radio_vol"]);

// ------------------------------------------------------------------
// Bookmarks
// ------------------------------------------------------------------
socket.on("bookmarks", (msg) => {
  bookmarks = msg.data || [];
  renderBookmarks();
});

function fmtBookmarkFreq(hz) {
  const mhz = hz / 1e6;
  return `${mhz.toFixed(3)} MHz`;
}

function renderBookmarks() {
  const listEl = document.getElementById("bookmark-list");
  if (!listEl) return;
  const filterEl = document.getElementById("bookmark-filter");
  const q = filterEl ? filterEl.value.trim().toLowerCase() : "";
  const filtered = bookmarks.filter(bm => {
    if (!q) return true;
    const freqText = fmtBookmarkFreq(bm.freq_hz).toLowerCase();
    const labelText = (bm.label || "").toLowerCase();
    const tagsText = (bm.tags || []).join(" ").toLowerCase();
    return labelText.includes(q) || freqText.includes(q) || tagsText.includes(q);
  });
  if (!filtered.length) {
    listEl.innerHTML = `<div class="event-empty">${bookmarks.length ? "No bookmarks match the filter." : "No bookmarks yet. Tune to a frequency and click + Add."}</div>`;
    return;
  }
  const rows = filtered.map(bm => {
    const label = escapeHtml(bm.label || "");
    const freq  = escapeHtml(fmtBookmarkFreq(bm.freq_hz));
    const demod = escapeHtml((bm.demod || "fm").toUpperCase());
    const tags  = (bm.tags || []).map(t => `<span class="bm-tag">${escapeHtml(t)}</span>`).join(" ");
    const id    = String(Number(bm.id));  // numeric-only; safe in data-id attributes
    return `<div class="bm-row" data-id="${id}">
      <div class="bm-main">
        <span class="bm-label">${label}</span>
        <span class="bm-freq">${freq}</span>
        <span class="bm-demod">${demod}</span>
        ${tags ? `<span class="bm-tags">${tags}</span>` : ""}
      </div>
      <div class="bm-actions">
        <button class="btn ghost small bm-tune" data-id="${id}" title="Tune and listen">Tune</button>
        <button class="btn ghost small bm-edit" data-id="${id}" title="Edit label">Edit</button>
        <button class="btn ghost small bm-del"  data-id="${id}" title="Delete">×</button>
      </div>
    </div>`;
  });
  listEl.innerHTML = rows.join("");

  listEl.querySelectorAll(".bm-tune").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = parseInt(btn.dataset.id, 10);
      const bm = bookmarks.find(b => b.id === id);
      if (!bm) return;
      tuneToBookmark(bm);
    });
  });

  listEl.querySelectorAll(".bm-edit").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = parseInt(btn.dataset.id, 10);
      const bm = bookmarks.find(b => b.id === id);
      if (!bm) return;
      const newLabel = prompt("Edit label:", bm.label || "");
      if (newLabel === null) return; // cancelled
      socket.emit("update_bookmark", { id, label: newLabel.trim() });
    });
  });

  listEl.querySelectorAll(".bm-del").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = parseInt(btn.dataset.id, 10);
      const bm = bookmarks.find(b => b.id === id);
      if (!bm) return;
      if (!confirm(`Delete bookmark "${bm.label || fmtBookmarkFreq(bm.freq_hz)}"?`)) return;
      socket.emit("delete_bookmark", { id });
    });
  });
}

const bookmarkFilterEl = document.getElementById("bookmark-filter");
if (bookmarkFilterEl) {
  bookmarkFilterEl.addEventListener("input", renderBookmarks);
}

document.getElementById("btn-add-bookmark").addEventListener("click", () => {
  const freq = parseFloat(radioFreqEl.value);
  if (!Number.isFinite(freq)) return;
  const labelEl = document.getElementById("bookmark-label");
  const rawLabel = labelEl ? labelEl.value.trim() : "";
  const label = rawLabel || `${freq.toFixed(3)} MHz`;
  socket.emit("add_bookmark", {
    freq_hz: Math.round(freq * 1e6),
    demod: radioDemod,
    label,
    source: "user",
  });
  if (labelEl) labelEl.value = "";
});

async function tuneToBookmark(bm) {
  setMode("radio");  // switch to the radio pane so Now Playing/Stop are visible
  const mhz = bm.freq_hz / 1e6;
  radioFreqEl.value = mhz.toFixed(1);
  setRadioDemod(bm.demod || "fm");
  try {
    await ensureRadioAudio();
  } catch (err) {
    console.error("[aetherscope] audio init failed", err);
    return;
  }
  radioNode.port.postMessage({ type: "flush" });
  radioPlaying = true;
  socket.emit("start_radio", { demod: radioDemod, freq_mhz: mhz });
  setRadioNow("Tuning…", mhz, radioDemod);
  socket.emit("bump_bookmark", { id: bm.id });
}

async function ensureRadioAudio() {
  if (radioCtx) {
    if (radioCtx.state === "suspended") await radioCtx.resume();
    return;
  }
  const Ctx = window.AudioContext || window.webkitAudioContext;
  radioCtx = new Ctx();
  await radioCtx.audioWorklet.addModule("/static/radio-audio-worklet.js");
  radioNode = new AudioWorkletNode(radioCtx, "radio-player", {
    numberOfInputs: 0,
    numberOfOutputs: 1,
    outputChannelCount: [1],
    processorOptions: { producerRate: radioRate },
  });
  radioGain = radioCtx.createGain();
  radioGain.gain.value = radioVolume();
  radioNode.connect(radioGain).connect(radioCtx.destination);
}

document.getElementById("btn-start-radio").addEventListener("click", async (e) => {
  flashClick(e.currentTarget, "start_radio");
  const freq = parseFloat(radioFreqEl.value);
  if (!Number.isFinite(freq)) return;
  try {
    await ensureRadioAudio();
  } catch (err) {
    console.error("[aetherscope] audio init failed", err);
    return;
  }
  radioNode.port.postMessage({ type: "flush" });
  radioPlaying = true;
  socket.emit("start_radio", { demod: radioDemod, freq_mhz: freq });
  setRadioNow("Tuning…", freq, radioDemod);
});

document.getElementById("btn-stop-radio").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "stop (radio)");
  socket.emit("stop");
  stopRadioPlayback();
});

document.getElementById("btn-snap-radio").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "snap_radio");
  // Backend recenters on the nearest strong carrier (or toasts if not playing);
  // the resulting radio_started refreshes the now-playing frequency.
  socket.emit("snap_radio");
});

// ---- WAV audio recording (records the live radio audio) ----
let audioRecording = false;
const recordBtn = document.getElementById("btn-record-audio");
function updateRecordBtn() {
  recordBtn.textContent = audioRecording ? "■ Stop recording" : "● Record";
  recordBtn.classList.toggle("recording", audioRecording);
}
recordBtn.addEventListener("click", () => {
  socket.emit(audioRecording ? "stop_audio_record" : "start_audio_record", {});
});
socket.on("audio_record_status", (s) => {
  audioRecording = !!(s && s.recording);
  updateRecordBtn();
});

// ---- Listen to a saved IQ capture (offset-tunable playback) ----
const iqPlayBar = document.getElementById("iq-play-bar");
const iqPlayName = document.getElementById("iq-play-name");
const iqPlayDemod = document.getElementById("iq-play-demod");
const iqPlayOffset = document.getElementById("iq-play-offset");
const iqPlayOffsetVal = document.getElementById("iq-play-offset-val");
let iqPlayCurrent = null;

async function startIqPlay(name, demod, offset) {
  setMode("radio");
  try { await ensureRadioAudio(); }
  catch (e) { showToast("error", "Audio init failed."); return; }
  radioNode.port.postMessage({ type: "flush" });
  radioPlaying = true;
  iqPlayCurrent = { name, demod, offset };
  socket.emit("play_capture", { name, demod, offset_hz: offset });
}
function retuneIqPlay() {
  if (!iqPlayCurrent) return;
  iqPlayCurrent.demod = iqPlayDemod.value;
  iqPlayCurrent.offset = parseInt(iqPlayOffset.value, 10) || 0;
  socket.emit("play_capture", {
    name: iqPlayCurrent.name, demod: iqPlayCurrent.demod, offset_hz: iqPlayCurrent.offset,
  });
}
iqPlayOffset.addEventListener("input", () => {
  iqPlayOffsetVal.textContent = `${Math.round((parseInt(iqPlayOffset.value, 10) || 0) / 1000)} kHz`;
});
iqPlayOffset.addEventListener("change", retuneIqPlay);
iqPlayDemod.addEventListener("change", retuneIqPlay);
document.getElementById("iq-play-stop").addEventListener("click", () => {
  socket.emit("stop");
  stopRadioPlayback();
  iqPlayBar.hidden = true;
  iqPlayCurrent = null;
});
socket.on("iq_play_started", (m) => {
  radioPlaying = true;   // keep audio alive across retunes
  iqPlayCurrent = { name: m.name, demod: m.demod, offset: m.offset_hz };
  iqPlayName.textContent = m.name;
  iqPlayDemod.value = m.demod;
  const half = Math.floor((m.sample_rate || 2000000) / 2);
  iqPlayOffset.min = String(-half);
  iqPlayOffset.max = String(half);
  iqPlayOffset.step = String(Math.max(1000, Math.round((m.sample_rate || 2000000) / 2000)));
  iqPlayOffset.value = String(Math.round(m.offset_hz || 0));
  iqPlayOffsetVal.textContent = `${Math.round((m.offset_hz || 0) / 1000)} kHz`;
  iqPlayBar.hidden = false;
});
socket.on("iq_play_done", (m) => {
  // "stopped" fires during retune/stop and is handled elsewhere; only a natural
  // end ("completed") should tear down the playback UI.
  if (m && m.reason !== "completed") return;
  iqPlayBar.hidden = true;
  stopRadioPlayback();
  iqPlayCurrent = null;
});

// ---- Scanner: cycle the marked frequencies, stop on activity ----
let scanSquelch = -45;
const radioSquelchEl = document.getElementById("radio_squelch");
if (radioSquelchEl) {
  radioSquelchEl.addEventListener("input", () => {
    scanSquelch = parseInt(radioSquelchEl.value, 10);
    const v = document.getElementById("radio_squelch_val");
    if (v) v.textContent = `${scanSquelch} dBFS`;
    updateSliderFills(["radio_squelch"]);
  });
  updateSliderFills(["radio_squelch"]);
}

document.getElementById("btn-scan-radio").addEventListener("click", async (e) => {
  flashClick(e.currentTarget, "scan_radio");
  if (serverMode === "scan_radio") { socket.emit("stop"); stopRadioPlayback(); return; }
  const freqs = marks.map(m => m.hz / 1e6);
  if (!freqs.length) return;   // backend also guards with a toast
  try {
    await ensureRadioAudio();
  } catch (err) {
    console.error("[aetherscope] audio init failed", err);
    return;
  }
  radioNode.port.postMessage({ type: "flush" });
  radioPlaying = true;
  socket.emit("start_scan_radio", { freqs, demod: radioDemod, squelch_dbfs: scanSquelch });
  setRadioNow("Scanning", null, radioDemod);
});

socket.on("scan_radio_started", (msg) => {
  if (msg && msg.sample_rate && radioNode) radioNode.port.postMessage({ type: "rate", rate: msg.sample_rate });
  radioPlaying = true;
});
socket.on("scan_pos", (msg) => { if (msg) setRadioNow("Scanning", msg.freq_mhz, radioDemod); });
socket.on("scan_hold", (msg) => { if (msg) setRadioNow("Holding", msg.freq_mhz, radioDemod); });
socket.on("scan_resume", () => setRadioNow("Scanning", null, radioDemod));

socket.on("radio_started", (msg) => {
  radioRate = msg.sample_rate || 50000;
  if (radioNode) radioNode.port.postMessage({ type: "rate", rate: radioRate });
  setRadioNow("Playing", msg.freq_mhz, msg.demod);
});

socket.on("radio_audio", (data) => {
  if (!radioNode || !radioPlaying) return;
  let ab = null;
  if (data instanceof ArrayBuffer) ab = data;
  else if (ArrayBuffer.isView(data)) ab = data.buffer;
  if (!ab) return;
  const i16 = new Int16Array(ab);
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
  radioNode.port.postMessage(f32, [f32.buffer]);
});

socket.on("radio_signal", (msg) => {
  if (!radioPlaying) return;
  setRadioSignal(msg && typeof msg.dbfs === "number" ? msg.dbfs : null);
});

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

// initial
renderMarks();
renderBookmarks();
setMode("sweep");
requestAnimationFrame(fitAll);
