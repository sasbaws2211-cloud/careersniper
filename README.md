# CareerSniper — B2C job search (CareerHound-style)

Find jobs posted on company career pages — the openings that often never appear on LinkedIn or Indeed.

Jobs are scraped from public ATS boards (Greenhouse, Lever, Ashby, …) into one shared pool. Job seekers get a free preview, then unlock the full database with a simple subscription.

## Pricing (consumer)

| Plan     | Price   | Access                          |
|----------|---------|---------------------------------|
| Weekly   | $7      | Full access for 7 days          |
| Monthly  | $20     | Full access for 30 days         |
| Lifetime | $50     | One-time, lifetime access       |

Free preview shows a limited number of matching jobs. Full search, filters, alerts, saved jobs, and AI tools require an active plan.

## Run locally

PostgreSQL is the only supported database. Tables are SQLModel classes (`app/models.py`), the app talks to the database through an
async engine (asyncpg), and the schema is managed with Alembic. Redis is used as an optional shared cache for public homepage,
company-directory, and salary data. The app continues to work without Redis, using its existing in-process caches instead.

```bash
pip install -r requirements.txt
cp .env.example .env              # set DATABASE_URL to your PostgreSQL database 
alembic upgrade head              # create / update the tables
python -m app.cli scrape          # seeds employers and pulls jobs
uvicorn app.main:app --port 8020
```

Changing a table: edit `app/models.py`, run `alembic revision --autogenerate -m "what changed"`, read the generated file, then `alembic upgrade head`.
Moving data from the old SQLite file: `python -m app.cli migrate

- Marketing homepage: http://localhost:8020/
- Job search (preview): http://localhost:8020/jobs
- Pricing: http://localhost:8020/pricing

## What changed (B2C pivot)

- Multi-tenant white-label (orgs / subdomains) removed (the schema history now starts at `0001_baseline`).
- Single global job feed and member accounts.
- CareerHound-style marketing homepage and hard paywall with free preview.
- Consumer plans: weekly / monthly / lifetime.
- Employer direct-posting and org admin remain in the codebase as secondary / legacy paths; the primary product is the B2C job seeker experience.

## SEO

Search and social metadata is built in one place, `app/seo.py`, and rendered by one partial, `app/templates/_seo.html`, that both base
templates include.

**What every public page gets:** a title of at most 60 characters ending in the brand, a description of at most 155 characters taken from the
page itself, one canonical address (no query strings, so `?unlocked=1` never makes a copy), Open Graph and Twitter tags with a 1200x630
image (`app/static/og-default.png`), and breadcrumb structured data. The homepage also has `WebSite` and `Organization` data.

**What is kept out of search:**
- A page with no `seo` settings is `noindex,nofollow`, so a private page can never reach search by being forgotten. `robots.txt` also tells crawlers to stay away from signed-in pages, `/go/`, `/api/` and the member tools.
- The job list with any filter, sort or page (`/jobs?...`) is `noindex,follow` and points to `/jobs`. Same for a searched or paged company or salary list.
- A job whose description is too short to be useful (under 150 characters of text) is `noindex,follow` and is left out of the sitemap. A company with no open jobs is `noindex,follow`.
- An expired or removed job is a 404, so it drops out of search by itself.

**Sitemaps:** `/sitemap.xml` is an index of `/sitemap-pages.xml`, `/sitemap-companies.xml`, `/sitemap-salaries.xml` and `/sitemap-jobs-1.xml`, `-2.xml`... (40,000 jobs per file). Each is built at most once an hour (in this process, shared through Redis when it is there). `robots.txt` points to the index. Submit `https://<your domain>/sitemap.xml` once in Google Search Console and Bing Webmaster Tools.

**Set `PUBLIC_URL`** to the real `https://` address of the site: every canonical, sitemap and social-image address is built from it.

**Adding a public page:** at the top of its template, after `{% extends %}`, set `seo` and drop any `{% block title %}`:

```jinja
{% set seo = {
  'title': brand_title('Page title in about 50 characters', PLATFORM),
  'description': clip('One or two sentences taken from the page itself.'),
  'canonical': '/the-page',
  'robots': 'index,follow,max-image-preview:large',
  'jsonld': [breadcrumbs([(PLATFORM, '/'), ('The page', '/the-page')])]
} %}
```

