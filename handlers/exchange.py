"""商业交易所 — 积分兑换流量 + 道具商城 + 黑市。

核心功能：
- 个人积分 → 真实流量 (需要外部接口)
- 道具商城 (buff道具)
- 黑市特惠 (每日刷新)
"""

from __future__ import annotations

import datetime as _dt

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cache.redis_client import get_redis
from commands import CMD_EXCHANGE
from config import settings as cfg
from db.engine import async_session
from keyboards.menus import tag_kb
from services.company_service import get_company_by_id, get_companies_by_owner
from services.shop_service import (
    buy_black_market_item,
    buy_item,
    get_black_market_items,
    load_shop_items,
)
from services.user_service import add_traffic, get_user_by_tg_id
from utils.formatters import fmt_traffic, fmt_real_traffic
from utils.timezone import BJ_TZ

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
    token = _source_to_token(source)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📶 积分→流量", callback_data=f"exchange:traffic:{token}")],
        [InlineKeyboardButton(text="🛒 道具商城", callback_data=f"shop:list:{token}")],
        [InlineKeyboardButton(text="🌙 黑市特惠", callback_data=f"blackmarket:list:{token}")],
        [InlineKeyboardButton(text="🔙 返回", callback_data=_exchange_back_callback(source))],
    ])
    return tag_kb(kb, tg_id)


@router.callback_query(F.data == "menu:exchange")
@router.callback_query(F.data.startswith("menu:exchange:"))
async def cb_exchange_menu(callback: types.CallbackQuery):
    tg_id = callback.from_user.id
    source = _extract_exchange_source(callback.data or "menu:exchange")
    rate = cfg.traffic_exchange_rate
    limit_mb = cfg.traffic_exchange_daily_limit_mb

    # 获取今日已兑换
    r = await get_redis()
    today_key = f"traffic_exchange:{tg_id}:{_dt.datetime.now(BJ_TZ).date().isoformat()}"
    used_today = int(await r.get(today_key) or 0)

    text = (
        f"🏦 商业交易所\n"
        f"{'─' * 24}\n"
        f"💱 兑换比例: {rate} 积分 = 1MB\n"
        f"📊 每日上限: {fmt_real_traffic(limit_mb)}\n"
        f"📈 今日已兑: {fmt_real_traffic(used_today)}\n"
        f"{'─' * 24}\n"
        f"个人积分可兑换真实手机流量！"
    )
    await callback.message.edit_text(text, reply_markup=_exchange_menu_kb(tg_id, source=source))
    await callback.answer()


# ========== 积分 → 流量 ==========

def _traffic_amounts_kb(tg_id: int | None = None, source: str = "main") -> InlineKeyboardMarkup:
    """流量兑换金额选择。"""
    rate = cfg.traffic_exchange_rate
    token = _source_to_token(source)
    # 预设兑换选项: MB数 -> 所需积分
    options = [
        (100, rate * 100),        # 100MB
        (500, rate * 500),        # 500MB
        (1024, rate * 1024),      # 1GB
        (5120, rate * 5120),      # 5GB
        (10240, rate * 10240),    # 10GB
    ]
    buttons = [
        [InlineKeyboardButton(
            text=f"{credits:,} 积分 → {fmt_real_traffic(mb)}",
            callback_data=f"exchange:traffic:do:{mb}:{token}",
        )]
        for mb, credits in options
    ]
    buttons.append([InlineKeyboardButton(text="🔙 返回交易所", callback_data=_exchange_entry_callback(source))])
    return tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), tg_id)


