"""Dividend handlers: manual distribution, command, and history."""

from __future__ import annotations

import re
import time

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from commands import CMD_DIVIDEND
from config import settings
from db.engine import async_session
from db.models import DailyReport, User
from keyboards.menus import tag_kb
from services.company_service import add_funds, get_company_by_id, get_companies_by_owner
from services.shareholder_service import get_shareholders
from services.user_service import add_traffic, get_user_by_tg_id
from utils.formatters import fmt_shares, fmt_traffic

router = Router()

# 分红税率使用统一税率
DIVIDEND_TAX_RATE = settings.tax_rate
DIVIDEND_INPUT_TIMEOUT_SECONDS = 5 * 60  # 自定义分红输入超时（5分钟）


class DividendInputState(StatesGroup):
    waiting_custom_amount = State()


def _parse_amount(text: str) -> int | None:
    """解析金额，支持逗号/下划线分隔。"""
    normalized = text.replace(",", "").replace("_", "").replace("，", "").strip()
    if not normalized:
        return None
    try:
        amount = int(normalized)
    except ValueError:
        return None
    if amount <= 0:
        return None
    return amount


def _dividend_amount_kb(company_id: int, tg_id: int) -> InlineKeyboardMarkup:
    """分红金额选择键盘。"""
    max_div = settings.max_daily_dividend
    amounts = [a for a in [1000, 5000, 10000, 50000] if a <= max_div]
    buttons = [
        [InlineKeyboardButton(
            text=f"💸 分红 {fmt_traffic(a)}",
            callback_data=f"dividend:confirm:{company_id}:{a}",
        )]
        for a in amounts
    ]
    buttons.append([InlineKeyboardButton(text="✍️ 自定义金额（文本）", callback_data=f"dividend:input:{company_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 返回股东", callback_data=f"shareholder:list:{company_id}")])
    return tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), tg_id)


async def _execute_dividend(
    session,
    company_id: int,
    amount: int,
) -> tuple[bool, str, list[tuple[str, float, int, int]]]:
    """执行分红逻辑（含税）。

    Returns:
        (success, message, distributions)
        distributions: [(name, shares, gross_amount, net_amount), ...]
    """
    company = await get_company_by_id(session, company_id)
    if not company:
        return False, "公司不存在", []

    if amount > settings.max_daily_dividend:
        return False, f"单次分红不可超过 {fmt_traffic(settings.max_daily_dividend)}", []

    if company.cp_points < amount:
        return False, f"公司积分不足，当前: {fmt_traffic(company.cp_points)}", []

    shareholders = await get_shareholders(session, company_id)
    if not shareholders:
        return False, "暂无股东，无法分红", []

    # 扣除公司积分
    ok = await add_funds(session, company_id, -amount)
    if not ok:
        return False, "扣款失败，请重试", []

    # 按股份比例分配，扣除分红税
    distributions = []
    failed_total = 0
    total_tax = 0

    for sh in shareholders:
        gross_amount = int(amount * sh.shares / 100)
        if gross_amount > 0:
            tax = int(gross_amount * DIVIDEND_TAX_RATE)
            net_amount = gross_amount - tax
            total_tax += tax

            if net_amount > 0:
                success = await add_traffic(session, sh.user_id, net_amount)
                if success:
                    u = await session.get(User, sh.user_id)
                    name = u.tg_name if u else "未知"
                    distributions.append((name, sh.shares, gross_amount, net_amount))
                else:
                    failed_total += gross_amount

    # 退还失败的部分（退还总额，税也退）
    if failed_total > 0:
        await add_funds(session, company_id, failed_total)

    total_distributed = sum(d[3] for d in distributions)  # net amounts
    total_gross = sum(d[2] for d in distributions)
    actual_tax = total_gross - total_distributed

    return True, f"分红成功！税后实发: {fmt_traffic(total_distributed)}，税金: {fmt_traffic(actual_tax)}", distributions


# ---- /cp_dividend 命令 ----

