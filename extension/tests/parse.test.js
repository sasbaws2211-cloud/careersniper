// Run with: node --test extension/tests/*.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const P = require("../parse.js");

test("LinkedIn page titles", () => {
  assert.deepEqual(P.parseLinkedInTitle("Acme hiring Backend Engineer in Accra, Greater Accra Region, Ghana | LinkedIn"),
    { company: "Acme", title: "Backend Engineer", location: "Accra, Greater Accra Region, Ghana" });
  assert.deepEqual(P.parseLinkedInTitle("(3) Acme hiring Backend Engineer | LinkedIn"), { company: "Acme", title: "Backend Engineer", location: "" });
  assert.deepEqual(P.parseLinkedInTitle("Backend Engineer | Acme | LinkedIn"), { title: "Backend Engineer", company: "Acme", location: "" });
  assert.deepEqual(P.parseLinkedInTitle("Backend Engineer - Acme | LinkedIn"), { title: "Backend Engineer", company: "Acme", location: "" });
  assert.deepEqual(P.parseLinkedInTitle("LinkedIn"), { title: "", company: "", location: "" });
  assert.deepEqual(P.parseLinkedInTitle(""), { title: "", company: "", location: "" });
  assert.deepEqual(P.parseLinkedInTitle(null), { title: "", company: "", location: "" });
});

test("a company called Hiring Solutions is not split wrongly", () => {
  const r = P.parseLinkedInTitle("Backend Engineer | Hiring Solutions Ltd | LinkedIn");
  assert.equal(r.title, "Backend Engineer");
  assert.equal(r.company, "Hiring Solutions Ltd");
});

test("Indeed page titles", () => {
  assert.deepEqual(P.parseIndeedTitle("Backend Engineer - Acme - Accra | Indeed.com"), { title: "Backend Engineer", company: "Acme", location: "Accra" });
  assert.deepEqual(P.parseIndeedTitle("Backend Engineer - Acme - Accra - Greater Accra | Indeed.com"), { title: "Backend Engineer", company: "Acme", location: "Accra - Greater Accra" });
  assert.deepEqual(P.parseIndeedTitle("Backend Engineer - Acme - Accra - Indeed.com"), { title: "Backend Engineer", company: "Acme", location: "Accra" });
});

test("with two parts Indeed's company is left empty, not guessed", () => {
  assert.deepEqual(P.parseIndeedTitle("Backend Engineer - Accra | Indeed.com"), { title: "Backend Engineer", company: "", location: "" });
});

test("which site", () => {
  assert.equal(P.site("www.linkedin.com"), "linkedin");
  assert.equal(P.site("linkedin.com"), "linkedin");
  assert.equal(P.site("gh.indeed.com"), "indeed");
  assert.equal(P.site("www.indeed.com"), "indeed");
  assert.equal(P.site("evil-linkedin.com"), "");
  assert.equal(P.site("linkedin.com.evil.test"), "");
  assert.equal(P.site("notindeed.com"), "");
  assert.equal(P.site(""), "");
});

test("clean collapses whitespace and limits length", () => {
  assert.equal(P.clean("  a \n\t b  "), "a b");
  assert.equal(P.clean("x".repeat(500)).length, 200);
  assert.equal(P.clean(undefined), "");
});

test("usable needs a real title and company", () => {
  assert.equal(P.usable({ title: "Backend Engineer", company: "Acme" }), true);
  assert.equal(P.usable({ title: "Backend Engineer", company: "" }), false);
  assert.equal(P.usable({ title: "QA", company: "Acme" }), false);
  assert.equal(P.usable(null), false);
});

test("jobKey ignores case and spacing so a re-render does not repeat the lookup", () => {
  assert.equal(P.jobKey({ title: "Backend  Engineer", company: "ACME", location: "Accra" }), P.jobKey({ title: "backend engineer", company: "Acme ", location: " accra" }));
  assert.notEqual(P.jobKey({ title: "A", company: "B", location: "" }), P.jobKey({ title: "A", company: "C", location: "" }));
});

test("links must stay on the CareerSniper site", () => {
  const base = "https://jobs.example.com";
  assert.equal(P.safeLink("https://jobs.example.com/jobs/5", base), "https://jobs.example.com/jobs/5");
  assert.equal(P.safeLink("https://evil.test/jobs/5", base), "");
  assert.equal(P.safeLink("https://jobs.example.com.evil.test/x", base), "");
  assert.equal(P.safeLink("javascript:alert(1)", base), "");
  assert.equal(P.safeLink("//evil.test", base), "");
  assert.equal(P.safeLink("not a url", base), "");
  assert.equal(P.safeLink("http://localhost:8020/jobs/1", "http://localhost:8020"), "http://localhost:8020/jobs/1");
  assert.equal(P.safeLink("http://localhost:9999/jobs/1", "http://localhost:8020"), "");
});

test("ago", () => {
  const now = Date.parse("2026-10-09T12:00:00Z");
  assert.equal(P.ago("2026-10-09T09:55:00", now), "2 hours ago");
  assert.equal(P.ago("2026-10-09T09:55:00Z", now), "2 hours ago");
  assert.equal(P.ago("2026-10-08T12:00:00", now), "1 day ago");
  assert.equal(P.ago("2026-10-09T11:58:00", now), "2 minutes ago");
  assert.equal(P.ago("2026-10-09T11:59:30", now), "just now");
  assert.equal(P.ago("2026-10-10T12:00:00", now), "just now");
  assert.equal(P.ago("garbage", now), "");
  assert.equal(P.ago(null, now), "");
});
