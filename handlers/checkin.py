"""每日打卡处理器。"""

from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from commands import CMD_CHECKIN
from db.engine import async_session
from keyboards.menus import tag_kb
from services.checkin_service import do_checkin
from services.user_service import get_or_create_user, add_traffic
from utils.panel_owner import mark_panel

router = Router()


def _checkin_kb(tg_id: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 打卡签到", callback_data="checkin:do")],
        [InlineKeyboardButton(text="🔙 返回", callback_data="menu:main")],
    ])
    return tag_kb(kb, tg_id)


@router.message(Command(CMD_CHECKIN))
async def cmd_checkin(message: types.Message):
    """命令入口：/company_checkin"""
    tg_id = message.from_user.id
    await _do_checkin(message, tg_id, is_callback=False)


@router.callback_query(F.data == "checkin:do")
async def cb_checkin(callback: types.CallbackQuery):
    """按钮打卡"""
    tg_id = callback.from_user.id
    await _do_checkin(callback, tg_id, is_callback=True)


@router.callback_query(F.data == "menu:checkin")
async def cb_checkin_menu(callback: types.CallbackQuery):
    """从主菜单进入打卡界面"""
    tg_id = callback.from_user.id
    text = (
        "🏢 每日打卡\n"
        f"{'─' * 24}\n"
        "每天打卡领取积分奖励！\n"
        "连续打卡7天可开启宝箱！\n"
        "断签重新计算连续天数。\n"
        f"{'─' * 24}\n"
        "点击下方按钮开始打卡 👇"
    )
    await callback.message.edit_text(text, reply_markup=_checkin_kb(tg_id))
    await callback.answer()


async def _do_checkin(event: types.Message | types.CallbackQuery, tg_id: int, *, is_callback: bool):
    """Core check-in logic shared by command and callback."""
    # Ensure user exists
    async with async_session() as session:
        async with session.begin():
            user, _ = await get_or_create_user(session, tg_id, event.from_user.full_name)

    success, msg, reward = await do_checkin(tg_id)

    if success and reward > 0:
        async with async_session() as session:
            async with session.begin():
                from services.user_service import get_user_by_tg_id
                user = await get_user_by_tg_id(session, tg_id)
                if user:
                    # 打卡奖励发到个人积分
                    await add_traffic(session, user.id, reward, reason="每日打卡")
                    msg += "\n\n💰 奖励已存入个人账户"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 我的公司", callback_data="menu:company")],
        [InlineKeyboardButton(text="🔙 主菜单", callback_data="menu:main")],
    ])
    kb = tag_kb(kb, tg_id)

    if is_callback:
        callback = event
        try:
            await callback.message.edit_text(msg, reply_markup=kb)
        except Exception:
            await callback.message.answer(msg, reply_markup=kb)
        await callback.answer()
    else:
        message = event
        sent = await message.reply(msg, reply_markup=kb)
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
