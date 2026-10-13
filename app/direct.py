"""Legacy direct-employer posting — purged for B2C.

Kept as a thin stub so scheduler and admin context imports still resolve.
Employer portal and paid direct-posting are not part of the product.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

# Logo limits (legacy)
LOGO_MAX_BYTES = 512 * 1024
LOGO_MAX_PX = 2048
LOGO_MIN_PX = 64


class DirectError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def pending_count(db: AsyncSession, org=None) -> int:
    return 0


def feature_requests(db: AsyncSession, org=None) -> list:
    return []


def expire_due(db: AsyncSession, now=None) -> int:
    return 0


def purge_unpaid(db: AsyncSession, now=None, days: int = 0) -> int:
    return 0


def notify_expiring(db: AsyncSession, now=None, days: int = 0) -> int:
    return 0
