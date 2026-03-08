"""商业交易所 — 道具商城 + 黑市。"""

from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from commands import CMD_EXCHANGE
from cache.redis_client import get_redis
from db.engine import async_session
from keyboards.menus import tag_kb
from services.company_service import get_company_by_id, get_companies_by_owner
from services.shop_service import (
    buy_black_market_item,
    buy_item,
    get_black_market_items,
    load_shop_items,
)
from services.user_service import get_user_by_tg_id

router = Router()


# ========== 交易所主菜单 ==========

def _normalize_source(raw: str | None) -> str:
    """Normalize source marker to: main | company:<id>."""
    if not raw:
        return "main"
    if raw == "main":
        return "main"
    if raw.startswith("company:"):
        cid = raw.split(":", 1)[1]
        if cid.isdigit():
            return f"company:{int(cid)}"
    if raw.startswith("company_"):
        cid = raw.split("_", 1)[1]
        if cid.isdigit():
            return f"company:{int(cid)}"
    return "main"


def _source_to_token(source: str) -> str:
    src = _normalize_source(source)
    if src == "main":
        return "main"
    cid = src.split(":", 1)[1]
    return f"company_{cid}"


def _token_to_source(token: str | None) -> str:
    if not token:
        return "main"
    return _normalize_source(token)


def _extract_exchange_source(data: str) -> str:
    # Legacy callback compatibility.
    if data == "menu:exchange":
        return "main"
    if data.startswith("menu:exchange:"):
        return _normalize_source(data.removeprefix("menu:exchange:"))
    return "main"


def _exchange_entry_callback(source: str) -> str:
    src = _normalize_source(source)
    if src == "main":
        return "menu:exchange:main"
    return f"menu:exchange:{src}"


def _exchange_back_callback(source: str) -> str:
    src = _normalize_source(source)
    if src == "main":
        return "menu:main"
    cid = src.split(":", 1)[1]
    return f"company:view:{cid}"


def _exchange_menu_kb(tg_id: int | None = None, source: str = "main") -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 道具商城", callback_data=f"shop:list:{_source_to_token(source)}")],
        [InlineKeyboardButton(text="🌙 黑市特惠", callback_data=f"blackmarket:list:{_source_to_token(source)}")],
        [InlineKeyboardButton(text="🔙 返回", callback_data=_exchange_back_callback(source))],
    ])
    return tag_kb(kb, tg_id)


@router.callback_query(F.data == "menu:exchange")
@router.callback_query(F.data.startswith("menu:exchange:"))
async def cb_exchange_menu(callback: types.CallbackQuery):
    source = _extract_exchange_source(callback.data or "menu:exchange")
    text = (
        f"🏦 商业交易所\n"
        f"{'─' * 24}\n"
        "🛒 道具商城：购买并激活经营类道具\n"
        "🌙 黑市特惠：限量折扣道具，每日刷新\n"
        f"{'─' * 24}\n"
        "请选择功能 👇"
    )
    await callback.message.edit_text(text, reply_markup=_exchange_menu_kb(callback.from_user.id, source=source))
    await callback.answer()


# ========== /cp_exchange 命令 ==========

@router.message(Command(CMD_EXCHANGE))
async def cmd_exchange(message: types.Message):
    """交易所命令：打开商城/黑市入口。"""
    await message.answer(
        "🏦 商业交易所\n"
        f"{'─' * 24}\n"
        "🛒 道具商城：购买并激活经营类道具\n"
        "🌙 黑市特惠：限量折扣道具，每日刷新\n"
        f"{'─' * 24}\n"
        "请选择功能 👇",
        reply_markup=_exchange_menu_kb(message.from_user.id, source="main"),
    )


# ========== 道具商城 ==========

@router.callback_query(F.data == "shop:list")
@router.callback_query(F.data.startswith("shop:list:"))
async def cb_shop_list(callback: types.CallbackQuery):
    source_token = "main"
    if callback.data and callback.data.startswith("shop:list:"):
        source_token = callback.data.split(":", 2)[2]
    source = _token_to_source(source_token)
    source_token = _source_to_token(source)

    items = load_shop_items()

    lines = ["🛒 道具商城", "─" * 24]
    buttons = []
    for key, item in items.items():
        lines.append(f"{item['name']} — {item['price']:,} 积分")
        lines.append(f"  {item['description']}")
        lines.append("")
        buttons.append([InlineKeyboardButton(
            text=f"{item['name']} ({item['price']:,}💰)",
            callback_data=f"shop:select:{key}:{source_token}",
        )])

    buttons.append([InlineKeyboardButton(text="🔙 返回", callback_data=_exchange_entry_callback(source))])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("shop:select:"))
