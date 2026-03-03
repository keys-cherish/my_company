"""Daily settlement: calculates income, distributes dividends, generates reports."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import Company, DailyReport, Product
from services.company_service import get_company_type_info, update_daily_revenue
from services.random_events import roll_daily_events
from services.research_service import check_and_complete_research
from services.settlement.pipeline import (
    apply_penalties,
    compute_base_income,
    compute_costs,
    finalize_settlement,
)
from services.settlement.breakdowns import IncomeBreakdown
from services.operations_service import (
    calc_extra_operating_costs,
    calc_immoral_buff,
    get_market_trend,
    get_operation_multipliers,
    get_or_create_profile,
    run_regulation_audit,
    save_recent_events,
    settle_profile_daily,
)
from cache.redis_client import update_leaderboard

logger = logging.getLogger(__name__)


async def settle_company(session: AsyncSession, company: Company) -> tuple[DailyReport | None, list[str]]:
    """结算单个公司的每日收支（流水线版本）。

    流程:
    1. 完成到期科研 → 刷新产品收入
    2. 计算总收入 = 产品 + 等级加成 + 合作 + 地产 + 声望 + 广告 + 商店 + 商战 + 类型
    3. 应用惩罚 = 改名惩罚 + 战斗减益 + 路演惩罚
    4. 扣除成本 = 税收 + 薪资 + 社保 + 办公 + 培训 + 监管 + 保险 + 工时
    5. 运行监管抽检（可能产生罚款）
    6. 计算利润 → 更新公司资金 → 分红 → 随机事件 → 生成日报

    Returns:
        (DailyReport, event_messages) 或 (None, []) 如果公司无产品
    """
    from cache.redis_client import get_redis

    today = dt.date.today().isoformat()

    # Check for completed research
    completed_techs = await check_and_complete_research(session, company.id)
    if completed_techs:
        logger.info("Company %s completed research: %s", company.name, completed_techs)

    # Recalculate daily revenue
    await update_daily_revenue(session, company.id)
    await session.refresh(company)
    now_utc = dt.datetime.now(dt.UTC)
    profile = await get_or_create_profile(session, company.id)
    multipliers = get_operation_multipliers(profile, now_utc)
    market = get_market_trend(company, now_utc)

    # 步骤1: 计算基础收入
    income = await compute_base_income(session, company, profile, market, multipliers)

    # 步骤2: 应用惩罚
    penalties, adjusted_product_income = await apply_penalties(company.id, income.product_income)

    # 用调整后的收入重新计算相关项
    # 惩罚只影响产品收入，所以需要重新计算基于产品收入的加成
    if penalties.total > 0:
        # 重新计算受产品收入影响的加成项
        from services.ad_service import get_ad_boost
        from services.company_service import calc_employee_income
        from services.cooperation_service import get_cooperation_bonus
        from services.shop_service import get_income_buff_multiplier
        from utils.formatters import reputation_buff_multiplier
        from db.models import User

        # 更新产品收入为惩罚后的值
        income.product_income = adjusted_product_income

        # 重新计算合作加成
        coop_bonus_rate = await get_cooperation_bonus(session, company.id)
        income.cooperation_bonus = int(adjusted_product_income * coop_bonus_rate)

        # 重新计算声望加成
        owner = await session.get(User, company.owner_id)
        rep_multiplier = reputation_buff_multiplier(owner.reputation) if owner else 1.0
        income.reputation_buff = int(adjusted_product_income * (rep_multiplier - 1.0))

        # 重新计算广告加成
        ad_boost_rate = await get_ad_boost(company.id)
        income.ad_boost = int(adjusted_product_income * ad_boost_rate)

        # 重新计算商店加成
        shop_buff_mult = await get_income_buff_multiplier(company.id)
        income.shop_buff = int(adjusted_product_income * (shop_buff_mult - 1.0))

        # 重新计算商战加成
        r = await get_redis()
        totalwar_buff_str = await r.get(f"totalwar_buff:{company.id}")
        totalwar_buff_rate = float(totalwar_buff_str) if totalwar_buff_str else 0.0
        income.totalwar_buff = int(adjusted_product_income * totalwar_buff_rate)

        # 重新计算类型加成
        type_info = get_company_type_info(company.company_type)
        type_income_bonus = type_info.get("income_bonus", 0.0) if type_info else 0.0
        income.type_bonus = int(adjusted_product_income * type_income_bonus)

        # 重新计算员工收入
        emp_base_output, emp_efficiency_bonus = calc_employee_income(
            company.employee_count, adjusted_product_income
        )
        income.employee_income = emp_base_output + emp_efficiency_bonus

        # 重新计算缺德buff
        immoral_mult = calc_immoral_buff(profile.ethics)
        income.immoral_buff = int(adjusted_product_income * (immoral_mult - 1.0)) if immoral_mult > 1.0 else 0

    # Quest progress for daily_revenue and employee_count
    from services.quest_service import update_quest_progress
    await update_quest_progress(session, company.owner_id, "daily_revenue", current_value=income.total)
    await update_quest_progress(session, company.owner_id, "employee_count", current_value=company.employee_count)

    # 步骤3: 计算成本
    type_info = get_company_type_info(company.company_type)
    extra_costs = calc_extra_operating_costs(
        profile,
        company.employee_count,
        income.total,
        company.employee_count * settings.employee_salary_base,
        int(company.employee_count * settings.employee_salary_base * settings.social_insurance_rate),
        now_utc,
    )
    reg_audit = run_regulation_audit(profile, income.total, now_utc)
    fine = int(reg_audit["fine"])

    costs = compute_costs(income, company, profile, type_info, extra_costs, fine)

    # 步骤4: 最终结算
    result = await finalize_settlement(session, company, income, penalties, costs)

    # Roll random events
    event_messages = await roll_daily_events(session, company)
    if market["income_mult"] > 1.0:
        event_messages.append(
            f"📈 行业景气加成：{market['label']}（营收+{(market['income_mult'] - 1.0) * 100:.0f}%）"
        )
    elif market["income_mult"] < 1.0:
        event_messages.append(
            f"📉 行业景气压制：{market['label']}（营收{(market['income_mult'] - 1.0) * 100:.0f}%）"
        )
    sampled_hours = int(reg_audit["sampled_hours"])
    overtime_hours = int(reg_audit["overtime_hours"])
    if overtime_hours > 0:
        if fine > 0:
            event_messages.append(
                f"🛂 工时抽检：{sampled_hours}h（超时{overtime_hours}h），重罚 -{fine:,} 积分"
            )
        else:
            event_messages.append(
                f"🛂 工时抽检：{sampled_hours}h（超时{overtime_hours}h），本次未触发处罚"
            )
    elif fine > 0:
        event_messages.append(f"⚖️ 合规抽检处罚：-{fine:,} 积分")

    # 惩罚事件消息
    if penalties.roadshow_penalty > 0:
        r = await get_redis()
        roadshow_penalty_str = await r.get(f"roadshow_penalty:{company.id}")
        roadshow_penalty_rate = float(roadshow_penalty_str) if roadshow_penalty_str else 0.0
        event_messages.append(
            f"🎭 路演翻车反噬：当日营收 -{int(roadshow_penalty_rate * 100)}% "
            f"(-{penalties.roadshow_penalty:,})"
        )

    # 商战Buff事件消息
    r = await get_redis()
    totalwar_buff_str = await r.get(f"totalwar_buff:{company.id}")
    totalwar_buff_rate = float(totalwar_buff_str) if totalwar_buff_str else 0.0
    if totalwar_buff_rate > 0:
        event_messages.append(
            f"⚔️ 全面商战Buff：营收+{int(totalwar_buff_rate * 100)}% "
            f"(+{income.totalwar_buff:,})"
        )
    # 缺德buff事件消息
    if income.immoral_buff > 0:
        immoral_mult = calc_immoral_buff(profile.ethics)
        event_messages.append(
            f"😈 缺德buff：道德{profile.ethics} → 营收+{int((immoral_mult - 1) * 100)}% (+{income.immoral_buff:,})"
        )
    await save_recent_events(company.id, event_messages)
    profile_msgs = await settle_profile_daily(session, profile, now_utc)
    event_messages.extend(profile_msgs)

    # Generate report
    from services.dividend_service import distribute_dividends
    distributions = await distribute_dividends(session, company, result.profit)
    total_dividend = sum(amt for _, amt in distributions)

    report = DailyReport(
        company_id=company.id,
        date=today,
        product_income=income.product_income,
        employee_income=income.employee_income,
        cooperation_bonus=income.cooperation_bonus,
        realestate_income=income.realestate_income,
        reputation_buff_income=income.reputation_buff,
        total_income=income.total,
        operating_cost=costs.total,
        dividend_paid=total_dividend,
    )
    session.add(report)
    await session.flush()

    # 更新排行榜（多维度）
    await update_leaderboard("revenue", company.name, income.total)
    await update_leaderboard("funds", company.name, company.total_funds)
    valuation = int(
        company.total_funds * settings.valuation_fund_coeff
        + company.daily_revenue * settings.valuation_income_days
    )
    await update_leaderboard("valuation", company.name, valuation)

    # 更新综合战力排行
    prod_count = (await session.execute(
        select(Product).where(Product.company_id == company.id)
    )).scalars().all()
    from db.models import ResearchProgress
    tech_count = (await session.execute(
        select(ResearchProgress).where(
            ResearchProgress.company_id == company.id,
            ResearchProgress.status == "completed",
        )
    )).scalars().all()
    power = (
        company.total_funds * 0.3
        + company.daily_revenue * 30
        + company.employee_count * 1000
        + len(tech_count) * 2000
        + len(prod_count) * 1500
        + company.level * 3000
    )
    await update_leaderboard("power", company.name, power)

    return report, event_messages


async def settle_all(session: AsyncSession) -> list[tuple[Company, DailyReport, list[str]]]:
    """Run daily settlement for all companies."""
    # Run data integrity checks before settlement
    from services.integrity_service import run_all_checks
    integrity_msgs = await run_all_checks(session)
    if integrity_msgs:
        logger.info("Integrity checks: %s", integrity_msgs)

    result = await session.execute(select(Company))
    companies = list(result.scalars().all())
    reports = []
    for company in companies:
        try:
            report, events = await settle_company(session, company)
            if report:
                reports.append((company, report, events))
        except Exception:
            logger.exception("Settlement failed for company %s", company.name)
    return reports


def format_daily_report(company: Company, report: DailyReport, events: list[str] | None = None) -> str:
    """Format a daily report for display."""
    from utils.formatters import fmt_traffic
    profit = report.total_income - report.operating_cost
    lines = [
        f"📊 【{company.name}】每日结算报告",
        f"日期: {report.date}",
        f"{'─' * 24}",
        f"产品收入: {fmt_traffic(report.product_income)}",
        f"👷 人力产出: +{fmt_traffic(report.employee_income)}",
        f"合作加成: +{fmt_traffic(report.cooperation_bonus)}",
        f"地产收入: +{fmt_traffic(report.realestate_income)}",
        f"声望加成: +{fmt_traffic(report.reputation_buff_income)}",
        f"{'─' * 24}",
        f"总收入: {fmt_traffic(report.total_income)}",
        f"运营成本(含税/薪/社保): -{fmt_traffic(report.operating_cost)}",
        f"净利润: {fmt_traffic(profit)}",
        f"分红支出: {fmt_traffic(report.dividend_paid)}",
        f"{'─' * 24}",
    ]
    if events:
        lines.append("🎲 今日事件:")
        lines.extend(events)
    return "\n".join(lines)
