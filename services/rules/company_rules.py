"""Company upgrade validation rules."""

from __future__ import annotations

from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Company, Product, ResearchProgress
from services.company_service import get_level_info, get_max_level
from utils.formatters import fmt_traffic
from utils.rules import Rule, RuleViolation


# ============================================================================
# Guard Rules (前置条件，顺序检查)
# ============================================================================

async def check_company_exists(
    session: AsyncSession,
    company_id: int,
    **_,
) -> RuleViolation | None:
    """检查公司是否存在。"""
    company = await session.get(Company, company_id)
    if not company:
        return RuleViolation(
            code="COMPANY_NOT_FOUND",
            actual=None,
            expected="exists",
            message="公司不存在",
        )
    return None


async def check_not_max_level(
    session: AsyncSession,
    company_id: int,
    **_,
) -> RuleViolation | None:
    """检查公司是否已达最高等级。"""
    company = await session.get(Company, company_id)
    if not company:
        return None  # 由 check_company_exists 处理
    max_level = get_max_level()
    if company.level >= max_level:
        return RuleViolation(
            code="MAX_LEVEL",
            actual=company.level,
            expected=max_level,
            message=f"已达最高等级 Lv.{max_level}",
        )
    return None


async def check_level_data_valid(
    session: AsyncSession,
    company_id: int,
    **_,
) -> RuleViolation | None:
    """检查下一等级数据是否存在。"""
    company = await session.get(Company, company_id)
    if not company:
        return None
    next_info = get_level_info(company.level + 1)
    if not next_info:
        return RuleViolation(
            code="LEVEL_DATA_INVALID",
            actual=None,
            expected="valid_level_data",
            message="等级数据异常",
        )
    return None


# ============================================================================
# Requirement Rules (升级需求，并行检查)
# ============================================================================

async def check_upgrade_funds(
    session: AsyncSession,
    company_id: int,
    next_info: dict,
    **_,
) -> RuleViolation | None:
    """检查公司资金是否足够升级。"""
    company = await session.get(Company, company_id)
    if not company:
        return None
    cost = next_info["upgrade_cost"]
    if company.cp_points < cost:
        return RuleViolation(
            code="INSUFFICIENT_FUNDS",
            actual=company.cp_points,
            expected=cost,
            message=f"积分: {fmt_traffic(company.cp_points)}/{fmt_traffic(cost)}",
        )
    return None


async def check_upgrade_employees(
    session: AsyncSession,
    company_id: int,
    next_info: dict,
    **_,
) -> RuleViolation | None:
    """检查员工数量是否满足升级要求。"""
    company = await session.get(Company, company_id)
    if not company:
        return None
    min_emp = next_info.get("min_employees", 0)
    if min_emp and company.employee_count < min_emp:
        return RuleViolation(
            code="INSUFFICIENT_EMPLOYEES",
            actual=company.employee_count,
            expected=min_emp,
            message=f"员工: {company.employee_count}/{min_emp}",
        )
    return None


async def check_upgrade_products(
    session: AsyncSession,
    company_id: int,
    next_info: dict,
    **_,
) -> RuleViolation | None:
    """检查产品数量是否满足升级要求。"""
    min_products = next_info.get("min_products", 0)
    if not min_products:
        return None
    prod_count = (await session.execute(
        select(sqlfunc.count()).where(Product.company_id == company_id)
    )).scalar() or 0
    if prod_count < min_products:
        return RuleViolation(
            code="INSUFFICIENT_PRODUCTS",
            actual=prod_count,
            expected=min_products,
            message=f"产品: {prod_count}/{min_products}",
        )
    return None


async def check_upgrade_techs(
    session: AsyncSession,
    company_id: int,
    next_info: dict,
    **_,
) -> RuleViolation | None:
    """检查科技数量是否满足升级要求。"""
    min_techs = next_info.get("min_techs", 0)
    if not min_techs:
        return None
    tech_count = (await session.execute(
        select(sqlfunc.count()).where(
            ResearchProgress.company_id == company_id,
            ResearchProgress.status == "completed",
        )
    )).scalar() or 0
    if tech_count < min_techs:
        return RuleViolation(
            code="INSUFFICIENT_TECHS",
            actual=tech_count,
            expected=min_techs,
            message=f"科技: {tech_count}/{min_techs}",
        )
    return None


async def check_upgrade_revenue(
    session: AsyncSession,
    company_id: int,
    next_info: dict,
    **_,
) -> RuleViolation | None:
    """检查日营收是否满足升级要求。"""
    company = await session.get(Company, company_id)
    if not company:
        return None
    min_revenue = next_info.get("min_daily_revenue", 0)
    if min_revenue and company.daily_revenue < min_revenue:
        return RuleViolation(
            code="INSUFFICIENT_REVENUE",
            actual=company.daily_revenue,
            expected=min_revenue,
            message=f"日营收: {fmt_traffic(company.daily_revenue)}/{fmt_traffic(min_revenue)}",
        )
    return None


# ============================================================================
# Rule Lists
# ============================================================================

UPGRADE_GUARD_RULES = [
    Rule("COMPANY_EXISTS", check_company_exists),
    Rule("NOT_MAX_LEVEL", check_not_max_level),
    Rule("LEVEL_DATA_VALID", check_level_data_valid),
]

UPGRADE_REQUIREMENT_RULES = [
    Rule("FUNDS", check_upgrade_funds),
    Rule("EMPLOYEES", check_upgrade_employees),
    Rule("PRODUCTS", check_upgrade_products),
    Rule("TECHS", check_upgrade_techs),
    Rule("REVENUE", check_upgrade_revenue),
]
