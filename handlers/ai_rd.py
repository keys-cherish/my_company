"""产品迭代处理器 — 简化版：一键概率提升收入 + AI段子。"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cache.redis_client import get_redis
from config import settings
from db.engine import async_session
from keyboards.menus import tag_kb
from services.ai_rd_service import (
    TIERS,
    generate_upgrade_blurb,
    get_rd_cost,
    quick_iterate,
)
from services.company_service import add_funds, get_company_by_id
from services.product_service import get_company_products
from services.user_service import get_user_by_tg_id
from utils.formatters import fmt_duration, fmt_traffic
from utils.panel_owner import mark_panel

router = Router()


def _rd_daily_limit() -> int:
    return max(1, int(settings.ai_rd_daily_limit))


def _rd_product_cd_seconds() -> int:
    return max(0, int(settings.ai_rd_product_cooldown_seconds))


def _rd_company_cd_seconds() -> int:
    return max(0, int(settings.ai_rd_company_cooldown_seconds))


def _app_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.app_timezone or "Asia/Shanghai")
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _seconds_until_local_day_reset() -> int:
    now = _dt.datetime.now(_app_tz())
    tomorrow = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


# ── 入口：选择产品 ────────────────────────────────────

@router.callback_query(F.data.startswith("aird:start:"))
async def cb_aird_start(callback: types.CallbackQuery):
    """开始产品迭代：选择要迭代的产品。"""
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        company = await get_company_by_id(session, company_id)
        if not company or not user or company.owner_id != user.id:
            await callback.answer("只有公司老板才能迭代产品", show_alert=True)
            return
        products = await get_company_products(session, company_id)

    if not products:
        await callback.answer("公司还没有产品，请先创建产品", show_alert=True)
        return

    # 检查每日限额
    r = await get_redis()
    daily_limit = _rd_daily_limit()
    daily_key = f"rd_daily:{company_id}"
    daily_count = int(await r.get(daily_key) or 0)
    remaining = max(0, daily_limit - daily_count)
    company_cd_key = f"rd_company_cd:{company_id}"
    company_cd_ttl = int(await r.ttl(company_cd_key) or 0)

    buttons = []
    for p in products:
        cost = get_rd_cost(p)
        buttons.append([InlineKeyboardButton(
            text=f"{p.name} v{p.version} (💰{cost}) 日收入:{p.daily_income}",
            callback_data=f"aird:confirm:{p.id}:{company_id}",
        )])
    buttons.append([InlineKeyboardButton(text="🔙 返回公司", callback_data=f"company:view:{company_id}")])

    # 概率说明
    tier_info = " | ".join(f"{t[5]}{t[6]}({t[0]}%)" for t in TIERS)

    await callback.message.edit_text(
        f"🧪 产品迭代\n"
        f"{'─' * 24}\n"
        f"概率：{tier_info}\n"
        f"今日剩余：{remaining}/{daily_limit}次\n"
        f"{'🏢 公司冷却：' + fmt_duration(company_cd_ttl) if company_cd_ttl > 0 else '🏢 公司冷却：可迭代'}\n"
        f"{'─' * 24}\n"
        f"选择要迭代的产品：",
        reply_markup=tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), tg_id),
    )
    await callback.answer()


# ── 确认面板 ──────────────────────────────────────────

@router.callback_query(F.data.startswith("aird:confirm:"))
async def cb_aird_confirm(callback: types.CallbackQuery):
    """显示迭代确认面板，包含费用和概率信息。"""
    parts = callback.data.split(":")
    product_id = int(parts[2])
    company_id = int(parts[3])
    tg_id = callback.from_user.id

    r = await get_redis()

    # 冷却检查
    cd_key = f"rd_cd:{product_id}"
    cd_ttl = await r.ttl(cd_key)
    if cd_ttl > 0:
        await callback.answer(f"该产品冷却中，剩余 {fmt_duration(cd_ttl)}", show_alert=True)
        return

    company_cd_key = f"rd_company_cd:{company_id}"
    company_cd_ttl = await r.ttl(company_cd_key)
    if company_cd_ttl > 0:
        await callback.answer(f"公司迭代冷却中，剩余 {fmt_duration(company_cd_ttl)}", show_alert=True)
        return

    # 每日限额检查
    daily_limit = _rd_daily_limit()
    daily_key = f"rd_daily:{company_id}"
    daily_count = int(await r.get(daily_key) or 0)
    if daily_count >= daily_limit:
        await callback.answer(f"今日迭代次数已达上限（{daily_limit}次/天）", show_alert=True)
        return

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        company = await get_company_by_id(session, company_id)
        if not company or not user or company.owner_id != user.id:
            await callback.answer("无权操作", show_alert=True)
            return
        from db.models import Product
        product = await session.get(Product, product_id)
        if not product or product.company_id != company_id:
            await callback.answer("产品不存在", show_alert=True)
            return

    cost = get_rd_cost(product)

    lines = [
        f"🧪 产品迭代确认",
        f"{'─' * 24}",
        f"产品：{product.name} v{product.version}",
        f"当前日收入：{fmt_traffic(product.daily_income)}",
        f"迭代费用：{fmt_traffic(cost)}",
        f"🏦 公司积分：{fmt_traffic(company.cp_points)}",
        f"{'─' * 24}",
        f"📦 小幅改进(40%) | 📈 稳步提升(30%)",
        f"🌟 重大突破(20%) | 🏆 创新飞跃(10%)",
        f"{'─' * 24}",
        f"⏱ 产品冷却：{fmt_duration(_rd_product_cd_seconds())}",
    ]
    if _rd_company_cd_seconds() > 0:
        lines.append(f"🏢 公司冷却：{fmt_duration(_rd_company_cd_seconds())}")
    if cost > company.cp_points:
        lines.append(f"❌ 积分不足！还差 {fmt_traffic(cost - company.cp_points)}")

    kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✅ 确认迭代（{fmt_traffic(cost)}）",
                callback_data=f"aird:exec:{product_id}:{company_id}",
            ),
            InlineKeyboardButton(text="🔙 返回", callback_data=f"aird:start:{company_id}"),
        ],
    ]), tg_id)
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await callback.answer()


# ── 执行迭代 ──────────────────────────────────────────

@router.callback_query(F.data.startswith("aird:exec:"))
async def cb_aird_exec(callback: types.CallbackQuery):
    """执行迭代：扣费 → 概率滚动 → 显示结果 + AI段子。"""
    parts = callback.data.split(":")
    product_id = int(parts[2])
    company_id = int(parts[3])
    tg_id = callback.from_user.id

    r = await get_redis()
    product_cd_seconds = _rd_product_cd_seconds()
    company_cd_seconds = _rd_company_cd_seconds()
    daily_limit = _rd_daily_limit()

    # 再次检查冷却和限额（防止并发点击）
    cd_key = f"rd_cd:{product_id}"
    if product_cd_seconds > 0 and await r.exists(cd_key):
        await callback.answer("该产品冷却中", show_alert=True)
        return
    company_cd_key = f"rd_company_cd:{company_id}"
    if company_cd_seconds > 0 and await r.exists(company_cd_key):
        ttl = await r.ttl(company_cd_key)
        await callback.answer(f"公司迭代冷却中，剩余 {fmt_duration(max(0, ttl))}", show_alert=True)
        return
    daily_key = f"rd_daily:{company_id}"
    daily_count = int(await r.get(daily_key) or 0)
    if daily_count >= daily_limit:
        await callback.answer(f"今日迭代次数已达上限（{daily_limit}次/天）", show_alert=True)
        return

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await callback.answer("用户不存在", show_alert=True)
                return
            company = await get_company_by_id(session, company_id)
            if not company or company.owner_id != user.id:
                await callback.answer("无权操作", show_alert=True)
                return

            from db.models import Product
            product = await session.get(Product, product_id)
            if not product or product.company_id != company_id:
                await callback.answer("产品不存在", show_alert=True)
                return

            # 扣费
            cost = get_rd_cost(product)
            ok = await add_funds(session, company_id, -cost)
            if not ok:
                await callback.answer(f"公司积分不足，需要 {fmt_traffic(cost)}", show_alert=True)
                return

            # 执行迭代
            ok, msg, income_increase, tier_key = await quick_iterate(
                session, product_id, user.id,
            )
            product_name = product.name

    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    # 设置冷却和增加每日计数
    if product_cd_seconds > 0:
        await r.setex(cd_key, product_cd_seconds, "1")
    if company_cd_seconds > 0:
        await r.setex(company_cd_key, company_cd_seconds, "1")
    await r.incr(daily_key)
    if await r.ttl(daily_key) < 0:
        await r.expire(daily_key, _seconds_until_local_day_reset() + 60)

    # 生成AI段子（非阻塞，失败不影响结果）
    tier_label = ""
    for t in TIERS:
        if t[4] == tier_key:
            tier_label = t[6]
            break
    blurb = await generate_upgrade_blurb(product_name, income_increase, tier_label)

    result_kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 继续迭代", callback_data=f"aird:start:{company_id}")],
        [InlineKeyboardButton(text="🔙 返回公司", callback_data=f"company:view:{company_id}")],
    ]), tg_id)

    await callback.message.edit_text(
        f"🧪 迭代完成！\n"
        f"{'─' * 24}\n"
        f"{msg}\n"
        f"💰 花费：{fmt_traffic(cost)}\n"
        f"{'─' * 24}\n"
        f"💬 {blurb}",
        reply_markup=result_kb,
    )
    await callback.answer()
