"""Legacy employer Flutterwave payments — purged for B2C.

Consumer billing lives in app.billing (Paystack). This module is a stub so
old imports and scheduler hooks do not break.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


def is_ours(reference: str | None) -> bool:
    """Former employer payment refs. Always false — consumer uses billing.py."""
    return False


def apply_verified_payment(db: AsyncSession, client, tx: dict) -> dict:
    return {"outcome": "ignored_legacy_employer"}


def expire_credits(db: AsyncSession, now=None) -> int:
    return 0


def warn_expiring_credits(db: AsyncSession, now=None, days: int = 0) -> int:
    return 0


def notify_plan_renewals(db: AsyncSession, now=None, days: int = 0) -> int:
    return 0
