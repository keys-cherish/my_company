"""Battle handler — reply to someone and either pick a random tactic or fight directly."""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from commands import CMD_BATTLE
from db.engine import async_session
from keyboards.menus import tag_kb
from services.battle_service import (
    BATTLE_POINT_COST,
    VALID_STRATEGY_HINT,
    battle,
    get_strategy_by_key,
    get_strategy_choices,
)
from utils.panel_owner import mark_panel

router = Router()
logger = logging.getLogger(__name__)


def _battle_strategy_text(defender_name: str, strategies) -> str:
    lines = [
        "⚔️ 商战战术选择",
        "─" * 24,
        f"目标：{defender_name}",
        f"确认后扣除：{BATTLE_POINT_COST} 积分",
        "本次随机战术（3选1）：",
    ]
    for idx, strategy in enumerate(strategies, 1):
        lines.append(f"{idx}. {strategy.name} — {strategy.summary}")
    lines.append("")
    lines.append(f"也可直接输入：/cp_battle [战术]，可选 {VALID_STRATEGY_HINT}")
    return "\n".join(lines)


def _battle_strategy_kb(defender_tg_id: int, strategies, tg_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{idx}. {strategy.name}", callback_data=f"battle:pick:{defender_tg_id}:{strategy.key}")]
        for idx, strategy in enumerate(strategies, 1)
    ]
    rows.append([
        InlineKeyboardButton(text="换一批", callback_data=f"battle:menu:{defender_tg_id}"),
        InlineKeyboardButton(text="取消", callback_data="battle:cancel"),
    ])
    return tag_kb(InlineKeyboardMarkup(inline_keyboard=rows), tg_id)


async def _get_target_name(tg_id: int, fallback_name: str | None = None) -> str:
    if fallback_name:
        return fallback_name
    from services.user_service import get_user_by_tg_id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
    return user.tg_name if user else str(tg_id)


async def _ensure_battle_ready(attacker_tg_id: int, defender_tg_id: int) -> tuple[bool, str]:
    from services.company_service import get_companies_by_owner
    from services.user_service import get_user_by_tg_id

    async with async_session() as session:
        attacker_user = await get_user_by_tg_id(session, attacker_tg_id)
        if not attacker_user:
            return False, "请先 /cp_create 创建公司"
        attacker_companies = await get_companies_by_owner(session, attacker_user.id)
        if not attacker_companies:
            return False, "❌ 你还没有公司，请先 /cp_create 创建公司"
        defender_user = await get_user_by_tg_id(session, defender_tg_id)
        if not defender_user:
            return False, "❌ 对方还未注册"
        defender_companies = await get_companies_by_owner(session, defender_user.id)
        if not defender_companies:
            return False, "❌ 对方没有公司，无法商战"
    return True, ""


async def _show_strategy_menu(
    target_message: types.Message,
    *,
    attacker_tg_id: int,
    defender_tg_id: int,
    defender_name: str | None = None,
) -> None:
    strategies = get_strategy_choices(3)
    text = _battle_strategy_text(await _get_target_name(defender_tg_id, defender_name), strategies)
    kb = _battle_strategy_kb(defender_tg_id, strategies, attacker_tg_id)
    sent = await target_message.answer(text, reply_markup=kb)
    await mark_panel(sent.chat.id, sent.message_id, attacker_tg_id)


async def _run_battle(attacker_tg_id: int, defender_tg_id: int, strategy_key: str | None) -> tuple[bool, str, dict]:
    async with async_session() as session:
        async with session.begin():
            return await battle(
                session,
                attacker_tg_id,
                defender_tg_id,
                attacker_strategy=strategy_key,
            )


