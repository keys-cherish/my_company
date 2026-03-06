"""Product creation and upgrade validation rules."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_client import get_redis
from db.models import Company, Product, User
from services.company_service import get_effective_employee_count_for_progress
from utils.formatters import fmt_traffic
from utils.rules import Rule, RuleViolation
from utils.validators import validate_name


def _today_utc(now: dt.datetime | None = None) -> dt.datetime:
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc)


def _daily_create_counter_key(company_id: int, now: dt.datetime | None = None) -> str:
    return f"product_create_daily:{company_id}:{_today_utc(now).date().isoformat()}"


def _product_upgrade_cooldown_key(company_id: int, tech_id: str) -> str:
    return f"product_upgrade_cd:{company_id}:{tech_id}"


# ============================================================================
# Product Creation Rules
# ============================================================================

async def check_product_template_valid(
    templates: dict,
    product_key: str,
    **_,
) -> RuleViolation | None:
    """检查产品模板是否有效。"""
    if product_key not in templates:
        return RuleViolation(
            code="INVALID_TEMPLATE",
            actual=product_key,
            expected="valid_template",
            message="无效的产品模板",
        )
    return None


async def check_product_company_exists(
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


async def check_product_owner(
    session: AsyncSession,
    company_id: int,
    owner_user_id: int,
    **_,
) -> RuleViolation | None:
    """检查是否是公司老板。"""
    company = await session.get(Company, company_id)
    if not company:
        return None
    if company.owner_id != owner_user_id:
        return RuleViolation(
            code="NOT_OWNER",
            actual=owner_user_id,
            expected=company.owner_id,
            message="只有公司老板才能创建产品",
        )
    return None


async def check_product_user_exists(
    session: AsyncSession,
    owner_user_id: int,
    **_,
) -> RuleViolation | None:
    """检查用户是否存在。"""
    owner = await session.get(User, owner_user_id)
    if not owner:
        return RuleViolation(
            code="USER_NOT_FOUND",
            actual=None,
            expected="exists",
            message="用户不存在",
        )
    return None


async def check_product_tech_completed(
    completed_techs: set[str],
    tmpl: dict,
    **_,
) -> RuleViolation | None:
    """检查前置科研是否完成。"""
    if tmpl["tech_id"] not in completed_techs:
        return RuleViolation(
            code="TECH_NOT_COMPLETED",
            actual=None,
            expected=tmpl["tech_id"],
            message="需要先完成对应科研",
        )
    return None


async def check_product_name_valid(
    name: str,
    **_,
) -> RuleViolation | None:
    """检查产品名称是否有效。"""
    name_err = validate_name(name, min_len=1, max_len=32)
    if name_err:
        return RuleViolation(
            code="INVALID_NAME",
            actual=name,
            expected="valid_name",
            message=name_err,
        )
    return None


async def check_product_name_unique(
    session: AsyncSession,
    company_id: int,
    name: str,
    **_,
) -> RuleViolation | None:
    """检查产品名称是否唯一（同公司内）。"""
    existing = await session.execute(
        select(Product).where(Product.company_id == company_id, Product.name == name)
    )
    if existing.scalar_one_or_none():
        return RuleViolation(
            code="DUPLICATE_NAME",
            actual=name,
            expected="unique_name",
            message=f"已存在同名产品「{name}」",
        )
    return None


async def check_product_daily_limit(
    session: AsyncSession,
    company_id: int,
    max_daily: int,
    **_,
) -> RuleViolation | None:
    """检查每日创建上限。"""
    today_count: int | None = None
    try:
        r = await get_redis()
        cached_count = await r.get(_daily_create_counter_key(company_id))
        if cached_count is not None:
            today_count = int(cached_count)
    except Exception:
        today_count = None

    if today_count is None:
        today_start = _today_utc().replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        today_count = (await session.execute(
            select(sqlfunc.count()).select_from(Product).where(
                Product.company_id == company_id,
                Product.created_at >= today_start,
            )
        )).scalar() or 0
    if today_count >= max_daily:
        return RuleViolation(
            code="DAILY_LIMIT_REACHED",
            actual=today_count,
            expected=max_daily,
            message=f"每日最多创建{max_daily}个产品",
        )
    return None


async def check_product_create_employees(
    session: AsyncSession,
    company_id: int,
    existing_count: int,
    employee_step: int,
    **_,
) -> RuleViolation | None:
    """检查员工数量是否满足创建要求。"""
    company = await session.get(Company, company_id)
    if not company:
        return None
    required_employees = max(1, 1 + existing_count * employee_step)
    effective_employees = get_effective_employee_count_for_progress(company.employee_count)
    if effective_employees < required_employees:
        return RuleViolation(
            code="INSUFFICIENT_EMPLOYEES",
            actual=effective_employees,
            expected=required_employees,
            message=(
                f"员工不足，创建该产品需要至少 {required_employees} 人，"
                f"当前有效员工 {effective_employees} 人（总员工 {company.employee_count}）"
            ),
        )
    return None


async def check_product_create_reputation(
    session: AsyncSession,
    owner_user_id: int,
    existing_count: int,
    reputation_step: int,
    **_,
) -> RuleViolation | None:
    """检查声望是否满足创建要求。"""
    owner = await session.get(User, owner_user_id)
    if not owner:
        return None
    required_reputation = max(0, existing_count * reputation_step)
    if owner.reputation < required_reputation:
        return RuleViolation(
            code="INSUFFICIENT_REPUTATION",
            actual=owner.reputation,
            expected=required_reputation,
            message=(
                f"声望不足，创建该产品需要声望 {required_reputation}，"
                f"当前仅 {owner.reputation}"
            ),
        )
    return None


async def check_product_create_funds(
    session: AsyncSession,
    company_id: int,
    dynamic_create_cost: int,
    **_,
) -> RuleViolation | None:
    """检查公司资金是否足够。"""
    company = await session.get(Company, company_id)
    if not company:
        return None
    if company.cp_points < dynamic_create_cost:
        return RuleViolation(
            code="INSUFFICIENT_FUNDS",
            actual=company.cp_points,
            expected=dynamic_create_cost,
            message=f"公司积分不足，需要 {fmt_traffic(dynamic_create_cost)}",
        )
    return None


# ============================================================================
# Product Upgrade Rules
# ============================================================================

async def check_product_exists(
    session: AsyncSession,
    product_id: int,
    **_,
) -> RuleViolation | None:
    """检查产品是否存在。"""
    product = await session.get(Product, product_id)
    if not product:
        return RuleViolation(
            code="PRODUCT_NOT_FOUND",
            actual=None,
            expected="exists",
            message="产品不存在",
        )
    return None


async def check_product_upgrade_owner(
    session: AsyncSession,
    product_id: int,
    owner_user_id: int,
    **_,
) -> RuleViolation | None:
    """检查是否是公司老板（升级产品）。"""
    product = await session.get(Product, product_id)
    if not product:
        return None
    company = await session.get(Company, product.company_id)
    if not company:
        return RuleViolation(
            code="COMPANY_NOT_FOUND",
            actual=None,
            expected="exists",
            message="公司不存在",
        )
    if company.owner_id != owner_user_id:
        return RuleViolation(
            code="NOT_OWNER",
            actual=owner_user_id,
            expected=company.owner_id,
            message="只有公司老板才能升级产品",
        )
    return None


async def check_product_upgrade_user_exists(
    session: AsyncSession,
    owner_user_id: int,
    **_,
) -> RuleViolation | None:
    """检查用户是否存在。"""
    owner = await session.get(User, owner_user_id)
    if not owner:
        return RuleViolation(
            code="USER_NOT_FOUND",
            actual=None,
            expected="exists",
            message="用户不存在",
        )
    return None


async def check_product_max_version(
    session: AsyncSession,
    product_id: int,
    max_version: int,
    **_,
) -> RuleViolation | None:
    """检查产品是否达到最高版本。"""
    product = await session.get(Product, product_id)
    if not product:
        return None
    if product.version >= max_version:
        return RuleViolation(
            code="MAX_VERSION",
            actual=product.version,
            expected=max_version,
            message=f"产品已达最高版本(v{max_version})",
        )
    return None


async def check_product_max_income(
    session: AsyncSession,
    product_id: int,
    max_income: int,
    **_,
) -> RuleViolation | None:
    """检查产品收入是否达到上限。"""
    product = await session.get(Product, product_id)
    if not product:
        return None
    if product.daily_income >= max_income:
        return RuleViolation(
            code="MAX_INCOME",
            actual=product.daily_income,
            expected=max_income,
            message=f"产品日收入已达上限({max_income})",
        )
    return None


async def check_product_upgrade_cooldown(
    session: AsyncSession,
    product_id: int,
    **_,
) -> RuleViolation | None:
    """检查产品升级冷却。"""
    product = await session.get(Product, product_id)
    if not product:
        return None
    r = await get_redis()
    cd_key = _product_upgrade_cooldown_key(product.company_id, product.tech_id)
    cd_ttl = await r.ttl(cd_key)
    if cd_ttl > 0:
        hours = cd_ttl // 3600
        minutes = (cd_ttl % 3600) // 60
        return RuleViolation(
            code="COOLDOWN",
            actual=cd_ttl,
            expected=0,
            message=f"产品迭代冷却中，剩余{hours}时{minutes}分",
        )
    return None


async def check_product_upgrade_employees(
    session: AsyncSession,
    product_id: int,
    employee_step: int,
    **_,
) -> RuleViolation | None:
    """检查员工数量是否满足升级要求。"""
    product = await session.get(Product, product_id)
    if not product:
        return None
    company = await session.get(Company, product.company_id)
    if not company:
        return None
    target_version = product.version + 1
    required_employees = max(1, target_version * employee_step)
    effective_employees = get_effective_employee_count_for_progress(company.employee_count)
    if effective_employees < required_employees:
        return RuleViolation(
            code="INSUFFICIENT_EMPLOYEES",
            actual=effective_employees,
            expected=required_employees,
            message=(
                f"员工不足，升级到 v{target_version} 需要至少 {required_employees} 人，"
                f"当前有效员工 {effective_employees} 人（总员工 {company.employee_count}）"
            ),
        )
    return None


async def check_product_upgrade_reputation(
    session: AsyncSession,
    product_id: int,
    owner_user_id: int,
    reputation_step: int,
    **_,
) -> RuleViolation | None:
    """检查声望是否满足升级要求。"""
    product = await session.get(Product, product_id)
    if not product:
        return None
    owner = await session.get(User, owner_user_id)
    if not owner:
        return None
    target_version = product.version + 1
    required_reputation = max(0, (target_version - 1) * reputation_step)
    if owner.reputation < required_reputation:
        return RuleViolation(
            code="INSUFFICIENT_REPUTATION",
            actual=owner.reputation,
            expected=required_reputation,
            message=(
                f"声望不足，升级到 v{target_version} 需要声望 {required_reputation}，"
                f"当前仅 {owner.reputation}"
            ),
        )
    return None


async def check_product_upgrade_funds(
    session: AsyncSession,
    product_id: int,
    upgrade_cost: int,
    **_,
) -> RuleViolation | None:
    """检查公司资金是否足够升级。"""
    product = await session.get(Product, product_id)
    if not product:
        return None
    company = await session.get(Company, product.company_id)
    if not company:
        return None
    if company.cp_points < upgrade_cost:
        return RuleViolation(
            code="INSUFFICIENT_FUNDS",
            actual=company.cp_points,
            expected=upgrade_cost,
            message=f"公司积分不足，升级需要 {fmt_traffic(upgrade_cost)}",
        )
    return None


# ============================================================================
# Rule Lists (动态构建，因为需要运行时参数)
# ============================================================================

def get_product_create_guard_rules() -> list[Rule]:
    """获取产品创建前置条件规则。"""
    return [
        Rule("TEMPLATE_VALID", check_product_template_valid),
        Rule("COMPANY_EXISTS", check_product_company_exists),
        Rule("IS_OWNER", check_product_owner),
        Rule("USER_EXISTS", check_product_user_exists),
        Rule("TECH_COMPLETED", check_product_tech_completed),
        Rule("NAME_VALID", check_product_name_valid),
        Rule("NAME_UNIQUE", check_product_name_unique),
        Rule("DAILY_LIMIT", check_product_daily_limit),
    ]


def get_product_create_requirement_rules() -> list[Rule]:
    """获取产品创建需求规则。"""
    return [
        Rule("EMPLOYEES", check_product_create_employees),
        Rule("REPUTATION", check_product_create_reputation),
        Rule("FUNDS", check_product_create_funds),
    ]


def get_product_upgrade_guard_rules() -> list[Rule]:
    """获取产品升级前置条件规则。"""
    return [
        Rule("PRODUCT_EXISTS", check_product_exists),
        Rule("IS_OWNER", check_product_upgrade_owner),
        Rule("USER_EXISTS", check_product_upgrade_user_exists),
        Rule("MAX_VERSION", check_product_max_version),
        Rule("MAX_INCOME", check_product_max_income),
        Rule("COOLDOWN", check_product_upgrade_cooldown),
    ]


def get_product_upgrade_requirement_rules() -> list[Rule]:
    """获取产品升级需求规则。"""
    return [
        Rule("EMPLOYEES", check_product_upgrade_employees),
        Rule("REPUTATION", check_product_upgrade_reputation),
        Rule("FUNDS", check_product_upgrade_funds),
    ]
