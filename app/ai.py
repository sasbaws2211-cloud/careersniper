"""One door to a language model, whichever provider sits behind it.

Providers (AI_PROVIDER):
  anthropic          Anthropic Messages API.
  openai             OpenAI Chat Completions.
  openai_compatible  Any server that speaks the OpenAI chat-completions protocol at AI_BASE_URL:
                     Ollama, LM Studio, vLLM, llama.cpp, OpenRouter, Groq, Together, Azure-style gateways, a company proxy ...

Plain httpx, no vendor SDKs, so a provider is a base URL and a key. Everything the rest of the app needs is `run()`:
it checks access and the member's daily credits, limits how many calls run at once, calls the model, and records usage
only for a call that succeeded. Failures are AIError with a message that is safe to show a member; details go to the log,
never the API key or the provider's raw response.
"""
from __future__ import annotations

import json
import logging
import re
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import billing, config, runtime_settings
from .models import AiUsage, Member

log = logging.getLogger("careersniper.ai")

PROVIDERS = ("anthropic", "openai", "openai_compatible")
ANTHROPIC_URL = "https://api.anthropic.com"
OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
ANTHROPIC_VERSION = "2023-06-01"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}

_transport = None                      # tests inject an httpx.MockTransport here
_slots: asyncio.Semaphore | None = None
_slots_loop = None
_slots_size = 0


class AIError(Exception):
    """A failure with a message that is safe to show a member."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Settings:
    provider: str
    base_url: str
    api_key: str
    model: str
    timeout: float
    max_output_tokens: int
    json_mode: bool
    token_param: str
    extra_body: dict | None = None      # merged into OpenAI-style request bodies (provider-specific switches)

    @property
    def label(self) -> str:
        return f"{self.provider} / {self.model}"


@dataclass(frozen=True)
class Result:
    text: str
    input_tokens: int
    output_tokens: int
    model: str


# ---------------------------------------------------------------- configuration
def _text(ov: dict, key: str, env_value: str) -> str:
    """A setting an admin stored in the database, else the environment's."""
    v = ov.get(key)
    return v if isinstance(v, str) and v.strip() else (env_value or "")


def _number(ov: dict, key: str, env_value):
    v = ov.get(key)
    return env_value if v is None else v


async def overrides(extra: dict | None = None) -> dict:
    """Settings an admin stored in the database (see runtime_settings), plus `extra` for a test of values not saved yet."""
    return {**(await runtime_settings.values()), **(extra or {})}


async def resolve(extra: dict | None = None) -> tuple[Settings | None, str]:
    """(settings, '') when AI is usable, else (None, why not) in words an admin can act on."""
    return resolve_with(await overrides(extra))


def enabled_cached() -> bool:
    """AI on or off from the last stored-settings read, without touching the database (for template context, which cannot await).
    The request middleware keeps that read fresh."""
    return resolve_with(runtime_settings.cached_values())[0] is not None


def _extra_body(ov: dict) -> dict | None:
    """AI_EXTRA_BODY (a JSON object) or None; anything that is not a JSON object is ignored."""
    raw = _text(ov, "ai_extra_body", config.AI_EXTRA_BODY).strip()
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        log.warning("AI_EXTRA_BODY is not valid JSON; ignored")
        return None
    return v if isinstance(v, dict) else None


