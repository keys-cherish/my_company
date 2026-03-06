"""Company creation and management."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import Company, Product, Shareholder, User
from services.user_service import add_traffic

_company_types: dict | None = None


def load_company_types() -> dict:
    global _company_types
    if _company_types is None:
        path = Path(__file__).resolve().parent.parent / "game_data" / "company_types.json"
        with open(path, encoding="utf-8") as f:
            _company_types = json.load(f)
    return _company_types


def get_company_type_info(company_type: str) -> dict | None:
    types = load_company_types()
    return types.get(company_type)


async def create_company(
    session: AsyncSession,
    owner: User,
    name: str,
    company_type: str = "tech",
) -> tuple[Company | None, str]:
    """Create a company. Deducts creation cost from owner's traffic."""
    types = load_company_types()
    if company_type not in types:
        return None, "无效的公司类型"

    # One company per person
    existing = await session.execute(
        select(Company).where(Company.owner_id == owner.id)
    )
    if existing.scalar_one_or_none():
        return None, "每人只能拥有一家公司"

    # Check duplicate name
    exists = await session.execute(select(Company).where(Company.name == name))
    if exists.scalar_one_or_none():
        return None, "公司名称已存在"

    # 先保存owner_id，因为add_traffic会expire owner对象
    owner_id = owner.id

    # Deduct traffic
    ok = await add_traffic(session, owner_id, -settings.company_creation_cost)
    if not ok:
        from utils.formatters import fmt_traffic
        return None, f"积分不足，创建公司需要 {fmt_traffic(settings.company_creation_cost)}"

    type_info = types[company_type]
    company = Company(
        name=name,
        company_type=company_type,
        owner_id=owner_id,
        total_funds=settings.company_creation_cost,
        employee_count=settings.base_employee_limit,
    )
    session.add(company)
    await session.flush()

    # Owner gets 100% shares
    shareholder = Shareholder(
        company_id=company.id,
        user_id=owner_id,
        shares=100.0,
        invested_amount=settings.company_creation_cost,
    )
    session.add(shareholder)
    await session.flush()
    return company, f"{type_info['emoji']} {type_info['name']}「{name}」创建成功!"


async def get_company_by_id(session: AsyncSession, company_id: int) -> Company | None:
    return await session.get(Company, company_id)


async def get_companies_by_owner(session: AsyncSession, owner_id: int) -> list[Company]:
    result = await session.execute(select(Company).where(Company.owner_id == owner_id))
    return list(result.scalars().all())


async def get_company_valuation(session: AsyncSession, company: Company) -> int:
    """Valuation = (total_funds * coeff + daily_revenue * 30) modified by ethics."""
    base = int(
        company.total_funds * settings.valuation_fund_coeff
        + company.daily_revenue * settings.valuation_income_days
    )
    # Ethics modifier
    from services.operations_service import get_or_create_profile
    profile = await get_or_create_profile(session, company.id)
    if profile.ethics > 70:
        return int(base * 1.15)  # +15%
    if profile.ethics < 30:
        return int(base * 0.80)  # -20%
    return base


async def update_daily_revenue(session: AsyncSession, company_id: int) -> int:
    """Recalculate daily revenue from products and return it."""
    result = await session.execute(select(Product).where(Product.company_id == company_id))
    products = result.scalars().all()
    total = sum(p.daily_income for p in products)
    await session.execute(
        update(Company).where(Company.id == company_id).values(daily_revenue=total)
    )
    return total


async def add_funds(
    session: AsyncSession,
    company_id: int,
    amount: int,
    reason: str = "未知",
) -> bool:
    """Atomically add/subtract funds with optimistic locking.

    Args:
        session: Database session
        company_id: Company ID
        amount: Amount to add (negative for deduction)
        reason: Reason for the change (for logging)
    """
    company = await session.get(Company, company_id)
    if company is None:
        return False
    if amount < 0 and company.total_funds + amount < 0:
        return False
    # Cap at max_company_funds
    new_funds = min(company.total_funds + amount, settings.max_company_funds)
    actual_amount = new_funds - company.total_funds
    if actual_amount == 0:
        return True
    old_ver = company.version
    result = await session.execute(
        update(Company)
        .where(Company.id == company_id, Company.version == old_ver)
        .values(total_funds=new_funds, version=Company.version + 1)
    )
    if result.rowcount == 0:
        return False
    # 立即刷新对象，避免惰性加载导致MissingGreenlet
    await session.refresh(company)

    # 记录资金日志
    from services.fundlog_service import log_fund_change
    await log_fund_change(
        "company",
        company_id,
        actual_amount,
        reason,
        balance_after=company.total_funds,
    )
    return True


# ---------- Company levels ----------

_company_levels: dict | None = None


