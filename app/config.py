import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")      # development convenience; real environment variables win

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/careersniper")
DB_POOL = os.getenv("DB_POOL", "")                                 # "null" = no pooling (tests)
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))
SCRAPE_BATCH_SIZE = int(os.getenv("SCRAPE_BATCH_SIZE", "100"))
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
PLATFORM_NAME = os.getenv("PLATFORM_NAME", "CareerSniper")
PLATFORM_TAGLINE = os.getenv("PLATFORM_TAGLINE", "Find jobs not on LinkedIn/Indeed")
SCRAPE_INTERVAL_HOURS = float(os.getenv("SCRAPE_INTERVAL_HOURS", "6"))
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "1") == "1"
JOB_STALE_DAYS = int(os.getenv("JOB_STALE_DAYS", "30"))
DEFAULT_MAX_JOB_AGE_DAYS = int(os.getenv("DEFAULT_MAX_JOB_AGE_DAYS", "90"))
CLOSE_GRACE_HOURS = float(os.getenv("CLOSE_GRACE_HOURS", str(2 * SCRAPE_INTERVAL_HOURS)))

# B2C consumer plans (CareerHound-style). amount is in cents for the billing provider.
# free = preview only (limited results, no full filters / alerts / AI beyond trial)
# USD is the quoted / marketing currency. GHS is derived for Paystack charge + secondary display.
# Override rate with USD_TO_GHS env (e.g. 15.5). Amounts charged are in pesewas (GHS * 100).
USD_TO_GHS = float(os.getenv("USD_TO_GHS", "15.50"))

def _ghs_pesewas(usd: float) -> int:
    """Convert a USD list price to GHS pesewas for Paystack (integer, no fractions)."""
    return int(round(usd * USD_TO_GHS * 100))

def _ghs_display(usd: float) -> str:
    """Human GHS amount, e.g. 'GH₵108.50'."""
    return f"GH₵{usd * USD_TO_GHS:,.2f}"

# Estimates shown next to prices for visitors in these countries (see currency.py). Payments are always taken in GHS.
# They are fixed numbers, not live rates: check them now and then. Override with USD_TO_NGN, USD_TO_KES, USD_TO_ZAR.
USD_TO_NGN = float(os.getenv("USD_TO_NGN", "1500"))
USD_TO_KES = float(os.getenv("USD_TO_KES", "129"))
USD_TO_ZAR = float(os.getenv("USD_TO_ZAR", "18"))
COUNTRY_HEADER = os.getenv("COUNTRY_HEADER", "CF-IPCountry")      # the header your host or CDN uses to report the visitor's country

CONSUMER_PLANS = {
    "pass": {
        "label": "3-day pass",
        "price_usd": 3,
        "price": 3,
        "amount_cents_usd": 300,
        "amount_pesewas": _ghs_pesewas(3),
        "price_ghs_display": _ghs_display(3),
        "interval": "once",          # one payment, no renewal
        "days": 3,
        "ai_requests_daily": 100,
        "description": "Full access for 3 days, paid once and never renewed. A low-risk way to try everything.",
    },
    "weekly": {
        "label": "Weekly",
        "price_usd": 7,
        "price": 7,  # alias for templates that use .price
        "amount_cents_usd": 700,
        "amount_pesewas": _ghs_pesewas(7),  # charged on Paystack
        "price_ghs_display": _ghs_display(7),
        "interval": "weekly",
        "days": 7,
        "ai_requests_daily": 150,
        "description": "Full access for 7 days. Perfect for an intensive search week.",
    },
    "monthly": {
        "label": "Monthly",
        "price_usd": 20,
        "price": 20,
        "amount_cents_usd": 2000,
        "amount_pesewas": _ghs_pesewas(20),
        "price_ghs_display": _ghs_display(20),
        "interval": "monthly",
        "days": 30,
        "ai_requests_daily": 300,
        "description": "Full access for 30 days. Best for most job seekers.",
    },
    "lifetime": {
        "label": "Lifetime",
        "price_usd": 50,
        "price": 50,
        "amount_cents_usd": 5000,
        "amount_pesewas": _ghs_pesewas(50),
        "price_ghs_display": _ghs_display(50),
        "interval": "lifetime",
        "days": 36500,
        "ai_requests_daily": 300,
        "description": "One-time payment. Lifetime access to the job database.",
    },
}

# Legacy alias used by some modules during the pivot
PLANS = {
    "free":   {"price": 0,  "ai_credits": 0,   "subscribers": 0, "featured_jobs": 0},
    "pass":   {"price": 3,  "ai_credits": 100,   "subscribers": 0, "featured_jobs": 0},
    "weekly": {"price": 7,  "ai_credits": 150,  "subscribers": 0, "featured_jobs": 0},
    "monthly":{"price": 20, "ai_credits": 300,  "subscribers": 0, "featured_jobs": 0},
    "lifetime":{"price": 50,"ai_credits": 300,  "subscribers": 0, "featured_jobs": 0},
    "pro":    {"price": 20, "ai_credits": 300,  "subscribers": 0, "featured_jobs": 0},  # alias
    "premium":{"price": 50, "ai_credits": 300,  "subscribers": 0, "featured_jobs": 0},  # alias
}

# Free preview limits (logged-out or free-tier members)
PREVIEW_JOB_LIMIT = int(os.getenv("PREVIEW_JOB_LIMIT", "15"))
PREVIEW_PAGE_LIMIT = 1

