"""股东注资处理器。"""

from __future__ import annotations

import json
import re
import time
import uuid

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cache.redis_client import RedisLock, get_redis
from commands import CMD_CANCEL, CMD_INVEST
from db.models import User
from db.engine import async_session
from keyboards.menus import invest_kb, shareholder_list_kb
from services.company_service import get_companies_by_owner, get_company_by_id
from services.shareholder_service import get_shareholders, invest
from utils.panel_owner import mark_panel
from services.user_service import get_user_by_tg_id
from utils.formatters import fmt_shares, fmt_traffic

router = Router()

# ── 常量 ──────────────────────────────────────────────
INVEST_INPUT_TIMEOUT_SECONDS = 5 * 60  # 自定义注资输入超时（5分钟）
INVEST_APPROVAL_TTL_SECONDS = 3 * 60 * 60  # 注资审批有效期（3小时）
INVEST_KEYWORD = chr(0x6295) + chr(0x8D44)  # "投资"
CN_COMMA = chr(0xFF0C)  # 中文逗号，用于金额解析
INVEST_SHORTCUT_RE = re.compile(rf"^\s*{INVEST_KEYWORD}\s*([0-9][0-9_,{CN_COMMA}]*)\s*$")


class InvestState(StatesGroup):
    waiting_custom_amount = State()


def _parse_amount(amount_text: str) -> int | None:
    """解析用户输入的金额文本，支持逗号/下划线/中文逗号分隔。"""
    normalized = amount_text.replace(",", "").replace("_", "").replace(CN_COMMA, "").strip()
    if not normalized:
        return None
    try:
        amount = int(normalized)
    except ValueError:
        return None
    if amount <= 0:
        return None
    return amount


def _approval_key(token: str) -> str:
    return f"invest_approval:{token}"


def _approval_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ 同意注资", callback_data=f"shareholder:invreq:approve:{token}"),
                InlineKeyboardButton(text="❌ 拒绝", callback_data=f"shareholder:invreq:reject:{token}"),
            ],
        ]
    )


async def _notify_investor(bot, investor_tg_id: int, text: str):
    try:
        await bot.send_message(investor_tg_id, text)
    except Exception:
        pass


async def _create_invest_approval_request(
    bot,
    *,
    investor_tg_id: int,
    investor_name: str,
    target_tg_id: int,
    target_company_id: int,
    target_company_name: str,
    amount: int,
) -> tuple[bool, str]:
    token = uuid.uuid4().hex
    payload = {
        "investor_tg_id": investor_tg_id,
        "target_tg_id": target_tg_id,
        "target_company_id": target_company_id,
        "target_company_name": target_company_name,
        "amount": amount,
        "created_at": int(time.time()),
    }
    r = await get_redis()
    await r.setex(_approval_key(token), INVEST_APPROVAL_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))

    try:
        await bot.send_message(
            target_tg_id,
            (
                "💼 收到一条注资请求\n"
                f"投资人：{investor_name}\n"
                f"目标公司：{target_company_name}\n"
                f"注资金额：{fmt_traffic(amount)}\n\n"
                f"请在 {INVEST_APPROVAL_TTL_SECONDS // 3600} 小时内处理："
            ),
            reply_markup=_approval_kb(token),
        )
    except Exception:
        await r.delete(_approval_key(token))
        return False, (
            "❌ 无法向对方发送私聊审批通知。\n"
            "请让对方先私聊机器人发送 /cp_start 后再重试。"
        )

    return True, (
        f"📨 注资请求已发送给对方私聊，等待同意。\n"
        f"目标公司：{target_company_name}\n"
        f"金额：{fmt_traffic(amount)}\n"
        f"有效期：3小时"
    )