def load_company_levels() -> dict:
    global _company_levels
    if _company_levels is None:
        path = Path(__file__).resolve().parent.parent / "game_data" / "company_levels.json"
        with open(path, encoding="utf-8") as f:
            _company_levels = json.load(f)
    return _company_levels


def get_level_info(level: int) -> dict | None:
    data = load_company_levels()
    return data["levels"].get(str(level))


def get_max_level() -> int:
    data = load_company_levels()
    return data.get("max_level", 10)


def get_level_revenue_bonus(level: int) -> int:
    """Get cumulative daily revenue bonus from all levels up to and including current level."""
    data = load_company_levels()
    total = 0
    for lv in range(1, level + 1):
        info = data["levels"].get(str(lv))
        if info:
            total += info.get("daily_revenue_bonus", 0)
    return total


def get_level_employee_bonus(level: int) -> int:
    """Get cumulative employee limit bonus from all levels."""
    data = load_company_levels()
    total = 0
    for lv in range(1, level + 1):
        info = data["levels"].get(str(lv))
        if info:
            total += info.get("employee_limit_bonus", 0)
    return total


def get_company_employee_limit(level: int, company_type: str | None = None) -> int:
    """Calculate company employee limit with level curve and hard cap."""
    max_level = max(1, get_max_level())
    safe_level = max(1, min(level, max_level))

    if max_level <= 1:
        progress = 1.0
    else:
        progress = (safe_level - 1) / (max_level - 1)

    curve_exp = max(1.0, float(settings.employee_limit_growth_exponent))
    curved_progress = progress ** curve_exp
    scaled_limit = int(
        settings.base_employee_limit
        + (settings.max_employee_limit - settings.base_employee_limit) * curved_progress
    )

    linear_legacy = settings.employee_limit_per_level * (safe_level - 1)
    total = scaled_limit + linear_legacy + get_level_employee_bonus(safe_level)
    if company_type:
        type_info = get_company_type_info(company_type)
        if type_info and type_info.get("extra_employee_limit"):
            total += int(type_info["extra_employee_limit"])

    return max(settings.base_employee_limit, min(total, settings.max_employee_limit))


def calc_employee_income(employee_count: int, revenue: int) -> tuple[int, int]:
    """Calculate employee workforce income contribution.

    Returns (base_output, efficiency_bonus).
    """
    if employee_count <= 0:
        return (0, 0)

    # Base output: each employee produces 1.5x their salary
    base_output = int(employee_count * settings.employee_salary_base * 1.5)

    # Efficiency bonus: proportional to revenue, diminishing past soft cap
    effective = min(employee_count, settings.employee_effective_cap_for_progress)
    efficiency_bonus = int(revenue * effective * 0.002)

    return (base_output, efficiency_bonus)


def get_effective_employee_count_for_progress(employee_count: int) -> int:
    """Soft-cap effective workforce used by progression gates."""
    if employee_count <= 0:
        return 0
    return min(employee_count, settings.employee_effective_cap_for_progress)


async def upgrade_company(
    session: AsyncSession,
    company_id: int,
) -> tuple[bool, str]:
    """Upgrade company to next level. Requires funds + employees + products + techs + revenue."""
    from utils.formatters import fmt_traffic
    from services.rules.company_rules import UPGRADE_GUARD_RULES, UPGRADE_REQUIREMENT_RULES
    from utils.rules import check_rules_sequential, check_rules_parallel

    ctx = {"session": session, "company_id": company_id}

    # 顺序检查前置条件（公司存在、未满级、等级数据有效）
    guard_fail = await check_rules_sequential(UPGRADE_GUARD_RULES, **ctx)
    if guard_fail:
        return False, guard_fail.message

    # 加载公司和等级信息
    company = await session.get(Company, company_id)
    next_level = company.level + 1
    next_info = get_level_info(next_level)
    ctx["next_info"] = next_info

    # 并行检查所有升级需求
    req_fails = await check_rules_parallel(UPGRADE_REQUIREMENT_RULES, **ctx)
    if req_fails:
        lines = [f"升级到 Lv.{next_level}「{next_info['name']}」条件不足:"]
        lines.extend(f"  ❌ {v.message}" for v in req_fails)
        return False, "\n".join(lines)

    # Deduct funds
    cost = next_info["upgrade_cost"]
    ok = await add_funds(session, company_id, -cost)
    if not ok:
        return False, "积分扣除失败"

    company.level = next_level
    await session.flush()
    await session.refresh(company)

    # Quest progress
    from services.quest_service import update_quest_progress
    await update_quest_progress(session, company.owner_id, "company_level", current_value=next_level)

    return True, (
        f"🎉 升级成功! {company.name} → Lv.{next_level}「{next_info['name']}」\n"
        f"{'─' * 24}\n"
        f"永久日营收加成: +{fmt_traffic(next_info['daily_revenue_bonus'])}\n"
        f"员工上限: +{next_info['employee_limit_bonus']}"
    )
