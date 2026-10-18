// Aetherscope — Auto-Scan mode. One mode module behind the Aether seam:
// it owns its socket handlers and DOM wiring, receives the core
// interface, and does not reach into other modes' globals.
"use strict";
(function (A) {
  const core = A.core;
  const socket = core.socket;
  const state = core.state;
  const html = core.html;
  const raw = core.raw;
  const flashClick = core.flashClick;
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
  scanResultsEl.innerHTML = html`<div class="event-empty">Scanning… results will appear here phase by phase.</div>`;
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
  scanResultsEl.innerHTML = html`<div class="event-empty">Scan failed.</div>`;
});
socket.on("scan_stopped", () => {
  resetPhases();
  scanResultsEl.innerHTML = html`<div class="event-empty">Scan stopped. Hit Start Scan to run again.</div>`;
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
    parts.push(`${k}=${v}`);
    if (parts.length >= 5) break;
  }
  return parts.join("  ");
}

function renderScanResults(isFinal, findings, summary) {
  const sections = [];

  if (isFinal && summary?.summary) {
    const s = summary.summary;
    sections.push(html`<div class="scan-summary">
      <div><div class="num">${s.peak_count}</div><span class="lbl">Spectrum peaks</span></div>
      <div><div class="num">${s.ism_433_devices}</div><span class="lbl">ISM 433 devices</span></div>
      <div><div class="num">${s.ism_915_devices}</div><span class="lbl">ISM 915 devices</span></div>
      <div><div class="num">${s.aircraft_count}</div><span class="lbl">Aircraft</span></div>
    </div>`);
  }

  const peaks = findings.sweep?.peaks || [];
  const peakRows = peaks.map(p => {
    const hint = p.decoder_hint
      ? html` <span class="hint-dec">→ ${p.decoder_hint}</span>`
      : "";
    const snr = typeof p.snr_db === "number" ? p.snr_db.toFixed(1) : "—";
    const peak = typeof p.peak_db === "number" ? p.peak_db.toFixed(1) : "—";
    return html`
        <div class="peak-row">
          <span class="freq">${fmtFreqHz(p.center_hz)}</span>
          <span class="band">${p.band || ""}${raw(hint)}</span>
          <span class="snr">${snr} dB</span>
          <span class="snr">${peak} dBFS</span>
        </div>`;
  }).join("");
  const peakBody = peaks.length === 0
    ? html`<div class="empty">No peaks above threshold yet.</div>`
    : peakRows;
  sections.push(html`<div class="scan-section">
    <h3>Spectrum Peaks (${peaks.length})</h3>
    ${raw(peakBody)}
  </div>`);

  for (const key of ["rtl433_433", "rtl433_915"]) {
    const phase = findings[key];
    const devices = phase?.devices || [];
    const label = key === "rtl433_433" ? "ISM 433 Devices" : "ISM 915 Devices";
    const deviceRows = devices.map(d => html`
          <div class="scan-device-row">
            <span class="model">${d.model || "unknown"}</span>
            <span class="fields">${summarizeDeviceFields(d)}</span>
            <span class="count">×${d._count}</span>
          </div>`).join("");
    const deviceBody = devices.length === 0
      ? html`<div class="empty">No devices decoded.</div>`
      : deviceRows;
    sections.push(html`<div class="scan-section">
      <h3>${label} (${devices.length})</h3>
      ${raw(deviceBody)}
    </div>`);
  }

  const aircraft = findings.adsb?.aircraft || [];
  const aircraftRows = aircraft.map(a => {
    const alt = a.alt_baro != null ? `${a.alt_baro} ft` : "—";
    const spd = a.gs != null ? `${Math.round(a.gs)} kts` : "—";
    const pos = a.lat != null ? `${a.lat.toFixed(3)}, ${a.lon.toFixed(3)}` : "no position";
    return html`
        <div class="scan-ac-row">
          <span class="hex">${(a.hex || "").toUpperCase()}</span>
          <span class="flight">${(a.flight || "").trim() || "—"}</span>
          <span class="alt">${alt}</span>
          <span class="spd">${spd}</span>
          <span class="pos">${pos}</span>
        </div>`;
  }).join("");
  const aircraftBody = aircraft.length === 0
    ? html`<div class="empty">No aircraft tracked.</div>`
    : aircraftRows;
  sections.push(html`<div class="scan-section">
    <h3>Aircraft (${aircraft.length})</h3>
    ${raw(aircraftBody)}
  </div>`);

  scanResultsEl.innerHTML = sections.join("");
}
core.registerApplyConfig((s) => { if (s.scan_config) applyScanConfigToInputs(s.scan_config); });
})(window.Aether = window.Aether || {});
