// Public site vanilla JS (Milestone 2). No framework, no build step —
// consistent with app/templates/backoffice/base.html's existing
// "bo-color-field" pattern: a few small, event-delegated listeners, each
// scoped to one genuinely-interactive widget (countdown, show/date
// picker, copy-link, live order total). Every page still renders its
// correct initial state server-side (see app/templates/public/landing.html)
// so nothing here is required for the page to be usable without JS, except
// switching between show/date panels and the auto-flip at sales-live time.

(function () {
  "use strict";

  // ---- Countdown: pre-sale -> buy flow at EventConfig.sales_live_at ----
  function initCountdown() {
    var section = document.querySelector(".pub-countdown-section");
    var countdown = document.getElementById("pub-countdown");
    var buySection = document.getElementById("pub-buy-section");
    var valueEl = document.getElementById("pub-countdown-value");
    var announcer = document.getElementById("pub-presale-announcer");
    if (!section || !countdown || !buySection || !valueEl) return;

    var salesLiveAtRaw = section.getAttribute("data-sales-live-at");
    if (!salesLiveAtRaw) return; // no gate configured — buy section is already visible server-side
    var salesLiveAt = new Date(salesLiveAtRaw).getTime();
    if (isNaN(salesLiveAt)) return;

    var unitDays = countdown.getAttribute("data-unit-days") || "d";
    var unitHours = countdown.getAttribute("data-unit-hours") || "h";
    var unitMinutes = countdown.getAttribute("data-unit-minutes") || "m";
    var unitSeconds = countdown.getAttribute("data-unit-seconds") || "s";
    var liveAnnouncement = countdown.getAttribute("data-live-announcement") || "";

    function render() {
      var remainingMs = salesLiveAt - Date.now();
      if (remainingMs <= 0) {
        countdown.hidden = true;
        buySection.hidden = false;
        if (announcer && liveAnnouncement) announcer.textContent = liveAnnouncement;
        clearInterval(timer);
        return;
      }
      var totalSeconds = Math.floor(remainingMs / 1000);
      var days = Math.floor(totalSeconds / 86400);
      var hours = Math.floor((totalSeconds % 86400) / 3600);
      var minutes = Math.floor((totalSeconds % 3600) / 60);
      var seconds = totalSeconds % 60;
      valueEl.textContent =
        days + unitDays + " " + hours + unitHours + " " + minutes + unitMinutes + " " + seconds + unitSeconds;
    }

    render();
    var timer = setInterval(render, 1000);
  }

  // ---- Show/date picker: reveal only the selected show's ticket panel ----
  function initShowPicker() {
    document.addEventListener("change", function (event) {
      if (!event.target.matches('input[name="show_choice"]')) return;
      var showId = event.target.value;
      document.querySelectorAll(".pub-show-panel").forEach(function (panel) {
        panel.hidden = panel.getAttribute("data-show-panel") !== showId;
      });
    });
  }

  // ---- Deep link (?ticket_type=<id>): highlight + focus its quantity field ----
  function initDeepLinkFocus() {
    var params = new URLSearchParams(window.location.search);
    var ticketTypeId = params.get("ticket_type");
    if (!ticketTypeId) return;
    var row = document.querySelector('[data-ticket-type-row="' + CSS.escape(ticketTypeId) + '"]');
    if (!row) return;
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    var qtyInput = row.querySelector(".pub-qty-input");
    if (qtyInput) qtyInput.focus();
  }

  // ---- Copy-link buttons (share this show / share this ticket type) ----
  function initCopyLinks() {
    document.addEventListener("click", function (event) {
      var button = event.target.closest("[data-copy-url]");
      if (!button) return;
      var url = button.getAttribute("data-copy-url");
      var announcer = document.getElementById("pub-copy-announcer");
      var announce = function (message) {
        if (announcer) announcer.textContent = message;
      };
      var originalText = button.textContent;
      var onCopied = function () {
        button.textContent = button.getAttribute("data-copied-label") || originalText;
        announce(button.getAttribute("data-copied-label") || originalText);
        setTimeout(function () {
          button.textContent = originalText;
        }, 2000);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(onCopied);
      } else {
        // Fallback for browsers without the async Clipboard API.
        var scratch = document.createElement("textarea");
        scratch.value = url;
        scratch.style.position = "fixed";
        scratch.style.opacity = "0";
        document.body.appendChild(scratch);
        scratch.select();
        try {
          document.execCommand("copy");
          onCopied();
        } finally {
          document.body.removeChild(scratch);
        }
      }
    });
  }

  // ---- Live order total for the currently-selected show's panel ----

  // Mirrors app.i18n.formatting.format_currency's two-locale convention
  // ("€ 15,00" nl vs "€15.00" en) so the live-typing total the buyer sees
  // while picking quantities doesn't visually disagree with the
  // server-rendered per-ticket prices right next to it.
  function formatCurrency(amount, locale) {
    var fixed = amount.toFixed(2);
    var parts = fixed.split(".");
    if (locale === "nl") {
      return "€ " + parts[0] + "," + parts[1];
    }
    return "€" + parts[0] + "." + parts[1];
  }

  function initOrderTotal() {
    var form = document.getElementById("pub-checkout-form");
    var totalEl = document.getElementById("pub-order-total");
    if (!form || !totalEl) return;
    var locale = document.documentElement.getAttribute("lang") || "en";

    function recompute() {
      var activePanel = form.querySelector(".pub-show-panel:not([hidden])");
      if (!activePanel) {
        totalEl.textContent = "";
        return;
      }
      var total = 0;
      activePanel.querySelectorAll(".pub-qty-input").forEach(function (input) {
        var qty = parseInt(input.value, 10) || 0;
        var priceCell = input.closest("tr").querySelector("[data-unit-price]");
        var price = priceCell ? parseFloat(priceCell.getAttribute("data-unit-price")) : 0;
        total += qty * price;
      });
      totalEl.textContent = totalEl.getAttribute("data-label") + " " + formatCurrency(total, locale);
    }

    form.addEventListener("input", function (event) {
      if (event.target.matches(".pub-qty-input")) recompute();
    });
    form.addEventListener("change", function (event) {
      if (event.target.matches('input[name="show_choice"]')) recompute();
    });
    recompute();
  }

  document.addEventListener("DOMContentLoaded", function () {
    initCountdown();
    initShowPicker();
    initDeepLinkFocus();
    initCopyLinks();
    initOrderTotal();
  });
})();
