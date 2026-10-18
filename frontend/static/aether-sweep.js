// Aetherscope — waterfall/spectrum rendering core and SDR/sweep controls. One mode module behind the Aether seam:
// it owns its socket handlers and DOM wiring, receives the core
// interface, and does not reach into other modes' globals.
"use strict";
(function (A) {
  const core = A.core;
  const socket = core.socket;
  const state = core.state;
  const updateSliderFills = core.updateSliderFills;
  const flashClick = core.flashClick;
const fftCanvas = document.getElementById("fft");
const waterfallCanvas = document.getElementById("waterfall");
const fftCtx = fftCanvas.getContext("2d");
const wfCtx = waterfallCanvas.getContext("2d", { willReadFrequently: true });

const hoverFreqEl  = document.getElementById("hover-freq");
const hoverPowerEl = document.getElementById("hover-power");
const sweepRateEl  = document.getElementById("sweep-rate");
const eventCountEl = document.getElementById("event-count");

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


// Cursor-following frequency label for the spectrum.
const hoverTag = document.createElement("div");
hoverTag.className = "hover-tag";
hoverTag.hidden = true;
document.body.appendChild(hoverTag);

const POWER_MIN     = -100;
const POWER_MAX     = -20;
const FFT_PADDING   = { top: 16, right: 14, bottom: 28, left: 64 };
const WF_PADDING    = { top: 0,  right: 14, bottom: 28, left: 64 };
const AXIS_FONT     = "12px ui-monospace, 'SF Mono', Menlo, monospace";
const DPR = Math.max(window.devicePixelRatio || 1, 1);

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
window.addEventListener("resize", () => { if (state.currentMode === "sweep") fitAll(); });

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

function updateSweepRate() {
  const now = performance.now();
  sweepTimestamps.push(now);
  sweepTimestamps = sweepTimestamps.filter(t => now - t < 3000);
  const rate = sweepTimestamps.length / 3.0;
  sweepRateEl.textContent = `${rate.toFixed(1)} Hz`;
}

socket.on("sweep", (msg) => {
  const rangeChanged = !lastSweep
    || Math.abs(lastSweep.f0 - msg.f0) > 1
    || Math.abs(lastSweep.f1 - msg.f1) > 1;
  if (rangeChanged && state.currentMode === "sweep") {
    const wr = waterfallCanvas.getBoundingClientRect();
    wfCtx.clearRect(0, 0, wr.width, wr.height);
  }
  lastSweep = { f0: msg.f0, f1: msg.f1, powers: msg.powers };
  if (rangeChanged) { maxHoldPowers = null; avgPowers = null; }
  updateHoldTraces(msg.powers);
  if (state.currentMode === "sweep") {
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

function highlightPreset(c) {
  document.querySelectorAll(".chip").forEach(b => {
    const match =
      parseInt(b.dataset.f0, 10) === c.f_start_mhz &&
      parseInt(b.dataset.f1, 10) === c.f_stop_mhz;
    b.classList.toggle("active", match);
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

document.getElementById("btn-start-sweep").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "start_sweep");
  socket.emit("start_sweep", readSweepConfig());
});
document.getElementById("btn-stop-sweep").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "stop (sweep)");
  socket.emit("stop");
});

document.getElementById("lna").addEventListener("input", e => {
  document.getElementById("lna_val").textContent = `${e.target.value} dB`;
  updateSliderFills(["lna"]);
});
document.getElementById("vga").addEventListener("input", e => {
  document.getElementById("vga_val").textContent = `${e.target.value} dB`;
  updateSliderFills(["vga"]);
});
updateSliderFills(["lna", "vga"]);

// hover
function handleHover(e) {
  if (!lastSweep || state.currentMode !== "sweep") return;
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
  if (!lastSweep || state.currentMode !== "sweep") return;
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
  if (!lastSweep || state.currentMode !== "sweep") return;
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
    tune.addEventListener("click", () => A.radio.tuneToMark(m.hz));

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
core.registerApplyConfig((s) => { if (s.sweep_config) applySweepConfigToInputs(s.sweep_config); });
core.registerEnterMode("sweep", () => requestAnimationFrame(fitAll));
core.registerLeftMode("sweep", clearSweepVisuals);

A.sweep = { fitAll, renderMarks, addMark, getMarks: () => marks, clearSweepVisuals };
})(window.Aether = window.Aether || {});
