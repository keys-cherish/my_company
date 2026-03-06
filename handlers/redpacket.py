"""公司红包处理器 — 发红包/抢红包。

Usage:
  /cp_redpacket <总金额> <个数> [口令]   — 发红包（从公司积分扣除）
  点击「🧧 抢红包」按钮                      — 抢红包（奖励存入个人/公司）
  口令红包需要输入正确口令才能抢
"""

from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from commands import CMD_REDPACKET
from config import settings
from db.engine import async_session
from services.company_service import add_funds, get_companies_by_owner
from services.redpacket_service import (
    check_password,
    create_redpacket,
    find_lucky_king,
    get_grabber_display_names,
    get_redpacket_info,
    get_redpacket_results,
    grab_redpacket,
    has_password,
    save_grabber_display_name,
)
from services.user_service import add_traffic, get_or_create_user, get_user_by_tg_id
from utils.formatters import fmt_traffic
from utils.panel_owner import mark_panel

router = Router()


class RedpacketState(StatesGroup):
    waiting_password = State()


def _grab_kb(packet_id: str, has_pw: bool = False) -> types.InlineKeyboardMarkup:
    """Red packet action button (no tag_kb — anyone can grab)."""
    if has_pw:
        return types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔐 输入口令抢红包", callback_data=f"rp:pw:{packet_id}")],
        ])
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🧧 抢红包!", callback_data=f"rp:grab:{packet_id}")],
    ])


async def _resolve_grabber_names(tg_ids: list[int], snapshot_names: dict[int, str]) -> dict[int, str]:
    """Resolve display names for grabbers: snapshot first, DB fallback."""
    name_map: dict[int, str] = dict(snapshot_names)
    missing = [tid for tid in set(tg_ids) if tid not in name_map]
    if not missing:
        return name_map

    from db.models import User

    async with async_session() as session:
        rows = (await session.execute(
            select(User.tg_id, User.tg_name).where(User.tg_id.in_(missing))
        )).all()

    for tg_id, tg_name in rows:
        if tg_name:
            name_map[int(tg_id)] = str(tg_name)

    for tid in missing:
        if tid not in name_map:
            name_map[tid] = f"用户{tid}"
    return name_map


async def _render_redpacket_detail(
    packet_id: str,
) -> tuple[str, types.InlineKeyboardMarkup | None] | tuple[None, None]:
    info = await get_redpacket_info(packet_id)
    if not info:
        return None, None

    results = await get_redpacket_results(packet_id)
    snapshot_names = await get_grabber_display_names(packet_id)

    total = int(info.get("total", 0))
    remaining = int(info.get("remaining", 0))
    count = int(info.get("count", 0))
    remaining_count = int(info.get("remaining_count", 0))
    company_name = info.get("company_name", "?")
    has_pw = bool(info.get("password"))
    claimed = total - remaining
    claimed_count = count - remaining_count

    lucky = await find_lucky_king(packet_id) if results else None
    name_map = await _resolve_grabber_names([tid for tid, _ in results], snapshot_names)

    lines = [
        f"🧧 红包详情｜「{company_name}」",
        f"💰 进度：总额 {total:,} | 已领 {claimed:,} | 剩余 {remaining:,}",
        f"📦 份数：总数 {count} | 已抢 {claimed_count} | 剩余 {remaining_count}",
        f"🏆 手气最佳额外+{int(settings.redpacket_lucky_bonus_pct * 100)}%奖金",
    ]
    if has_pw:
        lines.append("🔐 口令红包")

    if results:
        lines.append(f"🧾 领取记录（{len(results)}/{count}）:")
        for tg_id, amt in results:
            crown = " 👑" if lucky and tg_id == lucky[0] else ""
            who = name_map.get(tg_id, f"用户{tg_id}")
            lines.append(f"• {who}：{amt:,} 积分{crown}")
    else:
        lines.append("🧾 暂无领取记录")

    kb = _grab_kb(packet_id, has_pw) if remaining_count > 0 else None
    return "\n".join(lines), kb


def _looks_like_detail_message(text: str | None) -> bool:
    raw = text or ""
    return ("红包详情" in raw) or ("领取记录" in raw) or ("总数:" in raw and "已抢:" in raw)


async def _refresh_detail_message_if_needed(callback: types.CallbackQuery, packet_id: str, force: bool = False):
    if not callback.message:
        return
    if not force and not _looks_like_detail_message(callback.message.text):
        return

    detail_text, kb = await _render_redpacket_detail(packet_id)
    if not detail_text:
        return
    try:
        await callback.message.edit_text(detail_text, reply_markup=kb)
    except Exception:
        pass