async def _handle_reply_invest(message: types.Message, amount: int):
    """通过回复消息对目标用户的公司进行注资。"""
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("请先回复目标用户的消息，再发送注资命令。")
        return

    investor_tg_id = message.from_user.id
    target_user_tg = message.reply_to_message.from_user

    if target_user_tg.is_bot:
        await message.answer("无法对机器人注资。")
        return
    if target_user_tg.id == investor_tg_id:
        await message.answer("不能对自己注资，请回复其他玩家的消息。")
        return

    async with async_session() as session:
        async with session.begin():
            investor_user = await get_user_by_tg_id(session, investor_tg_id)
            if not investor_user:
                await message.answer("请先 /cp_start 注册账号。")
                return

            investor_companies = await get_companies_by_owner(session, investor_user.id)
            if not investor_companies:
                await message.answer("你还没有公司，无法注资。请先 /cp_create 创建公司。")
                return
            investor_company = investor_companies[0]
            investor_quota = int(investor_company.total_funds)
            if amount > investor_quota:
                await message.answer(
                    f"公司资金不足，当前可用：{fmt_traffic(investor_quota)}。"
                )
                return

            target_user = await get_user_by_tg_id(session, target_user_tg.id)
            if not target_user:
                await message.answer("目标用户尚未注册。")
                return

            target_companies = await get_companies_by_owner(session, target_user.id)
            if not target_companies:
                await message.answer("目标用户还没有公司。")
                return

            target_company = target_companies[0]
            ok, text = await _create_invest_approval_request(
                message.bot,
                investor_tg_id=investor_tg_id,
                investor_name=message.from_user.full_name or str(investor_tg_id),
                target_tg_id=target_user_tg.id,
                target_company_id=target_company.id,
                target_company_name=target_company.name,
                amount=amount,
            )

    await message.answer(text)
    if not ok:
        return


async def _queue_approval_or_invest_direct(
    *,
    bot,
    investor_tg_id: int,
    investor_user_id: int,
    investor_name: str,
    target_company_id: int,
    amount: int,
) -> tuple[bool, str, bool]:
    """Return (ok, msg, pending_approval)."""
    async with async_session() as session:
        async with session.begin():
            company = await get_company_by_id(session, target_company_id)
            if not company:
                return False, "目标公司不存在。", False

            owner = await session.get(User, company.owner_id)
            if not owner:
                return False, "目标公司所有者不存在。", False

            # 自己给自己公司注资，仍允许直接执行
            if owner.tg_id == investor_tg_id:
                ok, msg = await invest(session, investor_user_id, target_company_id, amount)
                return ok, msg, False

            # 他人公司必须审批
            ok, msg = await _create_invest_approval_request(
                bot,
                investor_tg_id=investor_tg_id,
                investor_name=investor_name,
                target_tg_id=owner.tg_id,
                target_company_id=target_company_id,
                target_company_name=company.name,
                amount=amount,
            )
            return ok, msg, True


