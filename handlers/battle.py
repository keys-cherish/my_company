"""Battle handler – reply to someone with /cp_battle to auto-PK."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command

from aiogram import types

from commands import CMD_BATTLE
from db.engine import async_session
from services.battle_service import battle

router = Router()
logger = logging.getLogger(__name__)


@router.message(Command(CMD_BATTLE))
async def cmd_battle(message: types.Message):
    """Initiate a business battle by replying to someone's message."""
    strategy = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2:
        strategy = parts[1].strip()

    if not message.reply_to_message:
        await message.answer(
            "⚔️ 使用方法: 回复某人的消息并发送 /cp_battle [战术]\n"
            "战术可选: 稳扎稳打 / 激进营销 / 奇袭渗透\n"
            "每次发起消耗 200 积分，商战可能触发收益Debuff/反噬"
        )
        return

    target = message.reply_to_message.from_user
    if not target or target.is_bot:
        await message.answer("❌ 不能对机器人发起商战")
        return

    attacker_tg_id = message.from_user.id
    defender_tg_id = target.id

    if attacker_tg_id == defender_tg_id:
        await message.answer("❌ 不能对自己发起商战")
        return

    try:
        # 检查攻击方是否有公司
        from services.user_service import get_user_by_tg_id
        from services.company_service import get_companies_by_owner
        async with async_session() as session:
            user = await get_user_by_tg_id(session, attacker_tg_id)
            if not user:
                await message.answer("请先 /cp_create 创建公司")
                return
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                await message.answer("❌ 你还没有公司，请先 /cp_create 创建公司")
                return

        async with async_session() as session:
            async with session.begin():
                ok, msg = await battle(
                    session,
                    attacker_tg_id,
                    defender_tg_id,
                    attacker_strategy=strategy,
                )

        await message.answer(msg)
    except Exception as e:
        logger.exception("battle command error")
        await message.answer(f"❌ 商战出错: {e}")
