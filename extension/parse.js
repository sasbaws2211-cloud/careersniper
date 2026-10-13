// Pure text helpers, no browser APIs, so they can be tested with plain Node.
(function (root) {
  "use strict";

  function clean(text, max) {
    return String(text == null ? "" : text).replace(/\s+/g, " ").trim().slice(0, max || 200);
  }

  // "Backend Engineer - Acme | LinkedIn"            -> title, company
  // "Acme hiring Backend Engineer in Accra | LinkedIn" -> company, title, location
  // "(3) Backend Engineer | Acme | LinkedIn"        -> title, company
  function parseLinkedInTitle(documentTitle) {
    var t = clean(documentTitle, 400).replace(/^\(\d+\)\s*/, "");
    var hiring = /^([^|]+?) hiring ([^|]+?)(?: in ([^|]+?))? \| LinkedIn$/i.exec(t);
    if (hiring) return { company: clean(hiring[1]), title: clean(hiring[2]), location: clean(hiring[3] || "") };
    t = t.replace(/\s*\|\s*LinkedIn$/i, "");
    var pipe = t.split(/\s+\|\s+/);
    if (pipe.length >= 2) return { title: clean(pipe[0]), company: clean(pipe[1]), location: "" };
    var dash = t.split(/\s+-\s+/);
    if (dash.length >= 2) return { title: clean(dash[0]), company: clean(dash[1]), location: "" };
    return { title: "", company: "", location: "" };
  }

  // "Backend Engineer - Acme - Accra | Indeed.com" -> title, company, location
  // With only two parts it is not clear which is the company, so the company is left empty rather than guessed.
  function parseIndeedTitle(documentTitle) {
    var t = clean(documentTitle, 400).replace(/\s*[|\-]\s*Indeed(?:\.[a-z.]+)?$/i, "");
    var parts = t.split(/\s+-\s+/);
    if (parts.length >= 3) return { title: clean(parts[0]), company: clean(parts[1]), location: clean(parts.slice(2).join(" - ")) };
    return { title: clean(parts[0] || ""), company: "", location: "" };
  }

  function site(hostname) {
    var h = String(hostname || "").toLowerCase();
    if (h === "linkedin.com" || h.endsWith(".linkedin.com")) return "linkedin";
    if (h === "indeed.com" || h.endsWith(".indeed.com")) return "indeed";
    return "";
  }

  // The key that says "this is the same job as before", so a page that re-renders does not trigger another lookup.
  function jobKey(job) {
    return [job.title, job.company, job.location].map(function (s) { return clean(s).toLowerCase(); }).join("|");
  }

  function usable(job) {
    return !!(job && clean(job.title).length >= 3 && clean(job.company).length >= 2);
  }

  // Only follow links that stay on the CareerSniper site: the answer comes from a server, but a link is still a link.
  function safeLink(url, apiBase) {
    try {
      var u = new URL(url), b = new URL(apiBase);
      return u.origin === b.origin && (u.protocol === "https:" || u.protocol === "http:") ? u.href : "";
    } catch (e) {
      return "";
    }
  }

  function ago(iso, nowMs) {
    var then = Date.parse(iso && !/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso + "Z" : iso);
    if (isNaN(then)) return "";
    var secs = Math.max(0, Math.round(((nowMs || Date.now()) - then) / 1000));
    var units = [[86400, "day"], [3600, "hour"], [60, "minute"]];
    for (var i = 0; i < units.length; i++) {
      if (secs >= units[i][0] * (units[i][1] === "minute" ? 1.5 : 1)) {
        var n = Math.floor(secs / units[i][0]);
        return n + " " + units[i][1] + (n === 1 ? "" : "s") + " ago";
      }
    }
    return "just now";
  }

  var api = { clean: clean, parseLinkedInTitle: parseLinkedInTitle, parseIndeedTitle: parseIndeedTitle, site: site, jobKey: jobKey, usable: usable, safeLink: safeLink, ago: ago };
  root.CSParse = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof self !== "undefined" ? self : globalThis);
