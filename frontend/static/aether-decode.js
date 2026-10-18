// Aetherscope — decoded events (Decode mode). One mode module behind the Aether seam:
// it owns its socket handlers and DOM wiring, receives the core
// interface, and does not reach into other modes' globals.
"use strict";
(function (A) {
  const core = A.core;
  const socket = core.socket;
  const state = core.state;
  const html = core.html;
  const raw = core.raw;
  const updateSliderFills = core.updateSliderFills;
  const flashClick = core.flashClick;
const MAX_EVENTS = 500;
let events = [];
let eventFilter = "";
const eventCountEl = document.getElementById("event-count");

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
    parts.push(html`<span class="k">${k}=</span><span class="v">${v}</span>`);
    if (parts.length >= 6) break;
  }
  return parts.join("  ");
}

function renderEvents() {
  if (events.length === 0) {
    eventListEl.innerHTML = html`<div class="event-empty">No events yet. Hit <strong>Start Decoder</strong> in the sidebar to begin.</div>`;
    return;
  }
  const filtered = events.filter(eventMatchesFilter);
  const rows = filtered.map(ev => {
    const time = fmtTime(ev.time);
    const model = ev.model || "unknown";
    const fields = summarizeFields(ev);
    const rssi = typeof ev.rssi_db === "number"
      ? `${ev.rssi_db.toFixed(0)} dB`
      : (ev.rssi != null ? String(ev.rssi) : "");
    return html`<div class="event-row">
      <span class="event-time">${time}</span>
      <span><span class="event-model">${model}</span> <span class="event-fields">${raw(fields)}</span></span>
      <span class="event-rssi">${rssi}</span>
    </div>`;
  });
  eventListEl.innerHTML = rows.join("");
}

socket.on("decoded", (ev) => {
  events.unshift(ev);
  if (events.length > MAX_EVENTS) events.length = MAX_EVENTS;
  eventCountEl.textContent = String(events.length);
  if (state.currentMode === "decode") renderEvents();
});

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

function highlightBandButton(freqMhz) {
  document.querySelectorAll(".band-btn").forEach(b => {
    b.classList.toggle("active", Math.abs(parseFloat(b.dataset.freq) - freqMhz) < 0.01);
  });
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

document.getElementById("btn-start-decode").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "start_decode");
  socket.emit("start_decode", readDecodeConfig());
});
document.getElementById("btn-stop-decode").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "stop (decode)");
  socket.emit("stop");
});

document.getElementById("dec_gain").addEventListener("input", e => {
  document.getElementById("dec_gain_val").textContent = `${e.target.value} dB`;
  updateSliderFills(["dec_gain"]);
});
updateSliderFills(["dec_gain"]);
core.registerApplyConfig((s) => { if (s.decode_config) applyDecodeConfigToInputs(s.decode_config); });
})(window.Aether = window.Aether || {});
