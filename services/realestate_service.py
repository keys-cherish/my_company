"""Real estate purchase, upgrade and management."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import RealEstate
from services.company_service import add_funds
from services.user_service import add_self_points

_buildings_data: dict | None = None

# Upgrade: each level adds 50% of base income; cost = purchase_price * upgrade_cost_mult * level
MAX_BUILDING_LEVEL = 10


def _load_buildings() -> dict:
    global _buildings_data
    if _buildings_data is None:
        path = Path(__file__).resolve().parent.parent / "game_data" / "buildings.json"
        with open(path, encoding="utf-8") as f:
            _buildings_data = json.load(f)
    return _buildings_data


def get_building_list() -> list[dict]:
    blds = _load_buildings()
    return [{"key": k, **v} for k, v in blds.items()]


def get_building_info(building_key: str) -> dict | None:
    blds = _load_buildings()
    if building_key not in blds:
        return None
    return {"key": building_key, **blds[building_key]}


async def get_company_estates(session: AsyncSession, company_id: int) -> list[RealEstate]:
    result = await session.execute(
        select(RealEstate).where(RealEstate.company_id == company_id)
    )
    return list(result.scalars().all())


async def get_total_estate_income(session: AsyncSession, company_id: int) -> int:
    estates = await get_company_estates(session, company_id)
    return sum(e.daily_dividend for e in estates)


async def count_company_building_type(
    session: AsyncSession, company_id: int, building_key: str,
) -> int:
    """Count how many of a specific building type a company owns."""
    result = await session.execute(
        select(sqlfunc.count()).where(
            RealEstate.company_id == company_id,
            RealEstate.building_type == building_key,
        )
    )
    return result.scalar() or 0


async def count_company_total_estates(
    session: AsyncSession, company_id: int,
) -> int:
    """Count total number of estates a company owns (all types)."""
    result = await session.execute(
        select(sqlfunc.count()).where(RealEstate.company_id == company_id)
    )
    return result.scalar() or 0


def calc_upgrade_cost(building_info: dict, current_level: int) -> int:
    """Calculate cost to upgrade from current_level to current_level+1."""
    base_price = building_info["purchase_price"]
    mult = building_info.get("upgrade_cost_mult", 0.6)
    return int(base_price * mult * current_level)


def calc_level_income(building_info: dict, level: int) -> int:
    """Calculate daily income at a given level."""
    base = building_info["daily_dividend"]
    # Each level adds 50% of base income
    return int(base * (1.0 + 0.5 * (level - 1)))


async def purchase_building(
    session: AsyncSession,
    company_id: int,
    owner_tg_id: int,
    building_key: str,
) -> tuple[bool, str]:
    """Purchase a building for a company using company funds."""
    buildings = _load_buildings()
    if building_key not in buildings:
        return False, "无效的地产类型"

    bld = buildings[building_key]
    price = bld["purchase_price"]

    # Check purchase limit
    max_count = bld.get("max_count", 99)
    current_count = await count_company_building_type(session, company_id, building_key)
    if current_count >= max_count:
        return False, f"「{bld['name']}」已达上限（{max_count}栋）"

    # Check global estate limit
    total_estates = await count_company_total_estates(session, company_id)
    if total_estates >= settings.max_total_estates:
        return False, f"地产总数已达上限（{settings.max_total_estates}栋）"

    ok = await add_funds(session, company_id, -price)
    if not ok:
        return False, f"公司积分不足，需要 {price:,} 积分"

    estate = RealEstate(
        company_id=company_id,
        building_type=building_key,
        daily_dividend=bld["daily_dividend"],
        purchase_price=price,
    )
    session.add(estate)
    await session.flush()

    await add_self_points(owner_tg_id, 15)

    remaining = max_count - current_count - 1
    return True, (
        f"成功购买「{bld['name']}」! 每日收益: {bld['daily_dividend']:,} 积分\n"
        f"剩余可购: {remaining}/{max_count}"
    )


async def upgrade_estate(
    session: AsyncSession,
    estate_id: int,
    company_id: int,
) -> tuple[bool, str]:
    """Upgrade a real estate building to the next level.

    使用 UPDATE 语句确保数据库修改被正确持久化。
    """
    estate = await session.get(RealEstate, estate_id)
    if not estate or estate.company_id != company_id:
        return False, "地产不存在"

    if estate.level >= MAX_BUILDING_LEVEL:
        return False, f"已达最高等级 Lv.{MAX_BUILDING_LEVEL}"

    bld_info = get_building_info(estate.building_type)
    if not bld_info:
        return False, "地产数据异常"

    cost = calc_upgrade_cost(bld_info, estate.level)
    ok = await add_funds(session, company_id, -cost)
    if not ok:
        return False, f"公司积分不足，升级需要 {cost:,} 积分"

    old_level = estate.level
    new_level = old_level + 1
    new_income = calc_level_income(bld_info, new_level)

    # 使用 update() 语句确保修改被持久化
    from sqlalchemy import update
    await session.execute(
        update(RealEstate)
        .where(RealEstate.id == estate_id)
        .values(level=new_level, daily_dividend=new_income)
    )
    await session.flush()

    return True, (
        f"「{bld_info['name']}」升级成功! "
        f"Lv.{old_level} → Lv.{new_level}，"
        f"日收益: {new_income:,} 积分"
    )
