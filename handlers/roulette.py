"""Roulette handlers: PvP, Co-op, and Hell mode game flow."""

from __future__ import annotations

import asyncio
import logging
from html import escape as html_escape

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from commands import CMD_DEMON
from keyboards.menus import tag_kb
from services.roulette_service import (
    ITEM_NAME,
    MIN_BET,
    _alive_players,
    _current_turn_tg_id,
    _get_player,
    _is_devil,
    _settle_game,
    cancel_game,
    check_ttl_refund,
    consume_self_points,
    create_room,
    devil_execute_step,
    get_game_state,
    get_player_room,
    join_room,
    leave_room,
    player_shoot,
    player_use_item,
    pop_pending_display,
    render_game_panel,
    start_game,
)
from services.user_service import add_points_by_tg_id, get_points_by_tg_id
from utils.panel_owner import mark_panel

router = Router()
logger = logging.getLogger(__name__)

DEVIL_STEP_DELAY = 1.5  # seconds between devil actions
ROUND_MSG_DELAY = 0.8   # seconds between round transition lines
DEVIL_ANIMATION_MAX_STEPS = 80
TARGETABLE_ITEM_KEYS = {"handcuffs", "adrenaline"}


def _parse_demon_bet_arg(text: str | None) -> tuple[bool, int]:
    """Parse optional /cp_demon amount argument."""
    if not text:
        return True, 0
    parts = text.strip().split(maxsplit=1)
    if len(parts) <= 1:
        return True, 0
    raw = parts[1].strip().replace(",", "").replace("_", "")
    if not raw:
        return True, 0
    if not raw.isdigit():
        return False, 0
    return True, int(raw)


async def _animate_pending(callback: types.CallbackQuery, room_id: str, tg_id: int):
    """Animate pending display messages (round start, item distribution) line by line."""
    for _ in range(20):  # safety limit
        await asyncio.sleep(ROUND_MSG_DELAY)

        msg, has_more, state = await pop_pending_display(room_id=room_id)
        if not state or msg is None:
            break

        text = render_game_panel(state, tg_id)
        kb = _game_kb(state, tg_id)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass

        if not has_more:
            break


def _bet_kb(company_id: int, tg_id: int) -> InlineKeyboardMarkup:
    bets = [1_000, 3_000, 5_000, 10_000]
    rows = [
        [
            InlineKeyboardButton(
                text=f"{b // 1000}K",
                callback_data=f"roulette:create:{company_id}:{b}",
            )
            for b in bets
        ],
        [InlineKeyboardButton(text="🔄 刷新余额", callback_data=f"roulette:start:{company_id}")],
        [InlineKeyboardButton(text="🔙 返回", callback_data=f"company:view:{company_id}")],
    ]
    return tag_kb(InlineKeyboardMarkup(inline_keyboard=rows), tg_id)


def _owner_cb(callback_data: str, owner_tg_id: int) -> str:
    """Attach owner suffix for panel_auth middleware."""
    return f"{callback_data}|{owner_tg_id}"


