"""Mini App preload aggregation and cache."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import func as sqlfunc, select

from cache.redis_client import get_redis
from db.engine import async_session
from db.models import Product, ResearchProgress, Shareholder
from services.company_service import get_companies_by_owner
from services.user_service import get_or_create_user, get_user_by_tg_id

_PRELOAD_CACHE_PREFIX = "miniapp:preload"


def _cache_key(tg_id: int, company_id: int | None) -> str:
    return f"{_PRELOAD_CACHE_PREFIX}:{tg_id}:{company_id or 0}"


def _safe_company_summary(company) -> dict[str, Any]:
    return {
        "id": company.id,
        "name": company.name,
        "company_type": company.company_type,
        "level": company.level,
        "employee_count": company.employee_count,
        "daily_revenue": int(company.daily_revenue),
        "cp_points": int(company.cp_points),
    }


async def ensure_user_exists(tg_id: int, tg_name: str) -> None:
    """Create user lazily when first entering Mini App."""
    async with async_session() as session:
        async with session.begin():
            await get_or_create_user(session, tg_id, tg_name)


async def _build_preload_payload(tg_id: int, company_id: int | None) -> dict[str, Any]:
    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if user is None:
            return {
                "user": None,
                "companies": [],
                "active_company": None,
                "meta": {
                    "preloaded_at": dt.datetime.now(dt.UTC).isoformat(),
                    "missing_user": True,
                },
            }

        companies = await get_companies_by_owner(session, user.id)
        active_company = None
        if company_id is not None:
            active_company = next((c for c in companies if c.id == company_id), None)
        if active_company is None and companies:
            active_company = companies[0]

        active_company_payload = None
        if active_company is not None:
            shareholder_count = (await session.execute(
                select(sqlfunc.count()).where(Shareholder.company_id == active_company.id)
            )).scalar() or 0
            product_count = (await session.execute(
                select(sqlfunc.count()).where(Product.company_id == active_company.id)
            )).scalar() or 0
            completed_research_count = (await session.execute(
                select(sqlfunc.count()).where(
                    ResearchProgress.company_id == active_company.id,
                    ResearchProgress.status == "completed",
                )
            )).scalar() or 0
            product_preview = (await session.execute(
                select(Product)
                .where(Product.company_id == active_company.id)
                .order_by(Product.quality.desc(), Product.id.asc())
                .limit(3)
            )).scalars().all()

            active_company_payload = {
                **_safe_company_summary(active_company),
                "shareholder_count": int(shareholder_count),
                "product_count": int(product_count),
                "completed_research_count": int(completed_research_count),
                "top_products": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "quality": p.quality,
                        "daily_income": int(p.daily_income),
                        "version": p.version,
                    }
                    for p in product_preview
                ],
            }

    company_list = [_safe_company_summary(c) for c in companies]
    return {
        "user": {
            "id": user.id,
            "tg_id": user.tg_id,
            "name": user.tg_name,
            "self_points": int(user.self_points),
            "reputation": int(user.reputation),
        },
        "companies": company_list,
        "active_company": active_company_payload,
        "meta": {
            "preloaded_at": dt.datetime.now(dt.UTC).isoformat(),
            "company_count": len(company_list),
        },
    }

async def _get_cached_preload(tg_id: int, company_id: int | None) -> dict[str, Any] | None:
    r = await get_redis()
    raw = await r.get(_cache_key(tg_id, company_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _set_cached_preload(
    tg_id: int,
    company_id: int | None,
    payload: dict[str, Any],
    ttl_seconds: int,
) -> None:
    ttl = max(1, int(ttl_seconds))
    r = await get_redis()
    await r.setex(_cache_key(tg_id, company_id), ttl, json.dumps(payload, ensure_ascii=False))


async def load_preload_data(
    tg_id: int,
    company_id: int | None,
    cache_ttl_seconds: int,
) -> dict[str, Any]:
    """Load preload payload with short redis snapshot cache."""
    cached = await _get_cached_preload(tg_id, company_id)
    if cached is not None:
        return cached
    payload = await _build_preload_payload(tg_id, company_id)
    await _set_cached_preload(tg_id, company_id, payload, cache_ttl_seconds)
    return payload