@router.message(Command(CMD_DIVIDEND))
async def cmd_dividend(message: types.Message):
    """命令分红：/cp_dividend <金额>

    从公司积分中分红给所有股东，按股份比例分配，扣除分红税后到账个人余额。
    """
    tg_id = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)

    if len(parts) < 2:
        await message.answer(
            f"💸 分红命令用法:\n"
            f"/cp_dividend <金额>\n\n"
            f"示例: /cp_dividend 10000\n\n"
            f"⚠️ 分红税率: {int(DIVIDEND_TAX_RATE * 100)}%\n"
            f"分红后扣税，税后金额进入股东个人余额"
        )
        return

    amount = _parse_amount(parts[1])
    if amount is None:
        await message.answer("❌ 金额必须为正整数，示例: /cp_dividend 10000")
        return

    if amount > settings.max_daily_dividend:
        await message.answer(
            f"❌ 单次分红不可超过 {fmt_traffic(settings.max_daily_dividend)}"
        )
        return

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await message.answer("请先 /cp_start 注册账号")
                return

            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                await message.answer("你还没有公司，请先 /cp_create 创建公司")
                return

            # 默认使用第一家公司
            company = companies[0]
            ok, msg, distributions = await _execute_dividend(session, company.id, amount)

    if not ok:
        await message.answer(f"❌ {msg}")
        return

    # 构建结果
    lines = [
        f"✅ {company.name} 分红发放成功!",
        f"{'─' * 24}",
        f"💸 分红总额: {fmt_traffic(amount)}",
        f"📊 分红税率: {int(DIVIDEND_TAX_RATE * 100)}%",
        f"",
        f"👥 分配详情 (税后到账):",
    ]
    for name, shares, gross, net in distributions:
        lines.append(f"  • {name} ({fmt_shares(shares)}): {fmt_traffic(gross)} → {fmt_traffic(net)}")

    total_net = sum(d[3] for d in distributions)
    total_tax = sum(d[2] - d[3] for d in distributions)
    lines.append(f"")
    lines.append(f"💰 实际到账: {fmt_traffic(total_net)}")
    lines.append(f"🏛️ 税金扣除: {fmt_traffic(total_tax)}")

    await message.answer("\n".join(lines))


# ---- 按钮分红 ----

@router.callback_query(F.data.startswith("dividend:distribute:"))
async def cb_dividend_distribute(callback: types.CallbackQuery):
    """显示分红金额选择界面。"""
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        company = await get_company_by_id(session, company_id)
        if not user or not company:
            await callback.answer("数据异常", show_alert=True)
            return
        if company.owner_id != user.id:
            await callback.answer("只有老板才能发放分红", show_alert=True)
            return

        shareholders = await get_shareholders(session, company_id)
        if not shareholders:
            await callback.answer("暂无股东，无法分红", show_alert=True)
            return

        lines = [
            "💸 发放分红",
            "─" * 24,
            f"🏢 公司: {company.name}",
            f"🏦 公司积分: {fmt_traffic(company.cp_points)}",
            f"📊 分红税率: {int(DIVIDEND_TAX_RATE * 100)}%",
            "",
            "👥 股东持股比例:",
        ]
        for sh in shareholders:
            u = await session.get(User, sh.user_id)
            name = u.tg_name if u else "未知"
            lines.append(f"  • {name}: {fmt_shares(sh.shares)}")
        lines.append("")
        lines.append("选择分红总金额（扣税后按股份比例分配）:")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=_dividend_amount_kb(company_id, tg_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dividend:confirm:"))
async def cb_dividend_confirm(callback: types.CallbackQuery):
    """显示分红确认界面（含税预览）。"""
    parts = callback.data.split(":")
    company_id = int(parts[2])
    amount = int(parts[3])
    tg_id = callback.from_user.id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        company = await get_company_by_id(session, company_id)
        if not user or not company:
            await callback.answer("数据异常", show_alert=True)
            return
        if company.owner_id != user.id:
            await callback.answer("只有老板才能发放分红", show_alert=True)
            return
        if company.cp_points < amount:
            await callback.answer(f"公司积分不足，当前: {fmt_traffic(company.cp_points)}", show_alert=True)
            return

        shareholders = await get_shareholders(session, company_id)
        if not shareholders:
            await callback.answer("暂无股东", show_alert=True)
            return

        lines = [
            "💸 分红确认",
            "─" * 24,
            f"分红总额: {fmt_traffic(amount)}",
            f"分红税率: {int(DIVIDEND_TAX_RATE * 100)}%",
            "",
            "👥 各股东将获得 (税后):",
        ]
        total_tax = 0
        total_net = 0
        for sh in shareholders:
            u = await session.get(User, sh.user_id)
            name = u.tg_name if u else "未知"
            gross = int(amount * sh.shares / 100)
            tax = int(gross * DIVIDEND_TAX_RATE)
            net = gross - tax
            total_tax += tax
            total_net += net
            lines.append(f"  • {name} ({fmt_shares(sh.shares)}): {fmt_traffic(gross)} → {fmt_traffic(net)}")

        lines.append("")
        lines.append(f"💰 税后实发: {fmt_traffic(total_net)}")
        lines.append(f"🏛️ 税金扣除: {fmt_traffic(total_tax)}")
        lines.append(f"🏦 公司积分: {fmt_traffic(company.cp_points)} → {fmt_traffic(company.cp_points - amount)}")

    kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ 确认发放", callback_data=f"dividend:execute:{company_id}:{amount}"),
            InlineKeyboardButton(text="🔙 取消", callback_data=f"dividend:distribute:{company_id}"),
        ],
    ]), tg_id)

    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("dividend:execute:"))
