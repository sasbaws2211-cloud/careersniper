# CareerSniper browser extension

On a LinkedIn or Indeed job page, a small card appears when **the same role is on the employer's own career page**:

> This role is on Acme's own career page. Confirmed open 2 hours ago.  [See it on CareerSniper]  [All Acme roles]

It sends people from the sites you compete with back to you, with a link to the job on CareerSniper (where subscribers get the apply link).
If the company is known but this exact role is not, it lists similar roles or the company page. If it knows nothing, **it shows nothing**.

## How it works

1. `content.js` reads the job already on screen (title, company, location) on `linkedin.com/jobs/*` and `*.indeed.com`.
2. `background.js` asks `GET <your site>/api/v1/lookup?title=…&company=…&location=…`. No cookies, no account.
3. The server (`app/lookup.py`) matches the company by name and the role by title similarity, and answers `role`, `similar`, `company` or `none`.
4. The card is drawn in a closed shadow DOM with text set through `textContent`, and only links that stay on your site are clickable.

Matching is deliberately cautious: it prefers saying nothing to claiming a role is on the wrong company's page.

## Try it (unpacked, against your local site)

1. Start the site: `python -m uvicorn app.main:app --port 8020` (the dev database needs some scraped jobs; see the main README).
2. Chrome → `chrome://extensions` → turn on **Developer mode** → **Load unpacked** → choose this `extension` folder.
3. Open a LinkedIn or Indeed job for a company that is in your registry (for example a Greenhouse/Lever/Ashby company with open roles).
4. Check the list in "Manual check" below.

## Build it for your real site

```
python extension/build.py --api https://jobs.example.com
```

Writes `dist/careersniper-extension-<version>.zip` with the server address and the manifest permission rewritten together. `http://` is only accepted for localhost.

## Manual check (LinkedIn and Indeed change their pages, so do this before every release)

- [ ] On a LinkedIn job whose company is in your registry **and** whose role you can find on `/jobs`: the card says the role is on the company's page, and "See it on CareerSniper" opens that exact job.
- [ ] Click through to other jobs in the LinkedIn list **without reloading**: the card updates (or disappears) for each job.
- [ ] A job at a company you do **not** track: no card at all.
- [ ] A job at a tracked company with a different title: the "similar roles" or "has N open roles" card.
- [ ] Same on Indeed (`www.indeed.com` and one country site such as `gh.indeed.com`).
- [ ] The popup's switch turns the card off everywhere, and back on.
- [ ] Stop the site and reload a job page: nothing breaks, no error shown to the user.
- [ ] In Chrome's DevTools → Network on a job page, the only request to your server is the lookup, with no cookies.
- [ ] If the card never appears on a site that used to work, the page markup probably changed: update `SELECTORS` in `extract.js` (the page-title fallback in `parse.js` often keeps it working meanwhile).

## Automated tests

```
node --test extension/tests/*.test.js     # title parsing, selector order, link safety (17 tests)
python -m pytest tests/test_extension.py   # packaging, permissions, and a scan for risky code
python -m pytest tests/test_lookup.py      # the server-side matching
```

What the tests **cannot** check: that LinkedIn's and Indeed's current markup still matches the selectors. That is the manual check above.

## Publishing to the Chrome Web Store

You need a Chrome Web Store developer account (one-time fee). Then:

- Upload the zip from `build.py`. The store wants a **128 × 128** icon; `icons/icon128.png` is the site's logo mark at a larger size, so resize it before uploading.
- **Single purpose** to state: "Shows whether the job you are viewing on LinkedIn or Indeed is also listed on the employer's own career page."
- **Permissions justification:** `storage` remembers the on/off switch. The host permission is your own site, used for the lookup. The content scripts run only on LinkedIn job pages and Indeed so they can read the job on screen.
- **Remote code:** none. No analytics. No third parties.

### Privacy policy text (put it on a page of your site and link it in the listing)

> **What the CareerSniper extension collects.** When you view a job on LinkedIn or Indeed, the extension reads that job's title, company name and location from the page and sends those three pieces of text to CareerSniper (`<your site>`) to check whether the job is on the employer's own career page. It sends no cookies, no account or login information, no other page content, and no browsing history, and it does nothing on other websites. CareerSniper does not store these lookups against you. The only thing the extension saves on your device is one setting, whether it is switched on.

Check that last sentence is true of your server before publishing: the lookup endpoint stores nothing, but your web server's access logs will contain the URL (job title and company) and the IP address. Either say so in the policy or turn that logging down.

### Terms of the sites it runs on

The extension only reads the page the person is already looking at, one job at a time, and never automates browsing, scrapes lists or sends anything to LinkedIn or Indeed. That is the design on purpose. If you change that, re-read their terms first.
