"""产品处理器。支持创建、升级、下架/删除产品。"""

from __future__ import annotations

import logging

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from commands import CMD_CLEAR_PRODUCT, CMD_NEW_PRODUCT
from db.engine import async_session
from handlers.common import is_super_admin
from keyboards.menus import tag_kb
from services.company_service import get_company_by_id, get_companies_by_owner, update_daily_revenue
from cache.redis_client import get_redis
from config import settings
from services.product_service import (
    create_product,
    get_available_product_templates,
    get_company_products,
    upgrade_product,
    PRODUCT_CREATE_COST_GROWTH,
    MAX_PRODUCT_VERSION,
)
from services.user_service import get_user_by_tg_id
from utils.formatters import fmt_traffic
from utils.panel_owner import mark_panel
from db.models import Product as ProductModel

router = Router()
logger = logging.getLogger(__name__)

def _format_new_product_usage(templates: list[dict]) -> str:
    lines = [
        "📦 用法: /cp_new_product <模板key> [自定义产品名]",
        "例1: /cp_new_product social_app",
        "例2: /cp_new_product social_app 超能社交",
        "",
    ]
    if templates:
        lines.append("🆕 当前可用模板:")
        for t in templates:
            lines.append(
                f"  - {t['product_key']}: {t['name']} "
                f"(基础日收入 {fmt_traffic(t['base_daily_income'])})"
            )
    else:
        lines.append("💡 暂无可用模板，请先在【科研】中完成前置科技。")
    return "\n".join(lines)


def _resolve_template_choice(raw_choice: str, templates: list[dict]) -> tuple[dict | None, list[dict]]:
    key = raw_choice.strip().lower()
    if not key:
        return None, []

    exact = [
        t for t in templates
        if t["product_key"].lower() == key or str(t["name"]).strip().lower() == key
    ]
    if exact:
        return exact[0], []

    candidates = [
        t for t in templates
        if t["product_key"].lower().startswith(key) or str(t["name"]).strip().lower().startswith(key)
    ]
    if len(candidates) == 1:
        return candidates[0], []
    return None, candidates


@router.message(Command(CMD_NEW_PRODUCT))
async def cmd_new_product(message: types.Message):
    """Create a product from unlocked templates: /cp_new_product <template_key> [custom_name]."""
    tg_id = message.from_user.id
    parts = (message.text or "").split(maxsplit=2)
    template_choice = parts[1].strip() if len(parts) >= 2 else ""
    custom_name = parts[2].strip() if len(parts) >= 3 else ""

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await message.answer("请先 /company_create 创建公司")
                return
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                await message.answer("你还没有公司")
                return
            company = companies[0]
            templates = await get_available_product_templates(session, company.id)

            if not templates:
                await message.answer(
                    "❌ 当前没有可创建的产品模板。\n"
                    "请先前往【科研】完成前置科技后再创建产品。"
                )
                return

            if not template_choice:
                await message.answer(_format_new_product_usage(templates))
                return

            selected_template, candidates = _resolve_template_choice(template_choice, templates)
            if selected_template is None and candidates:
                options = "\n".join(
                    f"  - {t['product_key']}: {t['name']}" for t in candidates
                )
                await message.answer(
                    "❌ 模板匹配到多个候选，请使用更完整的模板key：\n"
                    f"{options}"
                )
                return

            if selected_template is None:
                await message.answer(
                    "❌ 未找到可用模板。\n"
                    f"{_format_new_product_usage(templates)}"
                )
                return

            product, msg = await create_product(
                session,
                company.id,
                user.id,
                selected_template["product_key"],
                custom_name=custom_name,
            )
            if product:
                await update_daily_revenue(session, company.id)
                extra = f"\n模板: {selected_template['product_key']} ({selected_template['name']})"
                await message.answer(f"{msg}{extra}")
                return

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
            templates = await get_available_product_templates(session, company_id)

        # Filter out templates that already have products
        existing_tech_ids = {p.tech_id for p in products}
        templates = [t for t in templates if t["tech_id"] not in existing_tech_ids]

        lines = [f"📦 {company.name} — 产品列表", "─" * 24]

        product_buttons = []
        if products:
            for p in products:
                upgrade_cost = int(settings.product_upgrade_cost_base * (1.3 ** (p.version - 1)))
                remaining = min(5, MAX_PRODUCT_VERSION - p.version)
                x5_cost = sum(int(settings.product_upgrade_cost_base * (1.3 ** (p.version - 1 + i))) for i in range(remaining)) if remaining > 0 else 0
                lines.append(f"• {p.name} v{p.version} — {fmt_traffic(p.daily_income)}/日 (品质:{p.quality})")
                product_buttons.append([
                    InlineKeyboardButton(text=f"⬆️x1 {p.name} 💰{upgrade_cost:,}", callback_data=f"product:upgrade:{p.id}:1"),
                    InlineKeyboardButton(text=f"⬆️x5 {p.name} 💰{x5_cost:,}", callback_data=f"product:upgrade:{p.id}:5"),
                    InlineKeyboardButton(text=f"🗑 下架", callback_data=f"product:delete:{p.id}:{company_id}"),
                ])
        else:
            lines.append("暂无产品")

        if templates:
            create_cost = max(
                settings.product_create_cost,
                int(settings.product_create_cost * (1 + len(products) * PRODUCT_CREATE_COST_GROWTH)),
            )
            lines.append(f"\n🆕 可创建的产品 (创建费💰{create_cost:,}):")
        text = "\n".join(lines)

        template_buttons = [
            [InlineKeyboardButton(
                text=f"{t['name']} (收入{fmt_traffic(t['base_daily_income'])}/日)",
                callback_data=f"product:create:{company_id}:{t['product_key']}",
            )]
            for t in templates
        ]
        all_buttons = product_buttons + template_buttons
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
            await callback.answer("请先 /company_create 创建公司", show_alert=True)
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
            await callback.answer("请先 /company_create 创建公司", show_alert=True)
            return
        if not company or company.owner_id != user.id:
            await callback.answer("无权操作", show_alert=True)
            return

        products = await get_company_products(session, company_id)
        templates = await get_available_product_templates(session, company_id)

    # Filter out templates that already have products
    existing_tech_ids = {p.tech_id for p in products}
    templates = [t for t in templates if t["tech_id"] not in existing_tech_ids]

    lines = [f"📦 {company.name} — 产品列表", "─" * 24]

    product_buttons = []
    if products:
        for p in products:
            upgrade_cost = int(settings.product_upgrade_cost_base * (1.3 ** (p.version - 1)))
            remaining = min(5, MAX_PRODUCT_VERSION - p.version)
            x5_cost = sum(int(settings.product_upgrade_cost_base * (1.3 ** (p.version - 1 + i))) for i in range(remaining)) if remaining > 0 else 0
            lines.append(f"• {p.name} v{p.version} — {fmt_traffic(p.daily_income)}/日 (品质:{p.quality})")
            product_buttons.append([
                InlineKeyboardButton(text=f"⬆️x1 {p.name} 💰{upgrade_cost:,}", callback_data=f"product:upgrade:{p.id}:1"),
                InlineKeyboardButton(text=f"⬆️x5 {p.name} 💰{x5_cost:,}", callback_data=f"product:upgrade:{p.id}:5"),
                InlineKeyboardButton(text=f"🗑 下架", callback_data=f"product:delete:{p.id}:{company_id}"),
            ])
    else:
        lines.append("暂无产品")

    template_buttons = []
    if templates:
        create_cost = max(
            settings.product_create_cost,
            int(settings.product_create_cost * (1 + len(products) * PRODUCT_CREATE_COST_GROWTH)),
        )
        lines.append(f"\n🆕 可创建的产品 (创建费💰{create_cost:,}):")
        template_buttons = [
            [InlineKeyboardButton(
                text=f"{t['name']} (收入{fmt_traffic(t['base_daily_income'])}/日)",
                callback_data=f"product:create:{company_id}:{t['product_key']}",
            )]
            for t in templates
        ]
    else:
        lines.append("\n💡 完成科研可解锁产品模板")

    lines.append("\n📦 也可使用命令按模板创建产品:")
    lines.append("  /cp_new_product <模板key> [自定义名称]")
    text = "\n".join(lines)
    all_buttons = product_buttons + template_buttons
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

