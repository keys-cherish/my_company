"""🎰 老虎机游戏 — 每日奖励仅一次，可重复游玩。"""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router, types
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from keyboards.menus import tag_kb
from services.slot_service import do_spin
from utils.panel_owner import mark_panel

router = Router()
logger = logging.getLogger(__name__)

CMD_SLOT = "cp_slot"


# ── 命令入口 ──────────────────────────────────────────

@router.message(Command(CMD_SLOT))
async def cmd_slot(message: types.Message):
    """老虎机命令入口。"""
    tg_id = message.from_user.id
    result_text = await do_spin(tg_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 再来一次!", callback_data="slot:spin")],
        [InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:main")],
    ])
    try:
        sent = await message.reply(result_text, reply_markup=tag_kb(kb, tg_id))
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    except TelegramRetryAfter as exc:
        # Group-level flood control: wait once and retry to avoid silent failure.
        wait_seconds = max(1, int(exc.retry_after))
        logger.warning("slot cmd flood-limited, retry in %ss", wait_seconds)
        await asyncio.sleep(min(wait_seconds, 5))
        try:
            sent = await message.reply(result_text, reply_markup=tag_kb(kb, tg_id))
            await mark_panel(sent.chat.id, sent.message_id, tg_id)
        except TelegramRetryAfter:
            logger.warning("slot cmd still flood-limited after retry")
            return


@router.callback_query(F.data == "slot:spin")
async def cb_slot_spin(callback: types.CallbackQuery):
    """老虎机按钮再来一次。"""
    tg_id = callback.from_user.id
    result_text = await do_spin(tg_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 再来一次!", callback_data="slot:spin")],
        [InlineKeyboardButton(text="🔙 返回主菜单", callback_data="menu:main")],
    ])
    try:
        await callback.message.edit_text(result_text, reply_markup=tag_kb(kb, tg_id))
    except TelegramRetryAfter as exc:
        await callback.answer(f"频道限流中，请 {int(exc.retry_after)} 秒后再试。", show_alert=True)
        return
    except Exception:
        try:
            sent = await callback.message.answer(result_text, reply_markup=tag_kb(kb, tg_id))
            await mark_panel(sent.chat.id, sent.message_id, tg_id)
        except TelegramRetryAfter as exc:
            await callback.answer(f"频道限流中，请 {int(exc.retry_after)} 秒后再试。", show_alert=True)
            return
    await callback.answer()