async def cb_dividend_execute(callback: types.CallbackQuery):
    """执行分红操作（含税）。"""
    parts = callback.data.split(":")
    company_id = int(parts[2])
    amount = int(parts[3])
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            company = await get_company_by_id(session, company_id)
            if not user or not company:
                await callback.answer("数据异常", show_alert=True)
                return
            if company.owner_id != user.id:
                await callback.answer("只有老板才能发放分红", show_alert=True)
                return

            ok, msg, distributions = await _execute_dividend(session, company_id, amount)

    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    # 构建结果消息
    lines = [
        "✅ 分红发放成功!",
        "─" * 24,
        f"💸 分红总额: {fmt_traffic(amount)}",
        f"📊 分红税率: {int(DIVIDEND_TAX_RATE * 100)}%",
        "",
        "👥 分配详情 (税后到账):",
    ]
    for name, shares, gross, net in distributions:
        lines.append(f"  • {name} ({fmt_shares(shares)}): {fmt_traffic(gross)} → {fmt_traffic(net)}")

    total_net = sum(d[3] for d in distributions)
    total_tax = sum(d[2] - d[3] for d in distributions)
    lines.append(f"")
    lines.append(f"💰 实际到账: {fmt_traffic(total_net)}")
    lines.append(f"🏛️ 税金扣除: {fmt_traffic(total_tax)}")

    kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 返回股东列表", callback_data=f"shareholder:list:{company_id}")],
    ]), tg_id)

    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await callback.answer("分红发放成功!", show_alert=True)


# ---- 自定义分红金额输入 ----

@router.callback_query(F.data.startswith("dividend:input:"))
async def cb_dividend_input(callback: types.CallbackQuery, state: FSMContext):
    """Enter FSM for custom dividend amount input."""
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    await state.set_state(DividendInputState.waiting_custom_amount)
    await state.update_data(company_id=company_id, started_ts=int(time.time()))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ 取消输入", callback_data=f"dividend:input_cancel:{company_id}")],
        [InlineKeyboardButton(text="🔙 返回分红面板", callback_data=f"dividend:distribute:{company_id}")],
    ])
    await callback.message.edit_text(
        "✍️ 自定义分红金额\n"
        "请输入分红金额（整数，如 10000）\n"
        f"⏳ {DIVIDEND_INPUT_TIMEOUT_SECONDS // 60} 分钟内未输入将自动退出",
        reply_markup=tag_kb(kb, tg_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dividend:input_cancel:"))
async def cb_dividend_input_cancel(callback: types.CallbackQuery, state: FSMContext):
    company_id = int(callback.data.split(":")[2])
    await state.clear()
    await callback.message.edit_text(
        "选择分红总金额:",
        reply_markup=_dividend_amount_kb(company_id, callback.from_user.id),
    )
    await callback.answer("已取消输入")


@router.message(DividendInputState.waiting_custom_amount)
async def on_custom_dividend_amount(message: types.Message, state: FSMContext):
    """Handle custom dividend amount text input."""
    data = await state.get_data()
    company_id = int(data.get("company_id", 0))
    started_ts = int(data.get("started_ts", 0))
    now = int(time.time())

    if company_id <= 0:
        await state.clear()
        await message.answer("分红状态异常，已退出。")
        return

    if started_ts <= 0 or now - started_ts > DIVIDEND_INPUT_TIMEOUT_SECONDS:
        await state.clear()
        await message.answer(
            f"⏳ 分红输入超时（>{DIVIDEND_INPUT_TIMEOUT_SECONDS // 60}分钟），已自动退出。"
        )
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer("已退出分红输入模式。")
        return

    amount = _parse_amount(text)
    if amount is None:
        left = max(1, DIVIDEND_INPUT_TIMEOUT_SECONDS - (now - started_ts))
        await message.answer(
            f"请输入有效金额（正整数，如 10000）。剩余时间约 {left // 60}分{left % 60}秒"
        )
        return

    tg_id = message.from_user.id
    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await state.clear()
                await message.answer("请先 /cp_create 创建公司")
                return

            company = await get_company_by_id(session, company_id)
            if not company or company.owner_id != user.id:
                await state.clear()
                await message.answer("只有老板才能发放分红")
                return

            ok, msg, distributions = await _execute_dividend(session, company_id, amount)

    await state.clear()

    if not ok:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回分红面板", callback_data=f"dividend:distribute:{company_id}")],
        ])
        from utils.panel_owner import mark_panel
        sent = await message.answer(f"❌ {msg}", reply_markup=tag_kb(kb, tg_id))
        await mark_panel(message.chat.id, sent.message_id, tg_id)
        return

    lines = [
        "✅ 分红发放成功!",
        "─" * 24,
        f"💸 分红总额: {fmt_traffic(amount)}",
        f"📊 分红税率: {int(DIVIDEND_TAX_RATE * 100)}%",
        "",
        "👥 分配详情 (税后到账):",
    ]
    for name, shares, gross, net in distributions:
        lines.append(f"  • {name} ({fmt_shares(shares)}): {fmt_traffic(gross)} → {fmt_traffic(net)}")

    total_net = sum(d[3] for d in distributions)
    total_tax = sum(d[2] - d[3] for d in distributions)
    lines.append("")
    lines.append(f"💰 实际到账: {fmt_traffic(total_net)}")
    lines.append(f"🏛️ 税金扣除: {fmt_traffic(total_tax)}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 返回股东列表", callback_data=f"shareholder:list:{company_id}")],
    ])
    from utils.panel_owner import mark_panel
    sent = await message.answer("\n".join(lines), reply_markup=tag_kb(kb, tg_id))
    await mark_panel(message.chat.id, sent.message_id, tg_id)


