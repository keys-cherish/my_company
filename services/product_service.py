"""产品创建、迭代和管理。

修复要点：
- 产品迭代有每日CD（每个产品每天只能迭代1次）
- 产品收入有上限
- 产品名称重复检测
- 版本上限
"""

from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_client import get_redis
from config import settings
from db.models import Company, Product, User
from services.company_service import get_effective_employee_count_for_progress
from services.research_service import (
    check_and_complete_research,
    get_completed_techs,
    sync_research_progress_if_due,
)
from services.user_service import add_points
from utils.formatters import fmt_traffic
from utils.validators import validate_name

_products_data: dict | None = None

# 产品收入上限和版本上限
MAX_PRODUCT_DAILY_INCOME = 500_000
MAX_PRODUCT_VERSION = 50
MAX_DAILY_PRODUCT_CREATE = 3

# Progressive gate: higher stage requires stronger company capability.
PRODUCT_CREATE_COST_GROWTH = 0.30
PRODUCT_CREATE_REPUTATION_STEP = 6
PRODUCT_CREATE_EMPLOYEE_STEP = 1
PRODUCT_UPGRADE_REPUTATION_STEP = 12
PRODUCT_UPGRADE_EMPLOYEE_STEP = 2


def _load_products() -> dict:
    global _products_data
    if _products_data is None:
        path = Path(__file__).resolve().parent.parent / "game_data" / "products.json"
        with open(path, encoding="utf-8") as f:
            _products_data = json.load(f)
    return _products_data


def _today_utc(now: dt.datetime | None = None) -> dt.datetime:
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc)


def _daily_create_counter_key(company_id: int, now: dt.datetime | None = None) -> str:
    return f"product_create_daily:{company_id}:{_today_utc(now).date().isoformat()}"


def _seconds_until_next_utc_day(now: dt.datetime | None = None) -> int:
    current = _today_utc(now)
    tomorrow = (current + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - current).total_seconds()))


def _product_upgrade_cooldown_key(company_id: int, tech_id: str) -> str:
    return f"product_upgrade_cd:{company_id}:{tech_id}"


async def _mark_daily_product_create(company_id: int) -> None:
    """Increment daily create event counter; best-effort only."""
    try:
        r = await get_redis()
        key = _daily_create_counter_key(company_id)
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, _seconds_until_next_utc_day() + 60)
    except Exception:
        return


async def get_available_product_templates(session: AsyncSession, company_id: int) -> list[dict]:
    """返回公司可创建的产品模板（基于已完成科研）。"""
    await sync_research_progress_if_due(session, company_id)
    completed = set(await get_completed_techs(session, company_id))
    templates = _load_products()
    available = []
    for key, info in templates.items():
        if info["tech_id"] in completed:
            available.append({"product_key": key, **info})
    return available


async def get_company_products(session: AsyncSession, company_id: int) -> list[Product]:
    result = await session.execute(
        select(Product).where(Product.company_id == company_id).order_by(Product.daily_income.desc())
    )
    return list(result.scalars().all())


async def create_product(
    session: AsyncSession,
    company_id: int,
    owner_user_id: int,
    product_key: str,
    custom_name: str = "",
) -> tuple[Product | None, str]:
    """从模板创建产品。消耗流量。"""
    from services.rules.product_rules import (
        get_product_create_guard_rules,
        get_product_create_requirement_rules,
    )
    from utils.rules import check_rules_sequential, check_rules_parallel

    templates = _load_products()

    # 检查前置科研
    await check_and_complete_research(session, company_id)
    completed = set(await get_completed_techs(session, company_id))

    # 产品名称
    tmpl = templates.get(product_key, {})
    name = custom_name.strip() if custom_name.strip() else tmpl.get("name", "")

    # 构建上下文
    ctx = {
        "session": session,
        "company_id": company_id,
        "owner_user_id": owner_user_id,
        "product_key": product_key,
        "templates": templates,
        "tmpl": tmpl,
        "completed_techs": completed,
        "name": name,
        "max_daily": MAX_DAILY_PRODUCT_CREATE,
    }

    # 顺序检查前置条件
    guard_fail = await check_rules_sequential(get_product_create_guard_rules(), **ctx)
    if guard_fail:
        return None, guard_fail.message

    # 计算动态参数
    existing_count = (await session.execute(
        select(sqlfunc.count()).select_from(Product).where(Product.company_id == company_id)
    )).scalar() or 0

    dynamic_create_cost = max(
        settings.product_create_cost,
        int(settings.product_create_cost * (1 + existing_count * PRODUCT_CREATE_COST_GROWTH)),
    )

    ctx.update({
        "existing_count": existing_count,
        "employee_step": PRODUCT_CREATE_EMPLOYEE_STEP,
        "reputation_step": PRODUCT_CREATE_REPUTATION_STEP,
        "dynamic_create_cost": dynamic_create_cost,
    })

    # 并行检查需求条件
    req_fails = await check_rules_parallel(get_product_create_requirement_rules(), **ctx)
    if req_fails:
        # 返回第一个失败
        return None, req_fails[0].message

    # 扣除费用（从公司积分）
    from services.company_service import add_funds
    ok = await add_funds(session, company_id, -dynamic_create_cost)
    if not ok:
        return None, f"公司积分不足，需要 {fmt_traffic(dynamic_create_cost)}"

    product = Product(
        company_id=company_id,
        name=name,
        tech_id=tmpl["tech_id"],
        daily_income=tmpl["base_daily_income"],
        quality=tmpl["base_quality"],
    )
    session.add(product)
    await session.flush()
    owner = await session.get(User, owner_user_id)
    await add_points(owner_user_id, 10, session=session)

    # Quest progress
    from services.quest_service import update_quest_progress
    await update_quest_progress(session, owner_user_id, "product_count", increment=1)
    await _mark_daily_product_create(company_id)

    return product, (
        f"产品「{name}」打造成功! 日收入: {fmt_traffic(tmpl['base_daily_income'])} "
        f"(研发投入: {fmt_traffic(dynamic_create_cost)})"
    )


