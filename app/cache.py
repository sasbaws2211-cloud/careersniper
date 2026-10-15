"""Optional Redis-backed cache for public, read-only data."""
import json
import logging
import time
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from . import config

_logger = logging.getLogger("careersniper.cache")
_client: Redis | None = None
_unavailable_until = 0.0


def _redis() -> Redis | None:
    global _client
    if not config.REDIS_URL or time.monotonic() < _unavailable_until:
        return None
    if _client is None:
        _client = Redis.from_url(
            config.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=0.25,
            socket_timeout=0.25,
            health_check_interval=30,
        )
    return _client


def _mark_available() -> None:
    global _unavailable_until
    _unavailable_until = 0.0


def _mark_unavailable() -> None:
    global _unavailable_until
    _unavailable_until = time.monotonic() + 5


async def get_json(key: str) -> Any | None:
    client = _redis()
    if client is None:
        return None
    try:
        value = await client.get(key)
    except RedisError:
        _mark_unavailable()
        _logger.warning("Redis cache read failed for key %s", key, exc_info=True)
        return None
    _mark_available()
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        _logger.warning("Ignoring invalid JSON in Redis cache key %s", key)
        return None


async def set_json(key: str, value: Any, ttl: int) -> None:
    client = _redis()
    if client is None:
        return
    try:
        await client.set(key, json.dumps(value, separators=(",", ":")), ex=ttl)
    except RedisError:
        _mark_unavailable()
        _logger.warning("Redis cache write failed for key %s", key, exc_info=True)
    except (TypeError, ValueError):
        _logger.warning("Redis cache write failed for key %s", key, exc_info=True)


async def close() -> None:
    global _client
    if _client is None:
        return
    client, _client = _client, None
    try:
        await client.aclose()
    except RedisError:
        _logger.warning("Could not close Redis cache connection", exc_info=True)
