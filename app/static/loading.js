(function () {
  function setLoading(control) {
    control.classList.add("is-loading");
    control.setAttribute("aria-busy", "true");
    if (control instanceof HTMLButtonElement || control instanceof HTMLInputElement) {
      control.disabled = true;
    }
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
    try {
      url = new URL(link.href, window.location.href);
    } catch (_) {
      return;
    }
    if (url.origin !== window.location.origin
        || /\/download(?:\/|$)/.test(url.pathname)
        || url.pathname === window.location.pathname && url.search === window.location.search && url.hash) return;
    if (link.getAttribute("aria-busy") === "true") {
      event.preventDefault();
      return;
    }

    setLoading(link);
    window.setTimeout(function () {
      if (event.defaultPrevented && link.isConnected) {
        link.classList.remove("is-loading");
        link.removeAttribute("aria-busy");
      }
    }, 0);
  });
})();
