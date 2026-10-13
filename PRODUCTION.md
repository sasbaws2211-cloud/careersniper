# CareerSniper: production guide

What you need to put this in front of paying customers: the deploy walkthrough, the configuration checklist, operations
(backups, upgrades, monitoring), the security review (what was found and fixed, and what remains), and the limits to know about.

Checked 2026-10-06: the production image builds, refuses to start with an unsafe configuration, starts and passes its
health check with a correct one, and the full test suites (both on PostgreSQL) pass on the pinned dependencies.
**Not checked: real Paystack payments, real email delivery, real Claude calls, real Let's Encrypt certificates.** Those need your
accounts and keys; see "First-day checks".

---

## 1. Architecture in one minute

```
Internet ──► Caddy (HTTPS, certificates, body limit) ──► app (FastAPI, 1 process) ──► PostgreSQL
                                                              │
                              scheduler thread: refresh employers' jobs + send alert digests (hourly, DB-locked)
                              outbound: employers' public job APIs, Resend, Paystack, Claude API
```
* One app container (`--workers 1`) and one database. The platform lives at `BASE_DOMAIN`; each customer board at `<slug>.BASE_DOMAIN`;
  Pro customers can use their own domain.
* Schema changes only through Alembic (`alembic upgrade head`, run automatically by the container at start).
* Everything is configured with environment variables (`.env.production.example` lists them all).

## 2. Deploy walkthrough (single server, Docker Compose + Caddy)

**What you need:** a Linux server (2 vCPU / 4 GB is plenty to start) with Docker, a domain, an Resend provider, and optionally
Paystack and Anthropic accounts.

1. **DNS.** For `jobs.example.com`: an `A` record for `@` (the platform) and a wildcard `A` record for `*` (all boards), both to the server's IP.
   Open ports 80 and 443 only.
2. **Get the code onto the server** and `cd careersniper`.
3. **Configure.** `cp .env.production.example .env.production`, then fill in every value. Generate each secret with
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Keep this file out of git and readable only by you (`chmod 600`).
4. **Pre-flight.** `docker compose -f docker-compose.prod.yml --env-file .env.production build app` then
   `docker compose -f docker-compose.prod.yml --env-file .env.production run --rm app python -m app.cli doctor`.
   Fix every `FAIL`; read every `WARN` (a "schema is behind" warning on a brand-new database is normal: the container migrates at start). In production the app also refuses to start with an unsafe config and prints why.
5. **Start.** `docker compose -f docker-compose.prod.yml --env-file .env.production up -d`. The app applies migrations, then serves.
6. **Check.** `https://jobs.example.com/ready` returns `{"ok":true,"db":true}`; sign up a test organization; open its
   `https://<slug>.jobs.example.com`. The first visit to each new hostname takes a few seconds while its certificate is issued.
7. **Fill the job pool (important).** On first deploy run a full bootstrap so search is dense immediately:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production run --rm app \
  python -m app.cli bootstrap
```

This seeds the employer registry (~150+ boards), scrapes all of them, prunes dead tokens, and prints `data-health`.
After that, `ENABLE_SCHEDULER=1` refreshes stale boards every `SCRAPE_INTERVAL_HOURS` (default 6) and deactivates hard-404 boards with zero jobs.
Ops: `python -m app.cli scrape` · `prune-dead` · `data-health` · `discover candidates data/candidates.txt`.
   Add or verify employers with `python -m app.cli verify` (see README, "Registry").

### Wildcard certificates (when you onboard many boards)
Let's Encrypt allows 50 new certificates per week per registered domain, and the default Caddyfile issues one per board on first visit.
Past ~40 new boards a week, use one wildcard certificate instead: build Caddy with your DNS provider's plugin, replace the
`https://` block's `tls { on_demand }` with `tls { dns <provider> {env.DNS_API_TOKEN} }` for `*.jobs.example.com, jobs.example.com`, and keep the
on-demand block only for customers' custom domains.

