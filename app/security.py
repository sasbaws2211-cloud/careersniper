"""Security helpers: rate limiting, custom-domain validation and ownership verification, response headers.

Rate limits are per process (fine for the supported single-instance deployment; see PRODUCTION.md before scaling out).
"""
import ipaddress
import re
import socket
import threading
import time
from collections import defaultdict, deque

import httpx

from . import config

# ------------------------------------------------------------------ rate limiting
class RateLimiter:
    """Sliding-window counters keyed by an arbitrary string. Thread-safe; old keys are pruned so memory stays bounded."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, q: deque, window: float, now: float) -> None:
        while q and now - q[0] > window:
            q.popleft()

    def blocked(self, key: str, limit: int, window: float, now: float | None = None) -> bool:
        """Is this key already at its limit? (Does not record anything.)"""
        now = now or time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return False
            self._trim(q, window, now)
            return len(q) >= limit

    def record(self, key: str, now: float | None = None) -> None:
        now = now or time.monotonic()
        with self._lock:
            self._hits[key].append(now)
            if len(self._hits) > 50_000:   # crude bound against key-flooding: drop everything idle
                for k in [k for k, q in self._hits.items() if not q or now - q[-1] > 3600]:
                    del self._hits[k]

    def hit(self, key: str, limit: int, window: float, now: float | None = None) -> bool:
        """Record an attempt; False when it exceeds the limit (the attempt is still counted)."""
        over = self.blocked(key, limit, window, now)
        self.record(key, now)
        return not over

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request) -> str:
    """The caller's address. Behind a proxy, run uvicorn with --proxy-headers so this is the real client, not the proxy."""
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------------ custom domains
class DomainError(ValueError):
    pass


_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(rf"^(?:{_LABEL}\.)+[a-z]{{2,63}}$")   # needs a dot and an alphabetic TLD, so IP addresses never match
_BAD_TLDS = {"localhost", "local", "internal", "invalid", "test", "onion", "lan", "home", "corp"}
WELL_KNOWN = "/.well-known/careersniper-verify.txt"


def clean_domain(raw: str, base_domain: str | None = None) -> str:
    """Normalise a customer's domain or raise DomainError. A custom domain must never be (or sit under) the platform's own."""
    base = (base_domain or config.BASE_DOMAIN).lower()
    d = (raw or "").strip().lower().rstrip(".")
    if not d or len(d) > 253 or not _HOST_RE.fullmatch(d):
        raise DomainError("Enter a domain like jobs.yourcollege.edu (no https:// and no path).")
    if d == base or d.endswith("." + base):
        raise DomainError("That domain belongs to the platform. Use a domain you own.")
    if d.rsplit(".", 1)[-1] in _BAD_TLDS:
        raise DomainError("That kind of domain can't be used. Use a public domain you own.")
    return d


def _resolve(host: str) -> list[str]:
    try:
        return sorted({i[4][0] for i in socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)})
    except OSError:
        return []


def _fetch(ip: str, host: str, path: str) -> str:
    """GET http://<ip><path> with the customer's Host header. By IP, so DNS cannot change between our check and the request."""
    url = f"http://[{ip}]{path}" if ":" in ip else f"http://{ip}{path}"
    r = httpx.get(url, headers={"Host": host}, timeout=6, follow_redirects=False)
    return r.text if r.status_code == 200 else ""


def verify_domain(domain: str, token: str, resolver=None, fetch=None) -> tuple[bool, str]:
    """Prove the customer controls `domain`: it must resolve to public addresses (never loopback, private or link-local,
    so this check can't be aimed at our internal network) and serve our token at WELL_KNOWN. Only whoever controls the
    domain's DNS can make it reach this platform, so squatting someone else's domain can never verify."""
    ips = (resolver or _resolve)(domain)
    if not ips:
        return False, f"{domain} doesn't point anywhere yet. Add the DNS record shown below, wait a few minutes, then try again."
    for ip in ips:
        try:
            if not ipaddress.ip_address(ip).is_global:
                raise ValueError
        except ValueError:
            return False, f"{domain} resolves to a non-public address. It must point to this platform's public address."
    try:
        body = (fetch or _fetch)(ips[0], domain, WELL_KNOWN)
    except Exception:
        return False, f"We couldn't reach {domain}. Check the DNS record points here and port 80 is open, then try again."
    if body.strip() != token:
        return False, f"We reached {domain} but it didn't return our verification code. Check the DNS record points at this platform."
    return True, ""


# ------------------------------------------------------------------ response headers
def security_headers(https: bool) -> dict[str, str]:
    """Headers for every response. The app uses inline <script>/<style>, so CSP can't ban them, but it still blocks
    framing (clickjacking), <base> and form-action hijacks, plugins and mixed content."""
    h = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Content-Security-Policy": ("default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; "
                                    "script-src 'self' 'unsafe-inline' https://js.paystack.co; connect-src 'self' https://api.paystack.co https://checkout.paystack.com; "
                                    "frame-src https://checkout.paystack.com https://js.paystack.co; frame-ancestors 'none'; "
                                    "base-uri 'self'; form-action 'self'; object-src 'none'"),
    }
    if https:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return h
