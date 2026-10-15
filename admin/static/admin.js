// Risky actions use an in-page dialog instead of browser-owned confirmation/prompt popups.
(function () {
  var approved = new WeakSet();
  var pending = null;
  var dialog = document.createElement("dialog");
  dialog.className = "action-dialog";
  dialog.setAttribute("aria-labelledby", "action-dialog-title");
  dialog.innerHTML = '<div class="action-dialog-panel">' +
    '<h2 id="action-dialog-title"></h2><p class="action-dialog-message"></p>' +
    '<label class="action-dialog-input-wrap" hidden></label>' +
    '<div class="action-dialog-actions"><button type="button" class="ghost action-dialog-cancel">Cancel</button>' +
    '<button type="button" class="action-dialog-accept">Continue</button></div></div>';
  document.body.appendChild(dialog);
  var title = dialog.querySelector("#action-dialog-title");
  var message = dialog.querySelector(".action-dialog-message");
  var inputWrap = dialog.querySelector(".action-dialog-input-wrap");
  var cancel = dialog.querySelector(".action-dialog-cancel");
  var accept = dialog.querySelector(".action-dialog-accept");

  function close() {
    var trigger = pending && pending.trigger;
    pending = null;
    if (dialog.open) dialog.close();
    if (trigger && trigger.isConnected) trigger.focus({ preventScroll: true });
  }

  function submitPending() {
    var current = pending;
    if (!current || !current.form.isConnected) { close(); return; }
    if (current.prompt) {
      var field = current.form.elements.namedItem(current.form.getAttribute("data-prompt-field"));
      var answer = inputWrap.querySelector("input").value.trim();
      if (!(field instanceof HTMLInputElement) || !answer) {
        inputWrap.querySelector("input").focus();
        return;
      }
      field.value = answer.slice(0, 200);
    }
    approved.add(current.form);
    close();
    if (current.submitter && current.submitter.form === current.form) current.form.requestSubmit(current.submitter);
    else current.form.requestSubmit();
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (approved.has(form)) { approved.delete(form); return; }
    var ask = form.getAttribute("data-confirm");
    var prompt = form.getAttribute("data-prompt");
    if (!ask && !prompt) return;
    event.preventDefault();
    if (pending) return;
    pending = { form: form, submitter: event.submitter, prompt: prompt, trigger: event.submitter || document.activeElement };
    title.textContent = prompt ? "Confirm job removal" : "Please confirm";
    message.textContent = ask || prompt;
    accept.textContent = prompt ? "Continue" : "Confirm";
    inputWrap.replaceChildren();
    inputWrap.hidden = !prompt;
    if (prompt) {
      var label = document.createElement("span");
      label.textContent = "Enter a reason for the audit log";
      var input = document.createElement("input");
      input.type = "text";
      input.maxLength = 200;
      input.autocomplete = "off";
      inputWrap.append(label, input);
    }
    dialog.showModal();
    (prompt ? inputWrap.querySelector("input") : cancel).focus();
  });

  cancel.addEventListener("click", close);
  accept.addEventListener("click", submitPending);
  inputWrap.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      submitPending();
    }
  });
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) close();
  });
  dialog.addEventListener("cancel", function (event) {
    event.preventDefault();
    close();
  });

  function setLoading(control) {
    control.classList.add("is-loading");
    control.setAttribute("aria-busy", "true");
    if (control instanceof HTMLButtonElement || control instanceof HTMLInputElement) control.disabled = true;
  }

  document.addEventListener("submit", function (event) {
    if (event.defaultPrevented) return;
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var submitter = event.submitter;
    var target = (submitter && submitter.getAttribute("formtarget")) || form.target;
    if (target && target.toLowerCase() !== "_self") return;
    if (!submitter) {
      submitter = Array.from(form.elements).find(function (control) {
        return (control instanceof HTMLButtonElement && control.type === "submit"
          || control instanceof HTMLInputElement && (control.type === "submit" || control.type === "image"))
          && !control.disabled;
      });
    }
    if (submitter) setLoading(submitter);
  });

  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest && event.target.closest("a.btn[href], a[role=button][href]");
    if (!link || link.hasAttribute("download") || (link.target && link.target.toLowerCase() !== "_self")) return;
    var url;
    try { url = new URL(link.href, window.location.href); } catch (_) { return; }
    if (url.origin !== window.location.origin
        || /\/download(?:\/|$)/.test(url.pathname)
        || url.pathname === window.location.pathname && url.search === window.location.search && url.hash) return;
    if (link.getAttribute("aria-busy") === "true") { event.preventDefault(); return; }
    setLoading(link);
    window.setTimeout(function () {
      if (event.defaultPrevented && link.isConnected) {
        link.classList.remove("is-loading");
        link.removeAttribute("aria-busy");
      }
    }, 0);
  });
})();
