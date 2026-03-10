"""产品处理器。支持创建、升级、下架/删除产品。"""

from __future__ import annotations

import logging

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from commands import CMD_NEW_PRODUCT
from db.engine import async_session
from keyboards.menus import tag_kb
from services.company_service import get_company_by_id, get_companies_by_owner, update_daily_revenue
from cache.redis_client import get_redis
from config import settings
from services.product_service import (
    create_product,
    get_company_products,
    upgrade_product,
    MAX_PRODUCT_VERSION,
    get_max_products,
)
from services.user_service import get_user_by_tg_id
from utils.formatters import fmt_points
from utils.panel_owner import mark_panel

router = Router()
logger = logging.getLogger(__name__)


@router.message(Command(CMD_NEW_PRODUCT))
async def cmd_new_product(message: types.Message):
    """创建产品: /cp_new_product <产品名> <投资金额>"""
    tg_id = message.from_user.id
    parts = (message.text or "").split(maxsplit=2)

    if len(parts) < 3:
        await message.answer(
            "📦 用法: /cp_new_product <产品名> <投资金额>\n"
            f"例: /cp_new_product 超能社交 5000\n\n"
            f"💰 投资范围: {fmt_points(settings.product_min_investment)} ~ "
            f"{fmt_points(settings.product_max_investment)}\n"
            "🤖 AI将根据产品名评估方案，投资越多、评分越高，日收入越高"
        )
        return

    product_name = parts[1].strip()
    try:
        investment = int(parts[2].strip().replace(",", ""))
    except ValueError:
        await message.answer("❌ 投资金额必须是数字")
        return

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await message.answer("请先 /cp_create 创建公司")
                return
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                await message.answer("你还没有公司")
                return
            company = companies[0]
            product, msg = await create_product(
                session, company.id, user.id, product_name, investment,
            )
            if product:
                await update_daily_revenue(session, company.id)

    await message.answer(msg)


async def _refresh_product_list(callback: types.CallbackQuery, company_id: int):
    """操作后刷新产品列表消息。"""
    tg_id = callback.from_user.id
    try:
        async with async_session() as session:
            company = await get_company_by_id(session, company_id)
            if not company:
                return
            products = await get_company_products(session, company_id)

        lines = [f"📦 {company.name} — 产品列表 ({len(products)}/{get_max_products(company.level)})", "─" * 24]

        product_buttons = []
        if products:
            for p in products:
                upgrade_cost = int(settings.product_upgrade_cost_base * (1.3 ** (p.version - 1)))
                remaining = min(5, MAX_PRODUCT_VERSION - p.version)
                x5_cost = sum(int(settings.product_upgrade_cost_base * (1.3 ** (p.version - 1 + i))) for i in range(remaining)) if remaining > 0 else 0
                lines.append(f"• {p.name} v{p.version} — {fmt_points(p.daily_income)}/日 (品质:{p.quality})")
                product_buttons.append([
                    InlineKeyboardButton(text=f"⬆️x1 {p.name} 💰{upgrade_cost:,}", callback_data=f"product:upgrade:{p.id}:1"),
                    InlineKeyboardButton(text=f"⬆️x5 {p.name} 💰{x5_cost:,}", callback_data=f"product:upgrade:{p.id}:5"),
                    InlineKeyboardButton(text=f"🗑 下架", callback_data=f"product:delete:{p.id}:{company_id}"),
                ])
        else:
            lines.append("暂无产品")

        lines.append(
            f"\n📦 创建产品命令:\n"
            f"  /cp_new_product <产品名> <投资金额>\n"
            f"  投资范围: {fmt_points(settings.product_min_investment)} ~ "
            f"{fmt_points(settings.product_max_investment)}"
        )
        text = "\n".join(lines)

        all_buttons = product_buttons
        all_buttons.append([InlineKeyboardButton(text="🔙 返回", callback_data=f"company:view:{company_id}")])
        kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=all_buttons), tg_id)

        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass  # 消息未变化时edit会抛异常，忽略


@router.callback_query(F.data == "menu:product")
async def cb_product_menu(callback: types.CallbackQuery):
    """Auto-select company for products if only one, otherwise show selector."""
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
        await cb_product_list(callback, companies[0].id)
        return

    buttons = [
        [InlineKeyboardButton(text=c.name, callback_data=f"product:list:{c.id}")]
        for c in companies
    ]
    buttons.append([InlineKeyboardButton(text="🔙 返回", callback_data="menu:company")])
    await callback.message.edit_text(
        "📦 选择公司查看产品:",
        reply_markup=tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("product:list:"))
