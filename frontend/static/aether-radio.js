// Aetherscope — Radio listening, bookmarks, IQ playback and mark scanning (playback). One mode module behind the Aether seam:
// it owns its socket handlers and DOM wiring, receives the core
// interface, and does not reach into other modes' globals.
"use strict";
(function (A) {
  const core = A.core;
  const socket = core.socket;
  const state = core.state;
  const html = core.html;
  const raw = core.raw;
  const showToast = core.showToast;
  const setMode = core.setMode;
  const updateSliderFills = core.updateSliderFills;
  const flashClick = core.flashClick;
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

let bookmarks = [];

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
    const empty = bookmarks.length
      ? "No bookmarks match the filter."
      : "No bookmarks yet. Tune to a frequency and click + Add.";
    listEl.innerHTML = html`<div class="event-empty">${empty}</div>`;
    return;
  }
  const rows = filtered.map(bm => {
    const label = bm.label || "";
    const freq  = fmtBookmarkFreq(bm.freq_hz);
    const demod = (bm.demod || "fm").toUpperCase();
    const tags  = (bm.tags || []).map(t => html`<span class="bm-tag">${t}</span>`).join(" ");
    const tagsBlock = tags ? html`<span class="bm-tags">${raw(tags)}</span>` : "";
    const id    = String(Number(bm.id));  // numeric-only; safe in data-id attributes
    return html`<div class="bm-row" data-id="${id}">
      <div class="bm-main">
        <span class="bm-label">${label}</span>
        <span class="bm-freq">${freq}</span>
        <span class="bm-demod">${demod}</span>
        ${raw(tagsBlock)}
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
  if (state.serverMode === "scan_radio") { socket.emit("stop"); stopRadioPlayback(); return; }
  const freqs = A.sweep.getMarks().map(m => m.hz / 1e6);
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
core.registerApplyConfig((s) => {
  if (s.radio_config) applyRadioConfigToInputs(s.radio_config);
  if (Array.isArray(s.bookmarks)) { bookmarks = s.bookmarks; renderBookmarks(); }
});
core.registerRestoreSettings((st) => {
  if (Number.isFinite(st.last_radio_freq)) radioFreqEl.value = st.last_radio_freq;
  if (st.last_demod) setRadioDemod(st.last_demod);
  if (typeof st.radio_volume === "number") {
    radioVolEl.value = st.radio_volume;
    radioVolVal.textContent = `${st.radio_volume}%`;
    updateSliderFills(["radio_vol"]);
    if (radioGain) radioGain.gain.value = radioVolume();
  }
});
core.registerLeftMode("radio", stopRadioPlayback);
core.registerLeftMode("scan_radio", stopRadioPlayback);

A.radio = { tuneToMark, startIqPlay, renderBookmarks, stopRadioPlayback };
})(window.Aether = window.Aether || {});
