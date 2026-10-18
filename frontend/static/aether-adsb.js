// Aetherscope — ADS-B map and aircraft list. One mode module behind the Aether seam:
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
  const updateSliderFills = core.updateSliderFills;
  const flashClick = core.flashClick;
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
  const s = state.gps;
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
  const angle = (Number.isFinite(Number(track)) ? Number(track) : 0).toFixed(0);
  const c = color || "#4dd0e1";
  const svg = html`
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
    list.innerHTML = html`<div class="event-empty">No aircraft yet. Start ADS-B in the sidebar.</div>`;
    return;
  }
  const header = html`<div class="aircraft-header">
    <span>Hex</span><span>Flight</span><span>Alt (ft)</span>
    <span>Speed</span><span>Track</span><span>Position</span><span>RSSI</span>
  </div>`;
  const rows = filtered
    .sort((a, b) => (a.seen || 0) - (b.seen || 0))
    .map(a => {
      const hasPos = a.lat != null && a.lon != null;
      return html`<div class="ac-row ${hasPos ? "" : "no-pos"}">
        <span class="ac-hex">${(a.hex || "").toUpperCase()}</span>
        <span class="ac-flight">${(a.flight || "").trim() || "—"}</span>
        <span class="ac-alt">${fmtAlt(a.alt_baro)}</span>
        <span class="ac-spd">${fmtKnots(a.gs)}</span>
        <span class="ac-track">${fmtTrack(a.track)}</span>
        <span class="ac-pos">${fmtPos(a.lat, a.lon)}</span>
        <span class="ac-rssi">${fmtRssi(a.rssi)}</span>
      </div>`;
    }).join("");
  list.innerHTML = header + rows;
}

function haversineNm(lat1, lon1, lat2, lon2) {
  const R = 3440.065, toRad = d => d * Math.PI / 180;
  const dLat = toRad(lat2 - lat1), dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}
function bearingDeg(lat1, lon1, lat2, lon2) {
  const toRad = d => d * Math.PI / 180, toDeg = r => r * 180 / Math.PI;
  const dLon = toRad(lon2 - lon1);
  const y = Math.sin(dLon) * Math.cos(toRad(lat2));
  const x = Math.cos(toRad(lat1)) * Math.sin(toRad(lat2)) -
    Math.sin(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.cos(dLon);
  return (toDeg(Math.atan2(y, x)) + 360) % 360;
}
function getRxPosition() {
  const lat = parseFloat(document.getElementById("adsb_lat").value);
  const lon = parseFloat(document.getElementById("adsb_lon").value);
  if (Number.isFinite(lat) && Number.isFinite(lon)) return [lat, lon];
  if (state.gps && state.gps.enabled && state.gps.lat != null && !state.gps.stale)
    return [state.gps.lat, state.gps.lon];
  return null;
}
let adsbRings = [];
let adsbRingsKey = null;
function maybeDrawRings() {
  if (!adsbMap) return;
  const rx = getRxPosition();
  const key = rx ? rx.join(",") : null;
  if (key === adsbRingsKey) return;
  adsbRingsKey = key;
  for (const r of adsbRings) adsbMap.removeLayer(r);
  adsbRings = [];
  if (!rx) return;
  for (const nm of [50, 100, 150]) {
    const c = L.circle(rx, { radius: nm * 1852, color: "#3a4a6a", weight: 1,
      fill: false, dashArray: "4 6", interactive: false }).addTo(adsbMap);
    c.bindTooltip(`${nm} nm`);
    adsbRings.push(c);
  }
}

function updateAdsbMap(aircraft, meta) {
  if (!adsbMap) return;
  maybeDrawRings();
  const rx = getRxPosition();
  const seenHex = new Set();
  let withPos = 0, maxRangeNm = 0;
  for (const a of aircraft) {
    if (a.lat == null || a.lon == null) continue;
    withPos++;
    seenHex.add(a.hex);
    const flight = (a.flight || "").trim();
    let distStr = "";
    if (rx) {
      const d = haversineNm(rx[0], rx[1], a.lat, a.lon);
      if (d > maxRangeNm) maxRangeNm = d;
      distStr = html`Range: ${d.toFixed(0)} nm @ ${bearingDeg(rx[0], rx[1], a.lat, a.lon).toFixed(0)}&deg;<br>`;
    }
    const reg = a.registration ? html`Reg: ${a.registration}<br>` : "";
    const country = a.country ? html`${a.country}<br>` : "";
    const hexLine = flight ? html`${a.hex.toUpperCase()}<br>` : "";
    const popup = html`<b>${flight || a.hex.toUpperCase()}</b><br>
      ${raw(hexLine)}
      ${raw(reg)}${raw(country)}${raw(distStr)}
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
  let rateStr = "";
  if (meta && typeof meta.messages === "number" && typeof meta.now === "number") {
    if (adsbMsgPrev != null && meta.now > adsbMsgPrevT) {
      const r = (meta.messages - adsbMsgPrev) / (meta.now - adsbMsgPrevT);
      if (r >= 0) rateStr = ` · ${r.toFixed(0)} msg/s`;
    }
    adsbMsgPrev = meta.messages;
    adsbMsgPrevT = meta.now;
  }
  const rangeStr = maxRangeNm > 0 ? ` · max ${maxRangeNm.toFixed(0)} nm` : "";
  const statsEl = document.getElementById("adsb-stats");
  if (statsEl) statsEl.textContent = `${aircraft.length} tracked · ${withPos} with position${rangeStr}${rateStr}`;
}
let adsbMsgPrev = null;
let adsbMsgPrevT = 0;

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
  if (!state.gps || !state.gps.enabled || state.gps.lat == null || state.gps.stale) {
    showToast("warn", "No GPS fix to use. Enable GPS and wait for a fix.");
    return;
  }
  document.getElementById("adsb_lat").value = state.gps.lat;
  document.getElementById("adsb_lon").value = state.gps.lon;
  showToast("info", "Receiver location set from GPS.");
});

document.getElementById("btn-stop-adsb").addEventListener("click", (e) => {
  flashClick(e.currentTarget, "stop (adsb)");
  socket.emit("stop");
});

socket.on("adsb", (msg) => {
  adsbAircraft = msg.aircraft || [];
  if (state.currentMode === "adsb") {
    updateAdsbMap(adsbAircraft, msg.meta);
    renderAircraftList();
  }
});
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
  if (state.currentMode === "adsb") renderAircraftList();
}

core.registerApplyConfig((s) => { if (s.adsb_config) applyAdsbConfigToInputs(s.adsb_config); });
core.registerEnterMode("adsb", initAdsbMap);
core.registerLeftMode("adsb", clearAdsbVisuals);
core.registerGpsHook(updateGpsMarker);
})(window.Aether = window.Aether || {});