# ---- Billing (Paystack) ----
# Charge currency is GHS (pesewas). UI quotes USD primary + GHS secondary.
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY", "")
PAYSTACK_BASE_URL = os.getenv("PAYSTACK_BASE_URL", "https://api.paystack.co")
BILLING_CURRENCY = os.getenv("BILLING_CURRENCY", "GHS")  # Paystack charge currency
BILLING_QUOTE_CURRENCY = "USD"  # primary display currency
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8020").rstrip("/")
YEARLY_MONTHS = 10
BILLING_ENABLED = bool(PAYSTACK_SECRET_KEY)
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "localhost")
# Escape hatch: serve full access for free in production with billing off. Never set this on a paid site.
ALLOW_BILLING_OFF = os.getenv("ALLOW_BILLING_OFF", "0") == "1"

# Money-back promise shown on the pricing, checkout and refund pages. Refunds are issued by hand in the Paystack dashboard.
REFUND_DAYS = int(os.getenv("REFUND_DAYS", "7"))
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@careersniper.site")   # must be a mailbox you read: the refund promise depends on it

# ---- Email ----
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
MAIL_FROM = os.getenv("MAIL_FROM", "no-reply@careersniper.com")  # must be a verified domain in Resend
DIGEST_MAX_JOBS = 20


# ---- AI tools (job match, cover letters, interview coach) ----
# AI_PROVIDER: anthropic | openai | openai_compatible (alias: custom). Left empty it is guessed from the keys below.
#   anthropic          ANTHROPIC_API_KEY (AI_MODEL defaults to claude-sonnet-5-5)
#   openai             OPENAI_API_KEY and AI_MODEL (e.g. gpt-4.1-mini)
#   openai_compatible  AI_BASE_URL (e.g. http://localhost:11434/v1 for Ollama, https://openrouter.ai/api/v1) and AI_MODEL;
#                      AI_API_KEY (or OPENAI_API_KEY) only if that server wants one
# AI_API_KEY, when set, wins over the vendor-specific keys.
AI_PROVIDER = os.getenv("AI_PROVIDER", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
AI_TIMEOUT = float(os.getenv("AI_TIMEOUT", "60"))
AI_MAX_OUTPUT_TOKENS = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "6000"))   # hard ceiling on one answer, whatever a tool asks for
AI_MAX_CONCURRENCY = int(os.getenv("AI_MAX_CONCURRENCY", "8"))           # model calls in flight at once
AI_FREE_REQUESTS = int(os.getenv("AI_FREE_REQUESTS", "0"))               # lifetime AI requests for members without a paid plan
MIN_PAID_DAILY_REQUESTS = 100                                              # every paid plan starts at 100 AI requests a day, whatever an admin types
AI_JSON_MODE = os.getenv("AI_JSON_MODE", "0") == "1"                     # ask OpenAI-style servers for response_format=json_object (not all support it)
AI_TOKEN_PARAM = os.getenv("AI_TOKEN_PARAM", "")                         # override: max_tokens or max_completion_tokens
AI_EXTRA_BODY = os.getenv("AI_EXTRA_BODY", "")                           # JSON merged into every OpenAI-style request, e.g. {"chat_template_kwargs": {"enable_thinking": false}} to stop a reasoning model spending its token budget on thinking

# ---- Environment / production hardening ----
APP_ENV = os.getenv("APP_ENV", "development")
IS_PROD = APP_ENV.lower() == "production"
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
ALLOW_CONSOLE_MAIL = os.getenv("ALLOW_CONSOLE_MAIL", "0") == "1"



async def production_problems(force: bool = False) -> tuple[list[str], list[str]]:
    """(fatal, warnings) for a production deployment."""
    if not (IS_PROD or force):
        return [], []
    fatal, warn = [], []
    if SECRET_KEY == "dev-only-change-me" or len(SECRET_KEY) < 32:
        fatal.append("SECRET_KEY must be a long random value (32+ characters). It signs sessions and email links.")
    if not PUBLIC_URL.startswith("https://") or "localhost" in PUBLIC_URL:
        fatal.append("PUBLIC_URL must be the https:// URL of your platform site.")
    if not RESEND_API_KEY and not ALLOW_CONSOLE_MAIL:
        fatal.append("RESEND_API_KEY is not set: confirmation, password-reset, and job-alert emails would never be delivered.")
    if not PAYSTACK_SECRET_KEY:
        if ALLOW_BILLING_OFF:
            warn.append("Billing is off and ALLOW_BILLING_OFF=1: every logged-in member gets full access for free.")
        else:
            fatal.append("PAYSTACK_SECRET_KEY is not set: without it the paywall cannot sell or verify access. "
                         "Set it (or ALLOW_BILLING_OFF=1 to knowingly run free).")
    if "SUPPORT_EMAIL" not in os.environ:
        warn.append("SUPPORT_EMAIL is not set: the refund promise tells customers to write to "+SUPPORT_EMAIL+"; make sure that mailbox exists.")
    from . import ai
    warn.extend(await ai.config_problems())
    if not INTERNAL_TOKEN:
        warn.append("INTERNAL_TOKEN is not set.")
    return fatal, warn


def free_access() -> bool:
    """True when billing is off AND the deployment is allowed to hand out full access (dev, or the explicit override).
    Evaluated at call time so a production site that loses its Paystack key fails closed, not open."""
    return not BILLING_ENABLED and (not IS_PROD or ALLOW_BILLING_OFF)

# "migrations": refuse to start unless the database is at the newest Alembic revision. "none": skip the check (tests only).
SCHEMA_MODE = os.getenv("SCHEMA_MODE", "migrations")
