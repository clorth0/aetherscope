// hackrf-web — frontend SDR canvas: FFT trace + scrolling waterfall.

const socket = io();

const fftCanvas = document.getElementById("fft");
const waterfallCanvas = document.getElementById("waterfall");
const fftCtx = fftCanvas.getContext("2d");
const wfCtx = waterfallCanvas.getContext("2d", { willReadFrequently: true });

const statusDot  = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const hoverFreqEl  = document.getElementById("hover-freq");
const hoverPowerEl = document.getElementById("hover-power");
const sweepRateEl  = document.getElementById("sweep-rate");

let lastSweep = null;       // { f0, f1, powers: number[] }
let cursorX = -1;
let sweepTimestamps = [];

const POWER_MIN = -100;
const POWER_MAX = -20;
const FFT_PADDING = { top: 16, right: 14, bottom: 28, left: 64 };
const WF_PADDING  = { top: 0,  right: 14, bottom: 28, left: 64 };
const AXIS_FONT   = "12px ui-monospace, 'SF Mono', Menlo, monospace";

const DPR = Math.max(window.devicePixelRatio || 1, 1);

// ------------------------------------------------------------------
// Canvas sizing with DPR awareness
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
  // re-render last frame after resize so it doesn't go blank
  if (lastSweep) drawFFT(lastSweep.powers);
}
window.addEventListener("resize", fitAll);
requestAnimationFrame(fitAll);

// ------------------------------------------------------------------
// Viridis-ish colormap (low → high) precomputed LUT
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

// ------------------------------------------------------------------
// Frequency formatting
// ------------------------------------------------------------------
function fmtFreq(hz) {
  if (hz >= 1e9) return (hz / 1e9).toFixed(3) + " GHz";
  if (hz >= 1e6) return (hz / 1e6).toFixed(3) + " MHz";
  if (hz >= 1e3) return (hz / 1e3).toFixed(1) + " kHz";
  return hz.toFixed(0) + " Hz";
}

// Pick "nice" tick spacing for an axis
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

