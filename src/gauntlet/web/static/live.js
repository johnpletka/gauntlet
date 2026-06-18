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

  var channel = document.body.getAttribute("data-sse");
  if (!channel || regions().length === 0) return;

  var source = new EventSource(channel);
  // `snapshot` fires on (re)connect; `transition` on each edge-triggered change.
  source.addEventListener("snapshot", refresh);
  source.addEventListener("transition", refresh);
})();
