"""Real estate handlers — purchase, upgrade, list with confirmation dialogs."""

from __future__ import annotations

import logging

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db.engine import async_session
from keyboards.menus import tag_kb
from services.company_service import get_company_by_id
from services.realestate_service import (
    MAX_BUILDING_LEVEL,
    calc_level_income,
    calc_upgrade_cost,
    count_company_building_type,
    get_building_info,
    get_building_list,
    get_company_estates,
    purchase_building,
    upgrade_estate,
)
from services.user_service import get_user_by_tg_id
from utils.formatters import fmt_traffic

router = Router()
logger = logging.getLogger(__name__)


def _estate_list_kb(estates, buildings, company_id: int, tg_id: int, owned_counts: dict) -> InlineKeyboardMarkup:
    """Build the estate list keyboard with buy + upgrade buttons."""
    buttons = []

    # Upgrade buttons for owned estates (sorted by building type then level)
    if estates:
        sorted_estates = sorted(estates, key=lambda e: (e.building_type, e.level))
        for e in sorted_estates:
            if e.level < MAX_BUILDING_LEVEL:
                bld = get_building_info(e.building_type)
                cost = calc_upgrade_cost(bld, e.level) if bld else 0
                buttons.append([InlineKeyboardButton(
                    text=f"⬆️ {bld['name'] if bld else e.building_type} Lv.{e.level}→{e.level+1} ({fmt_traffic(cost)})",
                    callback_data=f"realestate:upg:{company_id}:{e.id}",
                )])

    # Buy buttons
    for b in buildings:
        owned = owned_counts.get(b["key"], 0)
        max_c = b.get("max_count", 99)
        if owned < max_c:
            buttons.append([InlineKeyboardButton(
                text=f"🏪 {b['name']} ({fmt_traffic(b['purchase_price'])}) [{owned}/{max_c}]",
                callback_data=f"realestate:buy:{company_id}:{b['key']}",
            )])

    buttons.append([InlineKeyboardButton(text="🔙 返回公司", callback_data=f"company:view:{company_id}")])
    return tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), tg_id)


async def _render_estate_list(company, estates, owned_counts: dict) -> str:
    """Render the estate list text."""
    lines = [f"🏗 {company.name} — 地产", "─" * 24]
    if estates:
        # Sort by building type then level for consistent display
        sorted_estates = sorted(estates, key=lambda e: (e.building_type, e.level))
        total_income = 0
        for e in sorted_estates:
            bld = get_building_info(e.building_type)
            name = bld["name"] if bld else e.building_type
            lines.append(f"• {name} Lv.{e.level} — {fmt_traffic(e.daily_dividend)}/日")
            total_income += e.daily_dividend
        lines.append(f"\n💰 总地产收入: {fmt_traffic(total_income)}/日")
    else:
        lines.append("暂无地产")

    lines.append(f"\n🏪 可购买地产（点击查看详情）:")
    buildings = get_building_list()
    # Sort by purchase price for consistent display
    buildings_sorted = sorted(buildings, key=lambda b: b["purchase_price"])
    for b in buildings_sorted:
        owned = owned_counts.get(b["key"], 0)
        max_c = b.get("max_count", 99)
        roi_days = b["purchase_price"] // b["daily_dividend"] if b["daily_dividend"] > 0 else 999
        lines.append(
            f"  {b['name']} — {fmt_traffic(b['purchase_price'])} → "
            f"{fmt_traffic(b['daily_dividend'])}/日 (回本{roi_days}天) [{owned}/{max_c}]"
        )
    return "\n".join(lines)


async def _refresh_estate_list(callback: types.CallbackQuery, company_id: int):
    """Refresh estate list after operation."""
    tg_id = callback.from_user.id
    try:
        async with async_session() as session:
            company = await get_company_by_id(session, company_id)
            if not company:
                return
            estates = await get_company_estates(session, company_id)
            buildings = get_building_list()
            owned_counts = {}
            for b in buildings:
                owned_counts[b["key"]] = await count_company_building_type(session, company_id, b["key"])

        text = await _render_estate_list(company, estates, owned_counts)
        kb = _estate_list_kb(estates, buildings, company_id, tg_id, owned_counts)
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@router.callback_query(F.data == "menu:realestate")
async def cb_realestate_menu(callback: types.CallbackQuery):
    """Auto-select company for real estate if only one, otherwise show selector."""
    tg_id = callback.from_user.id
    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await callback.answer("请先 /cp_create 创建公司", show_alert=True)
            return
        from services.company_service import get_companies_by_owner
        companies = await get_companies_by_owner(session, user.id)

    if not companies:
        await callback.answer("你还没有公司", show_alert=True)
        return

    if len(companies) == 1:
        await cb_estate_list(callback, companies[0].id)
        return

    buttons = [
        [InlineKeyboardButton(text=c.name, callback_data=f"realestate:list:{c.id}")]
        for c in companies
    ]
    buttons.append([InlineKeyboardButton(text="🔙 返回", callback_data="menu:company")])
    await callback.message.edit_text(
        "🏗 选择公司查看地产:",
        reply_markup=tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("realestate:list:"))
async def cb_estate_list(callback: types.CallbackQuery, company_id: int | None = None):
    if company_id is None:
        company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        company = await get_company_by_id(session, company_id)
        if not user:
            await callback.answer("请先 /cp_create 创建公司", show_alert=True)
            return
        if not company or company.owner_id != user.id:
            await callback.answer("无权操作", show_alert=True)
            return
        estates = await get_company_estates(session, company_id)
        buildings = get_building_list()
        owned_counts = {}
        for b in buildings:
            owned_counts[b["key"]] = await count_company_building_type(session, company_id, b["key"])

    text = await _render_estate_list(company, estates, owned_counts)
    kb = _estate_list_kb(estates, buildings, company_id, tg_id, owned_counts)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# ---- Purchase: confirmation dialog ----

