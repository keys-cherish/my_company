"""公司共享函数/常量/FSM状态 — 从 handlers/company.py 提取。"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import types
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from cache.redis_client import get_redis
from config import settings as cfg

_logger = logging.getLogger(__name__)
from db.engine import async_session
from db.models import DailyReport, Product, User
from keyboards.menus import company_detail_kb, tag_kb
from services.ad_service import get_ad_boost
from services.battle_service import get_company_revenue_debuff
from services.company_service import (
    get_company_by_id,
    get_company_employee_limit,
    get_company_type_info,
    get_level_info,
    get_max_level,
    get_level_revenue_bonus,
    load_company_types,
)
from services.cooperation_service import get_cooperation_bonus
from services.operations_service import (
    INSURANCE_LEVELS,
    LEGAL_WORK_HOURS,
    bar10,
    calc_immoral_buff,
    calc_extra_operating_costs,
    ethics_rating,
    get_market_trend,
    get_operation_multipliers,
    get_or_create_profile,
    get_training_info,
    load_recent_events,
    reputation_rating,
)
from services.product_service import get_max_products
from services.realestate_service import get_total_estate_income
from services.research_service import (
    get_effective_research_duration_seconds,
    get_in_progress_research,
    get_tech_tree_display,
    sync_research_progress_if_due,
)
from services.shop_service import get_income_buff_multiplier
from services.user_service import get_user_by_tg_id
from utils.formatters import fmt_duration, fmt_quota, fmt_points, reputation_buff_multiplier
from utils.panel_owner import mark_panel


# ── FSM 状态组 ────────────────────────────────────────

class CreateCompanyState(StatesGroup):
    waiting_type = State()
    waiting_name = State()


class RenameCompanyState(StatesGroup):
    waiting_new_name = State()


# ── 改名常量 ──────────────────────────────────────────

RENAME_COST_RATE = 0.05  # 改名费用 = 公司积分 * 5%
RENAME_MIN_COST = 5000   # 最低5000积分
RENAME_REVENUE_PENALTY = 0.50  # 改名当日营收降低50%
RENAME_COOLDOWN = 86400  # 改名冷却 24小时


# ── 共享函数 ──────────────────────────────────────────

async def _safe_edit_or_send(callback: types.CallbackQuery, text: str, reply_markup=None):
    """Prefer editing current panel; only send new message when edit is impossible.
    Auto-retries once on Telegram rate limit.
    """
    for attempt in range(2):
        try:
            await callback.message.edit_text(text, reply_markup=reply_markup)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
        except TelegramRetryAfter as e:
            if attempt == 0:
                _logger.warning("TG限流，等待 %d 秒后重试", e.retry_after)
                await asyncio.sleep(e.retry_after)
                continue
            await callback.answer(f"被TG限流，请{e.retry_after}秒后再试", show_alert=True)
            return
        except Exception:
            break  # Fall through to send a fresh panel

    try:
        sent = await callback.message.answer(text, reply_markup=reply_markup)
        await mark_panel(sent.chat.id, sent.message_id, callback.from_user.id)
    except TelegramRetryAfter as e:
        _logger.warning("TG限流（answer），等待 %d 秒", e.retry_after)
        await callback.answer(f"被TG限流，请{e.retry_after}秒后再试", show_alert=True)


async def render_company_detail(company_id: int, tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """加载公司数据并返回 (text, keyboard)，供多个handler复用。"""
    async with async_session() as session:
        company = await get_company_by_id(session, company_id)
        if not company:
            return "公司不存在", InlineKeyboardMarkup(inline_keyboard=[])
        user = await get_user_by_tg_id(session, tg_id)
        is_owner = user and company.owner_id == user.id
        owner = await session.get(User, company.owner_id)
        products = (await session.execute(
            select(Product).where(Product.company_id == company_id).order_by(Product.quality.desc(), Product.id.asc())
        )).scalars().all()
        prod_count = len(products)
        estate_income = await get_total_estate_income(session, company_id)
        coop_bonus_rate = await get_cooperation_bonus(session, company_id)
        battle_debuff_rate = await get_company_revenue_debuff(company_id)
        ad_boost_rate = await get_ad_boost(company_id)
        shop_buff_mult = await get_income_buff_multiplier(company_id)

        # Check rename penalty from Redis
        _r = await get_redis()
        _rename_pen_str = await _r.get(f"rename_penalty:{company_id}")
        rename_penalty_rate = float(_rename_pen_str) if _rename_pen_str else 0.0
        _roadshow_pen_str = await _r.get(f"roadshow_penalty:{company_id}")
        roadshow_penalty_rate = float(_roadshow_pen_str) if _roadshow_pen_str else 0.0
        _totalwar_str = await _r.get(f"totalwar_buff:{company_id}")
        totalwar_buff_rate = float(_totalwar_str) if _totalwar_str else 0.0
        profile = await get_or_create_profile(session, company_id)
        # 获取进行中的科研
        await sync_research_progress_if_due(session, company_id)
        in_progress_research = await get_in_progress_research(session, company_id)
        # LOCALTIMESTAMP matches started_at storage timezone
        from sqlalchemy import text as sa_text
        now_db = (await session.execute(select(sa_text("LOCALTIMESTAMP")))).scalar()
        if now_db is None:
            now_db = dt.datetime.utcnow()

    type_info = get_company_type_info(company.company_type)
    type_display = f"{type_info['emoji']} {type_info['name']}" if type_info else company.company_type

    level_info = get_level_info(company.level)
    level_name = level_info["name"] if level_info else f"Lv.{company.level}"
    max_level = max(1, get_max_level())
    if company.level >= max_level:
        level_badge = "👑"
    elif company.level >= 8:
        level_badge = "💎"
    elif company.level >= 6:
        level_badge = "🏆"
    elif company.level >= 4:
        level_badge = "🥇"
    elif company.level >= 2:
        level_badge = "🥈"
    else:
        level_badge = "🥉"
    level_rev_bonus = get_level_revenue_bonus(company.level)
    max_employees = get_company_employee_limit(company.level, company.company_type)

    now_utc = dt.datetime.now(dt.UTC)
    market = get_market_trend(company, now_utc)
    multipliers = get_operation_multipliers(profile, now_utc)
    work_info = multipliers["work"]
    office_info = multipliers["office"]
    training_info = multipliers["training"]
    insurance_info = INSURANCE_LEVELS.get(profile.insurance_level, INSURANCE_LEVELS["basic"])
    culture_income_rate = multipliers["culture_bonus_mult"] - 1.0
    culture_risk_reduce_rate = (profile.culture / 100) * 0.30
    if profile.work_hours > LEGAL_WORK_HOURS:
        reg_daily_delta = (profile.work_hours - LEGAL_WORK_HOURS) * 4
    else:
        reg_daily_delta = -(2 + (1 if profile.work_hours <= 6 else 0))

    if training_info["active"]:
        train_status = "✅ 生效中"
        train_mult = f"×{training_info['income_mult']:.2f}"
        expires_at = training_info.get("expires_at")
        if expires_at:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=dt.UTC)
            remain = max(0, int((expires_at - now_utc).total_seconds()))
            train_tail = f" | 剩余 {fmt_duration(remain)}"
    elif profile.training_level != "none" and profile.training_expires_at:
        train_status = "⚠️ 已过期"
        train_mult = "×1.00"
        train_tail = ""
    else:
        train_status = "❌ 无"
        train_mult = "×1.00"
        train_tail = ""

    # Work hours status indicator
    if profile.work_hours == LEGAL_WORK_HOURS:
        work_status = "✅"
    elif profile.work_hours > LEGAL_WORK_HOURS:
        work_status = "⚠️"
    else:
        work_status = "📉"

    # Office level indicator
    office_levels = {"standard": "🔹", "premium": "🔷", "luxury": "💎"}
    office_badge = office_levels.get(profile.office_level, "🔹")

    # Insurance level indicator
    ins_badges = {"basic": "🔹", "plus": "🔷", "supreme": "💎"}
    ins_badge = ins_badges.get(profile.insurance_level, "🔹")

    product_income = int(company.daily_revenue * multipliers["income_mult"] * market["income_mult"])
    if battle_debuff_rate > 0:
        product_income = max(0, int(product_income * (1.0 - battle_debuff_rate)))
    if rename_penalty_rate > 0:
        product_income = max(0, int(product_income * (1.0 - rename_penalty_rate)))
    cooperation_bonus = int(product_income * coop_bonus_rate)
    rep_multiplier = reputation_buff_multiplier(owner.reputation) if owner else 1.0
    reputation_buff_income = int(product_income * (rep_multiplier - 1.0))
    ad_boost_income = int(product_income * ad_boost_rate)
    shop_buff_income = int(product_income * (shop_buff_mult - 1.0))
    type_income_bonus = type_info.get("income_bonus", 0.0) if type_info else 0.0
    type_income = int(product_income * type_income_bonus)
    immoral_mult = calc_immoral_buff(profile.ethics)
    immoral_buff_income = int(product_income * (immoral_mult - 1.0)) if immoral_mult > 1.0 else 0

    # Buff summary (for concise company panel):
    # - gain/loss represent additive rate summary of active effects (excluding market cycle shown elsewhere).
    effect_rates = [
        multipliers["income_mult"] - 1.0,         # work/office/training/culture combined
        rep_multiplier - 1.0,                     # reputation
        coop_bonus_rate,                          # cooperation
        ad_boost_rate,                            # ad
        shop_buff_mult - 1.0,                     # shop buff
        type_income_bonus,                        # company type income bonus
        (immoral_mult - 1.0) if immoral_mult > 1.0 else 0.0,
        totalwar_buff_rate,                       # total war temporary buff
        -battle_debuff_rate,                      # battle debuff
        -rename_penalty_rate,                     # rename penalty
        -roadshow_penalty_rate,                   # roadshow penalty
    ]
    buff_gain_rate = sum(v for v in effect_rates if v > 0)
    buff_loss_rate = sum(-v for v in effect_rates if v < 0)
    buff_net_rate = buff_gain_rate - buff_loss_rate
    active_effect_count = sum(1 for v in effect_rates if abs(v) >= 0.001)
    estimated_income = (
        product_income
        + level_rev_bonus
        + cooperation_bonus
        + estate_income
        + reputation_buff_income
        + ad_boost_income
        + shop_buff_income
        + type_income
        + immoral_buff_income
    )
    tax = int(estimated_income * cfg.tax_rate)
    salary_cost = company.employee_count * cfg.employee_salary_base
    social_insurance = int(salary_cost * cfg.social_insurance_rate)
    type_cost_bonus = type_info.get("cost_bonus", 0.0) if type_info else 0.0
    base_operating = int(
        (int(estimated_income * cfg.daily_operating_cost_pct) + salary_cost + social_insurance)
        * (1.0 + type_cost_bonus)
    )
    extra_costs = calc_extra_operating_costs(
        profile,
        company.employee_count,
        estimated_income,
        salary_cost,
        social_insurance,
        now_utc,
    )
    estimated_cost = (
        base_operating
        + tax
        + extra_costs["office_cost"]
        + extra_costs["training_cost"]
        + extra_costs["regulation_cost"]
        + extra_costs["insurance_cost"]
        + extra_costs["work_cost_adjust"]
        + extra_costs["culture_maintenance"]
    )
    estimated_profit = estimated_income - estimated_cost

    # 科研进度文本
    research_block = ""
    if in_progress_research:
        tree = {t["tech_id"]: t for t in get_tech_tree_display()}
        rlines = []
        for rp in in_progress_research:
            tech_info = tree.get(rp.tech_id, {})
            name = tech_info.get("name", rp.tech_id)
            duration_sec = get_effective_research_duration_seconds(
                tech_info, company.company_type, rp.tech_id,
            )
            started = rp.started_at.replace(tzinfo=None) if rp.started_at.tzinfo else rp.started_at
            elapsed = (now_db - started).total_seconds()
            remaining = max(0, int(duration_sec - elapsed))
            if remaining > 0:
                rlines.append(f"  • {name} — 剩余 {fmt_duration(remaining)}")
            else:
                rlines.append(f"  • {name} — 即将完成")
        research_block = "⏳ 研究中:\n" + "\n".join(rlines) + "\n"

    products_block: list[str] = []
    if products:
        for p in products[:3]:
            icon = "🚀" if p.quality >= 90 else "🔬"
            products_block.append(f"  {icon} {p.name} ⭐{p.quality} 💰{fmt_quota(p.daily_income)}/日")
        if len(products) > 3:
            products_block.append(f"  ...还有 {len(products) - 3} 个产品")
    else:
        products_block.append("  暂无产品")

    recent_events = await load_recent_events(company_id, limit=3)
    events_block = [f"  · {e}" for e in recent_events] if recent_events else ["  · 暂无事件"]
    rep_value = owner.reputation if owner else 0
    if market["income_mult"] > 1.0:
        market_effect = f"（景气加成 +{(market['income_mult'] - 1.0) * 100:.0f}%）"
    elif market["income_mult"] < 1.0:
        market_effect = f"（景气减成 {(market['income_mult'] - 1.0) * 100:.0f}%）"
    else:
        market_effect = "（景气无加成）"

    text = (
        f"🏢 {company.name}\n\n"
        f"🖥️ 行业：{type_display} {market['label']} {market_effect}\n"
        f"📊 等级：{level_badge}【Lv.{company.level} {level_name}】[{bar10(company.level, 1, max_level)}]\n"
        f"⭐ 声望：{rep_value}（评级 {reputation_rating(rep_value)}）\n"
        f"👥 员工：{company.employee_count}/{max_employees}\n"
        f"💰 积分余额：{fmt_quota(company.cp_points)}\n"
        f"😐 道德：{profile.ethics} [{bar10(profile.ethics, -100, 100)}] {ethics_rating(profile.ethics)}\n"
        f"⏰ 工时：{profile.work_hours}h {work_status} {work_info['label']}\n"
        f"   营收×{work_info['income_mult']:.2f} | 成本×{work_info['cost_mult']:.2f} | 道德{work_info['ethics_delta']:+d}/日\n"
        f"🏢 办公：{office_badge} {office_info['name']} | 营收×{office_info['income_mult']:.2f} | {office_info['employee_cost']}/人/日\n"
        f"🏅 培训：{training_info['name']} {train_status} | 营收{train_mult}{train_tail}\n"
        f"👑 保险：{ins_badge} {insurance_info['name']} | 减免{int(insurance_info['fine_reduction'] * 100)}% | 费率{insurance_info['cost_rate'] * 100:.1f}%\n"
        f"🎭 文化：{profile.culture}/100 [{bar10(profile.culture)}] | 营收+{culture_income_rate*100:.1f}% | 风险-{culture_risk_reduce_rate*100:.1f}%\n"
        f"🛂 监管：{profile.regulation_pressure}/100 [{bar10(profile.regulation_pressure)}] | 每日变化 {reg_daily_delta:+d}\n"
        f"\n"
        f"📈 预估日营收：{fmt_quota(estimated_income)}\n"
        f"  产品收入：{fmt_quota(product_income)} | 地产收入：{fmt_quota(estate_income)}\n"
        f"📉 预估日成本：{fmt_quota(estimated_cost)}\n"
        f"💵 预估日净利：{'+' if estimated_profit >= 0 else ''}{fmt_quota(estimated_profit)}\n\n"
        f"✨ Buff总览：增益 +{buff_gain_rate*100:.0f}% | 减益 -{buff_loss_rate*100:.0f}% | "
        f"净影响 {'+' if buff_net_rate >= 0 else '-'}{abs(buff_net_rate)*100:.0f}%（{active_effect_count}项）\n"
        f"   详细说明请查看「✨ Buff一览」\n"
        f"{'─' * 24}\n"
        f"{research_block}"
        f"📦 产品（{prod_count}/{get_max_products(company.level)}个）：\n"
        f"{chr(10).join(products_block)}\n\n"
        f"📋 最近事件：\n"
        f"{chr(10).join(events_block)}\n"
    )
    return text, company_detail_kb(company_id, is_owner, tg_id=tg_id)


async def _refresh_company_view(callback: types.CallbackQuery, company_id: int):
    """操作后刷新公司面板消息。"""
    text, kb = await render_company_detail(company_id, callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass  # 消息未变化时edit会抛异常，忽略


def _finance_detail_kb(company_id: int, tg_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 刷新明细", callback_data=f"company:finance:{company_id}"),
            InlineKeyboardButton(text="🔙 返回公司", callback_data=f"company:view:{company_id}"),
        ],
    ])
    return tag_kb(kb, tg_id)


async def render_company_finance_detail(company_id: int, tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Render estimated income/cost breakdown plus latest settlement snapshot."""
    async with async_session() as session:
        company = await get_company_by_id(session, company_id)
        if not company:
            return "公司不存在", InlineKeyboardMarkup(inline_keyboard=[])

        user = await get_user_by_tg_id(session, tg_id)
        if not user or company.owner_id != user.id:
            return "只有公司老板可以查看收支明细", company_detail_kb(company_id, False, tg_id=tg_id)

        owner = await session.get(User, company.owner_id)
        type_info = get_company_type_info(company.company_type)
        estate_income = await get_total_estate_income(session, company_id)
        coop_bonus_rate = await get_cooperation_bonus(session, company_id)
        battle_debuff_rate = await get_company_revenue_debuff(company_id)
        ad_boost_rate = await get_ad_boost(company_id)
        shop_buff_mult = await get_income_buff_multiplier(company_id)
        profile = await get_or_create_profile(session, company_id)

        latest_report = (
            await session.execute(
                select(
                    DailyReport.date,
                    DailyReport.total_income,
                    DailyReport.operating_cost,
                    DailyReport.dividend_paid,
                )
                .where(DailyReport.company_id == company_id)
                .order_by(DailyReport.id.desc())
                .limit(1)
            )
        ).mappings().first()

    # Estimated income (same core formula as settlement, excluding one-shot random events).
    now_utc = dt.datetime.now(dt.UTC)
    market = get_market_trend(company, now_utc)
    multipliers = get_operation_multipliers(profile, now_utc)
    product_income = int(company.daily_revenue * multipliers["income_mult"] * market["income_mult"])
    if battle_debuff_rate > 0:
        product_income = max(0, int(product_income * (1.0 - battle_debuff_rate)))

    level_revenue_bonus = get_level_revenue_bonus(company.level)
    cooperation_bonus = int(product_income * coop_bonus_rate)
    rep_multiplier = reputation_buff_multiplier(owner.reputation) if owner else 1.0
    reputation_buff_income = int(product_income * (rep_multiplier - 1.0))
    ad_boost_income = int(product_income * ad_boost_rate)
    shop_buff_income = int(product_income * (shop_buff_mult - 1.0))
    totalwar_buff_income = 0  # uncertain at panel time; settlement applies Redis temporary buff.
    type_income_bonus = type_info.get("income_bonus", 0.0) if type_info else 0.0
    type_income = int(product_income * type_income_bonus)
    fin_immoral_mult = calc_immoral_buff(profile.ethics)
    fin_immoral_buff_income = int(product_income * (fin_immoral_mult - 1.0)) if fin_immoral_mult > 1.0 else 0

    estimated_income = (
        product_income
        + level_revenue_bonus
        + cooperation_bonus
        + estate_income
        + reputation_buff_income
        + ad_boost_income
        + shop_buff_income
        + totalwar_buff_income
        + type_income
        + fin_immoral_buff_income
    )

    # Estimated costs.
    tax = int(estimated_income * cfg.tax_rate)
    salary_cost = company.employee_count * cfg.employee_salary_base
    social_insurance = int(salary_cost * cfg.social_insurance_rate)
    overhead_cost = int(estimated_income * cfg.daily_operating_cost_pct)
    type_cost_bonus = type_info.get("cost_bonus", 0.0) if type_info else 0.0
    pre_type_cost = overhead_cost + salary_cost + social_insurance
    type_cost_adjust = int(pre_type_cost * (1.0 + type_cost_bonus)) - pre_type_cost

    extra_costs = calc_extra_operating_costs(
        profile,
        company.employee_count,
        estimated_income,
        salary_cost,
        social_insurance,
        now_utc,
    )
    estimated_cost = (
        pre_type_cost
        + type_cost_adjust
        + tax
        + extra_costs["office_cost"]
        + extra_costs["training_cost"]
        + extra_costs["regulation_cost"]
        + extra_costs["insurance_cost"]
        + extra_costs["work_cost_adjust"]
        + extra_costs["culture_maintenance"]
    )
    estimated_profit = estimated_income - estimated_cost

    lines = [
        f"📒 {company.name} — 收支明细（预估）",
        "─" * 24,
        "分红说明: 每日结算后按股权自动发放到个人积分",
        "",
        "【收入目录】",
        f"产品基础收入: {fmt_points(product_income)}",
        f"等级固定加成: +{fmt_points(level_revenue_bonus)}",
        f"合作加成: +{fmt_points(cooperation_bonus)}",
        f"地产收入: +{fmt_points(estate_income)}",
        f"声望加成: +{fmt_points(reputation_buff_income)}",
        f"广告加成: +{fmt_points(ad_boost_income)}",
        f"商城加成: +{fmt_points(shop_buff_income)}",
        f"公司类型加成: +{fmt_points(type_income)}",
    ]
    if fin_immoral_buff_income > 0:
        lines.append(f"😈 缺德buff: +{fmt_points(fin_immoral_buff_income)}")
    lines.extend([
        f"预估总收入: {fmt_points(estimated_income)}",
        "",
        "【成本目录】",
        f"基础运营(比例): -{fmt_points(overhead_cost)}",
        f"员工薪资: -{fmt_points(salary_cost)}",
        f"社保支出: -{fmt_points(social_insurance)}",
        f"公司类型成本修正: {'+' if type_cost_adjust >= 0 else ''}{fmt_points(type_cost_adjust)}",
        f"税费: -{fmt_points(tax)}",
        f"办公成本: -{fmt_points(extra_costs['office_cost'])}",
        f"培训成本: -{fmt_points(extra_costs['training_cost'])}",
        f"监管成本: -{fmt_points(extra_costs['regulation_cost'])}",
        f"保险成本: -{fmt_points(extra_costs['insurance_cost'])}",
        f"工时成本修正: {'+' if extra_costs['work_cost_adjust'] >= 0 else ''}{fmt_points(extra_costs['work_cost_adjust'])}",
        f"文化维护: -{fmt_points(extra_costs['culture_maintenance'])}",
        "随机罚款: 每日抽检工时，抽检>8h会触发重罚（不计入本预估）",
        f"预估总成本: {fmt_points(estimated_cost)}",
        "",
        f"💵 预估净利润: {'+' if estimated_profit >= 0 else ''}{fmt_points(estimated_profit)}",
    ])

    if latest_report:
        latest_profit = latest_report["total_income"] - latest_report["operating_cost"]
        lines += [
            "",
            "【最近一次结算】",
            f"日期: {latest_report['date']}",
            f"总收入: {fmt_points(latest_report['total_income'])}",
            f"总成本: -{fmt_points(latest_report['operating_cost'])}",
            f"净利润: {'+' if latest_profit >= 0 else ''}{fmt_points(latest_profit)}",
            f"分红支出: -{fmt_points(latest_report['dividend_paid'])}",
        ]
    else:
        lines += [
            "",
            "【最近一次结算】",
            "暂无历史结算记录。",
        ]

    return "\n".join(lines), _finance_detail_kb(company_id, tg_id)


