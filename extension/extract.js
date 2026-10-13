// Reads the job being shown on the current LinkedIn or Indeed page. Selectors are tried in order; if none work the page title is used.
// Both sites change their markup often, so every selector is a best guess and the title fallback is what keeps this working.
(function (root) {
  "use strict";
  var P = root.CSParse || (typeof require !== "undefined" ? require("./parse.js") : null);

  var SELECTORS = {
    linkedin: {
      title: [".job-details-jobs-unified-top-card__job-title h1", ".job-details-jobs-unified-top-card__job-title", ".jobs-unified-top-card__job-title",
              ".top-card-layout__title", "h1.t-24", "h1"],
      company: [".job-details-jobs-unified-top-card__company-name a", ".job-details-jobs-unified-top-card__company-name", ".jobs-unified-top-card__company-name a",
                ".jobs-unified-top-card__company-name", ".topcard__org-name-link", "a.topcard__org-name-link"],
      location: [".job-details-jobs-unified-top-card__primary-description-container .tvm__text", ".jobs-unified-top-card__bullet",
                 ".topcard__flavor--bullet", ".top-card-layout__first-subline .topcard__flavor--bullet"]
    },
    indeed: {
      title: ['h1[data-testid="jobsearch-JobInfoHeader-title"]', ".jobsearch-JobInfoHeader-title", "h1.jobsearch-JobInfoHeader-title", "h1"],
      company: ['[data-testid="inlineHeader-companyName"] a', '[data-testid="inlineHeader-companyName"]', '[data-company-name="true"]', ".jobsearch-CompanyInfoContainer a"],
      location: ['[data-testid="inlineHeader-companyLocation"]', '[data-testid="job-location"]', ".jobsearch-JobInfoHeader-subtitle div:last-child"]
    }
  };

  function firstText(doc, selectors) {
    for (var i = 0; i < selectors.length; i++) {
      var el = null;
      try { el = doc.querySelector(selectors[i]); } catch (e) { el = null; }
      var text = el ? P.clean(el.textContent) : "";
      if (text) return text;
    }
    return "";
  }

  // doc: a Document (or anything with querySelector and title). host: location.hostname.
  function extract(doc, host) {
    var which = P.site(host);
    if (!which) return null;
    var sel = SELECTORS[which];
    var fromTitle = which === "linkedin" ? P.parseLinkedInTitle(doc.title) : P.parseIndeedTitle(doc.title);
    var job = {
      title: firstText(doc, sel.title) || fromTitle.title,
      company: firstText(doc, sel.company) || fromTitle.company,
      location: firstText(doc, sel.location) || fromTitle.location,
      site: which
    };
    job.location = job.location.split(/\s[·•]\s/)[0];          // LinkedIn appends "· 2 days ago · 50 applicants"
    return P.usable(job) ? job : null;
  }

  var api = { extract: extract, SELECTORS: SELECTORS };
  root.CSExtract = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof self !== "undefined" ? self : globalThis);
