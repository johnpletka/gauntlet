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

  function runUrl(slug, verb) {
    return "/api/runs/" + encodeURIComponent(slug) + "/" + verb;
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-abort]");
    if (!btn) return;
    e.preventDefault();
    var slug = btn.getAttribute("data-slug");
    // FR-10.7 destructive-verb confirm: the misclick guard here is paired with
    // the server-side `confirm: true` requirement (the security boundary stays
    // loopback + token).
    if (!window.confirm("Abort run " + slug + "? This stops an in-flight run.")) return;
    post(runUrl(slug, "abort"), btn.getAttribute("data-token") || "", { confirm: true })
      .then(function () { location.reload(); })
      .catch(function (err) { window.alert("Abort failed: " + err.message); });
  });

  // P5 gate / recovery control forms (FR-4.4/FR-5/FR-10.7). The forms live in a
  // container carrying data-slug/data-token; each form declares its verb via a
  // data-* attribute. resume_intel decides which forms are rendered, so only
  // meaningful verbs ever appear (FR-5.3).
  document.addEventListener("submit", function (e) {
    var form = e.target.closest && e.target.closest("[data-approve],[data-reject],[data-resume]");
    if (!form) return;
    var box = form.closest("[data-slug]");
    if (!box) return;
    e.preventDefault();
    var slug = box.getAttribute("data-slug");
    var token = box.getAttribute("data-token") || "";
    var verb, body;
    if (form.hasAttribute("data-approve")) {
      verb = "approve";
      body = { notes: (form.notes && form.notes.value.trim()) || null };
    } else if (form.hasAttribute("data-resume")) {
      verb = "resume";
      body = null;
    } else {
      verb = "reject";
      var notes = (form.notes && form.notes.value.trim()) || "";
      if (!notes) { window.alert("Reject requires notes."); return; }
      // FR-10.7 destructive-verb confirm for reject.
      if (!window.confirm("Reject this gate? The run fails.")) return;
      body = { notes: notes, confirm: true };
    }
    post(runUrl(slug, verb), token, body)
      .then(function () { location.reload(); })
      .catch(function (err) { window.alert(verb + " failed: " + err.message); });
  });
})();
