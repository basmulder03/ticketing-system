// Large-display "beamer/TV" view (Milestone 9 -- see PROJECT_BRIEF.md's
// Responsive & Multi-Device section). Reuses the shared countdown tick core
// from app/static/countdown.js (the same one app/static/public.js's
// pre-sale countdown uses) so the "time remaining" math/cadence is
// identical everywhere in this app, not reimplemented here.
//
// Deliberately the ONLY script this page loads (see
// app/templates/beamer/base.html) -- no share buttons, no checkout form, no
// language switcher, no copy-link handlers: this page has no interactive
// elements at all, so there is nothing else here to wire up.
(function () {
  "use strict";

  function init() {
    var root = document.querySelector(".beamer-page");
    var countdownEl = document.getElementById("beamer-countdown");
    var valueEl = document.getElementById("beamer-countdown-value");
    var doorsOpenEl = document.getElementById("beamer-doors-open");
    var announcerEl = document.getElementById("beamer-doors-announcer");
    if (!root || !countdownEl || !valueEl || !doorsOpenEl || !window.BeaconCountdown) return;

    var doorsAtRaw = root.getAttribute("data-doors-at");
    if (!doorsAtRaw) return;
    var doorsAt = new Date(doorsAtRaw).getTime();
    if (isNaN(doorsAt)) return;

    var unitDays = countdownEl.getAttribute("data-unit-days") || "d";
    var unitHours = countdownEl.getAttribute("data-unit-hours") || "h";
    var unitMinutes = countdownEl.getAttribute("data-unit-minutes") || "m";
    var unitSeconds = countdownEl.getAttribute("data-unit-seconds") || "s";
    var liveAnnouncement = countdownEl.getAttribute("data-live-announcement") || "";

    window.BeaconCountdown.start(
      doorsAt,
      function (remainingMs) {
        var d = window.BeaconCountdown.breakdown(remainingMs);
        valueEl.textContent =
          d.days + unitDays + " " + d.hours + unitHours + " " + d.minutes + unitMinutes + " " + d.seconds + unitSeconds;
      },
      function () {
        // Doors time has passed: swap the ticking countdown for the
        // standing "doors open" state, same pattern as public.js's own
        // pre-sale countdown flipping to its buy section, and per this
        // view's brief: "show a simple 'Doors are open' ... message rather
        // than a negative/broken countdown".
        countdownEl.hidden = true;
        doorsOpenEl.hidden = false;
        if (announcerEl && liveAnnouncement) announcerEl.textContent = liveAnnouncement;
      }
    );
  }

  document.addEventListener("DOMContentLoaded", init);
})();