### Customer custom domains (Pro plan)
The customer enters a domain in the admin console and creates a `CNAME` to `CUSTOM_DOMAIN_TARGET`. They click **Verify**: the platform
resolves the domain (public addresses only), fetches `/.well-known/careersniper-verify.txt` from it and compares a secret token. Only the
owner of the domain's DNS can pass. Until verified the domain serves nothing. Caddy asks `/internal/tls-allowed` before issuing a
certificate, so only verified domains and real boards get one.

## 3. Configuration checklist

`python -m app.cli doctor` verifies all of this. **Required** means the app refuses to start without it in production.

| Variable | Required | Notes |
|---|---|---|
| `APP_ENV=production` | yes | Turns on HTTPS-only cookies, HSTS, hides `/docs`, enforces the rows below |
| `SECRET_KEY` | **yes** | 32+ random characters. Signs sessions and email links. Rotating it signs everyone out and voids pending links |
| `DATABASE_URL` | **yes** | PostgreSQL only (compose builds it from `DB_PASSWORD`) |
| `BASE_DOMAIN`, `PUBLIC_URL`, `TENANT_URL_TEMPLATE` | **yes** | Real domain; the two URLs must be `https://` (links in emails, payment redirects) |
| `Resend_HOST/PORT/USER/PASSWORD`, `MAIL_FROM` | **yes** | Sign-up confirmation, password reset, invites and alerts all use email. Set up **SPF, DKIM and DMARC** for the `MAIL_FROM` domain or mail goes to spam |
| `INTERNAL_TOKEN` | recommended | Needed for automatic HTTPS on custom domains |
| `PAYSTACK_SECRET_KEY`, `PAYSTACK_WEBHOOK_HASH` | for paid plans | Without them billing is off. Set the Paystack webhook URL to `<PUBLIC_URL>/webhooks/flutterwave` and its secret hash to `PAYSTACK_WEBHOOK_HASH` |
| *AI settings* | recommended | The AI provider, model, key and limits can instead be set in the admin app (**AI settings**), which stores them in the database (the key encrypted) and overrides the variables below without a restart. For that, run the public site and the admin app with the **same `SECRET_KEY`** (or `SETTINGS_SECRET`) |
| `AI_PROVIDER` | for AI tools | `anthropic`, `openai` or `openai_compatible` (alias `custom`). Empty: guessed from the keys. Without a usable provider the AI buttons are hidden |
| `ANTHROPIC_API_KEY` | if `anthropic` | `AI_MODEL` defaults to `claude-sonnet-5-5` |
| `OPENAI_API_KEY`, `AI_MODEL` | if `openai` | There is no default model: set one, e.g. `gpt-4.1-mini` |
| `AI_BASE_URL`, `AI_MODEL`, `AI_API_KEY` | if `openai_compatible` | Any OpenAI-style server: Ollama `http://localhost:11434/v1`, OpenRouter `https://openrouter.ai/api/v1`, Groq, Together, vLLM, LM Studio, a company gateway. The key is optional for local servers. Use `https://` for anything that is not on the same machine |
| `AI_TIMEOUT`, `AI_MAX_OUTPUT_TOKENS`, `AI_MAX_CONCURRENCY`, `AI_FREE_TRIALS`, `AI_JSON_MODE`, `AI_TOKEN_PARAM` | no | 60 s, 2000 tokens per answer, 8 calls at once, 0 free requests (AI is paid-only), JSON mode off, `max_completion_tokens` for OpenAI and `max_tokens` elsewhere. Daily requests per member come from the plan (15 / 30 / 50). Run `python -m app.cli ai-check` after setting these: it makes one small real call. Set a **monthly spend limit** with your provider
| `USD_TO_GHS`, `USD_TO_NGN`, `USD_TO_KES`, `USD_TO_ZAR`, `COUNTRY_HEADER` | recommended | Fixed exchange rates (defaults 15.5, 1500, 129, 18). **Payments are always taken in GHS** at `USD_TO_GHS`; the others only decide the "about ₦10,500" estimates shown next to prices, so check them now and then. `COUNTRY_HEADER` (default `CF-IPCountry`) is the request header your CDN uses to report the visitor's country; without one, the browser language is used |
| `SENTRY_DSN` | recommended | Error reporting (`pip install sentry-sdk` in the image). No personal data is sent |
| `SCRAPE_INTERVAL_HOURS` | no | Default 6 |

