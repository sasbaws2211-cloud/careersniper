// Asks the CareerSniper server whether a job is also on the employer's own career page.
// Runs here (not in the page) so the request is allowed by host permissions and is not blocked by LinkedIn's or Indeed's page rules.
importScripts("config.js");

const TTL_MS = 5 * 60 * 1000;
const TIMEOUT_MS = 8000;
const cache = new Map();

async function lookup(job) {
  const key = [job.title, job.company, job.location].join("|").toLowerCase();
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.result;
  const url = new URL("/api/v1/lookup", CS_API_BASE);
  url.searchParams.set("title", job.title);
  url.searchParams.set("company", job.company);
  if (job.location) url.searchParams.set("location", job.location);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    // No cookies and no account: the server is told only the three things in the URL.
    const res = await fetch(url, { credentials: "omit", cache: "no-store", signal: ctrl.signal });
    if (!res.ok) return { found: "none" };
    const result = await res.json();
    cache.set(key, { at: Date.now(), result });
    if (cache.size > 200) cache.delete(cache.keys().next().value);
    return result;
  } finally {
    clearTimeout(timer);
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "lookup" || sender.id !== chrome.runtime.id) return false;
  chrome.storage.sync.get({ enabled: true }, async ({ enabled }) => {
    if (!enabled) return sendResponse({ ok: true, result: { found: "none" } });
    try {
      sendResponse({ ok: true, result: await lookup(message.job), apiBase: CS_API_BASE });
    } catch (e) {
      sendResponse({ ok: false });
    }
  });
  return true;           // the answer is sent asynchronously
});