@router.callback_query(F.data.startswith("product:create:"))
async def cb_create_product(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    company_id = int(parts[2])
    product_key = parts[3]
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await callback.answer("请先 /company_create 创建公司", show_alert=True)
                return
            company = await get_company_by_id(session, company_id)
            if not company or company.owner_id != user.id:
                await callback.answer("只有公司老板才能创建产品", show_alert=True)
                return
            product, msg = await create_product(session, company_id, user.id, product_key)
            if product:
                await update_daily_revenue(session, company_id)

    await callback.answer(msg, show_alert=True)
    if product:
        await _refresh_product_list(callback, company_id)


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
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await callback.answer("请先 /company_create 创建公司", show_alert=True)
                return
            for i in range(count):
                if i > 0:
                    # Clear cooldown set by previous iteration to allow batch upgrades
                    r = await get_redis()
                    await r.delete(f"product_upgrade_cd:{product_id}")
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
            from db.models import Product as ProductModel
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
                await callback.answer("请先 /company_create 创建公司", show_alert=True)
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


# ---- /company_clear 管理员命令（限定 tg_id） ----


@router.message(Command(CMD_CLEAR_PRODUCT))
async def cmd_clear_product(message: types.Message):
    """管理员命令：回复某人消息，清除该用户所有产品。"""
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ 无权使用此命令")
        return

    if not message.reply_to_message:
        await message.answer("用法: 回复某人消息并发送 /company_clear")
        return

    target = message.reply_to_message.from_user
    if not target:
        await message.answer("❌ 无法获取目标用户")
        return

    from sqlalchemy import select, delete
    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, target.id)
            if not user:
                await message.answer("❌ 该用户未注册")
                return
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                await message.answer("❌ 该用户没有公司")
                return

            total_deleted = 0
            for company in companies:
                result = await session.execute(
                    delete(ProductModel).where(ProductModel.company_id == company.id)
                )
                total_deleted += result.rowcount
                await update_daily_revenue(session, company.id)

    await message.answer(
        f"✅ 已清除 {target.full_name} 的所有产品 (共 {total_deleted} 个)"
    )
