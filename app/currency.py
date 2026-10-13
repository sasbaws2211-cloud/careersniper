"""Show prices in the visitor's own currency, as an estimate.

Nothing here changes what is charged: every payment is taken in Ghana cedis (GHS) through Paystack at the rate in config.USD_TO_GHS.
The other currencies are shown so that "$7 a week" means something in Lagos, Nairobi or Johannesburg. The rates are fixed numbers set in
the environment (USD_TO_NGN, USD_TO_KES, USD_TO_ZAR), not live exchange rates: update them from time to time, and the page always says "about".

Which currency a visitor sees, in order: the one they picked (cookie `cur`), the country their host reports (header CF-IPCountry,
set by Cloudflare), the region in their browser language (en-NG), otherwise none.
"""
from __future__ import annotations

import re

from . import config

CURRENCIES = {
    "USD": {"symbol": "$", "name": "US dollar", "step": 1, "decimals": 0},
    "GHS": {"symbol": "GH₵", "name": "Ghana cedi", "step": 0.5, "decimals": 2},
    "NGN": {"symbol": "₦", "name": "Nigerian naira", "step": 100, "decimals": 0},
    "KES": {"symbol": "KSh ", "name": "Kenyan shilling", "step": 10, "decimals": 0},
    "ZAR": {"symbol": "R", "name": "South African rand", "step": 1, "decimals": 0},
}
BY_COUNTRY = {"GH": "GHS", "NG": "NGN", "KE": "KES", "ZA": "ZAR", "US": "USD"}
COOKIE = "cur"
_REGION = re.compile(r"[a-z]{2,3}-([A-Za-z]{2})\b")


def rate(code: str) -> float:
    return {"USD": 1.0, "GHS": config.USD_TO_GHS, "NGN": config.USD_TO_NGN, "KES": config.USD_TO_KES, "ZAR": config.USD_TO_ZAR}[code]


def valid(code: str | None) -> str:
    code = (code or "").strip().upper()
    return code if code in CURRENCIES else ""


def from_country(country: str | None) -> str:
    return BY_COUNTRY.get((country or "").strip().upper(), "")


def from_language(header: str | None) -> str:
    """The first region in an Accept-Language header that we have a currency for: 'en-GB,en-NG;q=0.8' -> NGN."""
    for part in (header or "")[:200].split(","):
        m = _REGION.match(part.strip().split(";")[0].strip())
        if m and from_country(m.group(1)):
            return from_country(m.group(1))
    return ""


def pick(cookie: str | None, country: str | None, language: str | None) -> str:
    return valid(cookie) or from_country(country) or from_language(language)


def amount(usd: float, code: str) -> str:
    """'₦4,500' for 3 dollars at 1,500 a dollar: rounded to a figure a shop would use, not to the last kobo."""
    spec = CURRENCIES[code]
    value = usd * rate(code)
    step = spec["step"]
    value = round(value / step) * step if step else value
    return f"{spec['symbol']}{value:,.{spec['decimals']}f}"


def approx(usd: float, code: str) -> str:
    """'about ₦4,500', or '' when there is nothing to add (no choice made, or dollars and cedis are already on the page)."""
    return f"about {amount(usd, code)}" if code in CURRENCIES and code not in ("USD", "GHS") else ""


def for_request(request) -> str:
    chosen = getattr(request.state, "cur", "")           # a choice made on this very request (?cur=NGN), before its cookie exists
    return chosen or pick(request.cookies.get(COOKIE), request.headers.get(config.COUNTRY_HEADER), request.headers.get("accept-language"))
