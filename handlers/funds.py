"""资金相关处理器 — 转账、流水查询。"""

from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command

from commands import CMD_LOG, CMD_TRANSFER
from config import settings
from db.engine import async_session
from services.company_service import get_companies_by_owner, get_company_by_id
from services.fundlog_service import format_log_entry, get_fund_logs
from services.user_service import add_traffic, get_user_by_tg_id
from utils.formatters import fmt_traffic

router = Router()

# 转账税率使用统一税率
TRANSFER_TAX_RATE = settings.tax_rate


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


# ---- /cp_log 查询流水 ----

@router.message(Command(CMD_LOG))
async def cmd_log(message: types.Message):
    """查询资金流水：/cp_log [user|company]"""
    tg_id = message.from_user.id
    args = (message.text or "").split()
    log_type = args[1].lower() if len(args) > 1 else "all"

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await message.answer("请先 /cp_start 注册账号")
            return

        companies = await get_companies_by_owner(session, user.id)
        company = companies[0] if companies else None

    lines = ["📒 资金流水记录", "─" * 24]

    # 个人流水
    if log_type in ("all", "user", "个人"):
        user_logs = await get_fund_logs("user", user.id, limit=15)
        lines.append(f"\n👤 个人账户流水:")
        if user_logs:
            for entry in user_logs:
                lines.append(f"  {format_log_entry(entry)}")
        else:
            lines.append("  暂无记录")

    # 公司流水
    if log_type in ("all", "company", "公司") and company:
        company_logs = await get_fund_logs("company", company.id, limit=15)
        lines.append(f"\n🏢 {company.name} 公司流水:")
        if company_logs:
            for entry in company_logs:
                lines.append(f"  {format_log_entry(entry)}")
        else:
            lines.append("  暂无记录")
    elif log_type in ("company", "公司") and not company:
        lines.append("\n🏢 你还没有公司")

    lines.append(f"\n💡 用法: /cp_log [user|company]")
    await message.answer("\n".join(lines))


# ---- /cp_transfer 转账 ----

@router.message(Command(CMD_TRANSFER))
async def cmd_transfer(message: types.Message):
    """转账命令：/cp_transfer <金额>

    回复目标用户的消息，发送此命令进行转账。
    """
    tg_id = message.from_user.id
    args = (message.text or "").split()

    if len(args) < 2:
        await message.answer(
            f"💸 转账命令用法:\n"
            f"回复目标用户的消息，发送:\n"
            f"/cp_transfer <金额>\n\n"
            f"示例: /cp_transfer 1000\n\n"
            f"⚠️ 转账税率: {int(TRANSFER_TAX_RATE * 100)}%\n"
            f"从你的个人余额转给对方（税后到账）"
        )
        return

    # 检查是否回复了消息
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("❌ 请回复目标用户的消息，再发送转账命令")
        return

    target_tg_user = message.reply_to_message.from_user
    if target_tg_user.is_bot:
        await message.answer("❌ 无法向机器人转账")
        return
    if target_tg_user.id == tg_id:
        await message.answer("❌ 不能给自己转账")
        return

    amount = _parse_amount(args[1])
    if amount is None:
        await message.answer("❌ 金额必须为正整数")
        return

    if amount < 100:
        await message.answer("❌ 最低转账金额为 100")
        return

    # 计算税后金额
    tax = int(amount * TRANSFER_TAX_RATE)
    net_amount = amount - tax

    async with async_session() as session:
        async with session.begin():
            sender = await get_user_by_tg_id(session, tg_id)
            if not sender:
                await message.answer("请先 /cp_start 注册账号")
                return

            if sender.traffic < amount:
                await message.answer(
                    f"❌ 余额不足！\n"
                    f"需要: {fmt_traffic(amount)}\n"
                    f"当前: {fmt_traffic(sender.traffic)}"
                )
                return

            receiver = await get_user_by_tg_id(session, target_tg_user.id)
            if not receiver:
                await message.answer("❌ 对方尚未注册账号")
                return

            # 扣除发送方
            sender_name = message.from_user.full_name or f"User {tg_id}"
            receiver_name = target_tg_user.full_name or f"User {target_tg_user.id}"

            ok = await add_traffic(
                session, sender.id, -amount,
                reason=f"转账给 {receiver_name}"
            )
            if not ok:
                await message.answer("❌ 扣款失败，请重试")
                return

            # 给接收方（税后）
            ok = await add_traffic(
                session, receiver.id, net_amount,
                reason=f"来自 {sender_name} 的转账"
            )
            if not ok:
                # 回滚
                await add_traffic(session, sender.id, amount, reason="转账失败退款")
                await message.answer("❌ 转账失败，已退款")
                return

    await message.answer(
        f"✅ 转账成功!\n"
        f"{'─' * 24}\n"
        f"👤 收款人: {receiver_name}\n"
        f"💸 转账金额: {fmt_traffic(amount)}\n"
        f"📊 转账税率: {int(TRANSFER_TAX_RATE * 100)}%\n"
        f"🏛️ 税金扣除: {fmt_traffic(tax)}\n"
        f"💰 实际到账: {fmt_traffic(net_amount)}"
    )