@router.message(Command(CMD_BATTLE))
async def cmd_battle(message: types.Message):
    strategy = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2:
        strategy = parts[1].strip()

    if not message.reply_to_message:
        await message.answer(
            "⚔️ 用法：回复某人的消息后发送 /cp_battle [战术]\n"
            "不填战术时，会随机给出 3 个内置战术供你选择。\n"
            f"可直接输入的战术：{VALID_STRATEGY_HINT}\n"
            f"每次真正发起商战时消耗 {BATTLE_POINT_COST:,} 个人积分。"
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
        ok_ready, ready_msg = await _ensure_battle_ready(attacker_tg_id, defender_tg_id)
        if not ok_ready:
            await message.answer(ready_msg)
            return

        if strategy:
            ok, msg, info = await _run_battle(attacker_tg_id, defender_tg_id, strategy)
            await message.answer(msg)
            if ok and info:
                asyncio.create_task(_try_aftermath(message.bot, message.chat.id, info))
            return

        await _show_strategy_menu(
            message,
            attacker_tg_id=attacker_tg_id,
            defender_tg_id=defender_tg_id,
            defender_name=target.full_name,
        )
    except Exception as e:
        logger.exception("battle command error")
        await message.answer(f"❌ 商战出错: {e}")


@router.callback_query(F.data.startswith("battle:menu:"))
async def cb_battle_menu(callback: types.CallbackQuery):
    defender_tg_id = int(callback.data.split(":")[2])
    attacker_tg_id = callback.from_user.id
    defender_name = await _get_target_name(defender_tg_id)
    strategies = get_strategy_choices(3)
    text = _battle_strategy_text(defender_name, strategies)
    kb = _battle_strategy_kb(defender_tg_id, strategies, attacker_tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb)
        await mark_panel(sent.chat.id, sent.message_id, attacker_tg_id)
    await callback.answer()


@router.callback_query(F.data.startswith("battle:pick:"))
async def cb_battle_pick(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    defender_tg_id = int(parts[2])
    strategy_key = parts[3]
    strategy = get_strategy_by_key(strategy_key)
    if not strategy:
        await callback.answer("❌ 无效战术", show_alert=True)
        return

    try:
        ok, msg, info = await _run_battle(callback.from_user.id, defender_tg_id, strategy.key)
    except Exception as e:
        logger.exception("battle pick error")
        await callback.answer(f"❌ 商战出错: {e}", show_alert=True)
        return

    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    try:
        await callback.message.edit_text(msg)
    except Exception:
        await callback.message.answer(msg)
    await callback.answer()

    if info:
        asyncio.create_task(_try_aftermath(callback.bot, callback.message.chat.id, info))


@router.callback_query(F.data == "battle:cancel")
async def cb_battle_cancel(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text("已取消本次商战策略选择。")
    except Exception:
        pass
    await callback.answer()


# ── AI Battle Aftermath ──────────────────────────────────────────────────

def _aftermath_kb(defender_tg_id: int, choices: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    style_emoji = {"稳健": "🛡", "激进": "⚔️", "创意": "💡"}
    for i, c in enumerate(choices):
        emoji = style_emoji.get(c.get("style", ""), "🎯")
        rows.append([InlineKeyboardButton(
            text=f"{emoji} {c['title']} — {c['effect_desc']}",
            callback_data=f"battle:aftermath:{defender_tg_id}:{i}",
        )])
    rows.append([InlineKeyboardButton(text="跳过", callback_data=f"battle:aftermath:{defender_tg_id}:skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _try_aftermath(bot, chat_id: int, info: dict):
    """Background task: check if aftermath triggers and send choice panel to defender."""
    from services.battle_ai_service import (
        generate_aftermath_choices,
        save_aftermath_state,
        should_trigger_aftermath,
    )

    # Only trigger for the defender when the attacker won
    if not info.get("attacker_won"):
        return

    defender_tg_id = info.get("defender_tg_id", 0)
    if not defender_tg_id:
        return

    if not await should_trigger_aftermath(defender_tg_id):
        return

    try:
        choices = await generate_aftermath_choices(
            attacker_name=info.get("attacker_name", "?"),
            defender_name=info.get("defender_name", "?"),
            attacker_strategy=info.get("strategy_name", "稳扎稳打"),
            battle_result="攻击方获胜",
            loot=0,
            battle_damage=0,
        )
        if not choices:
            return

        await save_aftermath_state(
            defender_tg_id=defender_tg_id,
            attacker_company_id=info["attacker_company_id"],
            defender_company_id=info["defender_company_id"],
            choices=choices,
        )

        lines = [
            f"🎭 {info.get('defender_name', '你')} 遭到商战攻击！",
            f"{'─' * 24}",
            "你的顾问团队紧急拟定了3个应对方案，请尽快选择：",
            "",
        ]
        for i, c in enumerate(choices, 1):
            lines.append(f"{i}. 「{c['title']}」{c.get('style', '')}")
            lines.append(f"   {c['desc']}")
            lines.append(f"   效果: {c['effect_desc']}")
            lines.append("")
        lines.append("⏰ 5分钟内有效，过期作废")

        kb = _aftermath_kb(defender_tg_id, choices)
        await bot.send_message(chat_id, "\n".join(lines), reply_markup=kb)
    except Exception:
        logger.debug("Aftermath trigger failed", exc_info=True)


@router.callback_query(F.data.startswith("battle:aftermath:"))
async def cb_battle_aftermath(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    defender_tg_id = int(parts[2])
    choice_raw = parts[3]

    if callback.from_user.id != defender_tg_id:
        await callback.answer("只有被攻击方可以选择", show_alert=True)
        return

    from services.battle_ai_service import apply_aftermath_choice, load_aftermath_state

    state = await load_aftermath_state(defender_tg_id)
    if not state:
        await callback.answer("选项已过期", show_alert=True)
        try:
            await callback.message.edit_text("⏰ 商战应对选项已过期。")
        except Exception:
            pass
        return

    if choice_raw == "skip":
        try:
            await callback.message.edit_text("你选择了按兵不动，静观其变。")
        except Exception:
            pass
        await callback.answer()
        return

    try:
        choice_index = int(choice_raw)
    except (ValueError, TypeError):
        await callback.answer("无效选项", show_alert=True)
        return

    result_msg = await apply_aftermath_choice(defender_tg_id, choice_index, state)
    try:
        await callback.message.edit_text(result_msg)
    except Exception:
        await callback.message.answer(result_msg)
    await callback.answer()