def _waiting_kb(room_id: str, creator_tg_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="加入", callback_data=f"roulette:join:{room_id}")],
        [
            InlineKeyboardButton(
                text="开始",
                callback_data=_owner_cb(f"roulette:begin:{room_id}", creator_tg_id),
            ),
            InlineKeyboardButton(
                text="恶魔模式",
                callback_data=_owner_cb(f"roulette:demon:{room_id}", creator_tg_id),
            ),
        ],
        [
            InlineKeyboardButton(text="退出房间", callback_data=f"roulette:leave:{room_id}"),
            InlineKeyboardButton(
                text="关闭房间",
                callback_data=_owner_cb(f"roulette:cancel:{room_id}", creator_tg_id),
            ),
        ],
        [InlineKeyboardButton(text="刷新", callback_data=f"roulette:refresh:{room_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _game_kb(state, viewer_tg_id: int) -> InlineKeyboardMarkup:
    """Game keyboard — clean text labels."""
    rows: list[list[InlineKeyboardButton]] = []
    current = _current_turn_tg_id(state)

    if state.phase != "playing":
        rows.append([InlineKeyboardButton(text="刷新", callback_data=f"roulette:refresh:{state.room_id}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    if _is_devil(current):
        rows.append([InlineKeyboardButton(text="刷新 (魔鬼回合)", callback_data=f"roulette:refresh:{state.room_id}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    player = _get_player(state, current)
    if not player or not player["alive"]:
        rows.append([InlineKeyboardButton(text="刷新", callback_data=f"roulette:refresh:{state.room_id}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # Shoot buttons
    shoot_row: list[InlineKeyboardButton] = []
    for p in _alive_players(state):
        if p["tg_id"] == current:
            shoot_row.append(
                InlineKeyboardButton(
                    text="射自己",
                    callback_data=f"roulette:shoot:{state.room_id}:{current}",
                )
            )
        else:
            name = p["name"][:4]
            shoot_row.append(
                InlineKeyboardButton(
                    text=f"射{name}",
                    callback_data=f"roulette:shoot:{state.room_id}:{p['tg_id']}",
                )
            )
    if shoot_row:
        rows.append(shoot_row)

    # Item buttons — text names instead of emoji soup
    items = player.get("items", [])
    if items:
        item_row: list[InlineKeyboardButton] = []
        counts: dict[str, int] = {}
        for item in items:
            counts[item] = counts.get(item, 0) + 1

        phone_count = counts.pop("phone", 0)
        for item_key, count in counts.items():
            label = ITEM_NAME.get(item_key, item_key)
            if count > 1:
                label = f"{label}x{count}"
            item_row.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"roulette:use:{state.room_id}:{item_key}",
                )
            )
        if phone_count > 0:
            label = ITEM_NAME.get("phone", "一次性手机")
            if phone_count > 1:
                label = f"{label}x{phone_count}"
            item_row.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"roulette:use:{state.room_id}:phone",
                )
            )
        if item_row:
            rows.append(item_row)

    rows.append([InlineKeyboardButton(text="放弃(-50%)", callback_data=f"roulette:cancel:{state.room_id}")])
    rows.append([InlineKeyboardButton(text="刷新", callback_data=f"roulette:refresh:{state.room_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _target_item_kb(state, item_key: str) -> InlineKeyboardMarkup:
    current = _current_turn_tg_id(state)
    target_buttons: list[InlineKeyboardButton] = []
    for player in _alive_players(state):
        if player["tg_id"] == current:
            continue
        target_buttons.append(
            InlineKeyboardButton(
                text=player["name"][:6],
                callback_data=f"roulette:use:{state.room_id}:{item_key}:{player['tg_id']}",
            )
        )

    rows = [target_buttons[idx:idx + 3] for idx in range(0, len(target_buttons), 3)]
    rows.append([InlineKeyboardButton(text="返回", callback_data=f"roulette:refresh:{state.room_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _animate_devil_turn(callback: types.CallbackQuery, room_id: str, tg_id: int):
    """Animate devil actions one by one with delays between edits.
    Handles consecutive devil turns (multiple devils in hell mode)."""
    for _ in range(DEVIL_ANIMATION_MAX_STEPS):
        state = await get_game_state(room_id)
        if not state:
            return
        if state.pending_display:
            await _animate_pending(callback, room_id, tg_id)
            continue
        if state.phase != "playing" or not _is_devil(_current_turn_tg_id(state)):
            return

        await asyncio.sleep(DEVIL_STEP_DELAY)

        has_more, step_msgs, state = await devil_execute_step(room_id=room_id)
        if not state:
            return
        if not step_msgs:
            if state.pending_display:
                await _animate_pending(callback, room_id, tg_id)
                continue
            if state.phase == "playing" and _is_devil(_current_turn_tg_id(state)):
                continue
            return

        # Update panel after each devil action
        text = render_game_panel(state, tg_id)
        if state.phase == "finished":
            text += "\n\n" + html_escape(await _settle_game(state), quote=False)
        kb = _game_kb(state, tg_id)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass

        if state.phase != "playing":
            return

        if not has_more and not state.pending_display:
            next_state = await get_game_state(room_id)
            if not next_state or next_state.phase != "playing" or not _is_devil(_current_turn_tg_id(next_state)):
                return


@router.message(Command(CMD_DEMON))
async def cmd_cp_demon(message: types.Message):
    tg_id = message.from_user.id
    ok_arg, bet = _parse_demon_bet_arg(message.text)
    if not ok_arg:
        await message.answer("❌ 用法: /cp_demon [金额]\n例如: /cp_demon 5000")
        return

    from db.engine import async_session
    from services.company_service import get_companies_by_owner
    from services.user_service import get_user_by_tg_id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await message.answer("请先 /company_start 注册")
            return
        companies = await get_companies_by_owner(session, user.id)
        if not companies:
            await message.answer("你还没有公司，请先 /company_create")
            return
        company = companies[0]

    existing_room = await get_player_room(tg_id)
    if existing_room:
        state = await get_game_state(existing_room)
        if state:
            text = render_game_panel(state, tg_id)
            kb = _game_kb(state, tg_id)
            sent = await message.answer(text, reply_markup=kb, parse_mode="HTML")
            await mark_panel(sent.chat.id, sent.message_id, tg_id)
            return

    # Check for TTL-expired game and auto-refund 50%
    refund = await check_ttl_refund(tg_id)
    if refund > 0:
        await message.answer(f"⏰ 上一局轮盘赌超时失效，已全额退还 {refund:,} 积分")

    if bet <= 0:
        pts = await get_points_by_tg_id(tg_id)
        text = (
            f"😈 恶魔轮盘赌\n{'━' * 20}\n"
            f"💎 可下注余额: {pts:,}\n"
            f"选择赌注（最低 {MIN_BET:,} 积分）："
        )
        sent = await message.answer(text, reply_markup=_bet_kb(company.id, tg_id))
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
        return

    deducted = await consume_self_points(tg_id, bet)
    if not deducted:
        pts = await get_points_by_tg_id(tg_id)
        if pts < bet:
            await message.answer(f"❌ 积分不足 (当前 {pts:,}，需要 {bet:,})")
        else:
            await message.answer("❌ 操作繁忙，请重试")
        return

    room_id = str(tg_id)
    ok, msg, state = await create_room(
        room_id=room_id,
        creator_tg_id=tg_id,
        creator_company_id=company.id,
        creator_name=message.from_user.full_name,
        bet=bet,
    )
    if not ok:
        await add_points_by_tg_id(tg_id, bet, reason="roulette_create_failed_refund")
        await message.answer(msg)
        return

    text = render_game_panel(state, tg_id)
    kb = _waiting_kb(room_id, tg_id)
    sent = await message.answer(text, reply_markup=kb, parse_mode="HTML")
    await mark_panel(sent.chat.id, sent.message_id, tg_id)


@router.callback_query(F.data.startswith("roulette:start:"))
async def cb_roulette_start(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    company_id = int(parts[2])
    tg_id = callback.from_user.id

    existing_room = await get_player_room(tg_id)
    if existing_room:
        state = await get_game_state(existing_room)
        if state:
            text = render_game_panel(state, tg_id)
            kb = _game_kb(state, tg_id)
            try:
                await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
                await mark_panel(sent.chat.id, sent.message_id, tg_id)
            await callback.answer()
            return

    pts = await get_points_by_tg_id(tg_id)
    text = (
        f"😈 恶魔轮盘赌\n{'━' * 20}\n"
        f"💎 可下注余额: {pts:,}\n"
        f"选择赌注（最低 {MIN_BET:,} 积分）："
    )
    try:
        await callback.message.edit_text(text, reply_markup=_bet_kb(company_id, tg_id))
    except Exception:
        sent = await callback.message.answer(text, reply_markup=_bet_kb(company_id, tg_id))
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    await callback.answer()


@router.callback_query(F.data.startswith("roulette:create:"))
async def cb_roulette_create(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    company_id = int(parts[2])
    bet = int(parts[3])
    tg_id = callback.from_user.id

    from db.engine import async_session
    from db.models import Company
    from services.user_service import get_user_by_tg_id

    async with async_session() as session:
        company = await session.get(Company, company_id)
        if not company:
            await callback.answer("公司不存在", show_alert=True)
            return
        user_obj = await get_user_by_tg_id(session, tg_id)
        if not user_obj:
            await callback.answer("用户不存在", show_alert=True)
            return
        if company.owner_id != user_obj.id:
            await callback.answer("只有公司老板才能使用该公司参赌", show_alert=True)
            return
        player_name = callback.from_user.full_name

    ok = await consume_self_points(tg_id, bet)
    if not ok:
        pts = await get_points_by_tg_id(tg_id)
        if pts < bet:
            await callback.answer(f"积分不足 (当前 {pts:,}，需要 {bet:,})", show_alert=True)
        else:
            await callback.answer("操作繁忙，请重试", show_alert=True)
        return

    room_id = str(tg_id)
    ok, msg, state = await create_room(
        room_id=room_id,
        creator_tg_id=tg_id,
        creator_company_id=company_id,
        creator_name=player_name,
        bet=bet,
    )

    if not ok:
        await add_points_by_tg_id(tg_id, bet, reason="roulette_create_failed_refund")
        await callback.answer(msg, show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    kb = _waiting_kb(room_id, tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    await callback.answer()


@router.callback_query(F.data.startswith("roulette:join:"))
async def cb_roulette_join(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    room_id = parts[2]
    tg_id = callback.from_user.id

    state = await get_game_state(room_id)
    if not state:
        await callback.answer("房间不存在", show_alert=True)
        return
    bet = state.bet

    from db.engine import async_session
    from services.company_service import get_companies_by_owner
    from services.user_service import get_user_by_tg_id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await callback.answer("请先创建账号", show_alert=True)
            return
        companies = await get_companies_by_owner(session, user.id)
        if not companies:
            await callback.answer("你还没有公司", show_alert=True)
            return
        company = companies[0]
        company_id = company.id
        player_name = callback.from_user.full_name

    ok = await consume_self_points(tg_id, bet)
    if not ok:
        pts = await get_points_by_tg_id(tg_id)
        await callback.answer(f"积分不足 (当前 {pts:,}，需要 {bet:,})", show_alert=True)
        return

    ok, msg, state = await join_room(
        room_id=room_id,
        tg_id=tg_id,
        company_id=company_id,
        player_name=player_name,
    )
    if not ok:
        await add_points_by_tg_id(tg_id, bet, reason="roulette_join_failed_refund")
        await callback.answer(msg, show_alert=True)
        return

    # Auto-start when room is full (3 players)
    if len(state.players) >= 3:
        ok2, msg2, state2 = await start_game(room_id=room_id, tg_id=state.creator_tg_id, mode="pvp")
        if ok2 and state2:
            state = state2

    if state.phase == "playing":
        text = render_game_panel(state, tg_id)
        kb = _game_kb(state, tg_id)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
            await mark_panel(sent.chat.id, sent.message_id, tg_id)
        await callback.answer("房间已满，游戏开始!")

        if _is_devil(_current_turn_tg_id(state)):
            await _animate_devil_turn(callback, room_id, tg_id)
    else:
        text = render_game_panel(state, tg_id)
        kb = _waiting_kb(room_id, state.creator_tg_id)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
            await mark_panel(sent.chat.id, sent.message_id, tg_id)
        await callback.answer(msg)


@router.callback_query(F.data.startswith("roulette:begin:"))
async def cb_roulette_begin(callback: types.CallbackQuery):
    room_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    ok, msg, state = await start_game(room_id=room_id, tg_id=tg_id, mode="pvp")
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    kb = _game_kb(state, tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    await callback.answer("游戏开始!")

    # If devil goes first, animate step by step
    if state and state.phase == "playing" and _is_devil(_current_turn_tg_id(state)):
        await _animate_devil_turn(callback, room_id, tg_id)


@router.callback_query(F.data.startswith("roulette:demon:"))
async def cb_roulette_demon_menu(callback: types.CallbackQuery):
    """Show demon mode sub-menu: coop vs hell."""
    room_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    state = await get_game_state(room_id)
    if not state or state.phase != "waiting":
        await callback.answer("房间不存在或已开始", show_alert=True)
        return

    text = (
        f"😈 恶魔模式\n"
        f"{'━' * 20}\n"
        f"👥 多人单挑: 所有玩家协力对抗1个魔鬼\n"
        f"  胜利后奖池均分\n\n"
        f"🔥 地狱模式: 对抗2-3个魔鬼\n"
        f"  入场费 5x（共 {state.bet * 5:,} 积分/人）\n"
        f"  基础奖励10x，后续每轮再×1.5 (1轮=10x, 2轮=15x, 3轮=22.5x)\n"
        f"  输了损失员工（1轮:1-3人, 2轮:6-10人, 3轮+:20-50人）\n"
        f"{'━' * 20}\n"
        f"赌注: {state.bet:,} 积分/人"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="👥 多人单挑",
                callback_data=_owner_cb(f"roulette:coop:{room_id}", tg_id),
            ),
            InlineKeyboardButton(
                text="🔥 地狱模式",
                callback_data=_owner_cb(f"roulette:hell:{room_id}", tg_id),
            ),
        ],
        [InlineKeyboardButton(text="🔙 返回房间", callback_data=f"roulette:refresh:{room_id}")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("roulette:coop:"))
async def cb_roulette_coop(callback: types.CallbackQuery):
    """Start co-op mode: all humans vs 1 devil."""
    room_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    ok, msg, state = await start_game(room_id=room_id, tg_id=tg_id, mode="coop")
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    kb = _game_kb(state, tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    await callback.answer("协力模式开始!")

    if state and state.phase == "playing" and _is_devil(_current_turn_tg_id(state)):
        await _animate_devil_turn(callback, room_id, tg_id)


@router.callback_query(F.data.startswith("roulette:hell:"))
async def cb_roulette_hell(callback: types.CallbackQuery):
    """Start hell mode: humans vs multiple devils, 10x base reward then ×1.5 each round."""
    room_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    ok, msg, state = await start_game(room_id=room_id, tg_id=tg_id, mode="hell")
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    kb = _game_kb(state, tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    await callback.answer("地狱模式开始! 5倍入场费，输了扣员工!")

    if state and state.phase == "playing" and _is_devil(_current_turn_tg_id(state)):
        await _animate_devil_turn(callback, room_id, tg_id)


@router.callback_query(F.data.startswith("roulette:shoot:"))
async def cb_roulette_shoot(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    room_id = parts[2]
    target_tg_id = int(parts[3])
    tg_id = callback.from_user.id

    ok, msg, state = await player_shoot(room_id=room_id, shooter_tg_id=tg_id, target_tg_id=target_tg_id)
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    if state and state.phase == "finished" and msg:
        text += "\n\n" + html_escape(msg, quote=False)  # msg is settlement text only
    kb = _game_kb(state, tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    await callback.answer()

    # Animate pending round transition messages if any
    state = await get_game_state(room_id)
    if state and state.pending_display:
        await _animate_pending(callback, room_id, tg_id)

    # If it's now the devil's turn, animate step by step
    state = await get_game_state(room_id)
    if state and state.phase == "playing" and _is_devil(_current_turn_tg_id(state)):
        await _animate_devil_turn(callback, room_id, tg_id)


@router.callback_query(F.data.startswith("roulette:use:"))
async def cb_roulette_use_item(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    room_id = parts[2]
    item_key = parts[3]
    item_arg = 0
    if len(parts) >= 5:
        try:
            item_arg = int(parts[4])
        except (TypeError, ValueError):
            item_arg = 0
    tg_id = callback.from_user.id

    if item_key in TARGETABLE_ITEM_KEYS and item_arg == 0:
        state = await get_game_state(room_id)
        if not state:
            await callback.answer("游戏不存在", show_alert=True)
            return
        if state.phase != "playing":
            await callback.answer("游戏未在进行中", show_alert=True)
            return
        if _current_turn_tg_id(state) != tg_id:
            await callback.answer("还没轮到你", show_alert=True)
            return

        targets = [player for player in _alive_players(state) if player["tg_id"] != tg_id]
        if not targets:
            await callback.answer("没有可指定的目标", show_alert=True)
            return

        item_name = html_escape(ITEM_NAME.get(item_key, item_key), quote=False)
        text = render_game_panel(state, tg_id) + f"\n\n请选择「{item_name}」的目标："
        kb = _target_item_kb(state, item_key)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
            await mark_panel(sent.chat.id, sent.message_id, tg_id)
        await callback.answer()
        return

    ok, msg, state = await player_use_item(
        room_id=room_id,
        tg_id=tg_id,
        item_key=item_key,
        target_tg_id=item_arg,
    )
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    if state and state.phase == "finished" and msg:
        text += "\n\n" + html_escape(msg, quote=False)
    kb = _game_kb(state, tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await mark_panel(sent.chat.id, sent.message_id, tg_id)

    # Show magnifier result as popup
    player = _get_player(state, tg_id) if state else None
    if item_key == "magnifier" and player and player.get("known_shell"):
        shell_text = "实弹!" if player["known_shell"] == "live" else "空弹"
        await callback.answer(f"偷看结果: {shell_text}", show_alert=True)
    else:
        await callback.answer()

    # Animate pending round transition messages if any
    state = await get_game_state(room_id)
    if state and state.pending_display:
        await _animate_pending(callback, room_id, tg_id)

    # If it's now the devil's turn, animate step by step
    state = await get_game_state(room_id)
    if state and state.phase == "playing" and _is_devil(_current_turn_tg_id(state)):
        await _animate_devil_turn(callback, room_id, tg_id)


@router.callback_query(F.data.startswith("roulette:leave:"))
async def cb_roulette_leave(callback: types.CallbackQuery):
    room_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    ok, msg, state = await leave_room(room_id=room_id, tg_id=tg_id)
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    if state:
        text = render_game_panel(state, state.creator_tg_id)
        kb = _waiting_kb(room_id, state.creator_tg_id)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await callback.answer(msg)


@router.callback_query(F.data.startswith("roulette:cancel:"))
async def cb_roulette_cancel(callback: types.CallbackQuery):
    room_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    ok, msg = await cancel_game(room_id=room_id, tg_id=tg_id)

    # Check if game is still going (other players continue)
    state = await get_game_state(room_id)
    if state and state.phase == "playing":
        # Game continues — update panel with current state for remaining players
        text = render_game_panel(state, tg_id)
        kb = _game_kb(state, tg_id)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        await callback.answer(msg, show_alert=True)

        # If it's now the devil's turn, animate
        current = _current_turn_tg_id(state)
        if _is_devil(current):
            await _animate_devil_turn(callback, room_id, tg_id)
    else:
        try:
            await callback.message.edit_text(msg)
        except Exception:
            await callback.message.answer(msg)
        await callback.answer()


@router.callback_query(F.data.startswith("roulette:refresh:"))
async def cb_roulette_refresh(callback: types.CallbackQuery):
    room_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    state = await get_game_state(room_id)
    if not state:
        # TTL expired — auto-refund 50%
        refund = await check_ttl_refund(tg_id)
        if refund > 0:
            try:
                await callback.message.edit_text(
                    f"⏰ 轮盘赌超时失效，已全额退还 {refund:,} 积分"
                )
            except Exception:
                pass
            await callback.answer()
        else:
            await callback.answer("游戏已结束", show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    kb = _waiting_kb(room_id, state.creator_tg_id) if state.phase == "waiting" else _game_kb(state, tg_id)

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await callback.answer()

    state = await get_game_state(room_id)
    if state and state.phase == "playing" and _is_devil(_current_turn_tg_id(state)):
        await _animate_devil_turn(callback, room_id, tg_id)