@router.callback_query(F.data.startswith("realestate:buy:"))
async def cb_buy_building(callback: types.CallbackQuery):
    """Show purchase confirmation with price/income/ROI details."""
    parts = callback.data.split(":")
    company_id = int(parts[2])
    building_key = parts[3]
    tg_id = callback.from_user.id

    bld = get_building_info(building_key)
    if not bld:
        await callback.answer("无效的地产类型", show_alert=True)
        return

    async with async_session() as session:
        company = await get_company_by_id(session, company_id)
        if not company:
            await callback.answer("公司不存在", show_alert=True)
            return
        owned = await count_company_building_type(session, company_id, building_key)

    max_c = bld.get("max_count", 99)
    roi_days = bld["purchase_price"] // bld["daily_dividend"] if bld["daily_dividend"] > 0 else 999

    lines = [
        f"🏗 购买地产确认",
        f"{'─' * 24}",
        f"🏢 {bld['name']} — {bld['description']}",
        f"{'─' * 24}",
        f"💰 购买价格：{fmt_traffic(bld['purchase_price'])}",
        f"📈 日收益：{fmt_traffic(bld['daily_dividend'])}",
        f"📅 回本周期：约{roi_days}天",
        f"⬆️ 可升级至 Lv.{MAX_BUILDING_LEVEL}（每级+50%基础收益）",
        f"{'─' * 24}",
        f"📦 已拥有：{owned}/{max_c}",
        f"🏦 公司积分：{fmt_traffic(company.total_funds)}",
    ]
    if bld["purchase_price"] > company.total_funds:
        lines.append(f"❌ 积分不足！还差 {fmt_traffic(bld['purchase_price'] - company.total_funds)}")

    kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✅ 确认购买（{fmt_traffic(bld['purchase_price'])}）",
                callback_data=f"realestate:xbuy:{company_id}:{building_key}",
            ),
            InlineKeyboardButton(text="🔙 取消", callback_data=f"realestate:list:{company_id}"),
        ],
    ]), tg_id)
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("realestate:xbuy:"))
async def cb_do_buy(callback: types.CallbackQuery):
    """Execute purchase after confirmation."""
    parts = callback.data.split(":")
    company_id = int(parts[2])
    building_key = parts[3]
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await callback.answer("请先 /cp_create 创建公司", show_alert=True)
                return
            company = await get_company_by_id(session, company_id)
            if not company or company.owner_id != user.id:
                await callback.answer("只有公司老板才能购买地产", show_alert=True)
                return
            ok, msg = await purchase_building(session, company_id, tg_id, building_key)

    await callback.answer(msg, show_alert=True)
    if ok:
        await _refresh_estate_list(callback, company_id)


# ---- Upgrade: confirmation dialog ----

@router.callback_query(F.data.startswith("realestate:upg:"))
async def cb_upgrade_estate(callback: types.CallbackQuery):
    """Show upgrade confirmation with cost/income details."""
    parts = callback.data.split(":")
    company_id = int(parts[2])
    estate_id = int(parts[3])
    tg_id = callback.from_user.id

    async with async_session() as session:
        company = await get_company_by_id(session, company_id)
        if not company:
            await callback.answer("公司不存在", show_alert=True)
            return
        from db.models import RealEstate as RE
        estate = await session.get(RE, estate_id)
        if not estate or estate.company_id != company_id:
            await callback.answer("地产不存在", show_alert=True)
            return

    bld = get_building_info(estate.building_type)
    if not bld:
        await callback.answer("地产数据异常", show_alert=True)
        return

    if estate.level >= MAX_BUILDING_LEVEL:
        await callback.answer(f"已达最高等级 Lv.{MAX_BUILDING_LEVEL}", show_alert=True)
        return

    cost = calc_upgrade_cost(bld, estate.level)
    new_income = calc_level_income(bld, estate.level + 1)
    income_increase = new_income - estate.daily_dividend

    lines = [
        f"⬆️ 地产升级确认",
        f"{'─' * 24}",
        f"🏢 {bld['name']} Lv.{estate.level} → Lv.{estate.level + 1}",
        f"{'─' * 24}",
        f"💰 升级费用：{fmt_traffic(cost)}",
        f"📈 当前日收益：{fmt_traffic(estate.daily_dividend)}",
        f"📈 升级后日收益：{fmt_traffic(new_income)}（+{fmt_traffic(income_increase)}）",
        f"{'─' * 24}",
        f"🏦 公司积分：{fmt_traffic(company.total_funds)}",
    ]
    if cost > company.total_funds:
        lines.append(f"❌ 积分不足！还差 {fmt_traffic(cost - company.total_funds)}")

    kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✅ 确认升级（{fmt_traffic(cost)}）",
                callback_data=f"realestate:xupg:{company_id}:{estate_id}",
            ),
            InlineKeyboardButton(text="🔙 取消", callback_data=f"realestate:list:{company_id}"),
        ],
    ]), tg_id)
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("realestate:xupg:"))
async def cb_do_upgrade(callback: types.CallbackQuery):
    """Execute upgrade after confirmation."""
    parts = callback.data.split(":")
    company_id = int(parts[2])
    estate_id = int(parts[3])
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await callback.answer("请先 /cp_create 创建公司", show_alert=True)
                return
            company = await get_company_by_id(session, company_id)
            if not company or company.owner_id != user.id:
                await callback.answer("只有公司老板才能升级地产", show_alert=True)
                return
            ok, msg = await upgrade_estate(session, estate_id, company_id)

    await callback.answer(msg, show_alert=True)
    if ok:
        await _refresh_estate_list(callback, company_id)
