/*!
 * Scanning-app camera logic (Milestone 7). Vanilla JS, no framework/build
 * step (matches this project's "no SPA framework" constraint) — mirrors
 * backoffice/base.html's existing inline-script style (a few focused
 * DOM-event listeners, no module system).
 *
 * QR decoding: prefers the browser-native `BarcodeDetector` API (fast, low
 * battery cost, Chrome/Edge/Android WebView) and falls back to the locally
 * vendored `jsQR` (app/static/vendor/jsqr.min.js) for browsers without it
 * (notably Safari/iOS). Both paths feed the exact same `submitToken()`.
 *
 * State machine: "scanning" (camera loop actively decoding) ->
 * "checking" (a token was decoded, POST in flight) -> "result" (terminal
 * outcome or network-error shown). Decoding is only attempted while
 * state === "scanning", which is what stops the same physical QR code
 * from being decoded and submitted twice in a row while its result is
 * still on screen (brief requirement) — the camera video keeps streaming
 * throughout (no stop/restart of the MediaStream), only the decode-and-
 * submit step is gated, so resuming is instant with no re-permission
 * prompt or black-frame flash.
 */
(function () {
  "use strict";

  var config = window.SCAN_CONFIG || {};

  var REQUEST_TIMEOUT_MS = 9000;
  var SCAN_INTERVAL_MS = 180; // throttle decode attempts, regardless of backend
  var AUTO_RESUME_MS = {
    pass: 3200,
    already_scanned: 4200,
    wrong_show: 4200,
    invalid: 3800,
    unpaid_scanner: 5500
  };

  var video = document.getElementById("scan-video");
  var canvas = document.getElementById("scan-canvas");
  var statusText = document.getElementById("scan-status-text");
  var overlay = document.getElementById("scan-overlay");
  var manualForm = document.getElementById("scan-manual-form");
  var manualInput = document.getElementById("scan-manual-input");

  var canvasCtx = canvas ? canvas.getContext("2d", { willReadFrequently: true }) : null;

  var state = "starting"; // starting | scanning | checking | result
  var resumeTimer = null;
  var lastAttemptAt = 0;
  var detectorMode = null; // "native" | "jsqr"
  var nativeDetector = null;

  function setStatus(text) {
    if (statusText) statusText.textContent = text;
  }

  function clearResumeTimer() {
    if (resumeTimer) {
      clearTimeout(resumeTimer);
      resumeTimer = null;
    }
  }

  function resumeScanning() {
    clearResumeTimer();
    overlay.hidden = true;
    overlay.className = "scan-overlay";
    overlay.innerHTML = "";
    state = "scanning";
    setStatus("Point the camera at the ticket QR code.");
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function buildDetailsList(rows) {
    var dl = el("dl", "scan-result__details");
    rows.forEach(function (row) {
      if (!row[1]) return;
      dl.appendChild(el("dt", null, row[0]));
      dl.appendChild(el("dd", null, row[1]));
    });
    return dl;
  }

  function formatDateTime(iso) {
    if (!iso) return null;
    try {
      return new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
    } catch (e) {
      return iso;
    }
  }

  // Real <form method="post"> (not fetch) per this milestone's design: an
  // ordinary, CSRF-protected navigation to the existing admin-only
  // mark-as-paid web route (app.web.routes.orders.mark_order_paid_web),
  // which redirects back to this exact scan page (`return_to`) on
  // completion so staff can immediately re-scan the same ticket.
  function buildMarkPaidForm(orderId) {
    var wrap = el("div", "scan-mark-paid");
    wrap.appendChild(el("h2", null, "Resolve payment"));
    wrap.appendChild(el(
      "p",
      "scan-hint",
      "Mark this order as paid, then re-scan the same ticket to complete entry."
    ));

    var form = document.createElement("form");
    form.method = "post";
    form.action = "/events/" + config.eventId + "/orders/" + orderId + "/mark-paid";

    var csrfInput = document.createElement("input");
    csrfInput.type = "hidden";
    csrfInput.name = "csrf_token";
    csrfInput.value = config.csrfToken;
    form.appendChild(csrfInput);

    var returnInput = document.createElement("input");
    returnInput.type = "hidden";
    returnInput.name = "return_to";
    returnInput.value = config.returnTo;
    form.appendChild(returnInput);

    var methodLabel = el("label", null, "Payment method");
    methodLabel.setAttribute("for", "scan-mark-paid-method");
    form.appendChild(methodLabel);
    var methodInput = document.createElement("input");
    methodInput.type = "text";
    methodInput.id = "scan-mark-paid-method";
    methodInput.name = "method_label";
    methodInput.required = true;
    methodInput.placeholder = "cash, card terminal, bank transfer...";
    form.appendChild(methodInput);

    var reasonLabel = el("label", null, "Reason (optional)");
    reasonLabel.setAttribute("for", "scan-mark-paid-reason");
    form.appendChild(reasonLabel);
    var reasonInput = document.createElement("input");
    reasonInput.type = "text";
    reasonInput.id = "scan-mark-paid-reason";
    reasonInput.name = "reason";
    reasonInput.placeholder = "e.g. door payment";
    form.appendChild(reasonInput);

    var submitBtn = document.createElement("button");
    submitBtn.type = "submit";
    submitBtn.className = "scan-btn scan-btn--light";
    submitBtn.textContent = "Mark as paid";
    form.appendChild(submitBtn);

    wrap.appendChild(form);
    return wrap;
  }

  function scheduleAutoResume(ms) {
    clearResumeTimer();
    resumeTimer = setTimeout(resumeScanning, ms);
  }

  function renderChecking() {
    state = "checking";
    overlay.className = "scan-overlay scan-overlay--checking";
    overlay.innerHTML = "";
    overlay.appendChild(el("div", "scan-spinner"));
    overlay.appendChild(el("p", "scan-result__heading", "Checking…"));
    overlay.hidden = false;
    setStatus("Checking ticket…");
  }

  // Distinct FOURTH-ish visual language from every confident outcome: a
  // neutral blue-gray (not the pass-green, unpaid-amber, or fail-red hues
  // used below) with its own "?" glyph in a plain (non-circle/diamond/
  // hexagon) box, so a timeout/network failure can never be mistaken for
  // any of the five confident `ScanOutcome` results — this is deliberately
  // NOT a sixth `ScanOutcome`, just a client-side "we don't know" state.
  function renderNetworkError(token, reason) {
    state = "result";
    overlay.className = "scan-overlay scan-overlay--network_error";
    overlay.innerHTML = "";
    var icon = el("div", "scan-result__icon");
    icon.appendChild(el("span", "scan-result__icon-glyph", "?"));
    overlay.appendChild(icon);
    overlay.appendChild(el("p", "scan-result__heading", "Connection issue"));
    overlay.appendChild(el(
      "p",
      "scan-result__message",
      "Could not confirm this scan (" + reason + "). This is NOT a pass or a fail — please retry."
    ));
    var actions = el("div", "scan-result__actions");
    var retryBtn = el("button", "scan-btn scan-btn--light", "Retry this scan");
    retryBtn.type = "button";
    retryBtn.addEventListener("click", function () {
      submitToken(token);
    });
    var skipBtn = el("button", "scan-btn scan-btn--outline-light", "Scan a different ticket");
    skipBtn.type = "button";
    skipBtn.addEventListener("click", resumeScanning);
    actions.appendChild(retryBtn);
    actions.appendChild(skipBtn);
    overlay.appendChild(actions);
    overlay.hidden = false;
    setStatus("Connection issue — retry or scan a different ticket.");
    // Deliberately no auto-resume timer: brief requires resuming
    // automatically "once dismissed" for this state specifically, i.e.
    // only after an explicit staff action, not on a fixed delay (unlike
    // the confident terminal outcomes below).
  }

  var OUTCOME_META = {
    pass: { heading: "Entry OK", glyph: "✓" },
    unpaid: { heading: "Not paid", glyph: "€" },
    already_scanned: { heading: "Already scanned", glyph: "↺" },
    wrong_show: { heading: "Wrong show", glyph: "≠" },
    invalid: { heading: "Invalid ticket", glyph: "✕" }
  };

  function renderResult(data) {
    state = "result";
    var outcome = data.outcome;
    var meta = OUTCOME_META[outcome] || { heading: outcome, glyph: "?" };
    overlay.className = "scan-overlay scan-overlay--" + outcome;
    overlay.innerHTML = "";

    var icon = el("div", "scan-result__icon");
    icon.appendChild(el("span", "scan-result__icon-glyph", meta.glyph));
    overlay.appendChild(icon);
    overlay.appendChild(el("p", "scan-result__heading", meta.heading));
    // `data.message` is the server's own wording (see app.services.scan) --
    // for `unpaid` this is the literal "NOT PAID — collect payment before
    // entry." string the brief requires verbatim; for `invalid` it's the
    // deliberately generic message with nothing more specific added here.
    overlay.appendChild(el("p", "scan-result__message", data.message));

    if (outcome === "pass") {
      overlay.appendChild(buildDetailsList([
        ["Ticket type", data.ticket_type_name],
        ["Buyer", data.buyer_name]
      ]));
    } else if (outcome === "unpaid") {
      overlay.appendChild(buildDetailsList([
        ["Ticket type", data.ticket_type_name],
        ["Buyer", data.buyer_name],
        ["Amount due", data.amount_due != null ? "€" + Number(data.amount_due).toFixed(2) : null],
        ["Order status", data.order_status]
      ]));
    } else if (outcome === "already_scanned") {
      overlay.appendChild(buildDetailsList([
        ["Ticket type", data.ticket_type_name],
        ["Buyer", data.buyer_name],
        ["Originally scanned", formatDateTime(data.scanned_at)],
        ["Scanned by", data.scanned_by_name]
      ]));
    } else if (outcome === "wrong_show") {
      overlay.appendChild(buildDetailsList([
        ["Actually belongs to", data.actual_show_label]
      ]));
    }
    // `invalid` intentionally renders no extra details block, matching the
    // backend's deliberate anti-enumeration design (see app.services.scan).

    if (outcome === "unpaid" && config.isAdmin && data.order_id) {
      overlay.appendChild(buildMarkPaidForm(data.order_id));
      var cancelActions = el("div", "scan-result__actions");
      var cancelBtn = el("button", "scan-btn scan-btn--outline-light", "Scan a different ticket instead");
      cancelBtn.type = "button";
      cancelBtn.addEventListener("click", resumeScanning);
      cancelActions.appendChild(cancelBtn);
      overlay.appendChild(cancelActions);
      // No auto-resume here: the mark-as-paid form holds live input, and
      // auto-dismissing it would silently discard whatever staff typed.
    } else {
      var actions = el("div", "scan-result__actions");
      var nextBtn = el("button", "scan-btn scan-btn--light", "Scan next ticket");
      nextBtn.type = "button";
      nextBtn.addEventListener("click", resumeScanning);
      actions.appendChild(nextBtn);
      overlay.appendChild(actions);
      var delay = outcome === "unpaid" ? AUTO_RESUME_MS.unpaid_scanner : (AUTO_RESUME_MS[outcome] || 4000);
      scheduleAutoResume(delay);
    }

    overlay.hidden = false;
    setStatus(meta.heading);
  }

  function submitToken(token) {
    if (!token) return;
    renderChecking();

    var controller = "AbortController" in window ? new AbortController() : null;
    var timeoutId = controller
      ? setTimeout(function () {
          controller.abort();
        }, REQUEST_TIMEOUT_MS)
      : null;

    fetch(config.scanEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ token: token }),
      signal: controller ? controller.signal : undefined
    })
      .then(function (response) {
        if (timeoutId) clearTimeout(timeoutId);
        if (!response.ok) {
          // Every genuine scan outcome is HTTP 200 (see
          // app.schemas.scan.ScanResponse's docstring) -- a non-200 here
          // means something unexpected happened server-side, not a
          // confident negative result. Treated identically to a network
          // failure rather than inventing a sixth UI state for it.
          renderNetworkError(token, "server error " + response.status);
          return null;
        }
        return response.json();
      })
      .then(function (data) {
        if (data) renderResult(data);
      })
      .catch(function (err) {
        if (timeoutId) clearTimeout(timeoutId);
        var reason = err && err.name === "AbortError" ? "timed out" : "network error";
        renderNetworkError(token, reason);
      });
  }

  // ---------- Manual entry fallback ----------
  // Not required by the brief, but cheap to add and doubles as: (a) an
  // accessibility fallback for a staff member who can't operate the
  // camera one-handed or whose device has none, (b) the path used to
  // exercise this page end-to-end without a physical camera/QR image.
  if (manualForm && manualInput) {
    manualForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (state !== "scanning") return;
      var value = manualInput.value.trim();
      if (value) submitToken(value);
      manualInput.value = "";
    });
  }

  // ---------- Camera + decode loop ----------

  function decodeFrame(timestamp) {
    requestAnimationFrame(decodeFrame);
    if (state !== "scanning") return;
    if (timestamp - lastAttemptAt < SCAN_INTERVAL_MS) return;
    lastAttemptAt = timestamp;
    if (video.readyState < video.HAVE_ENOUGH_DATA) return;

    if (detectorMode === "native") {
      nativeDetector
        .detect(video)
        .then(function (codes) {
          if (codes && codes.length && state === "scanning") {
            submitToken(codes[0].rawValue);
          }
        })
        .catch(function () {
          // Transient native-detector failure -- just try again next frame.
        });
      return;
    }

    if (detectorMode === "jsqr" && window.jsQR && canvasCtx) {
      var w = video.videoWidth;
      var h = video.videoHeight;
      if (!w || !h) return;
      canvas.width = w;
      canvas.height = h;
      canvasCtx.drawImage(video, 0, 0, w, h);
      var imageData;
      try {
        imageData = canvasCtx.getImageData(0, 0, w, h);
      } catch (e) {
        return;
      }
      var code = window.jsQR(imageData.data, imageData.width, imageData.height, {
        inversionAttempts: "dontInvert"
      });
      if (code && code.data && state === "scanning") {
        submitToken(code.data);
      }
    }
  }

  function startDecoding() {
    state = "scanning";
    setStatus("Point the camera at the ticket QR code.");
    requestAnimationFrame(decodeFrame);
  }

  function chooseDetector() {
    if ("BarcodeDetector" in window) {
      return window.BarcodeDetector.getSupportedFormats()
        .then(function (formats) {
          if (formats.indexOf("qr_code") !== -1) {
            nativeDetector = new window.BarcodeDetector({ formats: ["qr_code"] });
            detectorMode = "native";
          } else {
            detectorMode = "jsqr";
          }
        })
        .catch(function () {
          detectorMode = "jsqr";
        });
    }
    detectorMode = "jsqr";
    return Promise.resolve();
  }

  function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus("This browser doesn't support camera access — use “Enter code manually” below.");
      state = "scanning"; // manual entry still works without a camera
      return;
    }
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false })
      .then(function (stream) {
        video.srcObject = stream;
        return video.play();
      })
      .then(chooseDetector)
      .then(startDecoding)
      .catch(function (err) {
        setStatus(
          "Camera unavailable (" + (err && err.name ? err.name : "error") +
            ") — use “Enter code manually” below."
        );
        state = "scanning"; // manual entry still works even without a camera
      });
  }

  startCamera();
})();
