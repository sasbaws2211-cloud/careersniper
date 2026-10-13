// A fake page: querySelector answers from a map of selector -> text, so the order in which selectors are tried can be tested without a browser.
const test = require("node:test");
const assert = require("node:assert/strict");
const X = require("../extract.js");

function page(title, found) {
  return {
    title,
    querySelector(sel) {
      if (sel === "throws") throw new Error("bad selector");
      return Object.prototype.hasOwnProperty.call(found || {}, sel) ? { textContent: found[sel] } : null;
    }
  };
}

test("LinkedIn: the page elements win", () => {
  const doc = page("Something else | LinkedIn", {
    ".job-details-jobs-unified-top-card__job-title h1": "  Backend   Engineer ",
    ".job-details-jobs-unified-top-card__company-name a": "Acme",
    ".job-details-jobs-unified-top-card__primary-description-container .tvm__text": "Accra, Ghana · 2 days ago · 50 applicants"
  });
  assert.deepEqual(X.extract(doc, "www.linkedin.com"), { title: "Backend Engineer", company: "Acme", location: "Accra, Ghana", site: "linkedin" });
});

test("LinkedIn: when the markup changes the page title still works", () => {
  const doc = page("Acme hiring Backend Engineer in Accra, Ghana | LinkedIn", {});
  assert.deepEqual(X.extract(doc, "www.linkedin.com"), { title: "Backend Engineer", company: "Acme", location: "Accra, Ghana", site: "linkedin" });
});

test("a later selector is used when the first is missing", () => {
  const doc = page("", { ".jobs-unified-top-card__job-title": "Data Analyst", ".jobs-unified-top-card__company-name": "Globex" });
  const job = X.extract(doc, "www.linkedin.com");
  assert.equal(job.title, "Data Analyst");
  assert.equal(job.company, "Globex");
});

test("Indeed: elements, then title", () => {
  const doc = page("Whatever - Other - Elsewhere | Indeed.com", {
    'h1[data-testid="jobsearch-JobInfoHeader-title"]': "Backend Engineer",
    '[data-testid="inlineHeader-companyName"] a': "Acme",
    '[data-testid="inlineHeader-companyLocation"]': "Accra"
  });
  assert.deepEqual(X.extract(doc, "gh.indeed.com"), { title: "Backend Engineer", company: "Acme", location: "Accra", site: "indeed" });
  assert.deepEqual(X.extract(page("Backend Engineer - Acme - Accra | Indeed.com", {}), "www.indeed.com"),
    { title: "Backend Engineer", company: "Acme", location: "Accra", site: "indeed" });
});

test("a page with no usable job gives nothing", () => {
  assert.equal(X.extract(page("Jobs | LinkedIn", {}), "www.linkedin.com"), null);
  assert.equal(X.extract(page("Search results - Indeed.com", {}), "www.indeed.com"), null);
  assert.equal(X.extract(page("Backend Engineer - Accra | Indeed.com", {}), "www.indeed.com"), null);     // company unknown: do not guess
});

test("other sites are ignored", () => {
  assert.equal(X.extract(page("Acme hiring Backend Engineer | LinkedIn", {}), "evil.test"), null);
});

test("a selector that throws does not break extraction", () => {
  const doc = page("Acme hiring Backend Engineer | LinkedIn", {});
  const orig = X.SELECTORS.linkedin.title.slice();
  X.SELECTORS.linkedin.title.unshift("throws");
  try {
    assert.equal(X.extract(doc, "www.linkedin.com").title, "Backend Engineer");
  } finally {
    X.SELECTORS.linkedin.title.splice(0, X.SELECTORS.linkedin.title.length, ...orig);
  }
});