@router.callback_query(F.data.startswith("dividend:history:"))
async def cb_dividend_history(callback: types.CallbackQuery):
    """显示公司分红/结算历史记录。"""
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    async with async_session() as session:
        company = await get_company_by_id(session, company_id)
        if not company:
            await callback.answer("公司不存在", show_alert=True)
            return

        result = await session.execute(
            select(DailyReport)
            .where(DailyReport.company_id == company_id)
            .order_by(DailyReport.id.desc())
            .limit(5)
        )
        reports = result.scalars().all()

        lines = [f"📜 {company.name} — 分红记录", "─" * 24]
        if reports:
            for r in reports:
                profit = r.total_income - r.operating_cost
                lines.append(f"📅 {r.date}")
                lines.append(f"  收入: {fmt_traffic(r.total_income)}")
                lines.append(f"  成本: -{fmt_traffic(r.operating_cost)}")
                lines.append(f"  利润: {fmt_traffic(profit)}")
                lines.append(f"  分红: {fmt_traffic(r.dividend_paid)}")
                lines.append("")
        else:
            lines.append("暂无结算记录")

    kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 返回股东列表", callback_data=f"shareholder:list:{company_id}")],
    ]), tg_id)

    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await callback.answer()


# 保留旧的主菜单入口（兼容）
@router.callback_query(F.data == "menu:dividend")
async def cb_dividend_menu(callback: types.CallbackQuery):
    """主菜单分红入口 - 显示所有公司的结算记录。"""
    tg_id = callback.from_user.id
    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await callback.answer("请先 /cp_create 创建公司", show_alert=True)
            return
        companies = await get_companies_by_owner(session, user.id)

        if not companies:
            from keyboards.menus import main_menu_kb
            await callback.message.edit_text("你还没有公司。", reply_markup=main_menu_kb(tg_id=tg_id))
            await callback.answer()
            return

        lines = ["💰 最近分红/结算记录", "─" * 24]
        for company in companies:
            result = await session.execute(
                select(DailyReport)
                .where(DailyReport.company_id == company.id)
                .order_by(DailyReport.id.desc())
                .limit(3)
            )
            reports = result.scalars().all()
            if reports:
                from services.settlement_service import format_daily_report
                for r in reports:
                    lines.append(format_daily_report(company, r))
                    lines.append("")
            else:
                lines.append(f"「{company.name}」暂无结算记录")

    from keyboards.menus import main_menu_kb
    await callback.message.edit_text("\n".join(lines), reply_markup=main_menu_kb(tg_id=tg_id))
    await callback.answer()
