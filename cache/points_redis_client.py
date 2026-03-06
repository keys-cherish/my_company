"""Shared points Redis client.

Used to sync personal points with external bots (e.g. dice_bot / clans_bot).
"""

from __future__ import annotations

import redis.asyncio as aioredis

from config import settings

_pool: aioredis.Redis | None = None


def _build_points_redis_url() -> str:
    if settings.points_redis_host.strip():
        auth = f":{settings.points_redis_password}@" if settings.points_redis_password else ""
        return (
            f"redis://{auth}{settings.points_redis_host}:{settings.points_redis_port}/"
            f"{settings.points_redis_db}"
        )
    return settings.redis_url


async def get_points_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(_build_points_redis_url(), decode_responses=True)
    return _pool


async def close_points_redis():
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None

