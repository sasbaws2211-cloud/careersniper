"""Settings for the admin app. Its own secret and session rules: a stolen public-site cookie is useless here, and the other way round."""
import os
import secrets

ENV = os.getenv("ADMIN_ENV", "development")
IS_PROD = ENV.lower() == "production"
SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "")
SESSION_HOURS = float(os.getenv("ADMIN_SESSION_HOURS", "8"))          # a login never lasts longer than this
IDLE_MINUTES = float(os.getenv("ADMIN_IDLE_MINUTES", "120"))          # ...or this long without a click
HOST = os.getenv("ADMIN_HOST", "127.0.0.1")                           # loopback by default: put it behind a VPN or an access proxy, not the open internet
PORT = int(os.getenv("ADMIN_PORT", "8030"))
# Extra host names a form post may come from, comma separated: needed only behind a proxy that rewrites the Host header.
ALLOWED_HOSTS = {h.strip() for h in os.getenv("ADMIN_ALLOWED_HOSTS", "").split(",") if h.strip()}
MIN_PASSWORD = 12
LOGIN_FAILS_PER_ACCOUNT = 5
LOGIN_FAILS_PER_IP = 20
LOGIN_WINDOW = 15 * 60

if not SECRET_KEY and not IS_PROD:
    SECRET_KEY = secrets.token_urlsafe(48)        # development: a fresh key each start, so sessions end when the server restarts


def problems() -> list[str]:
    out = []
    if IS_PROD and len(SECRET_KEY) < 32:
        out.append("ADMIN_SECRET_KEY must be set to a long random value (32+ characters) when ADMIN_ENV=production.")
    if IS_PROD and HOST not in ("127.0.0.1", "::1", "localhost"):
        out.append(f"ADMIN_HOST is {HOST}: the admin app is listening on a public address. Keep it on 127.0.0.1 behind a VPN or access proxy "
                   "unless that is on purpose.")
    return out