async def _start_company_type_selection(message: types.Message, state: FSMContext, prefix: str = ""):
    """共用的公司类型选择面板。"""
    types_data = load_company_types()
    buttons = [
        [InlineKeyboardButton(
            text=f"{info['emoji']} {info['name']}",
            callback_data=f"company:type:{key}",
        )]
        for key, info in types_data.items()
    ]
    text = (
        f"{prefix}"
        "🏢 创建公司\n选择公司类型:\n\n" +
        "\n".join(f"{info['emoji']} {info['name']} — {info['description']}" for info in types_data.values())
    )
    sent = await message.reply(
        text,
        reply_markup=tag_kb(
            InlineKeyboardMarkup(inline_keyboard=buttons),
            message.from_user.id,
        ),
    )
    await mark_panel(message.chat.id, sent.message_id, message.from_user.id)
    await state.set_state(CreateCompanyState.waiting_type)


def _ops_menu_kb(company_id: int, tg_id: int, training_active: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="😴 6h", callback_data=f"ops:work:{company_id}:6"),
            InlineKeyboardButton(text="🏢 8h", callback_data=f"ops:work:{company_id}:8"),
            InlineKeyboardButton(text="🔥 10h", callback_data=f"ops:work:{company_id}:10"),
        ],
        [
            InlineKeyboardButton(text="💀 12h", callback_data=f"ops:work:{company_id}:12"),
            InlineKeyboardButton(text="☠️ 24h", callback_data=f"ops:work:{company_id}:24"),
        ],
        [
            InlineKeyboardButton(text="🏢 升级办公", callback_data=f"ops:cycle:{company_id}:office"),
            InlineKeyboardButton(text="👑 升级保险", callback_data=f"ops:cycle:{company_id}:insurance"),
        ],
        [
            InlineKeyboardButton(text="🎭 文化+8", callback_data=f"ops:cycle:{company_id}:culture"),
            InlineKeyboardButton(text="😐 道德+6", callback_data=f"ops:cycle:{company_id}:ethics"),
            InlineKeyboardButton(text="🛂 监管说明", callback_data=f"ops:cycle:{company_id}:regulation"),
        ],
        [
            InlineKeyboardButton(text="🏅 基础(×1.12)", callback_data=f"ops:train:{company_id}:basic"),
            InlineKeyboardButton(text="🏅 实训(×1.30)", callback_data=f"ops:train:{company_id}:pro"),
            InlineKeyboardButton(text="🏅 特训(×1.50)", callback_data=f"ops:train:{company_id}:elite"),
        ],
    ]
    if training_active:
        rows.append([InlineKeyboardButton(text="⛔ 停止培训", callback_data=f"ops:train:{company_id}:none")])
    rows.append([InlineKeyboardButton(text="🔙 返回公司", callback_data=f"company:view:{company_id}")])
    return tag_kb(InlineKeyboardMarkup(inline_keyboard=rows), tg_id)


async def _check_training_active(company_id: int) -> bool:
    """Check if training is currently active for a company."""
    async with async_session() as session:
        profile = await get_or_create_profile(session, company_id)
        info = get_training_info(profile, dt.datetime.now(dt.UTC))
        return info["active"]
