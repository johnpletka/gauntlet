// Gauntlet Console — live updates (P2, FR-8.2; D5: no build step, no framework).
//
// A ~30-line vendored vanilla shim standing in for "HTMX-driven live updates":
// open one EventSource to the page's `data-sse` channel and, on each transition,
// re-fetch every `[data-live-src]` region and swap its innerHTML. The server
// renders those regions as partials (/partials/runs, /partials/runs/{slug}) that
// are byte-identical to what the full page embeds, so a live swap == a reload.
//
// The stream only carries *signals* (snapshot/transition); the authoritative
// HTML always comes from the partial fetch, so a dropped-then-reconnected
// EventSource re-reads current state on its reconnect snapshot with no special
// handling. A semantic no-op on the server emits nothing, so quiet runs cause no
// fetches.
(function () {
  "use strict";
  if (typeof EventSource === "undefined") return;

  function regions() {
    return document.querySelectorAll("[data-live-src]");
  }

  function refresh() {
    regions().forEach(function (el) {
      fetch(el.getAttribute("data-live-src"), { credentials: "same-origin" })
        .then(function (resp) {
          return resp.ok ? resp.text() : null;
        })
        .then(function (html) {
          if (html !== null) el.innerHTML = html;
        })
        .catch(function () {
          /* transient fetch error: the next transition refreshes us */
        });
    });
  }

  // Build a same-origin deep link to a run that authenticates under the active
  // P1–P6 `?token=` scheme (review F-002). The in-tab notification is delivered
  // to a tab that is already authenticated, so we carry *that tab's* token from
  // the current URL rather than embedding it in the notification payload (which
  // would leak the serve token to every channel). P7's cookie login retires the
  // query token, at which point the bare `/runs/<slug>` link suffices.
  function runLink(slug) {
    var target = "/runs/" + encodeURIComponent(slug);
    var tok = new URLSearchParams(window.location.search).get("token");
    if (tok) target += "?token=" + encodeURIComponent(tok);
    return target;
  }

  // In-tab notification (P6, FR-9.2): the `notify` SSE event carries a
  // deduplicated Notification for the four "needs a human" moments. We ask for
  // permission lazily (on the first notify) and fail soft if the browser has no
  // Notification API or the user denied it — a notification can never break the
  // live view.
  function notify(ev) {
    if (typeof Notification === "undefined") return;
    var data;
    try {
      data = JSON.parse(ev.data);
    } catch (e) {
      return;
    }
    function show() {
      if (Notification.permission !== "granted") return;
      try {
        var n = new Notification(data.title, { body: data.body, tag: data.run_id });
        n.onclick = function () {
          window.open(runLink(data.slug), "_blank");
        };
      } catch (e) {
        /* fail soft: notification is best-effort */
      }
    }
    if (Notification.permission === "default") {
      Notification.requestPermission().then(show);
    } else {
      show();
    }
  }

  var channel = document.body.getAttribute("data-sse");
  if (!channel || regions().length === 0) return;

  var source = new EventSource(channel);
  // `snapshot` fires on (re)connect; `transition` on each edge-triggered change;
  // `notify` on a deduplicated FR-9.1 transition kind (gate / escalation / fail /
  // complete).
  source.addEventListener("snapshot", refresh);
  source.addEventListener("transition", refresh);
  source.addEventListener("notify", notify);
})();
