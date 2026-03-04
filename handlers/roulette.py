"""Roulette handlers: PvP and solo-vs-devil game flow."""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from keyboards.menus import tag_kb
from services.roulette_service import (
    DEVIL_TG_ID,
    ITEM_NAME,
    MIN_BET,
    _alive_players,
    _current_turn_tg_id,
    _get_player,
    _settle_game,
    cancel_game,
    consume_points,
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
from services.user_service import add_traffic_by_tg_id, get_traffic_by_tg_id
from utils.panel_owner import mark_panel

router = Router()
logger = logging.getLogger(__name__)

DEVIL_STEP_DELAY = 1.5  # seconds between devil actions
ROUND_MSG_DELAY = 0.8   # seconds between round transition lines


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
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass

        if not has_more:
            break


def _bet_kb(company_id: int, tg_id: int) -> InlineKeyboardMarkup:
    bets = [5_000, 10_000, 25_000, 50_000]
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
                text="单挑魔鬼",
                callback_data=_owner_cb(f"roulette:solo:{room_id}", creator_tg_id),
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

    if current == DEVIL_TG_ID:
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
            name = "魔鬼" if p["is_devil"] else p["name"][:4]
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
        rows.append(item_row)

    rows.append([InlineKeyboardButton(text="放弃(-50%)", callback_data=f"roulette:cancel:{state.room_id}")])
    rows.append([InlineKeyboardButton(text="刷新", callback_data=f"roulette:refresh:{state.room_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _animate_devil_turn(callback: types.CallbackQuery, room_id: str, tg_id: int):
    """Animate devil actions one by one with delays between edits."""
    for _ in range(20):  # safety limit
        await asyncio.sleep(DEVIL_STEP_DELAY)

        has_more, step_msgs, state = await devil_execute_step(room_id=room_id)
        if not state or not step_msgs:
            break

        # Update panel after each devil action
        text = render_game_panel(state, tg_id)
        if state.phase == "finished":
            text += "\n\n" + await _settle_game(state)
        kb = _game_kb(state, tg_id)
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass

        if not has_more or state.phase != "playing":
            break

    # After devil is done, animate any pending round transitions
    state = await get_game_state(room_id)
    if state and state.pending_display:
        await _animate_pending(callback, room_id, tg_id)

    # After devil is done, if game continues and it's a human's turn, notify
    state = await get_game_state(room_id)
    if state and state.phase == "playing":
        next_tid = _current_turn_tg_id(state)
        next_p = _get_player(state, next_tid)
        if next_p and not next_p.get("is_devil") and next_tid != tg_id:
            try:
                await callback.message.answer(
                    f"魔鬼行动完毕，轮到 "
                    f"<a href='tg://user?id={next_tid}'>{next_p['name']}</a>",
                    parse_mode="HTML",
                )
            except Exception:
                pass


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
                await callback.message.edit_text(text, reply_markup=kb)
            except Exception:
                sent = await callback.message.answer(text, reply_markup=kb)
                await mark_panel(sent.chat.id, sent.message_id, tg_id)
            await callback.answer()
            return

    pts = await get_traffic_by_tg_id(tg_id)
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
        player_name = company.name

    ok = await consume_points(tg_id, bet)
    if not ok:
        pts = await get_traffic_by_tg_id(tg_id)
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
        await add_traffic_by_tg_id(tg_id, bet, reason="roulette_create_failed_refund")
        await callback.answer(msg, show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    kb = _waiting_kb(room_id, tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb)
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
        player_name = company.name

    ok = await consume_points(tg_id, bet)
    if not ok:
        pts = await get_traffic_by_tg_id(tg_id)
        await callback.answer(f"积分不足 (当前 {pts:,}，需要 {bet:,})", show_alert=True)
        return

    ok, msg, state = await join_room(
        room_id=room_id,
        tg_id=tg_id,
        company_id=company_id,
        player_name=player_name,
    )
    if not ok:
        await add_traffic_by_tg_id(tg_id, bet, reason="roulette_join_failed_refund")
        await callback.answer(msg, show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    kb = _waiting_kb(room_id, state.creator_tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb)
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    await callback.answer(msg)


@router.callback_query(F.data.startswith("roulette:begin:"))
async def cb_roulette_begin(callback: types.CallbackQuery):
    room_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    ok, msg, state = await start_game(room_id=room_id, tg_id=tg_id, solo_vs_devil=False)
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    kb = _game_kb(state, tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb)
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    await callback.answer("游戏开始!")

    # If devil goes first, animate step by step
    if state and state.phase == "playing" and _current_turn_tg_id(state) == DEVIL_TG_ID:
        await _animate_devil_turn(callback, room_id, tg_id)


@router.callback_query(F.data.startswith("roulette:solo:"))
async def cb_roulette_solo(callback: types.CallbackQuery):
    room_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    ok, msg, state = await start_game(room_id=room_id, tg_id=tg_id, solo_vs_devil=True)
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    kb = _game_kb(state, tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb)
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    await callback.answer("单挑模式开始!")

    # If devil goes first, animate step by step
    if state and state.phase == "playing" and _current_turn_tg_id(state) == DEVIL_TG_ID:
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
        text += f"\n\n{msg}"  # msg is settlement text only
    kb = _game_kb(state, tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb)
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    await callback.answer()

    # Animate pending round transition messages if any
    state = await get_game_state(room_id)
    if state and state.pending_display:
        await _animate_pending(callback, room_id, tg_id)

    # If it's now the devil's turn, animate step by step
    state = await get_game_state(room_id)
    if state and state.phase == "playing" and _current_turn_tg_id(state) == DEVIL_TG_ID:
        await _animate_devil_turn(callback, room_id, tg_id)
    elif state and state.phase == "playing":
        # Notify next human player
        next_tid = _current_turn_tg_id(state)
        next_p = _get_player(state, next_tid)
        if next_p and not next_p.get("is_devil") and next_tid != tg_id:
            try:
                shooter_name = callback.from_user.first_name or "?"
                await callback.message.answer(
                    f"{shooter_name} 已行动，轮到 "
                    f"<a href='tg://user?id={next_tid}'>{next_p['name']}</a>",
                    parse_mode="HTML",
                )
            except Exception:
                pass


@router.callback_query(F.data.startswith("roulette:use:"))
async def cb_roulette_use_item(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    room_id = parts[2]
    item_key = parts[3]
    tg_id = callback.from_user.id

    ok, msg, state = await player_use_item(room_id=room_id, tg_id=tg_id, item_key=item_key)
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    if state and state.phase == "finished" and msg:
        text += f"\n\n{msg}"
    kb = _game_kb(state, tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb)
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
    if state and state.phase == "playing" and _current_turn_tg_id(state) == DEVIL_TG_ID:
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
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
    await callback.answer(msg)


@router.callback_query(F.data.startswith("roulette:cancel:"))
async def cb_roulette_cancel(callback: types.CallbackQuery):
    room_id = callback.data.split(":")[2]
    tg_id = callback.from_user.id

    ok, msg = await cancel_game(room_id=room_id, tg_id=tg_id)
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
        await callback.answer("游戏已结束", show_alert=True)
        return

    text = render_game_panel(state, tg_id)
    kb = _waiting_kb(room_id, state.creator_tg_id) if state.phase == "waiting" else _game_kb(state, tg_id)

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await callback.answer()