async def _render_traffic_menu(callback: types.CallbackQuery, source: str = "main"):
    tg_id = callback.from_user.id
    rate = cfg.traffic_exchange_rate
    limit_mb = cfg.traffic_exchange_daily_limit_mb

    # 获取用户当前积分
    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        balance = user.traffic if user else 0

    # 获取今日已兑换
    r = await get_redis()
    today_key = f"traffic_exchange:{tg_id}:{_dt.datetime.now(BJ_TZ).date().isoformat()}"
    used_today = int(await r.get(today_key) or 0)
    remaining = max(0, limit_mb - used_today)

    text = (
        f"📶 积分 → 流量\n"
        f"{'─' * 24}\n"
        f"💱 兑换比例: {rate} 积分 = 1MB\n"
        f"💰 当前个人积分: {fmt_traffic(balance)}\n"
        f"📊 今日剩余额度: {fmt_real_traffic(remaining)}\n"
        f"{'─' * 24}\n"
        f"选择兑换数量 👇"
    )
    await callback.message.edit_text(text, reply_markup=_traffic_amounts_kb(tg_id, source=source))
    await callback.answer()


async def _do_traffic_exchange(callback: types.CallbackQuery, mb_amount: int):
    tg_id = callback.from_user.id
    rate = cfg.traffic_exchange_rate
    credits_needed = mb_amount * rate

    # 检查流量接口是否配置
    if not cfg.traffic_exchange_api_url:
        await callback.answer(
            "🚧 流量兑换接口尚未接入\n"
            "该功能即将开放，敬请期待！",
            show_alert=True,
        )
        return

    # 检查每日上限
    r = await get_redis()
    today_key = f"traffic_exchange:{tg_id}:{_dt.datetime.now(BJ_TZ).date().isoformat()}"
    used_today = int(await r.get(today_key) or 0)
    if used_today + mb_amount > cfg.traffic_exchange_daily_limit_mb:
        remaining = max(0, cfg.traffic_exchange_daily_limit_mb - used_today)
        await callback.answer(
            f"❗ 超出今日兑换上限\n"
            f"今日已兑: {fmt_real_traffic(used_today)}\n"
            f"剩余额度: {fmt_real_traffic(remaining)}",
            show_alert=True,
        )
        return

    # 扣除积分
    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await callback.answer("用户不存在", show_alert=True)
                return
            if user.traffic < credits_needed:
                await callback.answer(
                    f"积分不足！\n需要: {fmt_traffic(credits_needed)}\n当前: {fmt_traffic(user.traffic)}",
                    show_alert=True,
                )
                return
            ok = await add_traffic(session, user.id, -credits_needed, reason=f"兑换流量 {fmt_real_traffic(mb_amount)}")
            if not ok:
                await callback.answer("扣款失败，请重试", show_alert=True)
                return

    # TODO: 调用外部流量接口发放流量
    # 记录今日兑换量
    await r.incrby(today_key, mb_amount)
    await r.expire(today_key, 172800)  # 保留2天

    await callback.answer(
        f"✅ 兑换成功！\n"
        f"消耗: {fmt_traffic(credits_needed)}\n"
        f"获得: {fmt_real_traffic(mb_amount)}\n"
        f"流量将在24小时内到账",
        show_alert=True,
    )


@router.callback_query(F.data == "exchange:traffic")
@router.callback_query(F.data.startswith("exchange:traffic:"))
async def cb_traffic_route(callback: types.CallbackQuery):
    data = callback.data or "exchange:traffic"
    parts = data.split(":")

    # Legacy: exchange:traffic
    if data == "exchange:traffic":
        await _render_traffic_menu(callback, source="main")
        return

    # New: exchange:traffic:do:<mb>:<source_token>
    if len(parts) >= 4 and parts[2] == "do":
        try:
            mb_amount = int(parts[3])
        except ValueError:
            await callback.answer("无效兑换数量", show_alert=True)
            return
        await _do_traffic_exchange(callback, mb_amount)
        return

    # Legacy: exchange:traffic:<mb>
    if len(parts) == 3 and parts[2].isdigit():
        await _do_traffic_exchange(callback, int(parts[2]))
        return

    # New: exchange:traffic:<source_token>
    source_token = parts[2] if len(parts) >= 3 else "main"
    source = _token_to_source(source_token)
    await _render_traffic_menu(callback, source=source)


# ========== /company_exchange 命令 ==========