async def cb_shop_select(callback: types.CallbackQuery):
    """Show item detail and ask which company to apply the buff to."""
    parts = callback.data.split(":")
    item_key = parts[2]
    source_token = parts[3] if len(parts) >= 4 else "main"
    source = _token_to_source(source_token)
    source_token = _source_to_token(source)

    items = load_shop_items()
    if item_key not in items:
        await callback.answer("无效道具", show_alert=True)
        return

    tg_id = callback.from_user.id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await callback.answer("请先 /cp_create 创建公司", show_alert=True)
            return
        companies = await get_companies_by_owner(session, user.id)

    if not companies:
        await callback.answer("你没有公司，无法使用道具", show_alert=True)
        return

    if len(companies) == 1:
        # Show confirmation before buying
        item = items[item_key]
        price = item["price"]
        price_note = ""
        if item_key == "speed_research":
            r = await get_redis()
            count_str = await r.get(f"research_accel_count:{companies[0].id}")
            accel_count = int(count_str) if count_str else 0
            if accel_count >= 3:
                await callback.answer("本轮研发加速已达上限（最多3次）", show_alert=True)
                return
            price = item["price"] * (2 ** accel_count)
            price_note = f"\n(第{accel_count + 1}次加速)"

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        buttons = [
            [InlineKeyboardButton(
                text=f"确认购买 ({price:,} 积分)",
                callback_data=f"shop:buy:{item_key}:{companies[0].id}:{source_token}",
            )],
            [InlineKeyboardButton(text="取消", callback_data=f"shop:list:{source_token}")],
        ]
        await callback.message.edit_text(
            f"确认购买 {item['name']}?\n"
            f"价格: {price:,} 积分{price_note}\n"
            f"{item['description']}",
            reply_markup=tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), tg_id),
        )
        await callback.answer()
        return

    # Multiple companies
    item = items[item_key]
    buttons = [
        [InlineKeyboardButton(text=c.name, callback_data=f"shop:buy:{item_key}:{c.id}:{source_token}")]
        for c in companies
    ]
    buttons.append([InlineKeyboardButton(text="🔙 返回", callback_data=f"shop:list:{source_token}")])

    await callback.message.edit_text(
        f"为哪家公司购买 {item['name']}?",
        reply_markup=tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), tg_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("shop:buy:"))
async def cb_shop_buy(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    item_key = parts[2]
    company_id = int(parts[3])
    source_token = parts[4] if len(parts) >= 5 else "main"
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            company = await get_company_by_id(session, company_id)
            if not user:
                await callback.answer("请先 /cp_create 创建公司", show_alert=True)
                return
            if not company or company.owner_id != user.id:
                await callback.answer("无权操作", show_alert=True)
                return
            ok, msg = await buy_item(session, tg_id, company_id, item_key)

    await callback.answer(msg, show_alert=True)
    # 购买后刷新回商店列表（确保价格同步）
    if ok:
        callback.data = f"shop:list:{source_token}"
        await cb_shop_list(callback)


# ========== 黑市特惠 ==========

@router.callback_query(F.data == "blackmarket:list")
@router.callback_query(F.data.startswith("blackmarket:list:"))
async def cb_blackmarket_list(callback: types.CallbackQuery):
    source_token = "main"
    if callback.data and callback.data.startswith("blackmarket:list:"):
        source_token = callback.data.split(":", 2)[2]
    source = _token_to_source(source_token)
    source_token = _source_to_token(source)

    deals = await get_black_market_items()

    lines = ["🌙 黑市特惠 — 每日刷新，先到先得", "─" * 24]
    buttons = []
    for i, deal in enumerate(deals):
        stock_text = f"库存: {deal['stock']}" if deal['stock'] > 0 else "已售罄"
        lines.append(
            f"{deal['name']} — {deal['price']:,} 积分 "
            f"(原价 {deal['original_price']:,}, 省{deal['discount_pct']}%)"
        )
        lines.append(f"  {deal['description']} [{stock_text}]")
        lines.append("")
        if deal['stock'] > 0:
            buttons.append([InlineKeyboardButton(
                text=f"购买 {deal['name']} ({deal['price']:,}💰)",
                callback_data=f"blackmarket:select:{i}:{source_token}",
            )])

    if not deals:
        lines.append("今日暂无特惠")

    buttons.append([InlineKeyboardButton(text="🔙 返回", callback_data=_exchange_entry_callback(source))])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("blackmarket:select:"))
async def cb_blackmarket_select(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    index = int(parts[2])
    source_token = parts[3] if len(parts) >= 4 else "main"
    source = _token_to_source(source_token)
    source_token = _source_to_token(source)

    tg_id = callback.from_user.id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await callback.answer("请先 /cp_create 创建公司", show_alert=True)
            return
        companies = await get_companies_by_owner(session, user.id)

    if not companies:
        await callback.answer("你没有公司，无法使用道具", show_alert=True)
        return

    if len(companies) == 1:
        async with async_session() as session:
            async with session.begin():
                ok, msg = await buy_black_market_item(session, tg_id, companies[0].id, index)
        await callback.answer(msg, show_alert=True)
        return

    buttons = [
        [InlineKeyboardButton(text=c.name, callback_data=f"blackmarket:buy:{index}:{c.id}:{source_token}")]
        for c in companies
    ]
    buttons.append([InlineKeyboardButton(text="🔙 返回", callback_data=f"blackmarket:list:{source_token}")])

    await callback.message.edit_text(
        "为哪家公司购买黑市道具?",
        reply_markup=tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), tg_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("blackmarket:buy:"))
async def cb_blackmarket_buy(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    index = int(parts[2])
    company_id = int(parts[3])
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            company = await get_company_by_id(session, company_id)
            if not user:
                await callback.answer("请先 /cp_create 创建公司", show_alert=True)
                return
            if not company or company.owner_id != user.id:
                await callback.answer("无权操作", show_alert=True)
                return
            ok, msg = await buy_black_market_item(session, tg_id, company_id, index)

    await callback.answer(msg, show_alert=True)
