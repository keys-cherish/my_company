"""Data integrity checker – auto-fixes illegal states.

Covers: products (employee over-allocation), shareholders (share overflow),
research (orphaned), real estate (orphaned), cooperations (expired/orphaned).
Each check runs in its own transaction so one failure won't block others.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Company, Product, User

from config import settings

logger = logging.getLogger(__name__)


async def cleanup_illegal_products(session: AsyncSession) -> list[str]:
    """Remove products whose assigned_employees exceed company's total employees.

    Keeps products with highest daily_income, removes lowest income first.
    """
    msgs = []
    result = await session.execute(select(Company))
    companies = list(result.scalars().all())

    for company in companies:
        # Sort by daily_income ASC so we remove lowest-income products first
        products = list((await session.execute(
            select(Product)
            .where(Product.company_id == company.id)
            .order_by(Product.daily_income.asc())
        )).scalars().all())

        total_assigned = sum(p.assigned_employees for p in products)
        if total_assigned <= company.employee_count:
            continue

        # Remove lowest-income products until assigned <= employee count
        removed = []
        for product in products:  # lowest income first
            if total_assigned <= company.employee_count:
                break
            total_assigned -= product.assigned_employees
            removed.append(product.name)
            await session.delete(product)

        if removed:
            from services.company_service import update_daily_revenue
            await update_daily_revenue(session, company.id)
            msg = f"⚠️ 「{company.name}」员工不足，自动下架(保留高收入): {', '.join(removed)}"
            msgs.append(msg)
            logger.warning(msg)

    if msgs:
        await session.flush()
    return msgs


async def cleanup_illegal_shareholders(session: AsyncSession) -> list[str]:
    """Fix shareholder data: total shares per company must not exceed 100%."""
    from db.models import Shareholder
    msgs = []

    result = await session.execute(select(Company))
    companies = list(result.scalars().all())

    for company in companies:
        shareholders = list((await session.execute(
            select(Shareholder)
            .where(Shareholder.company_id == company.id)
            .order_by(Shareholder.shares.desc())
        )).scalars().all())

        total_shares = sum(s.shares for s in shareholders)
        if total_shares <= 100.01:
            continue

        for sh in shareholders:
            sh.shares = round(sh.shares / total_shares * 100.0, 2)

        msg = f"⚠️ 「{company.name}」股份总和 {total_shares:.1f}% > 100%，已按比例修正"
        msgs.append(msg)
        logger.warning(msg)

    if msgs:
        await session.flush()
    return msgs


async def cleanup_orphaned_research(session: AsyncSession) -> list[str]:
    """Remove research records that reference non-existent companies."""
    from db.models import ResearchProgress
    msgs = []

    company_ids = [cid for (cid,) in (await session.execute(select(Company.id))).all()]
    if not company_ids:
        return msgs

    orphaned = list((await session.execute(
        select(ResearchProgress).where(
            ~ResearchProgress.company_id.in_(company_ids)
        )
    )).scalars().all())

    if orphaned:
        for r in orphaned:
            await session.delete(r)
        msg = f"⚠️ 清理了 {len(orphaned)} 条孤立研发记录"
        msgs.append(msg)
        logger.warning(msg)
        await session.flush()

    return msgs


async def cleanup_orphaned_realestate(session: AsyncSession) -> list[str]:
    """Remove real estate that references non-existent companies."""
    from db.models import RealEstate
    msgs = []

    company_ids = [cid for (cid,) in (await session.execute(select(Company.id))).all()]
    if not company_ids:
        return msgs

    orphaned = list((await session.execute(
        select(RealEstate).where(
            ~RealEstate.company_id.in_(company_ids)
        )
    )).scalars().all())

    if orphaned:
        for r in orphaned:
            await session.delete(r)
        msg = f"⚠️ 清理了 {len(orphaned)} 条孤立地产记录"
        msgs.append(msg)
        logger.warning(msg)
        await session.flush()

    return msgs


async def cleanup_expired_cooperations(session: AsyncSession) -> list[str]:
    """Remove cooperations that reference non-existent companies."""
    from db.models import Cooperation
    msgs = []

    company_ids = [cid for (cid,) in (await session.execute(select(Company.id))).all()]
    if not company_ids:
        return msgs

    orphaned = list((await session.execute(
        select(Cooperation).where(
            (~Cooperation.company_a_id.in_(company_ids))
            | (~Cooperation.company_b_id.in_(company_ids))
        )
    )).scalars().all())

    if orphaned:
        for c in orphaned:
            await session.delete(c)
        msg = f"⚠️ 清理了 {len(orphaned)} 条孤立合作记录"
        msgs.append(msg)
        logger.warning(msg)
        await session.flush()

    return msgs


async def cleanup_negative_funds(session: AsyncSession) -> list[str]:
    """Fix companies with negative funds."""
    msgs = []

    result = await session.execute(
        select(Company).where(Company.cp_points < 0)
    )
    negative_companies = list(result.scalars().all())

    for company in negative_companies:
        company.cp_points = 0
        msg = f"⚠️ 「{company.name}」积分为负数，已修正为0"
        msgs.append(msg)
        logger.warning(msg)

    if msgs:
        await session.flush()
    return msgs


async def cleanup_excess_estates(session: AsyncSession) -> list[str]:
    """Sell off cheapest estates when a company owns more than max_total_estates."""
    from db.models import RealEstate

    max_estates = settings.max_total_estates
    msgs = []
    result = await session.execute(select(Company))
    companies = list(result.scalars().all())

    for company in companies:
        estates = list((await session.execute(
            select(RealEstate)
            .where(RealEstate.company_id == company.id)
            .order_by(RealEstate.daily_dividend.asc())
        )).scalars().all())

        if len(estates) <= max_estates:
            continue

        to_remove = estates[:len(estates) - max_estates]
        refund = 0
        removed_names = []
        for e in to_remove:
            sell_price = e.purchase_price // 2
            refund += sell_price
            removed_names.append(f"{e.building_type} Lv.{e.level}")
            await session.delete(e)

        if refund > 0:
            new_funds = min(company.cp_points + refund, settings.max_company_funds)
            company.cp_points = new_funds

        msg = (
            f"⚠️ 「{company.name}」地产超过{max_estates}栋上限，"
            f"自动卖出(保留高收益): {', '.join(removed_names)}，"
            f"回收 {refund:,} 积分(半价)"
        )
        msgs.append(msg)
        logger.warning(msg)

    if msgs:
        await session.flush()
    return msgs


async def cleanup_excess_funds(session: AsyncSession) -> list[str]:
    """Cap company funds at max_company_funds."""
    max_funds = settings.max_company_funds
    msgs = []
    result = await session.execute(
        select(Company).where(Company.cp_points > max_funds)
    )
    over_companies = list(result.scalars().all())

    for company in over_companies:
        company.cp_points = max_funds
        msg = f"⚠️ 「{company.name}」积分超过上限({max_funds:,})，已修正"
        msgs.append(msg)
        logger.warning(msg)

    if msgs:
        await session.flush()
    return msgs


async def cleanup_excess_user_traffic(session: AsyncSession) -> list[str]:
    """Cap user self_points at dynamic max (base + per_level * company_level)."""
    from services.user_service import get_user_max_points
    msgs = []
    # Use base max to find candidates (actual max may be higher with company level)
    base_max = settings.max_self_points
    result = await session.execute(
        select(User).where(User.self_points > base_max)
    )
    over_users = list(result.scalars().all())

    for user in over_users:
        user_max = await get_user_max_points(session, user.id)
        if user.self_points <= user_max:
            continue  # Within dynamic cap for their company level
        overflow = user.self_points - user_max
        from services.user_service import add_self_points_by_user_id
        await add_self_points_by_user_id(
            session,
            user.id,
            -overflow,
            reason="integrity_cap_self_points",
        )

        # Try to invest overflow into user's company
        if overflow > 0:
            from services.company_service import get_companies_by_owner
            companies = await get_companies_by_owner(session, user.id)
            if companies:
                new_funds = min(
                    companies[0].cp_points + overflow,
                    settings.max_company_funds,
                )
                companies[0].cp_points = new_funds

        msg = f"⚠️ 用户「{user.tg_name}」个人积分超过上限({user_max:,})，已修正(溢出注资公司)"
        msgs.append(msg)
        logger.warning(msg)

    if msgs:
        await session.flush()
    return msgs


async def backfill_company_anomalies(session: AsyncSession) -> list[str]:
    """Backfill and correct abnormal company core fields."""
    from config import settings
    from services.company_service import (
        get_company_employee_limit,
        get_max_level,
        load_company_types,
    )

    msgs: list[str] = []
    companies = list((await session.execute(select(Company))).scalars().all())
    if not companies:
        return msgs

    valid_types = set(load_company_types().keys())
    max_level = get_max_level()

    revenue_rows = await session.execute(
        select(
            Product.company_id,
            sqlfunc.coalesce(sqlfunc.sum(Product.daily_income), 0),
        ).group_by(Product.company_id)
    )
    revenue_map = {int(cid): int(total or 0) for cid, total in revenue_rows.all()}

    fixed_bad_type = 0
    fixed_level = 0
    fixed_emp_floor = 0
    fixed_emp_cap = 0
    fixed_funds = 0
    fixed_version = 0
    fixed_daily_revenue = 0

    for company in companies:
        if company.company_type not in valid_types:
            company.company_type = "tech"
            fixed_bad_type += 1

        if company.level < 1:
            company.level = 1
            fixed_level += 1
        elif company.level > max_level:
            company.level = max_level
            fixed_level += 1

        max_emp = get_company_employee_limit(company.level, company.company_type)
        if company.employee_count < settings.base_employee_limit:
            company.employee_count = settings.base_employee_limit
            fixed_emp_floor += 1
        elif company.employee_count > max_emp:
            company.employee_count = max_emp
            fixed_emp_cap += 1

        if company.cp_points < 0:
            company.cp_points = 0
            fixed_funds += 1
        elif company.cp_points > settings.max_company_funds:
            company.cp_points = settings.max_company_funds
            fixed_funds += 1

        if company.version < 1:
            company.version = 1
            fixed_version += 1

        expected_revenue = revenue_map.get(company.id, 0)
        if company.daily_revenue != expected_revenue:
            company.daily_revenue = expected_revenue
            fixed_daily_revenue += 1

    if any((
        fixed_bad_type,
        fixed_level,
        fixed_emp_floor,
        fixed_emp_cap,
        fixed_funds,
        fixed_version,
        fixed_daily_revenue,
    )):
        await session.flush()

    if fixed_bad_type:
        msgs.append(f"公司类型异常回填: {fixed_bad_type} 条")
    if fixed_level:
        msgs.append(f"公司等级越界修正: {fixed_level} 条")
    if fixed_emp_floor:
        msgs.append(f"员工低于基线回填({settings.base_employee_limit}): {fixed_emp_floor} 条")
    if fixed_emp_cap:
        msgs.append(f"员工超上限回填: {fixed_emp_cap} 条")
    if fixed_funds:
        msgs.append(f"公司负积分修正: {fixed_funds} 条")
    if fixed_version:
        msgs.append(f"公司版本号修正: {fixed_version} 条")
    if fixed_daily_revenue:
        msgs.append(f"公司日营收回填(按产品汇总): {fixed_daily_revenue} 条")

    return msgs


async def run_all_checks(session: AsyncSession | None = None) -> list[str]:
    """Run all data integrity checks. Each check uses its own transaction."""
    from db.engine import async_session

    checks = [
        ("产品员工", cleanup_illegal_products),
        ("股东股份", cleanup_illegal_shareholders),
        ("孤立研发", cleanup_orphaned_research),
        ("孤立地产", cleanup_orphaned_realestate),
        ("孤立合作", cleanup_expired_cooperations),
        ("负数积分", cleanup_negative_funds),
        ("地产超限", cleanup_excess_estates),
        ("积分超限", cleanup_excess_funds),
        ("个人积分超限", cleanup_excess_user_traffic),
    ]
    msgs = []
    for name, check_fn in checks:
        try:
            async with async_session() as s:
                async with s.begin():
                    result = await check_fn(s)
                    msgs.extend(result)
        except Exception as e:
            msg = f"❌ {name}检查失败: {e}"
            msgs.append(msg)
            logger.exception(msg)
    return msgs
