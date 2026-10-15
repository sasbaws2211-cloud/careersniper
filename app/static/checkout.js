// In-page Paystack checkout: the form posts to /checkout/session, Paystack's popup mounts on this page, and we poll
// /checkout/status until the server has confirmed the payment with Paystack (no webhook needed, so it works on localhost).
// If anything in that path is unavailable (script blocked, popup error) the form falls back to the normal redirect flow.
(function () {
  var form = document.getElementById("co-form");
  if (!form || form.dataset.inline !== "1") return;

  var button = form.querySelector("button[type=submit]");
  var panel = document.getElementById("co-status");
  var title = document.getElementById("co-title");
  var detail = document.getElementById("co-detail");
  var actions = document.getElementById("co-actions");
  var spinner = panel.querySelector(".co-spinner");
  var label = button.textContent;
  var POLL_MS = 2000, GIVE_UP_MS = 180000, CLOSED_GRACE = 3;
  var timer = null, started = 0, closedChecks = 0, popupClosed = false, finished = false;

  function show(kind, heading, text, withSpinner) {
    panel.hidden = false;
    panel.className = "co-status co-" + kind;
    title.textContent = heading;
    detail.textContent = text || "";
    spinner.hidden = !withSpinner;
    actions.textContent = "";
  }
  function addButton(text, handler, plain) {
    var b = document.createElement(plain ? "a" : "button");
    b.textContent = text;
    b.className = plain ? "co-link" : "btn";
    if (plain) { b.href = plain; } else { b.type = "button"; b.addEventListener("click", handler); }
    actions.appendChild(b);
  }
  function busy(on) {
    button.disabled = on;
    button.textContent = on ? "Opening secure checkout…" : label;
    button.classList.toggle("is-loading", on);
    if (on) button.setAttribute("aria-busy", "true");
    else button.removeAttribute("aria-busy");
  }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }

  function loadPaystack() {
    return new Promise(function (resolve, reject) {
      if (window.PaystackPop) return resolve();
      var s = document.createElement("script");
      s.src = "https://js.paystack.co/v2/inline.js";
      s.onload = resolve;
      s.onerror = function () { reject(new Error("paystack script")); };
      document.head.appendChild(s);
    });
  }

  function paid(j) {
    finished = true;
    stop();
    var email = (document.getElementById("email") || {}).value || "your email";
    var next = j.next || "/jobs?unlocked=1";
    if (j.signed_in === false) {
      show("ok", "Payment confirmed", "Your plan is active on " + email + ". Create your account with that email to start using it.", false);
      addButton("Create my account", null, next);
    } else {
      show("ok", "Payment confirmed", "Your plan is active. Taking you to the jobs now…", false);
      addButton("Continue", null, next);
    }
    setTimeout(function () { window.location.href = next; }, 2500);
  }
  function failed(text) {
    finished = true;
    stop();
    busy(false);
    show("bad", "Payment was not completed", text || "Nothing was charged. You can try again.", false);
    addButton("Try again", function () { panel.hidden = true; form.requestSubmit(); });
  }

  function poll(reference) {
    fetch("/checkout/status?reference=" + encodeURIComponent(reference), { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (finished) return;
        if (j.status === "paid") return paid(j);
        if (j.status === "failed") return failed("The payment was reported as " + (j.provider || "failed") + ". Nothing was charged.");
        if (j.status === "unknown") return failed("We could not find that checkout. Please start again.");
        if (popupClosed && ++closedChecks >= CLOSED_GRACE) {
          return failed("The payment window was closed before paying. Nothing was charged.");
        }
        if (Date.now() - started > GIVE_UP_MS) {
          finished = true;
          stop();
          show("wait", "Still waiting for confirmation", "If you have paid, your plan will unlock as soon as the payment is confirmed. You can check your account at any time.", false);
          addButton("Open my account", null, "/account");
        }
      })
      .catch(function () { /* a dropped poll is fine: the next one asks again */ });
  }

  function watch(reference) {
    started = Date.now();
    closedChecks = 0;
    popupClosed = false;
    finished = false;
    show("wait", "Waiting for your payment", "Complete the payment in the secure payment window. We confirm it here automatically.", true);
    stop();
    timer = setInterval(function () { poll(reference); }, POLL_MS);
  }

  function openPopup(session) {
    var popup = new window.PaystackPop();
    popup.resumeTransaction(session.access_code, {
      onSuccess: function () { poll(session.reference); },
      onCancel: function () { popupClosed = true; closedChecks = 0; poll(session.reference); },
      onError: function () { stop(); form.submit(); }
    });
    watch(session.reference);
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    busy(true);
    panel.hidden = true;
    fetch("/checkout/session", { method: "POST", body: new URLSearchParams(new FormData(form)), headers: { Accept: "application/json" } })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          busy(false);
          show("bad", "We could not start checkout", (res.body && res.body.error) || "Please try again in a moment.", false);
          return null;
        }
        return loadPaystack().then(function () { busy(false); openPopup(res.body); });
      })
      .catch(function () { form.submit(); });      // anything unexpected: use the redirect flow
  });
})();