## 4. First-day checks (the things only you can verify)

1. **Email:** sign up as a member on a test board; the confirmation email arrives (not in spam). Do the same for password reset and an alert.
2. **Billing (test mode first):** with Paystack *test* keys, buy Growth with a test card, confirm the plan activates, then cancel.
   Only then switch to live keys. Confirm the webhook shows "delivered" in the Paystack dashboard.
3. **AI:** `docker compose ... exec app python -m app.cli ai-check` makes one real call and prints the model, tokens and request id.
4. **Certificates:** open a board over `https://` and a Pro customer's verified custom domain.
5. **Backups:** run `scripts/backup.sh` and **restore it into a scratch database** (`scripts/restore.sh`). Do this before launch.

## 5. Operations

**Backups.** `DATABASE_URL_PG=postgresql://... BACKUP_DIR=/backups scripts/backup.sh` writes a compressed dump and keeps 14 days.
Schedule it nightly (cron) and copy the files off the server. Test restores regularly.

**Upgrades.** Take a backup, `git pull`, `docker compose ... up -d --build`. The container runs `alembic upgrade head` first; if a migration fails the
app does not start and the old data is untouched. Migrations are forward-only; to go back, restore the backup taken before the upgrade.
Adding a column or table: change `app/models.py`, run `alembic revision --autogenerate -m "what changed"`, **read the generated file**, commit it.
The migration history starts at `0001_baseline` (generated from the SQLModel tables). A database moved over from the old SQLite file is loaded with `python -m app.cli migrate sqlite:///./careersniper.db postgresql://...` after `alembic upgrade head`.

**Monitoring.** Point an external uptime monitor at `/ready` (503 when the database is unreachable). Keep the app logs (a
`[scheduler] refreshed ...` line appears every hour; no line for several hours means the scheduler is stuck). Every server-error page shows a
reference id that matches the `request_id` in the logs and the `X-Request-ID` response header. Set Sentry for alerts.

**Dependencies.** `pip install -r requirements-dev.txt && pip-audit -r requirements.txt` regularly (monthly, and before each release).
Versions are pinned to what the tests pass on; bump deliberately and run both suites
(`python -m pytest tests`, and `TEST_PG_ADMIN_URL=postgresql://... python -m pytest tests_pg`).

**Incident quick reference**
* *Site down:* `docker compose ps`, `docker compose logs --tail=200 app`. A startup refusal prints exactly which setting is wrong.
* *Emails not arriving:* check Resend credentials and the SPF/DKIM records; `docker compose logs app | grep -i mail`.
* *A customer's custom domain doesn't work:* admin console shows "Not verified"; check their CNAME and that port 80 reaches you.
* *Suspected abuse of one board:* set `tools_disabled` / change their plan in the database, or remove the organization; AI use per board is in `ai_usage`.
* *Leaked key:* rotate `SECRET_KEY` (signs everyone out), the API key (admin console), the AI key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `AI_API_KEY`), Resend password, Paystack keys.

## 6. Security review