@router.callback_query(F.data.startswith("shareholder:invreq:"))
async def cb_invest_approval(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("请求格式错误", show_alert=True)
        return
    action = parts[2]
    token = parts[3]

    async with RedisLock(f"invest_approval:{token}", timeout=5):
        r = await get_redis()
        raw = await r.get(_approval_key(token))
        if not raw:
            await callback.answer("该请求已过期或已处理。", show_alert=True)
            return

        try:
            req = json.loads(raw)
        except Exception:
            await r.delete(_approval_key(token))
            await callback.answer("请求数据损坏，已作废。", show_alert=True)
            return

        target_tg_id = int(req.get("target_tg_id", 0))
        investor_tg_id = int(req.get("investor_tg_id", 0))
        target_company_id = int(req.get("target_company_id", 0))
        amount = int(req.get("amount", 0))
        target_company_name = str(req.get("target_company_name", "未知公司"))

        if callback.from_user.id != target_tg_id:
            await callback.answer("这不是发给你的审批请求。", show_alert=True)
            return

        if action == "reject":
            await r.delete(_approval_key(token))
            await callback.message.edit_text(
                "❌ 你已拒绝该注资请求。\n"
                f"目标公司：{target_company_name}\n"
                f"金额：{fmt_traffic(amount)}"
            )
            await _notify_investor(
                callback.bot,
                investor_tg_id,
                (
                    "❌ 对方拒绝了你的注资请求。\n"
                    f"目标公司：{target_company_name}\n"
                    f"金额：{fmt_traffic(amount)}"
                ),
            )
            await callback.answer("已拒绝")
            return

        if action != "approve":
            await callback.answer("未知操作", show_alert=True)
            return

        ok = False
        msg = "注资失败"
        async with async_session() as session:
            async with session.begin():
                target_user = await get_user_by_tg_id(session, target_tg_id)
                investor_user = await get_user_by_tg_id(session, investor_tg_id)
                company = await get_company_by_id(session, target_company_id)

                if not target_user or not investor_user:
                    msg = "用户不存在或未注册，注资失败。"
                elif not company or company.owner_id != target_user.id:
                    msg = "目标公司状态已变化，注资失败。"
                else:
                    ok, msg = await invest(session, investor_user.id, target_company_id, amount)
                    target_company_name = company.name

        await r.delete(_approval_key(token))

    if ok:
        await callback.message.edit_text(
            "✅ 你已同意该注资请求。\n"
            f"目标公司：{target_company_name}\n"
            f"金额：{fmt_traffic(amount)}"
        )
        await _notify_investor(
            callback.bot,
            investor_tg_id,
            f"✅ 对方已同意注资请求：{msg}\n目标公司：{target_company_name}",
        )
        await callback.answer("已同意")
        return

    await callback.message.edit_text(
        "⚠️ 你已同意，但执行注资失败。\n"
        f"原因：{msg}"
    )
    await _notify_investor(
        callback.bot,
        investor_tg_id,
        f"⚠️ 对方已同意，但注资执行失败：{msg}",
    )
    await callback.answer("执行失败", show_alert=True)


@router.message(Command(CMD_INVEST))
async def cmd_reply_invest(message: types.Message):
    """回复目标用户消息并注资。用法：/cp_invest <金额>"""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "用法：回复目标用户消息，发送 /cp_invest <金额>\n"
            "示例：/cp_invest 5000"
        )
        return

    amount = _parse_amount(parts[1])
    if amount is None:
        await message.answer("金额必须为正整数。")
        return

    await _handle_reply_invest(message, amount)


@router.message(F.text.startswith(INVEST_KEYWORD))
async def msg_reply_invest_shortcut(message: types.Message, state: FSMContext):
    """快捷注资：回复消息并发送"投资5000"。"""
    if await state.get_state() is not None:
        return

    matched = INVEST_SHORTCUT_RE.match((message.text or "").strip())
    if not matched:
        return

    amount = _parse_amount(matched.group(1))
    if amount is None:
        await message.answer("金额格式无效，示例：投资5000")
        return

    await _handle_reply_invest(message, amount)


async def _refresh_shareholder_list(callback: types.CallbackQuery, company_id: int):
    """操作后刷新股东列表消息。"""
    tg_id = callback.from_user.id
    try:
        async with async_session() as session:
            from services.company_service import get_company_by_id
            company = await get_company_by_id(session, company_id)
            user = await get_user_by_tg_id(session, tg_id)
            is_owner = company and user and company.owner_id == user.id

            shareholders = await get_shareholders(session, company_id)
            lines = ["👥 股东列表", "─" * 24]
            for sh in shareholders:
                from db.models import User
                u = await session.get(User, sh.user_id)
                name = u.tg_name if u else "未知"
                lines.append(f"• {name}: {fmt_shares(sh.shares)} (注资: {fmt_traffic(sh.invested_amount)})")
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=shareholder_list_kb(company_id, tg_id=tg_id, is_owner=is_owner),
        )
    except Exception:
        pass  # 消息未变化时edit会抛异常，忽略