// Resample a powers[] to a target width using max-pooling (preserves peaks)
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
// FFT panel render
// ------------------------------------------------------------------
function drawFFT(powers) {
  const { w, h } = cssSize(fftCanvas);
  fftCtx.clearRect(0, 0, w, h);

  // background
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

  // dB grid
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

  // freq ticks
  if (lastSweep) {
    fftCtx.textAlign = "center";
    fftCtx.textBaseline = "top";
    const ticks = niceTicks(lastSweep.f0, lastSweep.f1, Math.max(4, Math.floor(plot.w / 110)));
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

  // trace
  if (powers && powers.length) {
    const samples = resampleMax(powers, Math.floor(plot.w));
    const xScale = plot.w / samples.length;

    // glow fill below trace
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

    // glow stroke (two-pass: wide soft, narrow crisp)
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

  // cursor
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

  // border
  fftCtx.strokeStyle = "#171c25";
  fftCtx.lineWidth = 1;
  fftCtx.strokeRect(plot.x + 0.5, plot.y + 0.5, plot.w, plot.h);
}

// ------------------------------------------------------------------
// Waterfall: scroll down 1px and paint a new top row
// ------------------------------------------------------------------
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

  // shift waterfall area down by 1 css px (DPR-correct)
  const sx = Math.floor(plot.x * DPR);
  const sy = Math.floor(plot.y * DPR);
  const sw = Math.floor(cw * DPR);
  const sh = Math.floor(ch * DPR);

  // copy down by 1 css px (DPR rows in device pixels)
  const stride = Math.max(1, Math.floor(DPR));
  const img = wfCtx.getImageData(sx, sy, sw, sh - stride);
  wfCtx.putImageData(img, sx, sy + stride);

  // build the new top row in css coords (DPR px tall)
  const samples = resampleMax(powers, cw);
  const row = wfCtx.createImageData(cw, 1);
  for (let i = 0; i < cw; i++) {
    const lutIdx = powerToLut(samples[i]);
    row.data[i * 4 + 0] = COLORMAP[lutIdx * 3];
    row.data[i * 4 + 1] = COLORMAP[lutIdx * 3 + 1];
    row.data[i * 4 + 2] = COLORMAP[lutIdx * 3 + 2];
    row.data[i * 4 + 3] = 255;
  }
  // putImageData ignores transform; emulate via temp canvas scaled by DPR
  const tmp = document.createElement("canvas");
  tmp.width = cw;
  tmp.height = 1;
  tmp.getContext("2d").putImageData(row, 0, 0);
  wfCtx.drawImage(tmp, plot.x, plot.y, cw, 1);

  // axis labels on waterfall
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
    wfCtx.textAlign = "right";
    wfCtx.textBaseline = "middle";
    wfCtx.fillText("Hz", plot.x - 6, plot.y + plot.h + 10);
  }

  // cursor over waterfall
  if (cursorX >= plot.x && cursorX <= plot.x + plot.w) {
    wfCtx.strokeStyle = "rgba(255, 255, 255, 0.25)";
    wfCtx.lineWidth = 1;
    wfCtx.setLineDash([3, 3]);
    wfCtx.beginPath();
    wfCtx.moveTo(cursorX + 0.5, plot.y);
    wfCtx.lineTo(cursorX + 0.5, plot.y + plot.h);
    wfCtx.stroke();
    wfCtx.setLineDash([]);
  }
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

// ------------------------------------------------------------------
// Socket events
// ------------------------------------------------------------------
socket.on("connect", () => { /* status will arrive */ });

socket.on("status", (s) => {
  const running = !!s.running;
  statusDot.classList.toggle("running", running);
  statusDot.classList.toggle("stopped", !running);
  statusText.textContent = running ? "Running" : "Stopped";
  if (s.config) applyConfigToInputs(s.config);
  if (!running) sweepRateEl.textContent = "0.0 Hz";
});

socket.on("sweep", (msg) => {
  lastSweep = { f0: msg.f0, f1: msg.f1, powers: msg.powers };
  drawFFT(msg.powers);
  pushWaterfallRow(msg.powers);
  updateSweepRate();
});

// ------------------------------------------------------------------
// UI wiring
// ------------------------------------------------------------------
function applyConfigToInputs(c) {
  document.getElementById("f_start").value = c.f_start_mhz;
  document.getElementById("f_stop").value  = c.f_stop_mhz;
  document.getElementById("bin_width").value = c.bin_width_hz;
  document.getElementById("lna").value  = c.lna_gain;
  document.getElementById("vga").value  = c.vga_gain;
  document.getElementById("amp").checked = !!c.amp_enable;
  updateSliderFills();
  updateSliderLabels();
  highlightPreset(c);
}

function readConfig() {
  return {
    f_start_mhz: parseInt(document.getElementById("f_start").value, 10),
    f_stop_mhz:  parseInt(document.getElementById("f_stop").value, 10),
    bin_width_hz: parseInt(document.getElementById("bin_width").value, 10),
    lna_gain: parseInt(document.getElementById("lna").value, 10),
    vga_gain: parseInt(document.getElementById("vga").value, 10),
    amp_enable: document.getElementById("amp").checked,
  };
}

function updateSliderFills() {
  for (const id of ["lna", "vga"]) {
    const el = document.getElementById(id);
    const pct = ((el.value - el.min) / (el.max - el.min)) * 100;
    el.style.setProperty("--fill", `${pct}%`);
  }
}
function updateSliderLabels() {
  document.getElementById("lna_val").textContent = `${document.getElementById("lna").value} dB`;
  document.getElementById("vga_val").textContent = `${document.getElementById("vga").value} dB`;
}

function highlightPreset(c) {
  document.querySelectorAll(".chip").forEach(b => {
    const match =
      parseInt(b.dataset.f0, 10) === c.f_start_mhz &&
      parseInt(b.dataset.f1, 10) === c.f_stop_mhz;
    b.classList.toggle("active", match);
  });
}

document.querySelectorAll(".chip").forEach(b => {
  b.addEventListener("click", () => {
    document.getElementById("f_start").value = b.dataset.f0;
    document.getElementById("f_stop").value  = b.dataset.f1;
    document.getElementById("bin_width").value = b.dataset.bin;
    highlightPreset(readConfig());
    socket.emit("start", readConfig());
  });
});

document.getElementById("btn-start").addEventListener("click", () => {
  socket.emit("start", readConfig());
});
document.getElementById("btn-stop").addEventListener("click", () => {
  socket.emit("stop");
});

for (const id of ["lna", "vga"]) {
  document.getElementById(id).addEventListener("input", () => {
    updateSliderFills();
    updateSliderLabels();
  });
}
updateSliderFills();
updateSliderLabels();

// hover readout
function handleHover(e) {
  if (!lastSweep) return;
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
