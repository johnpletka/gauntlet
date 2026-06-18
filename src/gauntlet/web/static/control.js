// Console control actions (P3): launch + abort via the sanctioned CLI-verb
// endpoints (POST /api/runs, POST /api/runs/<slug>/abort). A ~40-line vanilla
// shim, no build step (D5) — the control = sanctioned CLI verb invariant lives
// in the server; this just POSTs. Delegated listeners so the abort button keeps
// working after live.js swaps the detail region (P2).
(function () {
  "use strict";

  function authHeaders(token) {
    var h = { "Content-Type": "application/json" };
    if (token) h["X-Gauntlet-Token"] = token;
    return h;
  }

  function post(url, token, body) {
    var status = 0;
    return fetch(url, {
      method: "POST",
      headers: authHeaders(token),
      body: body ? JSON.stringify(body) : undefined,
    })
      .then(function (r) {
        status = r.status;
        return r.json().catch(function () { return {}; });
      })
      .then(function (data) {
        if (status < 200 || status >= 300) {
          throw new Error(data.detail || ("HTTP " + status));
        }
        return data;
      });
  }

  document.addEventListener("submit", function (e) {
    var form = e.target.closest && e.target.closest("[data-launch]");
    if (!form) return;
    e.preventDefault();
    var slug = (form.slug.value || "").trim();
    if (!slug) return;
    var body = {
      slug: slug,
      pipeline: (form.pipeline.value || "").trim() || null,
      no_judge: form.no_judge.checked,
    };
    post("/api/runs", form.getAttribute("data-token") || "", body)
      .then(function () { location.reload(); })
      .catch(function (err) { window.alert("Launch failed: " + err.message); });
  });

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-abort]");
    if (!btn) return;
    e.preventDefault();
    var slug = btn.getAttribute("data-slug");
    // Lightweight misclick guard; the full FR-10.7 destructive-verb confirm
    // token is P5.
    if (!window.confirm("Abort run " + slug + "? This stops an in-flight run.")) return;
    post("/api/runs/" + encodeURIComponent(slug) + "/abort", btn.getAttribute("data-token") || "", null)
      .then(function () { location.reload(); })
      .catch(function (err) { window.alert("Abort failed: " + err.message); });
  });
})();
