// PROTOTYPE. Not imported by the frontend.
// A tagged template escapes every interpolated value. Markup the caller
// built on purpose is wrapped in raw() so the tag can see the intent.

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function raw(markup) {
  return { __html: String(markup) };
}

function html(strings, ...values) {
  let out = "";
  strings.forEach((part, i) => {
    out += part;
    if (i >= values.length) return;
    const value = values[i];
    if (value && typeof value === "object" && Object.hasOwn(value, "__html")) {
      out += value.__html;
      return;
    }
    out += escapeHtml(value ?? "");
  });
  return out;
}

const payload = '<img src=x onerror=alert(1)>';
const safe = html`<span class="event-model">${payload}</span>`;
if (safe.includes("<img")) {
  console.error("tagged template leaked a tag:", safe);
  process.exit(1);
}
if (!safe.includes("&lt;img")) {
  console.error("tagged template did not escape the payload:", safe);
  process.exit(1);
}

const nested = html`<div>${raw(html`<b>${payload}</b>`)}</div>`;
if (nested.includes("<img") || !nested.includes("<b>")) {
  console.error("raw() wrapper failed:", nested);
  process.exit(1);
}

const leaked = `<span class="event-model">${payload}</span>`;
if (!leaked.includes("<img")) {
  console.error("the control string was supposed to leak");
  process.exit(1);
}

console.log("tagged template escaped the payload");
console.log(safe);
console.log("plain template leaked it");
console.log(leaked);