@router.callback_query(F.data.startswith("shareholder:list:"))
async def cb_shareholders(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id
    async with async_session() as session:
        from services.company_service import get_company_by_id
        company = await get_company_by_id(session, company_id)
        user = await get_user_by_tg_id(session, tg_id)
        is_owner = company and user and company.owner_id == user.id

        shareholders = await get_shareholders(session, company_id)
        lines = ["👥 股东列表", "─" * 24]
        for sh in shareholders:
            from db.models import User
            u = await session.get(User, sh.user_id)
            name = u.tg_name if u else "未知"
            lines.append(f"• {name}: {fmt_shares(sh.shares)} (注资: {fmt_traffic(sh.invested_amount)})")
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=shareholder_list_kb(company_id, tg_id=tg_id, is_owner=is_owner),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("shareholder:invest:"))
async def cb_invest_menu(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])
    await callback.message.edit_text("选择注资积分:", reply_markup=invest_kb(company_id, tg_id=callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data.startswith("shareholder:input:"))
async def cb_invest_input(callback: types.CallbackQuery, state: FSMContext):
    company_id = int(callback.data.split(":")[2])

    await state.set_state(InvestState.waiting_custom_amount)
    await state.update_data(company_id=company_id, started_ts=int(time.time()))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ 取消输入", callback_data=f"shareholder:input_cancel:{company_id}")],
        [InlineKeyboardButton(text="🔙 返回注资面板", callback_data=f"shareholder:invest:{company_id}")],
    ])
    await callback.message.edit_text(
        "✍️ 自定义注资积分\n"
        "请输入注资积分（整数，如 5000）\n"
        f"⏳ {INVEST_INPUT_TIMEOUT_SECONDS // 60} 分钟内未输入将自动退出",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("shareholder:input_cancel:"))
async def cb_invest_input_cancel(callback: types.CallbackQuery, state: FSMContext):
    company_id = int(callback.data.split(":")[2])
    await state.clear()
    await callback.message.edit_text("选择注资积分:", reply_markup=invest_kb(company_id, tg_id=callback.from_user.id))
    await callback.answer("已取消输入")


@router.message(InvestState.waiting_custom_amount, Command(CMD_CANCEL))
async def on_invest_input_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("已取消注资输入。")


@router.message(InvestState.waiting_custom_amount)
async def on_custom_invest_amount(message: types.Message, state: FSMContext):
    data = await state.get_data()
    company_id = int(data.get("company_id", 0))
    started_ts = int(data.get("started_ts", 0))
    now = int(time.time())

    if company_id <= 0:
        await state.clear()
        await message.answer("注资状态异常，已退出。")
        return

    if started_ts <= 0 or now - started_ts > INVEST_INPUT_TIMEOUT_SECONDS:
        await state.clear()
        await message.answer(
            f"⏳ 注资输入超时（>{INVEST_INPUT_TIMEOUT_SECONDS // 60}分钟），已自动退出。"
        )
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer("已退出注资输入模式。请重新发送命令继续。")
        return

    amount_str = text.replace(",", "").replace("_", "")
    try:
        amount = int(amount_str)
    except ValueError:
        left = max(1, INVEST_INPUT_TIMEOUT_SECONDS - (now - started_ts))
        await message.answer(
            f"请输入有效积分（整数，例如 5000）。剩余时间约 {left // 60}分{left % 60}秒"
        )
        return

    tg_id = message.from_user.id
    await state.clear()
    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await message.answer("请先 /cp_create 创建公司")
                return

    ok, msg, pending = await _queue_approval_or_invest_direct(
        bot=message.bot,
        investor_tg_id=tg_id,
        investor_user_id=user.id,
        investor_name=message.from_user.full_name or str(tg_id),
        target_company_id=company_id,
        amount=amount,
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="继续注资", callback_data=f"shareholder:invest:{company_id}")],
        [InlineKeyboardButton(text="返回公司", callback_data=f"company:view:{company_id}")],
    ])
    sent = await message.answer(msg, reply_markup=kb)
    await mark_panel(message.chat.id, sent.message_id, message.from_user.id)


@router.callback_query(F.data.startswith("shareholder:doinvest:"))
async def cb_do_invest(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    company_id = int(parts[2])
    amount = int(parts[3])
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await callback.answer("请先 /cp_create 创建公司", show_alert=True)
                return
            investor_user_id = user.id

    ok, msg, pending = await _queue_approval_or_invest_direct(
        bot=callback.bot,
        investor_tg_id=tg_id,
        investor_user_id=investor_user_id,
        investor_name=callback.from_user.full_name or str(tg_id),
        target_company_id=company_id,
        amount=amount,
    )

    await callback.answer(msg, show_alert=True)
    if ok and not pending:
        await _refresh_shareholder_list(callback, company_id)