def resolve_with(ov: dict) -> tuple[Settings | None, str]:
    if ov.get("ai_enabled") is False:
        return None, "AI is switched off by an admin."
    provider = _text(ov, "ai_provider", config.AI_PROVIDER).strip().lower().replace("-", "_")
    if provider in ("custom", "compatible"):
        provider = "openai_compatible"
    base = _text(ov, "ai_base_url", config.AI_BASE_URL).strip().rstrip("/")
    key = _text(ov, "ai_api_key", config.AI_API_KEY).strip()
    if not provider:       # guess from what is set: an explicit key for a vendor wins, then a custom endpoint
        if config.ANTHROPIC_API_KEY:
            provider = "anthropic"
        elif config.OPENAI_API_KEY and not base:
            provider = "openai"
        elif base:
            provider = "openai_compatible"
        else:
            return None, "No AI provider is set up. Choose a provider and add its key (or, for your own server, its address and model)."
    if provider not in PROVIDERS:
        return None, f"“{provider}” is not a provider this platform supports."
    if provider == "anthropic":
        key = key or config.ANTHROPIC_API_KEY
        base = base or ANTHROPIC_URL
        model = _text(ov, "ai_model", config.AI_MODEL) or DEFAULT_ANTHROPIC_MODEL
        if not key:
            return None, "Anthropic needs an API key."
    elif provider == "openai":
        key = key or config.OPENAI_API_KEY
        base = base or OPENAI_URL
        model = _text(ov, "ai_model", config.AI_MODEL)
        if not key:
            return None, "OpenAI needs an API key."
        if not model:
            return None, "OpenAI needs a model name, for example gpt-4.1-mini."
    else:
        key = key or config.OPENAI_API_KEY      # local servers usually need none
        model = _text(ov, "ai_model", config.AI_MODEL)
        if not base:
            return None, "A custom (OpenAI-compatible) server needs its base address, for example http://localhost:11434/v1."
        if not model:
            return None, "A custom (OpenAI-compatible) server needs a model name."
    if urlparse(base).scheme not in ("http", "https") or not urlparse(base).hostname:
        return None, "The base address must start with http:// or https://."
    # OpenAI's newer models reject max_tokens; most other servers only know it
    token_param = _text(ov, "ai_token_param", config.AI_TOKEN_PARAM) or ("max_completion_tokens" if provider == "openai" else "max_tokens")
    return Settings(provider, base, key, model, float(_number(ov, "ai_timeout", config.AI_TIMEOUT)), int(_number(ov, "ai_max_output_tokens", config.AI_MAX_OUTPUT_TOKENS)),
                    bool(_number(ov, "ai_json_mode", config.AI_JSON_MODE)), token_param, _extra_body(ov)), ""


async def settings(extra: dict | None = None) -> Settings | None:
    """The resolved configuration, or None when AI is switched off or no usable provider is set up.
    Read on every call so a changed setting applies; a value stored by an admin beats the environment variable."""
    return (await resolve(extra))[0]


async def enabled() -> bool:
    return (await settings()) is not None


async def config_problems() -> list[str]:
    """Human-readable warnings for `doctor`: why AI is off or risky. Empty when fine or deliberately unset."""
    stored = await runtime_settings.values()
    explicit = bool((config.AI_PROVIDER or config.AI_BASE_URL or config.AI_API_KEY or config.ANTHROPIC_API_KEY or config.OPENAI_API_KEY
                     or any(k.startswith("ai_") for k in stored)))
    s = await settings()
    if not explicit:
        return []
    if not s:
        return ["AI is partly configured but not usable: a provider needs a key (Anthropic, OpenAI) or a base URL and model (OpenAI-compatible); "
                "OpenAI and compatible servers also need AI_MODEL."]
    out = []
    host = urlparse(s.base_url).hostname or ""
    if urlparse(s.base_url).scheme == "http" and host not in LOCAL_HOSTS:
        out.append(f"AI_BASE_URL is plain http to {host}: the API key and every resume would cross the network unencrypted.")
    return out


