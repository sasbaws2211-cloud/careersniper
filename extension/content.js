// Shows a small card on LinkedIn and Indeed job pages when the same role is on the employer's own career page.
// It reads only the job already on screen and sends its title, company and location to CareerSniper. Nothing else leaves the page.
(function () {
  "use strict";
  const P = self.CSParse, X = self.CSExtract;
  let lastKey = "", host = null, timer = null;

  function remove() {
    if (host) { host.remove(); host = null; }
  }

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    (children || []).forEach((c) => node.append(c));
    return node;
  }

  function link(text, href, primary) {
    const a = el("a", { textContent: text, href, target: "_blank", rel: "noopener noreferrer" });
    a.style.cssText = primary
      ? "display:inline-block;background:#2563eb;color:#fff;padding:7px 12px;border-radius:8px;text-decoration:none;font-weight:600"
      : "color:#2563eb;text-decoration:none;font-weight:600";
    return a;
  }

  function show(result, apiBase) {
    remove();
    if (!result || result.found === "none") return;
    const card = el("div");
    card.style.cssText = "font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;background:#fff;color:#111;border:1px solid #d1d5db;border-left:4px solid #16a34a;" +
      "border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.18);padding:12px 14px;width:300px;";
    const head = el("div", { textContent: "CareerSniper" });
    head.style.cssText = "font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:#6b7280;margin-bottom:4px;display:flex;justify-content:space-between";
    const close = el("button", { textContent: "×", title: "Hide" });
    close.style.cssText = "border:0;background:none;font-size:18px;line-height:1;cursor:pointer;color:#6b7280";
    close.addEventListener("click", remove);
    head.append(close);
    card.append(head);

    const name = (result.company && result.company.name) || "this company";
    const companyUrl = P.safeLink(result.company && result.company.url, apiBase);
    if (result.found === "role" && result.job) {
      const when = P.ago(result.job.confirmed_open);
      card.append(el("div", { textContent: "This role is on " + name + "'s own career page." }));
      if (when) card.append(el("div", { textContent: "Confirmed open " + when + "." }));
      const row = el("div"); row.style.cssText = "margin-top:8px;display:flex;gap:12px;align-items:center";
      const jobUrl = P.safeLink(result.job.url, apiBase);
      if (jobUrl) row.append(link("See it on CareerSniper", jobUrl, true));
      if (companyUrl) row.append(link("All " + name + " roles", companyUrl, false));
      card.append(row);
    } else if (result.found === "similar" && result.similar && result.similar.length) {
      card.append(el("div", { textContent: name + " has similar roles on its own career page:" }));
      result.similar.slice(0, 3).forEach((j) => {
        const u = P.safeLink(j.url, apiBase);
        if (u) { const d = el("div"); d.style.marginTop = "4px"; d.append(link(j.title, u, false)); card.append(d); }
      });
    } else if (result.found === "company" && companyUrl) {
      card.append(el("div", { textContent: name + " has " + ((result.company && result.company.open) || "other") + " open roles on its own career page." }));
      const d = el("div"); d.style.marginTop = "8px"; d.append(link("See them", companyUrl, true)); card.append(d);
    } else {
      return;
    }
    host = el("div");
    host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;";
    host.attachShadow({ mode: "closed" }).append(card);          // closed shadow root: the page's own styles and scripts cannot reach in
    document.documentElement.append(host);
  }

  function check() {
    const job = X.extract(document, location.hostname);
    if (!job) { lastKey = ""; remove(); return; }
    const key = P.jobKey(job);
    if (key === lastKey) return;
    lastKey = key;
    remove();
    chrome.runtime.sendMessage({ type: "lookup", job: { title: job.title, company: job.company, location: job.location } }, (reply) => {
      if (chrome.runtime.lastError || !reply || !reply.ok) return;
      if (P.jobKey(X.extract(document, location.hostname) || {}) !== key) return;     // the page moved on while we were asking
      show(reply.result, reply.apiBase);
    });
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(check, 700);          // both sites build the page in stages and swap jobs without reloading
  }

  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  window.addEventListener("popstate", schedule);
  schedule();
})();