Then add its path to `STATIC_PAGES` in `seo.py`, or to a sitemap in `main.py` if it is one of many.

**Why there is no `JobPosting` structured data (and no "Google for Jobs"):** Google's job-posting rules do not allow postings that need payment to apply, and an aggregator may not alter titles or supply pay. Job pages here keep the apply link behind a plan, so adding that markup would put the site at risk of losing rich results or a manual action. A test (`test_no_job_posting_markup...`) stops it being added by accident. If you ever want job-search rich results, the apply link would need to work without payment first.

## Blog and comparison pages

`/blog` and `/alternatives` are written pages that bring in search traffic: short guides, and fair "CareerSniper vs X" comparisons. They are
plain template files, not a database, so they are reviewed and versioned like code.

**Add a page:** add an `Entry(...)` to `BLOG` or `ALTERNATIVES` in `app/content.py` (slug, title, a title of at most 45 characters for search results, a
description of at most 155 characters, a summary for the listing, the dates), then write its body in
`app/templates/content/<blog|alternative>/<slug>.html`. The listing page, the sitemap (`/sitemap-content.xml`), the title tags, the canonical address and
the `Article` data all come from the record. Use `{{ CONSUMER_PLANS['monthly'].price_usd }}` and `{{ REFUND_DAYS }}` for our own prices and refund window so a page
can never disagree with the pricing page.

**Rules for a comparison page** (a test, `tests/test_content.py`, enforces what it can):
- Say only what the other product says about itself on its own site, or what could be checked there, and give the date it was checked. Quote its own words.
- Where something could not be confirmed (a price, a limit), say so in "What we could not confirm" instead of quoting a third party. This is why the Jobright and Simplify pages carry no price of theirs: their pricing pages could not be opened.
- Be fair: spell out where the other product is the better choice. No reviews, no complaints, no claims about how a company treats its customers.
- Do not claim features we do not have. We have no autofill, networking or recruiter introductions.
- Re-check each comparison when prices or features change, and update its dates (`updated`, and the "Checked on" sentence in its description). Stale comparisons are worse than none.

**Blog posts:** useful and original, with no invented statistics. A post that says a number must say where it came from.

## Config (env)

`DATABASE_URL`, `REDIS_URL` (optional cache), `SECRET_KEY`, `PUBLIC_URL`, `PAYSTACK_SECRET_KEY` / `PAYSTACK_SECRET_KEY (webhook sig)` (billing), `ANTHROPIC_API_KEY` (AI tools), SMTP settings for email.

See [PRODUCTION.md](PRODUCTION.md) for deploy notes.

## Tests

```bash
python -m pytest tests -q
```

The suite runs on a separate PostgreSQL database named `<your database>_test` (created on first run, on the same server as `DATABASE_URL`;
set `TEST_DATABASE_URL` to use another). It drops and recreates every table, so it refuses to use a name that does not end in `_test`.


### Checkout & webhooks

Consumer billing is wired to Paystack:

| Route | Purpose |
|-------|---------|
| `GET /checkout?plan=weekly\|monthly\|lifetime` | Plan selection + email form |
| `POST /checkout` | Creates Paystack hosted checkout (`bw_*` tx_ref) |
| `POST /checkout/session` | JSON: starts a checkout for the in-page Paystack popup (`reference`, `access_code`) |
| `GET /checkout/status?reference=` | Polled by the checkout page: verifies with Paystack and applies the payment (works without a webhook, e.g. on localhost) |
| `GET /billing/return` | Browser return; re-verifies transaction (the redirect-flow fallback) |
| `POST /webhooks/paystack` | `charge.completed` (initial + renewals) and `subscription.cancelled` |
| `POST /account/billing/cancel` | Cancel recurring; access until period end |

Prices are quoted in **USD** with a smaller **GHS** amount underneath (rate: `USD_TO_GHS`, default 15.50). Charges are collected in GHS via Paystack.\n\nSet `PAYSTACK_SECRET_KEY` and `PAYSTACK_SECRET_KEY (webhook sig)`. Without them, `BILLING_ENABLED` is false and `/checkout` grants access in dev when `SECRET_KEY` is the default.

Access control: `_member_has_access` uses `billing.has_active_access(email)` when billing is on.

### Résumé upload, the filtered board and the résumé coach

