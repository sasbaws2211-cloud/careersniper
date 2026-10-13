"""Outbound transactional email via Resend (https://resend.com).

When RESEND_API_KEY is unset, messages are logged and stored in OUTBOX (dev/tests).
"""
from __future__ import annotations

import logging
from html import escape

import re

import httpx

from . import config

log = logging.getLogger("careersniper.mail")
OUTBOX: list[dict] = []
_OUTBOX_MAX = 200
RESEND_URL = "https://api.resend.com/emails"


class MailError(Exception):
    pass


def clean_header(value: str, limit: int = 200) -> str:
    """One line, no control characters — safe for email headers."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in (value or "")).split())[:limit]


def configured() -> bool:
    return bool(getattr(config, "RESEND_API_KEY", "") or "")


def wrap_html(body: str, brand_color: str = "#2563eb", footer: str = "") -> str:
    """Minimal responsive HTML shell for transactional mail."""
    name = escape(getattr(config, "PLATFORM_NAME", "CareerSniper") or "CareerSniper")
    color = escape(brand_color or "#2563eb")
    foot = footer or f"You're receiving this because of your account on {name}."
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#111">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f4f6;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%" style="max-width:560px;background:#ffffff;border-radius:12px;border:1px solid #e5e7eb;overflow:hidden">
<tr><td style="padding:16px 24px;background:{color};color:#fff;font-weight:700;font-size:16px">{name}</td></tr>
<tr><td style="padding:24px;font-size:15px;line-height:1.55">{body}</td></tr>
<tr><td style="padding:16px 24px;font-size:12px;color:#6b7280;border-top:1px solid #e5e7eb">{escape(foot)}</td></tr>
</table>
</td></tr></table>
</body></html>"""


def button(url: str, label: str, brand_color: str = "#2563eb") -> str:
    u, lab, color = escape(url), escape(label), escape(brand_color or "#2563eb")
    return (
        f'<p style="margin:20px 0">'
        f'<a href="{u}" style="background:{color};color:#fff;padding:12px 20px;border-radius:8px;'
        f'text-decoration:none;font-weight:600;display:inline-block">{lab}</a></p>'
        f'<p style="font-size:12px;color:#6b7280">Or copy this link:<br><a href="{u}" style="color:{color}">{u}</a></p>'
    )


def _from_header(from_name: str = "") -> str:
    name = clean_header(from_name or config.PLATFORM_NAME, 100)
    addr = clean_header(config.MAIL_FROM, 320)
    inner = re.search(r"<([^<>]+)>", addr)             # MAIL_FROM may be written as "Name <addr@domain>"; use just the address
    if inner:
        addr = inner.group(1).strip()
    return f"{name} <{addr}>" if name else addr


async def send(
    to: str,
    subject: str,
    text: str,
    html: str,
    from_name: str = "",
    headers: dict | None = None,
    reply_to: str = "",
) -> None:
    """Send one message via Resend. Raises MailError on failure so callers can retry."""
    to = clean_header(to, 320)
    subject = clean_header(subject, 250)
    reply_to = clean_header(reply_to, 320)
    api_key = getattr(config, "RESEND_API_KEY", "") or ""

    if not api_key:
        if getattr(config, "IS_PROD", False) and not getattr(config, "ALLOW_CONSOLE_MAIL", False):
            raise MailError("RESEND_API_KEY is not configured")
        OUTBOX.append({
            "to": to, "subject": subject, "text": text, "html": html,
            "headers": headers or {},
        })
        del OUTBOX[:-_OUTBOX_MAX]
        log.info("dev mail (no RESEND_API_KEY) to=%s subject=%s", to, subject)
        print(f"--- DEV MAIL (no RESEND_API_KEY) to={to} subject={subject}\n{text}\n---", flush=True)
        return

    payload: dict = {
        "from": _from_header(from_name),
        "to": [to],
        "subject": subject,
        "text": text,
        "html": html,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    # Optional custom headers (Resend supports a limited set)
    if headers:
        payload["headers"] = {
            clean_header(k, 60): clean_header(v, 500) for k, v in headers.items()
        }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                RESEND_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code >= 400:
            detail = r.text[:300]
            try:
                detail = r.json().get("message") or detail
            except Exception:
                pass
            raise MailError(f"Resend HTTP {r.status_code}: {detail}")
        log.info("sent mail via Resend to=%s subject=%s id=%s", to, subject, (r.json() or {}).get("id"))
    except httpx.HTTPError as e:
        log.warning("Resend request failed to=%s: %s", to, e)
        raise MailError(str(e)) from e


async def send_simple(to: str, subject: str, body_text: str, body_html: str = "", brand_color: str = "#2563eb") -> None:
    """Convenience: wrap HTML body in branded shell."""
    html = wrap_html(body_html or f"<p>{escape(body_text).replace(chr(10), '<br>')}</p>", brand_color=brand_color)
    await send(to, subject, body_text, html)


async def test_connection() -> str:
    """Check that Resend is configured (API key present). Does not send mail."""
    if not configured():
        return "RESEND_API_KEY not set (dev OUTBOX mode)"
    # Lightweight auth check: list domains requires key; use a dry call by validating key format
    key = config.RESEND_API_KEY
    if not key.startswith("re_"):
        return "WARN RESEND_API_KEY does not look like a Resend key (expected re_...)"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.resend.com/domains",
                headers={"Authorization": f"Bearer {key}"},
            )
        if r.status_code == 401:
            raise MailError("Resend API key rejected (401)")
        if r.status_code >= 400:
            raise MailError(f"Resend API error HTTP {r.status_code}: {r.text[:200]}")
        return "OK Resend API key accepted"
    except httpx.HTTPError as e:
        raise MailError(str(e)) from e
