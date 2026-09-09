// Shared countdown-tick core (Milestone 9): factored out of
// app/static/public.js's original pre-sale countdown (Milestone 2) so the
// large-display "beamer" view (app/static/beamer.js) reuses the exact same
// tick cadence/remaining-time math instead of a second implementation --
// see PROJECT_BRIEF.md's instruction to reuse the landing page's existing
// countdown logic rather than reinventing it for the beamer view.
//
// No framework, no build step, consistent with public.js's own module
// comment. This file only computes; it never touches the DOM itself --
// each page's own init function (public.js's initCountdown, beamer.js's
// init) owns its own markup/wiring and decides what "expired" looks like
// for that page.
(function () {
  "use strict";

  /**
   * Start a 1-second-interval countdown toward `targetMs` (epoch
   * milliseconds, e.g. `new Date(isoString).getTime()`).
   *
   * Calls `onTick(remainingMs)` once immediately and then once a second
   * for as long as `remainingMs > 0`. Once the deadline passes, calls
   * `onExpire()` exactly once and stops the interval -- callers never see
   * a negative/zero remaining value, so there is no "broken-looking"
   * negative countdown state to guard against separately.
   *
   * Returns the interval id, in case a caller ever needs to stop it early
   * (none currently do).
   */
  function start(targetMs, onTick, onExpire) {
    function render() {
      var remainingMs = targetMs - Date.now();
      if (remainingMs <= 0) {
        onExpire();
        clearInterval(timer);
        return;
      }
      onTick(remainingMs);
    }
    render();
    var timer = setInterval(render, 1000);
    return timer;
  }

  /** Split a positive remaining-ms duration into whole days/hours/minutes/seconds. */
  function breakdown(remainingMs) {
    var totalSeconds = Math.floor(remainingMs / 1000);
    return {
      days: Math.floor(totalSeconds / 86400),
      hours: Math.floor((totalSeconds % 86400) / 3600),
      minutes: Math.floor((totalSeconds % 3600) / 60),
      seconds: totalSeconds % 60,
    };
  }

  window.BeaconCountdown = { start: start, breakdown: breakdown };
})();