async def upgrade_product(
    session: AsyncSession,
    product_id: int,
    owner_user_id: int,
) -> tuple[bool, str]:
    """升级产品：+版本 +收入 +品质。每个产品每天只能迭代1次。"""
    from services.rules.product_rules import (
        get_product_upgrade_guard_rules,
        get_product_upgrade_requirement_rules,
    )
    from utils.rules import check_rules_sequential, check_rules_parallel

    # 获取产品信息用于计算成本
    product = await session.get(Product, product_id)
    upgrade_cost = 0
    if product:
        upgrade_cost = int(settings.product_upgrade_cost_base * (1.3 ** (product.version - 1)))

    # 构建上下文
    ctx = {
        "session": session,
        "product_id": product_id,
        "owner_user_id": owner_user_id,
        "max_version": MAX_PRODUCT_VERSION,
        "max_income": MAX_PRODUCT_DAILY_INCOME,
        "employee_step": PRODUCT_UPGRADE_EMPLOYEE_STEP,
        "reputation_step": PRODUCT_UPGRADE_REPUTATION_STEP,
        "upgrade_cost": upgrade_cost,
    }

    # 顺序检查前置条件
    guard_fail = await check_rules_sequential(get_product_upgrade_guard_rules(), **ctx)
    if guard_fail:
        return False, guard_fail.message

    # 并行检查需求条件
    req_fails = await check_rules_parallel(get_product_upgrade_requirement_rules(), **ctx)
    if req_fails:
        return False, req_fails[0].message

    # 重新获取产品（确保最新状态）
    product = await session.get(Product, product_id)
    company = await session.get(Company, product.company_id)

    from services.company_service import add_funds
    ok = await add_funds(session, product.company_id, -upgrade_cost)
    if not ok:
        return False, f"公司积分不足，升级需要 {fmt_traffic(upgrade_cost)}"

    # 负道德时产品升级有失败率
    from services.operations_service import get_or_create_profile
    profile = await get_or_create_profile(session, product.company_id)
    if profile.ethics < 0:
        fail_rate = min(0.40, abs(profile.ethics) * 0.004)
        if random.random() < fail_rate:
            return False, f"⚠️ 产品升级失败！(道德{profile.ethics}，失败率{int(fail_rate * 100)}%)"

    # 迭代收入增幅随版本递减（防止无限刷）
    diminish = max(0.05, settings.product_upgrade_income_pct - (product.version - 1) * 0.01)
    income_boost = max(1, int(product.daily_income * diminish))
    new_income = min(product.daily_income + income_boost, MAX_PRODUCT_DAILY_INCOME)
    actual_boost = new_income - product.daily_income

    product.version += 1
    product.daily_income = new_income
    product.quality = min(product.quality + 3, 100)
    await session.flush()

    # 设置24小时CD
    r = await get_redis()
    cd_key = _product_upgrade_cooldown_key(product.company_id, product.tech_id)
    await r.setex(cd_key, 86400, "1")

    await add_points(owner_user_id, 5, session=session)

    return True, (
        f"产品「{product.name}」升级到v{product.version}! "
        f"日收入+{actual_boost} → {fmt_traffic(product.daily_income)}"
    )
