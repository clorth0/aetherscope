// hackrf-web frontend: socket.io client + canvas FFT + scrolling waterfall.

const socket = io();

const fftCanvas = document.getElementById("fft");
const waterfallCanvas = document.getElementById("waterfall");
const fftCtx = fftCanvas.getContext("2d");
const wfCtx = waterfallCanvas.getContext("2d", { willReadFrequently: true });

const statusDot = document.getElementById("status");
const hoverFreqEl = document.getElementById("hover-freq");
const hoverPowerEl = document.getElementById("hover-power");
const sweepRateEl = document.getElementById("sweep-rate");

let lastSweep = null;       // { f0, f1, powers: Float32Array }
let sweepTimestamps = [];
const POWER_MIN = -100;     // dB
const POWER_MAX = -20;

// Resize canvases to actual pixel size
function fitCanvas(c) {
  const r = c.getBoundingClientRect();
  if (c.width !== Math.floor(r.width) || c.height !== c.clientHeight) {
    c.width = Math.floor(r.width);
    c.height = c.clientHeight;
  }
}
window.addEventListener("resize", () => { fitCanvas(fftCanvas); fitCanvas(waterfallCanvas); });
fitCanvas(fftCanvas);
fitCanvas(waterfallCanvas);

// Viridis-ish colormap
function colorFor(dB) {
  const t = Math.max(0, Math.min(1, (dB - POWER_MIN) / (POWER_MAX - POWER_MIN)));
  // simple blue->cyan->green->yellow->red gradient
  const stops = [
    [0.00, 0,   0,   40],
    [0.20, 0,   60,  140],
    [0.40, 0,   180, 180],
    [0.60, 80,  220, 60],
    [0.80, 240, 220, 40],
    [1.00, 255, 60,  40],
  ];
  for (let i = 0; i < stops.length - 1; i++) {
    const [a, ar, ag, ab] = stops[i];
    const [b, br, bg, bb] = stops[i + 1];
    if (t >= a && t <= b) {
      const k = (t - a) / (b - a);
      return [ar + (br - ar) * k, ag + (bg - ag) * k, ab + (bb - ab) * k];
    }
  }
  return [255, 60, 40];
}

function resampleToWidth(powers, width) {
  // Nearest-neighbor; cheap. Replace with proper resampler later.
  const out = new Float32Array(width);
  if (powers.length === 0) return out;
  for (let i = 0; i < width; i++) {
    const idx = Math.floor((i / width) * powers.length);
    out[i] = powers[idx];
  }
  return out;
}

function drawFFT(powers) {
  const w = fftCanvas.width;
  const h = fftCanvas.height;
  fftCtx.fillStyle = "#000";
  fftCtx.fillRect(0, 0, w, h);

  // gridlines
  fftCtx.strokeStyle = "#222";
  fftCtx.lineWidth = 1;
  for (let dB = -100; dB <= 0; dB += 20) {
    const y = h - ((dB - POWER_MIN) / (POWER_MAX - POWER_MIN)) * h;
    fftCtx.beginPath();
    fftCtx.moveTo(0, y);
    fftCtx.lineTo(w, y);
    fftCtx.stroke();
  }

  // trace
  fftCtx.strokeStyle = "#4dd0e1";
  fftCtx.lineWidth = 1;
  fftCtx.beginPath();
  const samples = resampleToWidth(powers, w);
  for (let i = 0; i < w; i++) {
    const dB = samples[i];
    const y = h - ((dB - POWER_MIN) / (POWER_MAX - POWER_MIN)) * h;
    if (i === 0) fftCtx.moveTo(i, y); else fftCtx.lineTo(i, y);
  }
  fftCtx.stroke();
}

function pushWaterfallRow(powers) {
  const w = waterfallCanvas.width;
  const h = waterfallCanvas.height;
  // shift everything down by 1px
  const img = wfCtx.getImageData(0, 0, w, h - 1);
  wfCtx.putImageData(img, 0, 1);
  // draw new row at y=0
  const samples = resampleToWidth(powers, w);
  const row = wfCtx.createImageData(w, 1);
  for (let i = 0; i < w; i++) {
    const [r, g, b] = colorFor(samples[i]);
    row.data[i * 4 + 0] = r;
    row.data[i * 4 + 1] = g;
    row.data[i * 4 + 2] = b;
    row.data[i * 4 + 3] = 255;
  }
  wfCtx.putImageData(row, 0, 0);
}

function updateSweepRate() {
  const now = performance.now();
  sweepTimestamps.push(now);
  sweepTimestamps = sweepTimestamps.filter(t => now - t < 2000);
  const rate = sweepTimestamps.length / 2.0;
  sweepRateEl.textContent = `${rate.toFixed(1)} Hz`;
}

socket.on("status", (s) => {
  statusDot.classList.toggle("running", !!s.running);
  statusDot.classList.toggle("stopped", !s.running);
  statusDot.title = s.running ? "running" : "stopped";
  if (s.config) applyConfigToInputs(s.config);
});

socket.on("sweep", (msg) => {
  lastSweep = { f0: msg.f0, f1: msg.f1, powers: msg.powers };
  drawFFT(msg.powers);
  pushWaterfallRow(msg.powers);
  updateSweepRate();
});

function applyConfigToInputs(c) {
  document.getElementById("f_start").value = c.f_start_mhz;
  document.getElementById("f_stop").value = c.f_stop_mhz;
  document.getElementById("bin_width").value = c.bin_width_hz;
  document.getElementById("lna").value = c.lna_gain;
  document.getElementById("vga").value = c.vga_gain;
  document.getElementById("amp").checked = !!c.amp_enable;
}

function readConfig() {
  return {
    f_start_mhz: parseInt(document.getElementById("f_start").value, 10),
    f_stop_mhz: parseInt(document.getElementById("f_stop").value, 10),
    bin_width_hz: parseInt(document.getElementById("bin_width").value, 10),
    lna_gain: parseInt(document.getElementById("lna").value, 10),
    vga_gain: parseInt(document.getElementById("vga").value, 10),
    amp_enable: document.getElementById("amp").checked,
  };
}

document.getElementById("btn-start").addEventListener("click", () => {
  socket.emit("start", readConfig());
});
document.getElementById("btn-stop").addEventListener("click", () => {
  socket.emit("stop");
});

// Hover readout on FFT canvas
fftCanvas.addEventListener("mousemove", (e) => {
  if (!lastSweep) return;
  const rect = fftCanvas.getBoundingClientRect();
  const x = (e.clientX - rect.left) / rect.width;
  const freqHz = lastSweep.f0 + x * (lastSweep.f1 - lastSweep.f0);
  const idx = Math.floor(x * lastSweep.powers.length);
  const dB = lastSweep.powers[Math.max(0, Math.min(lastSweep.powers.length - 1, idx))];
  hoverFreqEl.textContent = `${(freqHz / 1e6).toFixed(3)} MHz`;
  hoverPowerEl.textContent = `${dB.toFixed(1)} dBFS`;
});
