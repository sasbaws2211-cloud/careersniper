// The only script on the admin pages: confirmation questions before risky buttons. Loaded from this site, so the page policy can forbid inline scripts.
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  var ask = form.getAttribute("data-confirm");
  if (ask && !window.confirm(ask)) { event.preventDefault(); return; }
  var prompt = form.getAttribute("data-prompt");
  if (prompt) {
    var answer = window.prompt(prompt, "");
    if (!answer || !answer.trim()) { event.preventDefault(); return; }
    var field = form.querySelector('input[name="' + form.getAttribute("data-prompt-field") + '"]');
    if (field) field.value = answer.trim().slice(0, 200);
  }
});
