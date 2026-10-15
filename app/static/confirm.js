(function () {
  var approved = new WeakSet();
  var pending = null;
  var dialog = document.createElement("dialog");
  dialog.className = "confirm-dialog";
  dialog.setAttribute("aria-labelledby", "confirm-dialog-title");
  dialog.setAttribute("aria-describedby", "confirm-dialog-message");
  dialog.innerHTML = '<div class="confirm-dialog-panel"><h2 id="confirm-dialog-title">Please confirm</h2>' +
    '<p id="confirm-dialog-message"></p><div class="confirm-dialog-actions">' +
    '<button type="button" class="ghost confirm-dialog-cancel">Cancel</button>' +
    '<button type="button" class="confirm-dialog-accept">Confirm</button></div></div>';
  document.body.appendChild(dialog);
  var cancel = dialog.querySelector(".confirm-dialog-cancel");
  var accept = dialog.querySelector(".confirm-dialog-accept");

  function close() {
    var trigger = pending && pending.trigger;
    pending = null;
    if (dialog.open) dialog.close();
    if (trigger && trigger.isConnected) trigger.focus({ preventScroll: true });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (approved.has(form)) {
      approved.delete(form);
      return;
    }
    var message = form.getAttribute("data-confirm");
    if (!message) return;
    event.preventDefault();
    if (pending) return;
    pending = { form: form, submitter: event.submitter, trigger: event.submitter || document.activeElement };
    dialog.querySelector("#confirm-dialog-message").textContent = message;
    dialog.showModal();
    cancel.focus();
  });

  cancel.addEventListener("click", close);
  accept.addEventListener("click", function () {
    var current = pending;
    if (!current || !current.form.isConnected) {
      close();
      return;
    }
    approved.add(current.form);
    pending = null;
    dialog.close();
    if (current.submitter && current.submitter.form === current.form) current.form.requestSubmit(current.submitter);
    else current.form.requestSubmit();
  });
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) close();
  });
  dialog.addEventListener("cancel", function (event) {
    event.preventDefault();
    close();
  });
})();
