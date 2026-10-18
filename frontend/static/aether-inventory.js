// Aetherscope — contact inventory (contacts). One mode module behind the Aether seam:
// it owns its socket handlers and DOM wiring, receives the core
// interface, and does not reach into other modes' globals.
"use strict";
(function (A) {
  const core = A.core;
  const socket = core.socket;
  const state = core.state;
  const html = core.html;
  const fmtAgo = core.fmtAgo;
// ---- Device inventory (persistent "what's around me" catalog) ----
let contacts = [];
let invFilter = "";
const inventoryListEl = document.getElementById("inventory-list");
const invFilterEl = document.getElementById("inv-filter");

function infoSummary(info) {
  if (!info || typeof info !== "object") return "";
  return Object.keys(info).filter(k => info[k] != null)
    .slice(0, 4).map(k => `${k}=${info[k]}`).join(" · ");
}
function renderInventory() {
  if (!inventoryListEl) return;
  if (!contacts.length) {
    inventoryListEl.innerHTML = html`<div class="event-empty">No contacts yet. Run ADS-B or Decode to populate the inventory.</div>`;
    return;
  }
  const filtered = contacts.filter(c => {
    if (!invFilter) return true;
    const blob = `${c.label} ${c.ident} ${c.kind} ${(c.info && c.info.country) || ""}`.toLowerCase();
    return blob.includes(invFilter);
  });
  const header = html`<div class="inv-header">
    <span>Kind</span><span>Label</span><span>Ident</span><span>Info</span>
    <span>Count</span><span>First</span><span>Last</span><span>Location</span>
  </div>`;
  const rows = filtered.map(c => {
    const loc = (c.lat != null && c.lon != null) ? `${c.lat.toFixed(3)}, ${c.lon.toFixed(3)}` : "—";
    return html`<div class="inv-row">
      <span class="inv-kind ${c.kind}">${c.kind}</span>
      <span>${c.label || ""}</span>
      <span class="mono">${String(c.ident || "")}</span>
      <span class="inv-info">${infoSummary(c.info)}</span>
      <span>${c.count}</span>
      <span>${fmtAgo(c.first_seen)}</span>
      <span>${fmtAgo(c.last_seen)}</span>
      <span class="mono">${loc}</span>
    </div>`;
  }).join("");
  inventoryListEl.innerHTML = header + rows;
}
socket.on("contacts", (msg) => {
  contacts = (msg && msg.data) || [];
  if (state.currentMode === "inventory") renderInventory();
});
if (invFilterEl) {
  invFilterEl.addEventListener("input", () => {
    invFilter = invFilterEl.value.trim().toLowerCase();
    renderInventory();
  });
}
document.getElementById("btn-clear-inventory").addEventListener("click", () => {
  if (confirm("Clear the entire device inventory?")) socket.emit("clear_inventory");
});
core.registerEnterMode("inventory", () => socket.emit("list_inventory"));
})(window.Aether = window.Aether || {});