async def cb_product_list(callback: types.CallbackQuery, company_id: int | None = None):
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

        products = await get_company_products(session, company_id)

    lines = [f"📦 {company.name} — 产品列表 ({len(products)}/{get_max_products(company.level)})", "─" * 24]

    product_buttons = []
    if products:
        for p in products:
            upgrade_cost = int(settings.product_upgrade_cost_base * (1.3 ** (p.version - 1)))
            remaining = min(5, MAX_PRODUCT_VERSION - p.version)
            x5_cost = sum(int(settings.product_upgrade_cost_base * (1.3 ** (p.version - 1 + i))) for i in range(remaining)) if remaining > 0 else 0
            lines.append(f"• {p.name} v{p.version} — {fmt_points(p.daily_income)}/日 (品质:{p.quality})")
            product_buttons.append([
                InlineKeyboardButton(text=f"⬆️x1 {p.name} 💰{upgrade_cost:,}", callback_data=f"product:upgrade:{p.id}:1"),
                InlineKeyboardButton(text=f"⬆️x5 {p.name} 💰{x5_cost:,}", callback_data=f"product:upgrade:{p.id}:5"),
                InlineKeyboardButton(text=f"🗑 下架", callback_data=f"product:delete:{p.id}:{company_id}"),
            ])
    else:
        lines.append("暂无产品")

    lines.append(
        f"\n📦 创建产品命令:\n"
        f"  /cp_new_product <产品名> <投资金额>\n"
        f"  投资范围: {fmt_points(settings.product_min_investment)} ~ "
        f"{fmt_points(settings.product_max_investment)}"
    )
    text = "\n".join(lines)
    all_buttons = product_buttons
    all_buttons.append([InlineKeyboardButton(text="🔙 返回", callback_data=f"company:view:{company_id}")])
    kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=all_buttons), callback.from_user.id)

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            sent = await callback.message.answer(text, reply_markup=kb)
            await mark_panel(sent.chat.id, sent.message_id, callback.from_user.id)
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb)
        await mark_panel(sent.chat.id, sent.message_id, callback.from_user.id)

    await callback.answer()

@router.callback_query(F.data.startswith("product:upgrade:"))
async def cb_upgrade_product(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    product_id = int(parts[2])
    count = int(parts[3]) if len(parts) > 3 else 1
    tg_id = callback.from_user.id

    upgraded = 0
    last_msg = ""

    async with async_session() as session:
        async with session.begin():
            from db.models import Product as ProductModel

            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await callback.answer("请先 /cp_create 创建公司", show_alert=True)
                return
            for i in range(count):
                if i > 0:
                    # Clear cooldown set by previous iteration to allow batch upgrades
                    r = await get_redis()
                    p = await session.get(ProductModel, product_id)
                    cd_key = f"product_upgrade_cd:{p.company_id}:{p.tech_id}"
                    await r.delete(cd_key)
                ok, msg = await upgrade_product(session, product_id, user.id)
                if not ok:
                    if upgraded == 0:
                        # First attempt failed, show original error
                        await callback.answer(msg, show_alert=True)
                        return
                    else:
                        # Some succeeded, break and report partial success
                        last_msg = msg
                        break
                upgraded += 1
                last_msg = msg
            # Get final product state for the summary
            product = await session.get(ProductModel, product_id)
            await update_daily_revenue(session, product.company_id)

    if upgraded == 1:
        await callback.answer(last_msg, show_alert=True)
    else:
        await callback.answer(
            f"产品「{product.name}」连续升级{upgraded}次! "
            f"当前v{product.version}，日收入: {product.daily_income}积分",
            show_alert=True,
        )
    await _refresh_product_list(callback, product.company_id)


@router.callback_query(F.data.startswith("product:delete:"))
async def cb_delete_product(callback: types.CallbackQuery):
    """下架/删除产品。"""
    parts = callback.data.split(":")
    product_id = int(parts[2])
    company_id = int(parts[3])
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await callback.answer("请先 /cp_create 创建公司", show_alert=True)
                return
            company = await get_company_by_id(session, company_id)
            if not company or company.owner_id != user.id:
                await callback.answer("只有公司老板才能下架产品", show_alert=True)
                return
            from db.models import Product
            product = await session.get(Product, product_id)
            if not product or product.company_id != company_id:
                await callback.answer("产品不存在", show_alert=True)
                return
            name = product.name
            await session.delete(product)
            await update_daily_revenue(session, company_id)

    await callback.answer(f"产品「{name}」已下架", show_alert=True)
    await _refresh_product_list(callback, company_id)