@router.message(Command(CMD_REDPACKET))
async def cmd_redpacket(message: types.Message):
    """发红包命令：/cp_redpacket <总金额> <个数> [口令]"""
    tg_id = message.from_user.id
    args = (message.text or "").split()

    if len(args) < 3:
        await message.answer(
            "🧧 公司红包\n"
            f"{'─' * 24}\n"
            "用法: /cp_redpacket <总金额> <个数> [口令]\n"
            "例: /cp_redpacket 5000 5\n"
            "例: /cp_redpacket 5000 5 恭喜发财\n"
            f"{'─' * 24}\n"
            f"最低: {settings.redpacket_min_amount:,} 积分\n"
            f"最高: {settings.redpacket_max_amount:,} 积分\n"
            "红包从公司积分中扣除\n"
            "群里所有人都可以抢！\n"
            "设置口令后需要输入正确口令才能抢"
        )
        return

    try:
        total_amount = int(args[1])
        count = int(args[2])
    except ValueError:
        await message.answer("❌ 金额和个数必须是数字")
        return

    # 可选口令
    password = args[3] if len(args) > 3 else ""

    # Get company
    async with async_session() as session:
        async with session.begin():
            user, _ = await get_or_create_user(session, tg_id, message.from_user.full_name)
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                await message.answer("❌ 你还没有公司，无法发红包")
                return
            company = companies[0]

            # Pre-validate
            ok, err_msg, _ = await create_redpacket(tg_id, company.name, total_amount, count, password)
            if not ok:
                await message.answer(err_msg)
                return

            # Deduct funds
            deduct_ok = await add_funds(
                session, company.id, -total_amount,
                reason=f"发红包 ({count}个)"
            )
            if not deduct_ok:
                await message.answer(
                    f"❌ 公司积分不足！需要 {fmt_traffic(total_amount)}，"
                    f"当前余额 {fmt_traffic(company.cp_points)}"
                )
                return

            company_name = company.name

    # Create the actual red packet (after successful deduction)
    ok, err_msg, packet_id = await create_redpacket(tg_id, company_name, total_amount, count, password)
    if not ok:
        # Rollback funds if creation somehow fails
        async with async_session() as session:
            async with session.begin():
                await add_funds(session, company.id, total_amount, reason="红包创建失败退款")
        await message.answer(err_msg)
        return

    detail_text, kb = await _render_redpacket_detail(packet_id)
    if not detail_text:
        await message.answer("❌ 红包创建成功，但详情加载失败，请稍后重试")
        return
    sent = await message.reply(detail_text, reply_markup=kb)
    await mark_panel(sent.chat.id, sent.message_id, tg_id)


@router.callback_query(F.data.startswith("rp:pw:"))
async def cb_password_prompt(callback: types.CallbackQuery, state: FSMContext):
    """口令红包 — 提示输入口令"""
    packet_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    # 检查是否已经抢过
    info = await get_redpacket_info(packet_id)
    if not info:
        await callback.answer("❌ 红包已过期", show_alert=True)
        return

    from cache.redis_client import get_redis
    r = await get_redis()
    already = await r.sismember(f"redpacket_grabs:{packet_id}", str(tg_id))
    if already:
        await callback.answer("🧧 你已经抢过这个红包了", show_alert=True)
        return

    remaining_count = int(info.get("remaining_count", 0))
    if remaining_count <= 0:
        await callback.answer("🧧 红包已被抢完了！", show_alert=True)
        return

    # 保存状态，等待口令输入
    await state.set_state(RedpacketState.waiting_password)
    await state.update_data(packet_id=packet_id)

    await callback.answer()
    await callback.message.answer(
        "🔐 请输入红包口令：\n"
        "（直接发送口令文本即可）\n"
        "发送 /cp_cancel 取消"
    )