| Piece | What it does |
|-------|--------------|
| `POST /resume/upload` | PDF, Word `.docx` or text file (2 MB max). Only the text is kept, never the file. Saving also reads the member's job titles, level and skills **for free** (not deducted from the daily token budget; logged as `free:match_profile`). |
| `/jobs`, `/companies` | A member with a résumé sees jobs that fit it by default (title words and level, no AI call per page view). `?for=all` shows everything; a specific search (`q`) pauses it. |
| `POST /resume/profile/edit` | The member can correct the job titles and level that were read. |
| `POST /jobs/{id}/tailor` | Full résumé rewrite for one job: the posting is analysed first (cached per job, free), the writer must quote proof for each requirement, then the rewrite is fact-checked against the original and corrected. Unprovable claims become `[ADD: ...]` prompts or gaps. |
| `POST /jobs/{id}/coach` | Résumé coach for one job: what the résumé shows, what is missing, specific edits (claim-adding edits are marked "only if true"). Uses AI tokens the first time and is remembered per résumé until the text changes. Tailor résumé stays as the optional "apply these edits" step. |
| `?template=classic\|modern\|compact` | Layout for a tailored résumé or cover letter, on `/tailored/{id}` and `/letters/{id}` and their `/pdf`, `/docx` (Word), `/download` (.txt) and `/print` routes. Exactly three, all one column and ATS-friendly; a résumé and a letter in the same template match. The last pick is remembered in the `cs_template` cookie (no migration); an unknown name falls back to Classic. Layouts live in `app/docfmt.py` (`TEMPLATES`). Words never change, only layout. |

### Data quality

```bash
python -m app.cli scrape          # seed registry (if empty) + scrape all active employers
python -m app.cli data-health     # employers, errors, job counts, stale stats
python -m app.cli prune-dead      # deactivate hard-404 boards with zero jobs
python -m app.cli verify          # spot-check board tokens vs live pages
python -m app.cli discover candidates data/candidates.txt
```

In-process scheduler (`ENABLE_SCHEDULER=1`) runs `scrape_all` every `SCRAPE_INTERVAL_HOURS` (default 6).

Freshness:
- `JOB_STALE_DAYS` (30): jobs not seen on a board for this long are deactivated
- `DEFAULT_MAX_JOB_AGE_DAYS` (90): feed hides older postings by `posted_at` / `first_seen`
- `CLOSE_GRACE_HOURS` (12): wait before treating a missing listing as closed

Dedupe: same title+description within a company shares a `dup_key`; search shows one card with “+N locations”.

### Member flow (access, paying, onboarding)

- **First-time onboarding.** After signup, new members land on `/onboarding` (situation, desired title, level, salary range, location, biggest frustration). Primary CTA goes to `/pricing?from=onboarding`; secondary continues with free preview. Answers are stored in `member_profiles`.

- **A plan belongs to a confirmed email.** `_member_has_access` needs `member.verified` as well as an active subscription, so signing up with
  someone else's address never borrows their plan. A browser that completes a checkout is trusted for that address (`session["paid_email"]`),
  so a guest who pays and then signs up with the same email is confirmed on the spot. Migration `0003` confirms members who already pay.
- **Buying while paid up never loses time.** A lifetime holder cannot buy anything; a weekly/monthly bought on top of a live plan starts when the
  live plan ends; `current_subscription` returns the row that lasts longest, not the newest.
- **Pay and come back.** `GET /checkout?plan=…&next=/jobs/123` remembers the page; after payment the member lands there with `?unlocked=1`.
  `/go/{id}` for a non-subscriber goes to checkout with the job remembered. A new signup starts on `/resume?welcome=1`.
- **Apply fills the tracker.** `/go/{id}` adds (or advances) the job in the member's tracker as *applied*.
- **Reports.** `POST /jobs/{id}/report-closed`: three confirmed members hide a listing (`jobs.status = 'closed'`, `app/reports.py`).
- **Expiry reminders.** The scheduler (`app/reminders.py`) emails once, ~a day before a pass or cancelled plan ends.
- **Alerts.** A signed-in confirmed member's alert for their own address needs no confirmation email; `POST /alerts/from-resume` makes one
  daily alert per job title read from the résumé.
- **Application checklist.** `GET /jobs/{id}/prepare` lists coach → tailor → letter → interview → apply with what is done and what each costs.