@router.message(Command(CMD_EXCHANGE))
async def cmd_exchange(message: types.Message):
    """积分兑换流量命令：/company_exchange <MB数>"""
    tg_id = message.from_user.id
    args = (message.text or "").split()
    rate = cfg.traffic_exchange_rate

    if len(args) < 2:
        await message.answer(
            f"📶 积分兑换流量\n"
            f"{'─' * 24}\n"
            f"用法: /company_exchange <MB数>\n"
            f"例: /company_exchange 1000  (兑换1GB)\n"
            f"{'─' * 24}\n"
            f"💱 兑换比例: {rate} 积分 = 1MB\n"
            f"📊 每日上限: {fmt_real_traffic(cfg.traffic_exchange_daily_limit_mb)}"
        )
        return

    try:
        mb_amount = int(args[1].replace(",", "").replace("_", ""))
    except ValueError:
        await message.answer("❌ MB数必须是整数")
        return

    if mb_amount <= 0:
        await message.answer("❌ MB数必须大于0")
        return

    credits_needed = mb_amount * rate

    # 检查流量接口
    if not cfg.traffic_exchange_api_url:
        await message.answer(
            "🚧 流量兑换接口尚未接入\n"
            "该功能即将开放，敬请期待！"
        )
        return

    # 检查每日上限
    r = await get_redis()
    today_key = f"traffic_exchange:{tg_id}:{_dt.datetime.now(BJ_TZ).date().isoformat()}"
    used_today = int(await r.get(today_key) or 0)
    if used_today + mb_amount > cfg.traffic_exchange_daily_limit_mb:
        remaining = max(0, cfg.traffic_exchange_daily_limit_mb - used_today)
        await message.answer(
            f"❗ 超出今日兑换上限\n"
            f"今日已兑: {fmt_real_traffic(used_today)}\n"
            f"剩余额度: {fmt_real_traffic(remaining)}"
        )
        return

    # 扣除积分
    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await message.answer("请先 /company_start 注册账号")
                return
            if user.traffic < credits_needed:
                await message.answer(
                    f"❌ 积分不足！\n需要: {fmt_traffic(credits_needed)}\n当前: {fmt_traffic(user.traffic)}"
                )
                return
            ok = await add_traffic(session, user.id, -credits_needed, reason=f"兑换流量 {fmt_real_traffic(mb_amount)}")
            if not ok:
                await message.answer("❌ 扣款失败，请重试")
                return

    # TODO: 调用外部流量接口
    await r.incrby(today_key, mb_amount)
    await r.expire(today_key, 172800)

    await message.answer(
        f"✅ 兑换成功！\n"
        f"{'─' * 24}\n"
        f"💸 消耗: {fmt_traffic(credits_needed)}\n"
        f"📶 获得: {fmt_real_traffic(mb_amount)}\n"
        f"流量将在24小时内到账"
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
            await callback.answer("请先 /company_create 创建公司", show_alert=True)
            return
        companies = await get_companies_by_owner(session, user.id)

    if not companies:
        await callback.answer("你没有公司，无法使用道具", show_alert=True)
        return

    if len(companies) == 1:
        async with async_session() as session:
            async with session.begin():
                ok, msg = await buy_item(session, tg_id, companies[0].id, item_key)
        await callback.answer(msg, show_alert=True)
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
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            company = await get_company_by_id(session, company_id)
            if not user:
                await callback.answer("请先 /company_create 创建公司", show_alert=True)
                return
            if not company or company.owner_id != user.id:
                await callback.answer("无权操作", show_alert=True)
                return
            ok, msg = await buy_item(session, tg_id, company_id, item_key)

    await callback.answer(msg, show_alert=True)


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
            await callback.answer("请先 /company_create 创建公司", show_alert=True)
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
                await callback.answer("请先 /company_create 创建公司", show_alert=True)
                return
            if not company or company.owner_id != user.id:
                await callback.answer("无权操作", show_alert=True)
                return
            ok, msg = await buy_black_market_item(session, tg_id, company_id, index)

    await callback.answer(msg, show_alert=True)