Method: read every route and module against the OWASP categories, wrote a failing test for each real problem *before* fixing it,
then ran the whole suite. Found and fixed during this pass:

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | **A Pro customer could set their custom domain to the platform's own domain** (it was matched before everything else) and take over the marketing and login pages: a phishing risk. Two boards claiming one domain also crashed with a 500 | High | **Fixed.** Reserved/malformed domains rejected; a domain serves nothing until its DNS ownership is verified; the verifier refuses private/loopback addresses (SSRF-safe) and fetches by IP |
| 2 | **One slow AI call froze the whole server for every tenant.** AI endpoints were `async` but called the SDK synchronously (measured: `/health` waited 1.22 s behind a single call; calls can run 90 s) | High | **Fixed.** Calls run in worker threads, capped at `AI_MAX_CONCURRENCY`; regression test measures it. Same for the payment webhook |
| 3 | Known vulnerabilities in pinned libraries (Starlette: 14 advisories, python-multipart: 12) | High | **Fixed.** Upgraded to current versions, whole suite re-run, `pip-audit` reports none |
| 4 | No brute-force protection on admin login; no admin password reset (a locked-out owner had no recourse) | Medium | **Fixed.** 8 failures per account+IP and 40 per IP per 15 min; emailed single-use 1-hour reset links |
| 5 | Sessions survived password changes | Medium | **Fixed.** A password change or reset ends every other session (admin and member) |
| 6 | No security headers; cookies not forced `Secure` | Medium | **Fixed.** CSP (frame-ancestors, base-uri, form-action, object-src), nosniff, referrer policy, HSTS and `Secure` in production |
| 7 | Organization names containing newlines could crash email sending / inject headers | Medium | **Fixed.** Names are sanitised and every mail header is cleaned |
| 8 | Unlimited public sign-ups, alert sign-ups, payment-return calls, API calls; inflatable click analytics | Medium | **Fixed.** Per-IP / per-key limits; one counted click per visitor per job per 10 min |
| 9 | API key could not be replaced | Low | **Fixed.** One-click rotation |
| 10 | Interactive API docs public; stack traces possible | Low | **Fixed.** Docs off in production; errors show a reference id, never internals |
| 11 | No request size limit | Low | **Fixed.** 1 MB in the app and 2 MB at Caddy |
| 12 | Logo field accepted any URL scheme | Low | **Fixed.** http(s) only |

Verified clean: every one of the 17 admin routes requires an admin session and every member route checks the member; tenants cannot read each
other's data (tests for resumes, documents, sessions, tokens, jobs hidden by curation); SQL is parameterised; templates auto-escape (the
resume editor builds the page with `textContent`); redirects only go to same-site paths; downloaded file names are sanitised; billing trusts only
provider-verified transactions and applies each event once; AI input is fenced and size-capped.

**Remaining risks (decide whether they matter for you)**
* **Sessions are signed cookies**, valid for 14 days. A stolen cookie works until it expires or the password changes; "log out" clears only that browser.
* **No two-factor authentication** for admins, and no audit log of admin actions.
* **CSP still allows inline scripts** (the pages use them), so XSS protection rests on auto-escaping, which is applied everywhere and tested.
* **Rate limits live in memory** of the single process (fine for the supported single-instance setup; use a shared store such as Redis before running several instances).
* **Email deliverability** depends entirely on your Resend provider and DNS records.
* **Legal:** you need your own Terms of Service and Privacy Policy (the app ships none), a data-retention policy, and a way to handle data-export/deletion requests
  from organization admins (members can already delete their own account; deletion of an entire organization is manual). Resumes are personal data.
* **Job data:** the job pool comes from employers' public job-board APIs. Review each source's terms before charging for the data, and keep
  the employer list verified (`python -m app.cli verify`).
* **AI cost exposure** is capped per board and per member, but also set a spend limit in the Anthropic console. AI output is a draft; the UI tells members to check every fact.

## 7. Known limits

* One app process by design (scheduler + in-memory limits). A Postgres advisory lock stops a second instance from double-sending alert digests, but
  scaling out needs a shared rate-limit store and a look at the thread-pool sizing.
* Search handles tens of thousands of jobs comfortably (20 to 150 ms on 31,000); beyond several hundred thousand, consider a dedicated search service.
* PDFs come from the browser's print dialog (no server-generated PDFs). No Word export.
* Paystack recurring billing is card-only and one currency per deployment (`BILLING_CURRENCY`).
