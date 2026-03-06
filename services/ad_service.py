"""Advertising system - spend self_points for temporary revenue boost.

Ad campaigns last N days and provide a percentage revenue bonus.
Active ad boosts are stored in Redis for fast lookup.
"""

from __future__ import annotations

import json

from cache.redis_client import get_redis
from config import settings

# Ad tiers
AD_TIERS = [
    {"key": "basic", "name": "基础推广", "cost": 500, "boost_pct": 0.05, "days": 3, "description": "+5%营收 3天"},
    {"key": "standard", "name": "标准广告", "cost": 1500, "boost_pct": 0.10, "days": 5, "description": "+10%营收 5天"},
    {"key": "premium", "name": "高级营销", "cost": 4000, "boost_pct": 0.20, "days": 7, "description": "+20%营收 7天"},
    {"key": "viral", "name": "病毒营销", "cost": 10000, "boost_pct": 0.35, "days": 10, "description": "+35%营收 10天"},
]


def get_ad_tiers() -> list[dict]:
    return AD_TIERS


async def buy_ad(company_id: int, tier_key: str) -> tuple[bool, str, int]:
    """Purchase an ad campaign. Returns (success, message, cost).

    Stores active ad in Redis with TTL = days * 86400.
    """
    tier = next((t for t in AD_TIERS if t["key"] == tier_key), None)
    if tier is None:
        return False, "无效的广告类型", 0

    r = await get_redis()

    # Check if already has active ad
    existing = await r.get(f"ad:{company_id}")
    if existing:
        return False, "已有进行中的广告活动，请等待结束后再购买", 0

    # Store ad data
    ad_data = json.dumps({"boost_pct": tier["boost_pct"], "tier": tier_key})
    ttl_seconds = tier["days"] * 86400
    await r.setex(f"ad:{company_id}", ttl_seconds, ad_data)

    return True, f"广告「{tier['name']}」投放成功! {tier['description']}", tier["cost"]


async def get_ad_boost(company_id: int) -> float:
    """Get current ad revenue boost percentage for a company. Returns 0 if no active ad."""
    r = await get_redis()
    data = await r.get(f"ad:{company_id}")
    if not data:
        return 0.0
    try:
        ad = json.loads(data)
        return ad.get("boost_pct", 0.0)
    except (json.JSONDecodeError, TypeError):
        return 0.0


async def cancel_ad(company_id: int) -> bool:
    """取消当前广告（用于回滚）。"""
    r = await get_redis()
    return bool(await r.delete(f"ad:{company_id}"))


async def get_active_ad_info(company_id: int) -> dict | None:
    """Return info about active ad campaign, or None."""
    r = await get_redis()
    data = await r.get(f"ad:{company_id}")
    if not data:
        return None
    ttl = await r.ttl(f"ad:{company_id}")
    try:
        ad = json.loads(data)
        ad["remaining_seconds"] = ttl
        ad["remaining_days"] = max(1, ttl // 86400)
        tier = next((t for t in AD_TIERS if t["key"] == ad.get("tier")), None)
        if tier:
            ad["name"] = tier["name"]
        return ad
    except (json.JSONDecodeError, TypeError):
        return None
