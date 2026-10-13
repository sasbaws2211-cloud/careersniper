"""Paystack billing for B2C consumer subscriptions.

Plans (config.CONSUMER_PLANS) are quoted in USD; charges go through Paystack in GHS
(pesewas). Secondary GHS display is computed from USD_TO_GHS.

  - weekly  : Paystack plan, interval weekly
  - monthly : Paystack plan, interval monthly
  - lifetime: one-time transaction (no plan)

Rules:
  * Money is only believed after Paystack verify confirms status == success.
  * Amount/currency on the verified transaction must match the checkout.
  * Each provider event is applied once (unique BillingEvent.event_key).
  * Cancelling keeps access until current_period_end.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import config
from .models import (BillingCheckout, BillingEvent, BillingProviderPlan,
                     Subscription, User)

PAID_PLANS = list(config.CONSUMER_PLANS.keys())
RECURRING_INTERVALS = ("weekly", "monthly")
ONE_TIME_INTERVALS = ("lifetime", "once")     # a single payment: no provider plan, nothing to cancel


class PaystackError(Exception):
    def __init__(self, message, http_status=None, transient=False):
        super().__init__(message)
        self.http_status, self.transient = http_status, transient


class BillingError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class PaystackClient:
    def __init__(self, secret_key: str, base_url: str = config.PAYSTACK_BASE_URL, transport=None, timeout=20):
        self.base = base_url.rstrip("/")
        self.secret_key = secret_key
        self.timeout, self.transport = timeout, transport
        self.headers = {
            "Authorization": f"Bearer {secret_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _call(self, method: str, path: str, body=None):
        try:       # one short-lived client per call: a pooled async client cannot be shared between event loops
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport, headers=self.headers) as http:
                r = await http.request(method, self.base + path, json=body)
        except httpx.HTTPError as e:
            raise PaystackError(f"Could not reach Paystack: {e}", None, True)
        try:
            j = r.json()
        except ValueError:
            j = None
        if r.status_code >= 400:
            msg = (j.get("message") if isinstance(j, dict) else None) or f"Paystack HTTP {r.status_code}"
            raise PaystackError(str(msg), r.status_code, r.status_code >= 500 or r.status_code == 429)
        if not isinstance(j, dict) or not j.get("status"):
            raise PaystackError((j or {}).get("message") or "Paystack returned an unexpected response", r.status_code)
        return j.get("data")

    async def create_plan(self, name: str, amount_pesewas: int, interval: str, currency: str = "GHS") -> str:
        """Create a Paystack plan; returns plan_code (e.g. PLN_xxx)."""
        d = await self._call("POST", "/plan", {
            "name": name,
            "amount": amount_pesewas,
            "interval": interval,
            "currency": currency,
        })
        code = d.get("plan_code") or d.get("id")
        if not code:
            raise PaystackError("Paystack did not return a plan code")
        return str(code)

    async def initialize_transaction(self, email: str, amount_pesewas: int, reference: str,
                               callback_url: str, plan_code: str | None, metadata: dict,
                               currency: str = "GHS") -> str:
        """Returns authorization_url for hosted checkout."""
        body = {
            "email": email,
            "amount": amount_pesewas,
            "reference": reference,
            "callback_url": callback_url,
            "currency": currency,
            "metadata": metadata or {},
        }
        if plan_code:
            body["plan"] = plan_code
            # When plan is set, Paystack uses the plan amount; amount is still required by some accounts
        d = await self._call("POST", "/transaction/initialize", body)
        url = d.get("authorization_url") or ""
        if not url.startswith("https://"):
            raise PaystackError("Paystack did not return a usable authorization URL")
        return url

    async def verify_transaction(self, reference: str) -> dict:
        return await self._call("GET", f"/transaction/verify/{reference}")

    async def list_subscriptions(self, customer_email: str | None = None) -> list:
        # Paystack list is paginated; for lookup we use customer code when available
        path = "/subscription?perPage=50"
        d = await self._call("GET", path)
        items = d if isinstance(d, list) else (d.get("data") if isinstance(d, dict) else []) or []
        if customer_email:
            items = [s for s in items
                     if ((s.get("customer") or {}).get("email") or "").lower() == customer_email.lower()]
        return items

    async def disable_subscription(self, subscription_code: str, email_token: str) -> None:
        await self._call("POST", "/subscription/disable", {
            "code": subscription_code,
            "token": email_token,
        })


_client: PaystackClient | None = None


def get_client() -> PaystackClient:
    global _client
    if _client is None:
        _client = PaystackClient(config.PAYSTACK_SECRET_KEY)
    return _client


# Back-compat aliases for payments.py and older imports
FlutterwaveError = PaystackError
FlutterwaveClient = PaystackClient


# ------------------------------------------------------------------ pricing helpers

def plan_info(plan: str) -> dict:
    info = config.CONSUMER_PLANS.get(plan)
    if not info:
        raise BillingError(f"Unknown plan: {plan}")
    return info


def price_cents(plan: str) -> int:
    """USD cents (display / legacy)."""
    return int(plan_info(plan).get("amount_cents_usd") or plan_info(plan).get("amount_cents") or 0)


def price_pesewas(plan: str) -> int:
    """GHS pesewas charged on Paystack."""
    return int(plan_info(plan)["amount_pesewas"])


def add_period(start: datetime, plan: str) -> datetime:
    info = plan_info(plan)
    if plan == "lifetime":
        return start + timedelta(days=int(info.get("days") or 36500))
    if plan == "weekly":
        return start + timedelta(days=7)
    if plan == "monthly":
        m = start.month - 1 + 1
        y, m = start.year + m // 12, m % 12 + 1
        day = min(start.day, calendar.monthrange(y, m)[1])
        return start.replace(year=y, month=m, day=day)
    return start + timedelta(days=int(info.get("days") or 30))


def _cents(v) -> int:
    return round(float(v) * 100)


def _provider_failure(e: PaystackError) -> BillingError:
    return BillingError(
        "Our payment provider is not responding right now. Please try again in a moment."
        if e.transient else
        "We could not start the payment. Please try again, or contact support.",
        502,
    )


async def _record_event(db: AsyncSession, key: str, typ: str, outcome_or_org=None, outcome=None, payload=None) -> bool:
    """Idempotent event log. Accepts new and legacy (org_id) call shapes."""
    if outcome is not None and not isinstance(outcome_or_org, str):
        final_outcome = outcome
        final_payload = payload or {}
    else:
        final_outcome = outcome_or_org if isinstance(outcome_or_org, str) else (outcome or "")
        final_payload = payload if payload is not None else (outcome if isinstance(outcome, dict) else {})
    try:
        async with db.begin_nested():
            db.add(BillingEvent(
                event_key=key, event_type=typ,
                outcome=(final_outcome or "")[:200],
                payload=final_payload or {},
            ))
            await db.flush()
        return True
    except IntegrityError:
        return False


def _summary(tx: dict) -> dict:
    customer = tx.get("customer") or {}
    return {
        "id": tx.get("id"),
        "reference": tx.get("reference"),
        "status": tx.get("status"),
        "amount": tx.get("amount"),
        "currency": tx.get("currency"),
        "email": customer.get("email") if isinstance(customer, dict) else None,
        "plan": (tx.get("plan") or {}).get("plan_code") if isinstance(tx.get("plan"), dict) else tx.get("plan"),
    }


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Paystack: HMAC SHA512 of body with secret key, hex digest in x-paystack-signature."""
    if not config.PAYSTACK_SECRET_KEY or not signature:
        return False
    digest = hmac.new(
        config.PAYSTACK_SECRET_KEY.encode("utf-8"),
        raw_body,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(digest, signature)


# ------------------------------------------------------------------ provider plans (recurring)

async def ensure_provider_plan(db: AsyncSession, ps: PaystackClient, plan: str) -> str | None:
    info = plan_info(plan)
    interval = info["interval"]
    if interval in ONE_TIME_INTERVALS:
        return None
    pesewas = info["amount_pesewas"]
    currency = config.BILLING_CURRENCY  # GHS
    row = await db.scalar(select(BillingProviderPlan).where(
        BillingProviderPlan.plan == plan,
        BillingProviderPlan.interval == interval,
        BillingProviderPlan.currency == currency,
        BillingProviderPlan.amount_cents == pesewas,
    ))
    if row:
        return row.provider_plan_id
    code = await ps.create_plan(
        name=f"{config.PLATFORM_NAME} {info['label']}",
        amount_pesewas=pesewas,
        interval=interval,
        currency=currency,
    )
    try:
        async with db.begin_nested():
            db.add(BillingProviderPlan(
                plan=plan, interval=interval, currency=currency,
                amount_cents=pesewas, provider_plan_id=code,
            ))
            await db.flush()
    except IntegrityError:
        await db.rollback()
        row = await db.scalar(select(BillingProviderPlan).where(
            BillingProviderPlan.plan == plan,
            BillingProviderPlan.interval == interval,
            BillingProviderPlan.currency == currency,
            BillingProviderPlan.amount_cents == pesewas,
        ))
        if row:
            return row.provider_plan_id
        raise
    return code


# ------------------------------------------------------------------ checkout

async def start_checkout(db: AsyncSession, ps: PaystackClient, email: str, plan: str, *, user_id: int | None = None, name: str = "") -> str:
    """Create the pending checkout and return Paystack's hosted-page URL (the redirect flow)."""
    return (await start_checkout_session(db, ps, email, plan, user_id=user_id, name=name))[0]


def access_code_of(url: str) -> str:
    """Paystack's hosted URL is https://checkout.paystack.com/<access_code>; the inline popup opens by that code."""
    return url.rstrip("/").rsplit("/", 1)[-1]


async def start_checkout_session(
    db: AsyncSession,
    ps: PaystackClient,
    email: str,
    plan: str,
    *,
    user_id: int | None = None,
    name: str = "",
) -> tuple[str, str]:
    """Create the pending checkout. Returns (hosted URL, our transaction reference)."""
    if plan not in PAID_PLANS:
        raise BillingError("Unknown plan.")
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise BillingError("A valid email is required to start checkout.")

    info = plan_info(plan)
    live = await current_subscription(db, email=email)
    now_ = datetime.utcnow()
    if live and live.current_period_end and live.current_period_end > now_:
        if live.plan == "lifetime":
            raise BillingError("You already have lifetime access, so there is nothing more to buy.", 409)
        if info["interval"] == "once":
            # a short pass would replace (and so cut short) a plan the person already has: refuse instead
            raise BillingError(f"You already have access until {live.current_period_end:%d %b %Y}. "
                               "A 3-day pass would replace it, so it is not needed.", 409)
    currency = config.BILLING_CURRENCY
    pesewas = info["amount_pesewas"]
    interval = info["interval"]

    try:
        plan_code = await ensure_provider_plan(db, ps, plan)
    except PaystackError as e:
        raise _provider_failure(e)

    reference = f"bw_{plan}_{secrets.token_hex(12)}"
    callback = f"{config.PUBLIC_URL}/billing/return"

    try:
        url = await ps.initialize_transaction(
            email=email,
            amount_pesewas=pesewas,
            reference=reference,
            callback_url=callback,
            plan_code=plan_code,
            metadata={
                "plan": plan,
                "interval": interval,
                "user_id": user_id or "",
                "price_usd": info.get("price_usd") or info.get("price"),
                "custom_fields": [
                    {"display_name": "Plan", "variable_name": "plan", "value": info["label"]},
                    {"display_name": "USD price", "variable_name": "usd", "value": f"${info.get('price_usd') or info.get('price')}"},
                ],
            },
            currency=currency,
        )
    except PaystackError as e:
        raise _provider_failure(e)

    db.add(BillingCheckout(
        tx_ref=reference,
        plan=plan,
        interval=interval,
        amount_cents=pesewas,  # stored as charged pesewas
        currency=currency,
        provider_plan_id=plan_code or "",
        customer_email=email,
        checkout_url=url,
        status="pending",
    ))
    await db.commit()
    return url, reference


async def checkout_status(db: AsyncSession, ps: PaystackClient, reference: str) -> dict:
    """Where a checkout stands, for the page that is waiting on it. Never trusts the browser: a payment counts only once Paystack
    itself reports it successful for this reference. {'status': 'paid' | 'pending' | 'failed' | 'unknown', 'provider': <paystack's word>}."""
    reference = (reference or "").strip()
    co = await db.scalar(select(BillingCheckout).where(BillingCheckout.tx_ref == reference)) if reference else None
    if not co:
        return {"status": "unknown"}
    if co.status == "paid":
        return {"status": "paid"}
    try:
        tx = await ps.verify_transaction(reference)
    except PaystackError as e:
        # 404: Paystack has not seen a payment attempt yet. Anything else (outage, rate limit): ask again shortly.
        return {"status": "pending", "provider": "not_started" if e.http_status == 404 else "unreachable"}
    word = (tx.get("status") or "").lower()
    if word == "success":
        await apply_verified_payment(db, ps, tx)
        await db.refresh(co)
        return {"status": "paid"} if co.status == "paid" else {"status": "pending", "provider": "confirming"}
    if word in ("failed", "reversed"):
        return {"status": "failed", "provider": word}
    return {"status": "pending", "provider": word or "pending"}      # abandoned/ongoing/pending: still open or not paid yet


# ------------------------------------------------------------------ apply verified payments


async def send_payment_receipt(email: str, plan: str, amount_label: str) -> None:
    """Best-effort receipt after a successful consumer payment."""
    try:
        from . import mailer, config
        name = config.PLATFORM_NAME
        text = (
            f"Thanks for subscribing to {name}.\n\n"
            f"Plan: {plan}\nAmount: {amount_label}\n\n"
            f"Access your jobs: {config.PUBLIC_URL.rstrip('/')}/jobs\n"
        )
        body = (
            f"<p>Thanks for subscribing to <b>{name}</b>.</p>"
            f"<p>Plan: <b>{plan}</b><br>Amount: {amount_label}</p>"
            + mailer.button(f"{config.PUBLIC_URL.rstrip('/')}/jobs", "Open job search")
        )
        await mailer.send(email, f"Payment received · {name}", text, mailer.wrap_html(body), from_name=name)
    except Exception:
        pass

async def apply_verified_payment(db: AsyncSession, ps: PaystackClient, tx: dict) -> dict:
    """Apply a Paystack-verified transaction (status success) to a pending checkout."""
    now = datetime.utcnow()
    status = (tx.get("status") or "").lower()
    reference = tx.get("reference") or ""
    key = f"charge:{tx.get('id') or reference}"

    if status != "success":
        await _record_event(db, key, "charge.success", f"ignored: status={status}", _summary(tx))
        await db.commit()
        return {"outcome": "ignored", "reason": f"status={status}"}

    co = await db.scalar(select(BillingCheckout).where(BillingCheckout.tx_ref == reference))
    if not co:
        # Might be a subscription renewal (no pending checkout)
        return await apply_renewal(db, ps, tx)

    return await _apply_initial(db, ps, tx, co, key, now)


async def _apply_initial(db, ps, tx, co: BillingCheckout, key: str, now: datetime) -> dict:
    paid = int(tx.get("amount") or 0)  # Paystack returns minor units
    tx_currency = (tx.get("currency") or "").upper()
    if tx_currency != co.currency.upper() or paid < co.amount_cents:
        if (await _record_event(db, key, "charge.success", "rejected: amount or currency mismatch", _summary(tx))):
            if co.status == "pending":
                co.status = "failed"
                co.provider_tx_id = str(tx.get("id") or "")
                co.completed_at = now
        await db.commit()
        return {"outcome": "rejected", "reason": "amount or currency mismatch"}

    if co.status == "paid":
        await _record_event(db, key, "charge.success", "ignored: checkout already paid", _summary(tx))
        await db.commit()
        return {"outcome": "ignored", "reason": "checkout already paid"}

    customer = tx.get("customer") or {}
    payer = (customer.get("email") if isinstance(customer, dict) else "") or co.customer_email
    payer = (payer or "").strip().lower()
    period_start = now
    if co.plan != "lifetime":
        owned = await current_subscription(db, email=payer)
        if owned and owned.plan != "lifetime" and owned.current_period_end and owned.current_period_end > now:
            period_start = owned.current_period_end      # buying again while still paid up adds time, never replaces it
    period_end = add_period(period_start, co.plan)

    if not (await _record_event(db, key, "charge.success", "applied", _summary(tx))):
        await db.rollback()
        return {"outcome": "duplicate"}

    for old in (await db.scalars(
        select(Subscription).where(
            Subscription.customer_email == payer,
            Subscription.status.in_(["active", "cancelled"]),
        )
    )):
        if old.status == "active" and old.provider_subscription_id:
            await _try_disable_subscription(ps, old)
        old.status = "replaced"

    user = await db.scalar(select(User).where(User.email == payer))
    user_id = user.id if user else None

    # Capture subscription_code from authorization / plan if present
    sub_code = None
    if isinstance(tx.get("plan"), dict):
        # Not always present on first charge
        pass
    auth = tx.get("authorization") or {}

    sub = Subscription(
        user_id=user_id,
        plan=co.plan,
        interval=co.interval,
        amount_cents=co.amount_cents,
        currency=co.currency,
        status="active",
        customer_email=payer,
        provider_plan_id=co.provider_plan_id or None,
        provider_subscription_id=sub_code,
        current_period_start=period_start,
        current_period_end=period_end,
        last_payment_at=now,
    )
    db.add(sub)
    await db.flush()
    if user:
        user.subscription_id = sub.id

    co.status = "paid"
    co.provider_tx_id = str(tx.get("id") or "")
    co.completed_at = now
    await db.commit()
    try:      # a friend's first purchase earns the person who invited them a free week; never let that fail the payment
        from . import referrals
        await referrals.reward_for_payment(db, payer, plan=co.plan)
    except Exception:
        await db.rollback()
    # Best-effort receipt (never fail the payment path)
    try:
        plan_label = co.plan
        amount_label = f"{(co.amount_cents or 0) / 100:.2f} {co.currency}"
        await send_payment_receipt(payer, plan_label, amount_label)
    except Exception:
        pass
    return {"outcome": "applied_initial", "email": payer, "plan": co.plan}


async def apply_renewal(db: AsyncSession, ps: PaystackClient, tx: dict) -> dict:
    now = datetime.utcnow()
    key = f"renewal:{tx.get('id') or tx.get('reference')}"
    if (tx.get("status") or "").lower() != "success":
        await _record_event(db, key, "charge.success", f"ignored: renewal status={tx.get('status')}", _summary(tx))
        await db.commit()
        return {"outcome": "ignored", "reason": "status"}
    customer = tx.get("customer") or {}
    email = (customer.get("email") if isinstance(customer, dict) else "") or ""
    email = email.strip().lower()
    if not email:
        await _record_event(db, key, "charge.success", "ignored: no email on renewal", _summary(tx))
        await db.commit()
        return {"outcome": "ignored", "reason": "no email"}

    sub = await db.scalar(
        select(Subscription).where(
            Subscription.customer_email == email,
            Subscription.status.in_(["active", "cancelled"]),
        ).order_by(Subscription.id.desc())
    )
    if not sub:
        await _record_event(db, key, "charge.success", "ignored: no subscription for renewal", _summary(tx))
        await db.commit()
        return {"outcome": "ignored", "reason": "no subscription"}

    if not (await _record_event(db, key, "charge.success", "applied renewal", _summary(tx))):
        await db.rollback()
        return {"outcome": "duplicate"}

    base = max(now, sub.current_period_end or now)
    sub.current_period_end = add_period(base, sub.plan)
    sub.last_payment_at = now
    if sub.status != "cancelled":
        sub.status = "active"
    await db.commit()
    return {"outcome": "applied_renewal", "email": email, "period_end": sub.current_period_end.isoformat()}


async def apply_subscription_created(db: AsyncSession, *, code: str, email_token: str, email: str, event_id: str = "") -> dict:
    """Paystack's subscription.create webhook: remember the code and token so that cancelling can really stop the billing."""
    email = (email or "").strip().lower()
    sub = (await db.scalar(select(Subscription).where(
        Subscription.customer_email == email, Subscription.status.in_(["active", "cancelled"]),
    ).order_by(Subscription.id.desc()))) if email else None
    if not code or not sub:
        return {"outcome": "ignored", "reason": "no matching subscription yet"}   # not recorded: a retry may find it
    sub.provider_subscription_id, sub.email_token = str(code), (email_token or None)
    await db.commit()
    return {"outcome": "applied_subscription_created", "email": email}


async def apply_subscription_cancelled(
    db: AsyncSession,
    *,
    sub_id=None,
    email: str | None = None,
    plan_id=None,
    event_id: str | None = None,
) -> dict:
    now = datetime.utcnow()
    key = f"sub_disable:{event_id or sub_id or email}"
    live = list((await db.scalars(select(Subscription).where(Subscription.status == "active"))))
    sid = str(sub_id) if sub_id is not None else None
    em = (email or "").strip().lower() or None

    sub = next((s for s in live if sid and s.provider_subscription_id == sid), None)
    if not sub and em:
        sub = next(
            (s for s in live if s.customer_email.lower() == em
             and (plan_id is None or s.provider_plan_id == str(plan_id))),
            None,
        )
    if not sub:
        await _record_event(db, key, "subscription.disable", "ignored: no matching live subscription",
                      {"id": sid, "email": em})
        await db.commit()
        return {"outcome": "ignored", "reason": "no matching live subscription"}

    if not (await _record_event(db, key, "subscription.disable", "applied", {"id": sid, "email": em})):
        await db.rollback()
        return {"outcome": "duplicate"}

    sub.status = "cancelled"
    sub.cancelled_at = now
    await db.commit()
    return {"outcome": "applied_cancellation", "email": sub.customer_email}


async def _try_disable_subscription(ps: PaystackClient, sub: Subscription) -> None:
    if not sub.provider_subscription_id:
        return
    try:
        # Paystack needs the subscription code and the email token (both arrive in the subscription.create webhook)
        token = sub.email_token or ""
        if token:
            await ps.disable_subscription(sub.provider_subscription_id, token)
    except PaystackError:
        pass


async def refund_member(db: AsyncSession, email: str, now: datetime | None = None) -> int:
    """End a refunded customer's paid access right away (the money itself is returned in the Paystack dashboard). Returns plans ended."""
    now = now or datetime.utcnow()
    n = 0
    for sub in (await db.scalars(select(Subscription).where(Subscription.customer_email == email.strip().lower(), Subscription.status.in_(["active", "cancelled"])))):
        sub.status = "refunded"
        sub.current_period_end = min(sub.current_period_end, now) if sub.current_period_end else now
        n += 1
    await db.commit()
    return n


async def cancel_for_email(db: AsyncSession, ps: PaystackClient, email: str, now: datetime | None = None) -> None:
    email = email.strip().lower()
    sub = await db.scalar(
        select(Subscription).where(
            Subscription.customer_email == email,
            Subscription.status == "active",
        ).order_by(Subscription.id.desc())
    )
    if not sub:
        raise BillingError("There is no active subscription to cancel.", 404)
    if sub.interval in ONE_TIME_INTERVALS:
        raise BillingError("A one-time plan does not renew, so there is nothing to cancel: it simply ends when its time is up.", 400)
    await _try_disable_subscription(ps, sub)
    sub.status = "cancelled"
    sub.cancelled_at = now or datetime.utcnow()
    await db.commit()


async def current_subscription(db: AsyncSession, email: str | None = None, user_id: int | None = None) -> Subscription | None:
    q = select(Subscription).where(Subscription.status.in_(["active", "cancelled"]))
    if user_id is not None:
        q = q.where(Subscription.user_id == user_id)
    elif email:
        q = q.where(Subscription.customer_email == email.strip().lower())
    else:
        return None
    # the row that lasts longest wins (a newer, shorter plan must never hide a longer one); newest breaks ties
    return await db.scalar(q.order_by(Subscription.current_period_end.desc(), Subscription.id.desc()))


async def has_active_access(db: AsyncSession, email: str | None = None, user_id: int | None = None,
                      now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    sub = await current_subscription(db, email=email, user_id=user_id)
    if not sub:
        return False
    return sub.current_period_end is not None and sub.current_period_end > now


# Legacy aliases
def cancel_for_org(db, fw, org, now=None):
    raise BillingError("Org billing has been retired. Use cancel_for_email.", 400)


def start_checkout_org(db, fw, org, user, plan, interval):
    raise BillingError("Org billing has been retired. Use start_checkout.", 400)
