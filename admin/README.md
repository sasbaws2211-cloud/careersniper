# CareerSniper platform admin

A separate web app for running the platform: its own login and session. It shares the database with the public site and
uses the site's own code for anything that has to behave identically (refunds, withdrawing a referral reward, deleting a member, scraping a company).
The public app also mounts it at `/platform-admin`, so a single Render web service can serve both the public site and the full admin dashboard.

## Run it

```
python -m admin create-admin you@example.com --owner      # the first owner; asks for a password (12+ characters)
python -m admin run                                       # http://127.0.0.1:8030
```

It reads the same `DATABASE_URL` as the public site. Other commands: `list-admins`, `set-password <email>`.
For scripts, `ADMIN_NEW_PASSWORD` supplies the password instead of the prompt.

When using the main site service, open `https://<your-render-domain>/platform-admin/login`. Set `APP_ENV=production` and `ADMIN_SECRET_KEY` in Render (use a unique random value of at least 32 characters). To create the first owner without Render Shell access, temporarily set `ADMIN_BOOTSTRAP_EMAIL` and `ADMIN_BOOTSTRAP_PASSWORD` in the Render environment settings, then deploy. The password must follow the rules below. On startup, the app creates an owner only if no active admin exists; it never changes an existing account's password. After the deploy succeeds, remove both bootstrap variables from Render and deploy again. The admin dashboard uses the same database as the public site. Keep the login protected with a strong, unique password; an access proxy is recommended for a public Render domain.

| Setting | Default | Meaning |
|---|---|---|
| `ADMIN_ENV` | `APP_ENV` (development locally) | `production` makes the app refuse to start with an unsafe setup, and turns on HTTPS-only cookies and HSTS |
| `ADMIN_SECRET_KEY` | random per start (dev) | Signs the session cookie. **Required in production**, 32+ random characters. Different from the public site's `SECRET_KEY` on purpose |
| `ADMIN_HOST` / `ADMIN_PORT` | `127.0.0.1` / `8030` | Keep it on loopback and reach it over a VPN, SSH tunnel or an access proxy (Cloudflare Access, Tailscale ...). Do not publish it on the open internet |
| `ADMIN_SESSION_HOURS` | `8` | A sign-in never lasts longer than this |
| `ADMIN_IDLE_MINUTES` | `120` | ...or this long without a click |

In production the database schema comes from migrations (`alembic upgrade head`, which now includes `0016` for the audit log), as for the public site.

## What is in it

- **Dashboard**: members, live plans, money taken in 30 days, failed and stuck payments, live jobs, scraper health, AI use, 14-day charts, recent activity.
- **Members**: search and filter; one page per member with their plans, payments, AI use, referrals and activity. Actions: confirm email, give free days (with a reason), refund (ends the plan and withdraws the referral reward), delete account (owner only, type the email to confirm).
- **Billing**: payments, plans and the raw Paystack event log.
- **Companies**: the scraper registry with errors and staleness, add a company (scraped at once), switch off, scrape now.
- **Jobs**: search; remove a job for good (it stays hidden even if the employer keeps it listed) or restore it.
- **AI settings**: the platform-wide AI provider (Anthropic, OpenAI or any OpenAI-compatible server), model, API key, limits and a master on/off switch. See below.
- **AI usage**, **Referrals** (withdraw a reward), **Audit log**, **Admins** (owners add and switch off admins), **System**.

## AI settings

Open **AI settings** to choose the provider and model for job match, cover letters, tailored résumés and interview coaching, paste the API key, and
set the limits: free trial requests, requests per day for each plan, calls at once, longest answer and timeout. Everything here is stored in the
database and read by the public site, so **a change reaches the site within about 10 seconds, with no restart**. A value that is not set here falls back to the
server's environment variable (`AI_PROVIDER`, `AI_MODEL`, `ANTHROPIC_API_KEY` ...), so an existing setup keeps working.

- **Master switch**: untick "AI features are on" to hide every AI feature on the site at once.
- **Test these values** sends one tiny real request using what is in the form, saved or not, so you find a wrong key or model name before customers do. It saves nothing.
- **The API key is stored encrypted** and is never shown again (only that one is saved and its last four characters). It is never written to the audit log.
  The public site and this app must run with the **same `SECRET_KEY`** (or set the same `SETTINGS_SECRET` in both): the key is encrypted with it. If they differ the
  page says the key cannot be read and AI stays off until you enter it again.
- A server on the internet must use `https://`: every résumé is sent to it. Plain `http://` is accepted only for this machine or a private network address.
- Only an **owner** can change these settings; other admins can see them. Every change and test is in the audit log.

## Safety

- Own login: bcrypt passwords, 5 failures per account or 20 per address locks sign-in for 15 minutes, sessions end after the password changes or the admin is switched off.
- Every change is a POST with a per-session token and a same-site check; cookies are `SameSite=Strict`; pages forbid inline scripts and styles and framing.
- **Every sign-in, failed sign-in and change is written to the audit log** (who, what, which record, from which address). The app has no way to edit or delete audit entries.
- Reasons are required for removing a job and for giving free access, so the log says why.
- Only an **owner** can add or switch off admins and delete members. The last owner cannot be switched off.

## Things to know

- **The public site no longer has an admin sign-up.** Its `/signup` page used to create an admin account for anyone who asked; it now returns 404. Admins are created only here, from the command line. If you ran the public site on the internet before this change, run `python -m admin list-admins` and check there are no accounts you do not recognise.
- The old `/admin` pages on the public site still exist but nobody can reach them without an admin account made here; use this app instead.
- The **System** page shows this app's own environment. If the public site runs with different settings, they will differ.
- Refunds do not move money. The refund action ends access and withdraws the referral reward; you return the money in the Paystack dashboard.