# ---------------------------------------------------------------- calling the model
async def _slot() -> asyncio.Semaphore:
    """The semaphore capping model calls in flight. One per event loop and size (a semaphore belongs to the loop that first waits on it)."""
    global _slots, _slots_size, _slots_loop
    size = max(1, int(_number((await overrides()), "ai_max_concurrency", config.AI_MAX_CONCURRENCY)))
    loop = asyncio.get_running_loop()
    if _slots is None or _slots_size != size or _slots_loop is not loop:
        _slots, _slots_size, _slots_loop = asyncio.Semaphore(size), size, loop
    return _slots


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):           # content parts: [{"type": "text", "text": "..."}]
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _estimate(text: str) -> int:
    return max(1, len(text) // 4)


def _fail(s: Settings, r: httpx.Response) -> AIError:
    """Turn an HTTP error into a safe message; the provider's body goes to the log only."""
    log.warning("AI call failed provider=%s model=%s status=%s body=%s", s.provider, s.model, r.status_code, r.text[:300].replace("\n", " "))
    if r.status_code in (401, 403):
        return AIError("The AI service is not set up correctly. Please try again later.", 503)
    if r.status_code == 429:
        return AIError("The AI service is busy right now. Please try again in a minute.", 503)
    if r.status_code in (400, 413, 422):
        return AIError("That text was too long or could not be processed. Try with less text.", 400)
    return AIError("The AI service is not responding right now. Please try again in a moment.", 502)


async def complete(system: str, user: str, max_tokens: int = 1200, want_json: bool = False, using: Settings | None = None,
                   timeout: float | None = None) -> Result:
    """One model call, no accounting. Raises AIError. `using` runs against settings other than the live ones (the admin's "test" button)."""
    s = using or (await settings())
    if not s:
        raise AIError("AI tools are not available right now.", 503)
    max_tokens = max(16, min(int(max_tokens), s.max_output_tokens))
    if s.provider == "anthropic":
        url = s.base_url + "/v1/messages"
        headers = {"x-api-key": s.api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        body = {"model": s.model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
    else:
        url = s.base_url + "/chat/completions"
        headers = {"content-type": "application/json"}
        if s.api_key:
            headers["authorization"] = f"Bearer {s.api_key}"
        body = {"model": s.model, s.token_param: max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if want_json and s.json_mode:
            body["response_format"] = {"type": "json_object"}
        if s.extra_body:
            body.update(s.extra_body)
    started = time.monotonic()
    async with (await _slot()):
        try:
            async with httpx.AsyncClient(timeout=max(s.timeout, timeout or 0), transport=_transport) as client:
                r = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException:
            log.warning("AI call timed out provider=%s model=%s", s.provider, s.model)
            raise AIError("The AI service took too long to answer. Please try again.", 504)
        except httpx.HTTPError as e:
            log.warning("AI call could not connect provider=%s: %s", s.provider, type(e).__name__)
            raise AIError("The AI service is not responding right now. Please try again in a moment.", 502)
    if r.status_code >= 400:
        raise _fail(s, r)
    try:
        data = r.json()
        if s.provider == "anthropic":
            text = _text_of(data.get("content"))
            usage = data.get("usage") or {}
            tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        else:
            text = _text_of((data["choices"][0].get("message") or {}).get("content"))
            usage = data.get("usage") or {}
            tin, tout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        log.warning("AI reply could not be read provider=%s model=%s body=%s", s.provider, s.model, r.text[:300].replace("\n", " "))
        raise AIError("The AI service sent an answer we could not read. Please try again.", 502)
    text = text.strip()
    if not text:
        raise AIError("The AI service sent back an empty answer. Please try again.", 502)
    log.info("AI call ok provider=%s model=%s ms=%d", s.provider, s.model, int((time.monotonic() - started) * 1000))
    return Result(text, tin or _estimate(system + user), tout or _estimate(text), data.get("model") or s.model)


def extract_json(text: str):
    """The first JSON object or array in a model reply, tolerating code fences and chatter around it. AIError if there is none."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.I)
    decoder = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(t[i:])
                return value
            except ValueError:
                continue
    raise AIError("The AI service did not return the structured answer we asked for. Please try again.", 502)


# ---------------------------------------------------------------- who may use it, and how much
async def daily_limit(db: AsyncSession, member: Member) -> int:
    """AI requests per day this member may make: set by their plan; 0 without a paid plan (the free requests aside)."""
    sub = await billing.current_subscription(db, email=member.email)
    if sub and sub.current_period_end and sub.current_period_end > datetime.utcnow():
        return await plan_daily(sub.plan)
    if config.free_access():                                   # development with billing off behaves like a monthly plan
        return await plan_daily("monthly")
    return 0


async def plan_daily(plan: str) -> int:
    """AI requests a day on this plan: what an admin set, else the plan's default in config. Never below the 100 every paid plan starts at."""
    stored = await runtime_settings.get(f"ai_requests_{plan}")
    value = int(stored if stored is not None else (config.CONSUMER_PLANS.get(plan, {}).get("ai_requests_daily") or 0))
    return max(config.MIN_PAID_DAILY_REQUESTS, value)


async def free_trials() -> int:
    return int(_number((await overrides()), "ai_free_requests", config.AI_FREE_REQUESTS))


FREE_PREFIX = "free:"      # calls made on the member's behalf at no charge (reading a résumé's profile); logged, but outside the budget


async def used_today(db: AsyncSession, member: Member, now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((await db.scalar(select(func.count()).select_from(AiUsage).where(AiUsage.member_id == member.id, AiUsage.created_at >= start, ~AiUsage.tool.startswith(FREE_PREFIX)))) or 0)


async def used_ever(db: AsyncSession, member: Member) -> int:
    return int((await db.scalar(select(func.count()).select_from(AiUsage).where(AiUsage.member_id == member.id, ~AiUsage.tool.startswith(FREE_PREFIX)))) or 0)


async def credits(db: AsyncSession, member: Member, now: datetime | None = None) -> dict:
    """{'limit', 'used', 'left', 'trial'}, all in requests (one per call to the model), for the tool pages and for run().
    A call is allowed while any requests are left."""
    limit = await daily_limit(db, member)
    if limit:
        used = await used_today(db, member, now)
        return {"limit": limit, "used": used, "left": max(0, limit - used), "trial": False}
    trials = await free_trials()
    used = await used_ever(db, member)
    return {"limit": trials, "used": min(used, trials), "left": max(0, trials - used), "trial": True}


async def run(db: AsyncSession, member: Member, tool: str, system: str, user: str, max_tokens: int = 1200, want_json: bool = False,
              charge: bool = True, timeout: float | None = None) -> Result:
    """The only entry point tools use. Checks the member's credits, calls the model, records usage if it worked."""
    if not (await enabled()):
        raise AIError("AI tools are not available right now.", 503)
    if not getattr(member, "verified", True):
        raise AIError("Confirm your email to use the AI tools. We sent you a link; use the button at the top of the page to resend it.", 403)
    c = await credits(db, member) if charge else {"left": 1, "trial": False, "limit": 0}
    if c["left"] <= 0:
        if c["trial"]:
            raise AIError("AI tools are part of a paid plan. Subscribe to unlock résumé help, job matching and interview practice.", 402)
        raise AIError(f"You have used today's {c['limit']:,} AI requests. They reset at midnight UTC.", 429)
    result = await complete(system, user, max_tokens=max_tokens, want_json=want_json, timeout=timeout)
    db.add(AiUsage(member_id=member.id, tool=(tool if charge else FREE_PREFIX + tool)[:30], model=result.model[:60],
                   input_tokens=result.input_tokens, output_tokens=result.output_tokens))
    await db.commit()
    return result


async def usage_report(db: AsyncSession) -> list[dict]:
    """Per tool: how many calls and the average and total tokens, so the real cost per call can be worked out."""
    q = (select(AiUsage.tool, func.count(), func.avg(AiUsage.input_tokens), func.avg(AiUsage.output_tokens),
                func.sum(AiUsage.input_tokens), func.sum(AiUsage.output_tokens)).group_by(AiUsage.tool).order_by(AiUsage.tool))
    return [{"tool": t, "calls": n, "avg_in": round(ai_ or 0), "avg_out": round(ao or 0), "total_in": int(ti or 0), "total_out": int(to or 0)}
            for t, n, ai_, ao, ti, to in (await db.execute(q))]


async def check(using: Settings | None = None) -> str:
    """For `python -m app.cli ai-check` and the admin's test button: one tiny real call, so you learn about a bad key or model name
    before a customer does. `using` tests settings that are not saved yet."""
    s = using or (await settings())
    if not s:
        return "AI is not configured. Set AI_PROVIDER plus a key (anthropic, openai) or AI_BASE_URL and AI_MODEL (openai_compatible)."
    started = time.monotonic()
    r = await complete("Reply with the single word OK.", "Ping", max_tokens=16, using=s)
    host = urlparse(s.base_url).netloc
    return f"OK  {s.label} at {host}  answered {r.text[:40]!r} in {int((time.monotonic() - started) * 1000)} ms ({r.input_tokens} in / {r.output_tokens} out tokens)"