@router.message(RedpacketState.waiting_password)
async def on_password_input(message: types.Message, state: FSMContext):
    """处理口令输入"""
    text = (message.text or "").strip()

    if text.startswith("/"):
        await state.clear()
        await message.answer("已取消")
        return

    data = await state.get_data()
    packet_id = data.get("packet_id")
    if not packet_id:
        await state.clear()
        await message.answer("❌ 红包状态异常，请重新点击按钮")
        return

    # 验证口令
    ok, err = await check_password(packet_id, text)
    if not ok:
        await message.answer(f"{err}\n请重新输入口令，或发送 /cp_cancel 取消")
        return

    await state.clear()

    # 口令正确，执行抢红包
    tg_id = message.from_user.id
    ok, msg, amount = await grab_redpacket(tg_id, packet_id)

    if not ok:
        await message.answer(msg)
        return

    await save_grabber_display_name(packet_id, tg_id, message.from_user.full_name or "")

    # Deposit reward
    async with async_session() as session:
        async with session.begin():
            user, _ = await get_or_create_user(session, tg_id, message.from_user.full_name)
            companies = await get_companies_by_owner(session, user.id)
            if companies:
                await add_funds(session, companies[0].id, amount, reason="抢红包")
                dest = f"「{companies[0].name}」"
            else:
                await add_traffic(session, user.id, amount, reason="抢红包")
                dest = "个人账户"

    await message.answer(f"🧧 口令正确！抢到 {amount:,} 积分！\n已存入{dest}")

    # Check if fully claimed
    await _check_and_announce_lucky(packet_id)


async def _check_and_announce_lucky(packet_id: str):
    """检查红包是否抢完，公布手气最佳"""
    info = await get_redpacket_info(packet_id)
    if not info:
        return

    remaining_count = int(info.get("remaining_count", 0))
    if remaining_count > 0:
        return

    lucky = await find_lucky_king(packet_id)
    if not lucky:
        return

    lucky_tg_id, lucky_amount = lucky
    bonus = int(lucky_amount * settings.redpacket_lucky_bonus_pct)
    if bonus <= 0:
        return

    # Give lucky bonus
    async with async_session() as session:
        async with session.begin():
            lucky_user = await get_user_by_tg_id(session, lucky_tg_id)
            if lucky_user:
                lk_companies = await get_companies_by_owner(session, lucky_user.id)
                if lk_companies:
                    await add_funds(session, lk_companies[0].id, bonus, reason="手气最佳奖金")
                else:
                    await add_traffic(session, lucky_user.id, bonus, reason="手气最佳奖金")


@router.callback_query(F.data.startswith("rp:grab:"))
async def cb_grab_redpacket(callback: types.CallbackQuery):
    """抢红包（无口令）"""
    packet_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    # 检查是否需要口令
    if await has_password(packet_id):
        await callback.answer("🔐 这是口令红包，请点击「输入口令抢红包」", show_alert=True)
        return

    ok, msg, amount = await grab_redpacket(tg_id, packet_id)

    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    await save_grabber_display_name(packet_id, tg_id, callback.from_user.full_name or "")

    # Deposit reward
    async with async_session() as session:
        async with session.begin():
            user, _ = await get_or_create_user(session, tg_id, callback.from_user.full_name)
            companies = await get_companies_by_owner(session, user.id)
            if companies:
                await add_funds(session, companies[0].id, amount, reason="抢红包")
                dest = f"「{companies[0].name}」"
            else:
                await add_traffic(session, user.id, amount, reason="抢红包")
                dest = "个人账户"

    # Check if packet is fully claimed
    info = await get_redpacket_info(packet_id)
    remaining_count = int(info["remaining_count"]) if info else 0

    if remaining_count <= 0 and info:
        # Packet fully claimed — announce lucky king
        lucky = await find_lucky_king(packet_id)
        if lucky:
            lucky_tg_id, lucky_amount = lucky
            bonus = int(lucky_amount * settings.redpacket_lucky_bonus_pct)
            if bonus > 0:
                # Give lucky bonus
                async with async_session() as session:
                    async with session.begin():
                        lucky_user = await get_user_by_tg_id(session, lucky_tg_id)
                        if lucky_user:
                            lk_companies = await get_companies_by_owner(session, lucky_user.id)
                            if lk_companies:
                                await add_funds(session, lk_companies[0].id, bonus, reason="手气最佳奖金")
                            else:
                                await add_traffic(session, lucky_user.id, bonus, reason="手气最佳奖金")

        await _refresh_detail_message_if_needed(callback, packet_id, force=True)
    else:
        # Always refresh current packet message to keep records real-time.
        await _refresh_detail_message_if_needed(callback, packet_id, force=True)

    await callback.answer(
        f"🧧 抢到 {amount:,} 积分！已存入{dest}",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("rp:info:"))
async def cb_redpacket_info(callback: types.CallbackQuery):
    """查看红包详情"""
    packet_id = callback.data.split(":")[2]

    info = await get_redpacket_info(packet_id)
    if not info:
        await callback.answer("红包已过期", show_alert=True)
        return

    await callback.answer()
    detail_text, kb = await _render_redpacket_detail(packet_id)
    if not detail_text:
        return
    try:
        await callback.message.edit_text(detail_text, reply_markup=kb)
    except Exception:
        await callback.message.answer(detail_text, reply_markup=kb)
