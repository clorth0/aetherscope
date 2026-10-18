// Aetherscope — capture controls and capture list. One mode module behind the Aether seam:
// it owns its socket handlers and DOM wiring, receives the core
// interface, and does not reach into other modes' globals.
"use strict";
(function (A) {
  const core = A.core;
  const socket = core.socket;
  const state = core.state;
  const html = core.html;
  const raw = core.raw;
  const fmtAgo = core.fmtAgo;
  const showToast = core.showToast;
  const setMode = core.setMode;
  const updateSliderFills = core.updateSliderFills;
  const flashClick = core.flashClick;
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

function renderCaptures() {
  if (!captures.length) {
    capturesListEl.innerHTML = html`<div class="event-empty">No captures yet. Configure in the sidebar and hit Record.</div>`;
    return;
  }
  const filtered = captures.filter(c => {
    if (!capFilter) return true;
    const blob = `${c.name} ${c.label} ${c.user_label || ""} ${c.freq_hz}`.toLowerCase();
    return blob.includes(capFilter);
  });
  capturesListEl.innerHTML = filtered.map(c => {
    const displayName = c.user_label || c.label || c.name;
    const tags = Array.isArray(c.tags) ? c.tags : [];
    const tagsHtml = tags.length
      ? tags.map(t => html`<span class="bm-tag">${t}</span>`).join(" ")
      : "";
    const tagsBlock = tagsHtml ? html`<div class="cap-tags">${raw(tagsHtml)}</div>` : "";
    const missingBadge = c.missing ? html`<span class="cap-missing-badge">missing</span>` : "";
    const geo = c.geolocation;
    const hasGeo = geo && typeof geo.lat === "number" && typeof geo.lon === "number";
    const geoBadge = hasGeo
      ? html`<span class="cap-geo-badge" title="lat ${geo.lat.toFixed(6)}, lon ${geo.lon.toFixed(6)}">located</span>`
      : (c.geolocation_redacted ? html`<span class="cap-geo-badge redacted">location removed</span>` : "");
    const redactBtn = hasGeo ? html`<button data-redact="${c.name}">Remove location</button>` : "";
    const isAudio = c.kind === "audio";
    const kindBadge = isAudio ? html`<span class="cap-kind-badge">audio</span>` : "";
    const sigmfName = c.sigmf_name || "";  // server-derived in backend/catalogue.py; JS no longer re-derives it
    const sigmfLink = (c.sigmf && sigmfName)
      ? html`<a href="/captures/${encodeURIComponent(sigmfName)}" download title="SigMF metadata (portable to other SDR tools)">SigMF</a>`
      : "";
    const dur = (c.duration_s || 0).toFixed(1);
    const detail = isAudio
      ? html`${fmtMHz(c.freq_hz)}<span class="sep">·</span>${(c.demod || "").toUpperCase()}<span class="sep">·</span>${dur}s<span class="sep">·</span>${fmtBytes(c.file_size)}`
      : html`${fmtMHz(c.freq_hz)}<span class="sep">·</span>${fmtMSps(c.sample_rate)}<span class="sep">·</span>${dur}s<span class="sep">·</span>${fmtBytes(c.file_size)}`;
    const metaTail = isAudio
      ? (c.audio_rate ? `audio ${Math.round(c.audio_rate / 1000)} kHz` : "audio")
      : (c.sample_format || "");
    const playOrReplay = isAudio
      ? html`<button data-play="${c.name}">Play</button>`
      : html`<button data-replay="${c.name}">Replay</button> <button data-listen="${c.name}">Listen</button> <button data-decode-file="${c.name}">Decode</button>`;
    return html`
    <div class="cap-row">
      <div class="cap-main">
        <div class="cap-name">${displayName}${raw(kindBadge)}${raw(missingBadge)}${raw(geoBadge)}</div>
        ${raw(tagsBlock)}
        <div class="cap-detail">${raw(detail)}</div>
        <div class="cap-meta">${c.name} · ${fmtAgo(c.started_at)} · ${metaTail}</div>
      </div>
      <div class="cap-actions">
        <a href="/captures/${encodeURIComponent(c.name)}" download>Download</a>
        ${raw(sigmfLink)}
        <button data-edit="${c.name}">Edit</button>
        ${raw(playOrReplay)}
        ${raw(redactBtn)}
        <button class="danger" data-delete="${c.name}">Delete</button>
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
    btn.addEventListener("click", () => A.radio.startIqPlay(btn.dataset.listen, "fm", 0));
  });
  capturesListEl.querySelectorAll("[data-decode-file]").forEach(btn => {
    btn.addEventListener("click", () => {
      socket.emit("decode_capture", { name: btn.dataset.decodeFile });
      setMode("decode");   // events stream into the Decode panel
    });
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

function applyGainsToCaptureUI(lna, vga, amp) {
  document.getElementById("cap_lna").value = lna;
  document.getElementById("cap_vga").value = vga;
  document.getElementById("cap_amp").checked = !!amp;
  document.getElementById("cap_lna_val").textContent = `${lna} dB`;
  document.getElementById("cap_vga_val").textContent = `${vga} dB`;
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
core.registerApplyConfig((s) => { if (s.capture_config) applyCaptureConfigToInputs(s.capture_config); });
core.registerEnterMode("capture", () => socket.emit("list_captures"));
})(window.Aether = window.Aether || {});
