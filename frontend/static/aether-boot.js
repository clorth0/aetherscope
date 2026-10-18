// Aetherscope bootstrap: loaded last, after every mode module has
// registered itself with the core. Preserves the monolith's initial
// calls, in the same order.
"use strict";
(function (A) {
  A.sweep.renderMarks();
  A.radio.renderBookmarks();
  A.core.setMode("sweep");
  requestAnimationFrame(A.sweep.fitAll);
})(window.Aether = window.Aether || {});
