// hackrf-web — frontend SDR canvas: FFT trace, scrolling waterfall,
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
const paneSweep   = document.getElementById("pane-sweep");
const paneDecode  = document.getElementById("pane-decode");
const paneCapture = document.getElementById("pane-capture");

let currentMode  = "sweep";         // UI mode (sweep | decode)
let serverMode   = "idle";          // backend mode (idle | sweep | decode)
let lastSweep    = null;
let cursorX      = -1;
let sweepTimestamps = [];
let events       = [];
let eventFilter  = "";

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
  paneSweep.hidden   = mode !== "sweep";
  paneDecode.hidden  = mode !== "decode";
  paneCapture.hidden = mode !== "capture";
  document.querySelectorAll(".mode-tab").forEach(t => {
    t.classList.toggle("active", t.dataset.mode === mode);
  });
  if (mode === "sweep") requestAnimationFrame(fitAll);
  if (mode === "capture") socket.emit("list_captures");
}

document.querySelectorAll(".mode-tab").forEach(tab => {
  tab.addEventListener("click", () => setMode(tab.dataset.mode));
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
    fftCtx.fillText(`${dB}`, plot.x - 6, y);
  }
  fftCtx.textAlign = "left";
  fftCtx.fillText("dB", plot.x + plot.w + 4, plot.y + 4);
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

function summarizeFields(ev) {
  // Skip noisy metadata, surface useful KV pairs
  const skip = new Set(["time", "model", "freq", "freq1", "freq2", "rssi", "snr", "noise", "mod"]);
  const parts = [];
  for (const [k, v] of Object.entries(ev)) {
    if (skip.has(k)) continue;
    if (v === null || v === undefined) continue;
    parts.push(`<span class="k">${k}=</span><span class="v">${String(v)}</span>`);
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
      <span><span class="event-model">${model}</span> <span class="event-fields">${fields}</span></span>
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
  else statusText.textContent = "Idle";
  if (serverMode !== "sweep") sweepRateEl.textContent = "0.0 Hz";
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

socket.on("status", (s) => {
  serverMode = s.mode || "idle";
  if (s.sweep_config) applySweepConfigToInputs(s.sweep_config);
  if (s.decode_config) applyDecodeConfigToInputs(s.decode_config);
  if (s.capture_config) applyCaptureConfigToInputs(s.capture_config);
  refreshStatusUI();
});

socket.on("sweep", (msg) => {
  lastSweep = { f0: msg.f0, f1: msg.f1, powers: msg.powers };
  if (currentMode === "sweep") {
    drawFFT(msg.powers);
    pushWaterfallRow(msg.powers);
  }
  updateSweepRate();
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

// preset chips
document.querySelectorAll(".chip").forEach(b => {
  b.addEventListener("click", () => {
    document.getElementById("f_start").value = b.dataset.f0;
    document.getElementById("f_stop").value  = b.dataset.f1;
    document.getElementById("bin_width").value = b.dataset.bin;
    highlightPreset(readSweepConfig());
    socket.emit("start_sweep", readSweepConfig());
  });
});

// band buttons
document.querySelectorAll(".band-btn").forEach(b => {
  b.addEventListener("click", () => {
    document.getElementById("dec_freq").value = b.dataset.freq;
    highlightBandButton(parseFloat(b.dataset.freq));
  });
});

// buttons
document.getElementById("btn-start-sweep").addEventListener("click", () => {
  socket.emit("start_sweep", readSweepConfig());
});
document.getElementById("btn-stop-sweep").addEventListener("click", () => {
  socket.emit("stop");
});
document.getElementById("btn-start-decode").addEventListener("click", () => {
  socket.emit("start_decode", readDecodeConfig());
});
document.getElementById("btn-stop-decode").addEventListener("click", () => {
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
  hoverPowerEl.textContent = `${dB.toFixed(1)} dBFS`;
  drawFFT(lastSweep.powers);
}
fftCanvas.addEventListener("mousemove", handleHover);
fftCanvas.addEventListener("mouseleave", () => {
  cursorX = -1;
  hoverFreqEl.textContent  = "— MHz";
  hoverPowerEl.textContent = "— dB";
  if (lastSweep) drawFFT(lastSweep.powers);
});
waterfallCanvas.addEventListener("mousemove", handleHover);
waterfallCanvas.addEventListener("mouseleave", () => {
  cursorX = -1;
  hoverFreqEl.textContent  = "— MHz";
  hoverPowerEl.textContent = "— dB";
});

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
    const blob = `${c.name} ${c.label} ${c.freq_hz}`.toLowerCase();
    return blob.includes(capFilter);
  });
  capturesListEl.innerHTML = filtered.map(c => `
    <div class="cap-row">
      <div class="cap-main">
        <div class="cap-name">${c.label || c.name}</div>
        <div class="cap-detail">
          ${fmtMHz(c.freq_hz)}<span class="sep">·</span>
          ${fmtMSps(c.sample_rate)}<span class="sep">·</span>
          ${c.duration_s.toFixed(1)}s<span class="sep">·</span>
          ${fmtBytes(c.file_size)}
        </div>
        <div class="cap-meta">${c.name} · ${fmtAgo(c.started_at)} · ${c.sample_format}</div>
      </div>
      <div class="cap-actions">
        <a href="/captures/${encodeURIComponent(c.name)}" download>Download</a>
        <button class="danger" data-delete="${c.name}">Delete</button>
      </div>
    </div>
  `).join("");

  capturesListEl.querySelectorAll("[data-delete]").forEach(btn => {
    btn.addEventListener("click", () => {
      if (confirm(`Delete ${btn.dataset.delete}?`)) {
        socket.emit("delete_capture", { name: btn.dataset.delete });
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
    document.querySelectorAll(".band-btn-cap").forEach(x => x.classList.toggle("active", x === b));
  });
});

document.getElementById("btn-start-capture").addEventListener("click", () => {
  socket.emit("start_capture", readCaptureConfig());
});
document.getElementById("btn-cancel-capture").addEventListener("click", () => {
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

// initial
setMode("sweep");
requestAnimationFrame(fitAll);
