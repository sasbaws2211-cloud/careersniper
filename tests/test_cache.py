import asyncio
import time

from redis.exceptions import RedisError

from app import cache


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex):
        self.values[key] = value
        self.ttls[key] = ex

    async def aclose(self):
        pass


def test_redis_cache_json_round_trip(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(cache.config, "REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(cache, "_client", client)
    monkeypatch.setattr(cache, "_unavailable_until", 0.0)

    async def run():
        await cache.set_json("public:test:v1", {"count": 3}, 60)
        assert await cache.get_json("public:test:v1") == {"count": 3}
        assert client.ttls["public:test:v1"] == 60

    asyncio.run(run())


def test_redis_cache_failure_fails_open(monkeypatch):
    class BrokenRedis:
        async def get(self, key):
            raise RedisError("offline")

    monkeypatch.setattr(cache.config, "REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(cache, "_client", BrokenRedis())
    monkeypatch.setattr(cache, "_unavailable_until", 0.0)

    async def run():
        assert await cache.get_json("public:test:v1") is None
        assert cache._unavailable_until > time.monotonic()
        assert cache._redis() is None

    asyncio.run(run())


def test_redis_cache_is_optional(monkeypatch):
    monkeypatch.setattr(cache.config, "REDIS_URL", "")
    monkeypatch.setattr(cache, "_client", None)

    async def run():
        assert await cache.get_json("public:test:v1") is None
        await cache.set_json("public:test:v1", {"count": 3}, 60)

    asyncio.run(run())
