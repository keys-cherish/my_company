"""广告处理器（仅群组）。"""

from __future__ import annotations

from aiogram import F, Router, types

from db.engine import async_session
from keyboards.menus import main_menu_kb, tag_kb
from services.ad_service import get_active_ad_info, get_ad_tiers, buy_ad, cancel_ad
from services.company_service import add_funds, get_company_by_id
from services.user_service import get_user_by_tg_id
from handlers.company_helpers import _refresh_company_view

router = Router()


def _promo_menu_kb(company_id: int, tg_id: int | None = None):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎤 路演", callback_data=f"roadshow:do:{company_id}"),
            InlineKeyboardButton(text="📢 广告", callback_data=f"ad:menu:{company_id}"),
        ],
        [InlineKeyboardButton(text="🔙 返回公司", callback_data=f"company:view:{company_id}")],
    ])
    return tag_kb(kb, tg_id)


def _ad_menu_kb(company_id: int, tg_id: int | None = None):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    tiers = get_ad_tiers()
    buttons = [
        [InlineKeyboardButton(
            text=f"{t['name']} ({t['cost']}💰 {t['description']})",
            callback_data=f"ad:buy:{company_id}:{t['key']}",
        )]
        for t in tiers
    ]
    buttons.append([InlineKeyboardButton(text="🔙 返回推广", callback_data=f"promo:menu:{company_id}")])
    return tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), tg_id)


@router.callback_query(F.data.startswith("promo:menu:"))
async def cb_promo_menu(callback: types.CallbackQuery):
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

    text = (
        "📣 推广中心\n"
        f"{'─' * 24}\n"
        "选择推广方式：\n"
        "• 🎤 路演：立即执行，获得随机推广收益\n"
        "• 📢 广告：购买持续生效的营收加成"
    )
    await callback.message.edit_text(text, reply_markup=_promo_menu_kb(company_id, tg_id=tg_id))
    await callback.answer()


@router.callback_query(F.data.startswith("ad:menu:"))
async def cb_ad_menu(callback: types.CallbackQuery):
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

    ad_info = await get_active_ad_info(company_id)
    tg_id = callback.from_user.id
    if ad_info:
        text = (
            f"📢 广告投放\n"
            f"当前活动: {ad_info.get('name', '广告')}\n"
            f"营收加成: +{ad_info['boost_pct']*100:.0f}%\n"
            f"剩余天数: {ad_info['remaining_days']}天\n\n"
            "当前已有活动广告，请等待结束后购买新广告。"
        )
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回推广", callback_data=f"promo:menu:{company_id}")]
        ]), tg_id)
        await callback.message.edit_text(text, reply_markup=kb)
    else:
        await callback.message.edit_text("📢 选择广告方案:", reply_markup=_ad_menu_kb(company_id, tg_id=tg_id))
    await callback.answer()


@router.callback_query(F.data.startswith("ad:buy:"))
async def cb_buy_ad(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    company_id = int(parts[2])
    tier_key = parts[3]
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            company = await get_company_by_id(session, company_id)
            if not company or not user or company.owner_id != user.id:
                await callback.answer("只有公司老板才能购买广告", show_alert=True)
                return

            # 先检查积分是否足够，再购买广告（原子性保证）
            from services.ad_service import AD_TIERS
            tier = next((t for t in AD_TIERS if t["key"] == tier_key), None)
            if not tier:
                await callback.answer("无效的广告类型", show_alert=True)
                return

            cost = tier["cost"]
            fund_ok = await add_funds(session, company_id, -cost)
            if not fund_ok:
                await callback.answer(f"公司积分不足，需要 {cost:,} 积分", show_alert=True)
                return

            # 积分扣除成功后再购买广告
            ok, msg, _ = await buy_ad(company_id, tier_key)
            if not ok:
                # 回滚积分
                await add_funds(session, company_id, cost)
                await callback.answer(msg, show_alert=True)
                return

    await callback.answer(msg, show_alert=True)
    await _refresh_company_view(callback, company_id)
