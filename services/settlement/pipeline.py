"""Settlement pipeline functions.

This module provides the individual pipeline steps for daily company settlement,
each step is a pure or near-pure function that can be tested independently.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_client import get_redis
from config import settings
from db.models import Company, User
from services.settlement.breakdowns import (
    CostBreakdown,
    IncomeBreakdown,
    PenaltyBreakdown,
    SettlementResult,
)


async def compute_base_income(
    session: AsyncSession,
    company: Company,
    profile,
    market: dict,
    multipliers: dict,
) -> IncomeBreakdown:
    """步骤1: 计算基础收入（只读数据库）。

    Args:
        session: 数据库会话
        company: 公司对象
        profile: 公司运营档案
        market: 市场趋势数据
        multipliers: 运营乘数

    Returns:
        收入明细
    """
    from services.ad_service import get_ad_boost
    from services.company_service import (
        calc_employee_income,
        get_company_employee_limit,
        get_company_type_info,
        get_level_revenue_bonus,
    )
    from services.cooperation_service import get_cooperation_bonus
    from services.realestate_service import get_total_estate_income
    from services.research_service import get_research_buffs
    from services.shop_service import get_income_buff_multiplier
    from utils.formatters import reputation_buff_multiplier

    breakdown = IncomeBreakdown()
    research_buffs = await get_research_buffs(session, company.id)

    # 1. Product income (base)
    breakdown.product_income = int(
        company.daily_revenue * multipliers["income_mult"] * market["income_mult"]
    )

    # 2. Level bonus (permanent)
    breakdown.level_bonus = get_level_revenue_bonus(company.level)

    # 3. Cooperation bonus
    coop_bonus_rate = await get_cooperation_bonus(session, company.id)
    breakdown.cooperation_bonus = int(breakdown.product_income * coop_bonus_rate)

    # 4. Real estate income
    breakdown.realestate_income = await get_total_estate_income(session, company.id)

    # 5. Reputation buff
    owner = await session.get(User, company.owner_id)
    rep_multiplier = reputation_buff_multiplier(owner.reputation) if owner else 1.0
    breakdown.reputation_buff = int(breakdown.product_income * (rep_multiplier - 1.0))

    # 6. Advertising boost
    ad_boost_rate = await get_ad_boost(company.id)
    breakdown.ad_boost = int(breakdown.product_income * ad_boost_rate)

    # 7. Shop buff (market_analysis)
    shop_buff_mult = await get_income_buff_multiplier(company.id)
    breakdown.shop_buff = int(breakdown.product_income * (shop_buff_mult - 1.0))

    # 8. Total war buff + battle aftermath buff
    r = await get_redis()
    totalwar_buff_str = await r.get(f"totalwar_buff:{company.id}")
    totalwar_buff_rate = float(totalwar_buff_str) if totalwar_buff_str else 0.0
    aftermath_buff_str = await r.get(f"battle_aftermath_buff:{company.id}")
    aftermath_buff_rate = float(aftermath_buff_str) if aftermath_buff_str else 0.0
    combined_buff_rate = totalwar_buff_rate + aftermath_buff_rate
    breakdown.totalwar_buff = int(breakdown.product_income * combined_buff_rate)

    # 9. Company type buff
    type_info = get_company_type_info(company.company_type)
    type_income_bonus = type_info.get("income_bonus", 0.0) if type_info else 0.0
    breakdown.type_bonus = int(breakdown.product_income * type_income_bonus)

    # 10. Employee workforce income
    employee_limit = get_company_employee_limit(
        company.level,
        company.company_type,
        research_employee_bonus=int(research_buffs.get("employee_limit", 0)),
    )
    emp_base_output, emp_efficiency_bonus = calc_employee_income(
        company.employee_count,
        breakdown.product_income,
        employee_limit=employee_limit,
    )
    breakdown.employee_income = emp_base_output + emp_efficiency_bonus

    # 11. Immoral buff (缺德buff)
    from services.operations_service import calc_immoral_buff
    immoral_mult = calc_immoral_buff(profile.ethics)
    if immoral_mult > 1.0:
        breakdown.immoral_buff = int(breakdown.product_income * (immoral_mult - 1.0))

    # 12. Research buff (科研加成)
    income_bonus = research_buffs.get("income_bonus", 0.0)
    if income_bonus > 0:
        breakdown.research_buff = int(breakdown.product_income * income_bonus)

    return breakdown


async def apply_penalties(
    company_id: int,
    product_income: int,
) -> tuple[PenaltyBreakdown, int]:
    """步骤2: 应用惩罚（只读Redis，返回调整后的产品收入）。

    Args:
        company_id: 公司ID
        product_income: 产品基础收入

    Returns:
        (惩罚明细, 调整后的产品收入)
    """
    from services.battle_service import get_company_revenue_debuff

    breakdown = PenaltyBreakdown()
    r = await get_redis()

    # 1. Rename penalty (当日营收降低)
    rename_penalty_str = await r.get(f"rename_penalty:{company_id}")
    if rename_penalty_str:
        rename_penalty_rate = float(rename_penalty_str)
        breakdown.rename_penalty = int(product_income * rename_penalty_rate)
        await r.delete(f"rename_penalty:{company_id}")

    # 2. Battle debuff (until next settlement)
    battle_debuff_rate = await get_company_revenue_debuff(company_id)
    if battle_debuff_rate > 0:
        breakdown.battle_debuff = int(product_income * battle_debuff_rate)

    # 2b. Demon event debuff/buff
    from services.demon_event_service import get_demon_revenue_debuff, get_demon_revenue_buff
    demon_debuff_rate = await get_demon_revenue_debuff(company_id)
    if demon_debuff_rate > 0:
        breakdown.battle_debuff += int(product_income * demon_debuff_rate)
    demon_buff_rate = await get_demon_revenue_buff(company_id)
    if demon_buff_rate > 0:
        breakdown.totalwar_buff += int(breakdown.product_income * demon_buff_rate)

    # 3. Roadshow satire penalty (one-time)
    roadshow_penalty_str = await r.get(f"roadshow_penalty:{company_id}")
    if roadshow_penalty_str:
        try:
            roadshow_penalty_rate = float(roadshow_penalty_str)
            breakdown.roadshow_penalty = int(product_income * roadshow_penalty_rate)
        except (TypeError, ValueError):
            pass
        await r.delete(f"roadshow_penalty:{company_id}")

    # 4. Brand conflict penalty
    conflict_index_key = f"brand_conflicts:{company_id}"
    conflict_pids = await r.smembers(conflict_index_key)
    if conflict_pids:
        import json
        total_conflict_rate = 0.0
        for pid_str in conflict_pids:
            conflict_key = f"brand_conflict:{company_id}:{pid_str}"
            raw = await r.get(conflict_key)
            if raw:
                try:
                    data = json.loads(raw)
                    total_conflict_rate += data.get("penalty_rate", 0.0)
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                # Expired — clean up index
                await r.srem(conflict_index_key, pid_str)
        total_conflict_rate = min(0.50, total_conflict_rate)
        if total_conflict_rate > 0:
            breakdown.brand_conflict_penalty = int(product_income * total_conflict_rate)

    # Adjusted income
    adjusted_income = max(0, product_income - breakdown.total)

    return breakdown, adjusted_income


def compute_costs(
    income: IncomeBreakdown,
    company: Company,
    profile,
    type_info: dict | None,
    extra_costs: dict,
    regulation_fine: int,
    *,
    research_buffs: dict[str, float] | None = None,
) -> CostBreakdown:
    """步骤3: 计算成本（纯函数）。

    Args:
        income: 收入明细
        company: 公司对象
        profile: 公司运营档案
        type_info: 公司类型信息
        extra_costs: 额外运营成本
        regulation_fine: 监管罚款

    Returns:
        成本明细
    """
    breakdown = CostBreakdown()

    # Tax (on gross income)
    breakdown.tax = int(income.total * settings.tax_rate)

    # Employee salary + social insurance
    breakdown.salary = company.employee_count * settings.employee_salary_base
    breakdown.social_insurance = int(breakdown.salary * settings.social_insurance_rate)

    # Base operating cost
    breakdown.base_operating = int(income.total * settings.daily_operating_cost_pct)

    # Company type cost modifier
    type_cost_bonus = type_info.get("cost_bonus", 0.0) if type_info else 0.0
    base_before_type = breakdown.base_operating + breakdown.salary + breakdown.social_insurance
    breakdown.type_cost_modifier = int(base_before_type * type_cost_bonus)

    # Extra costs from operations profile
    breakdown.office_cost = extra_costs.get("office_cost", 0)
    breakdown.training_cost = extra_costs.get("training_cost", 0)
    breakdown.regulation_cost = extra_costs.get("regulation_cost", 0)
    breakdown.insurance_cost = extra_costs.get("insurance_cost", 0)
    breakdown.work_cost_adjust = extra_costs.get("work_cost_adjust", 0)
    breakdown.culture_maintenance = extra_costs.get("culture_maintenance", 0)

    # Regulation fine
    breakdown.regulation_fine = regulation_fine

    # Research buff: cost reduction
    cost_reduction = float((research_buffs or {}).get("cost_reduction", 0.0))
    if cost_reduction > 0:
        reducible = breakdown.base_operating + breakdown.office_cost + breakdown.training_cost
        saving = int(reducible * cost_reduction)
        breakdown.base_operating = max(0, breakdown.base_operating - saving)

    return breakdown


async def finalize_settlement(
    session: AsyncSession,
    company: Company,
    income: IncomeBreakdown,
    penalties: PenaltyBreakdown,
    costs: CostBreakdown,
) -> SettlementResult:
    """步骤4: 最终结算（写入数据库）。

    Args:
        session: 数据库会话
        company: 公司对象
        income: 收入明细
        penalties: 惩罚明细
        costs: 成本明细

    Returns:
        结算结果
    """
    from sqlalchemy import update

    from services.company_service import add_funds
    from services.dividend_service import distribute_dividends

    gross = income.total
    net = gross  # Net is after costs
    profit = gross - costs.total

    # Apply net profit/loss to company funds
    if profit != 0:
        success = await add_funds(session, company.id, profit)
        if not success and profit < 0:
            # 亏损超过现有积分，将积分清零
            old_ver = company.version
            await session.execute(
                update(Company)
                .where(Company.id == company.id, Company.version == old_ver)
                .values(cp_points=0, version=Company.version + 1)
            )
            await session.refresh(company)

    # Distribute dividends
    distributions = await distribute_dividends(session, company, profit)
    total_dividend = sum(amt for _, amt in distributions)

    return SettlementResult(
        income=income,
        penalties=penalties,
        costs=costs,
        gross_income=gross,
        net_income=net,
        profit=profit,
        events=[],
    )
