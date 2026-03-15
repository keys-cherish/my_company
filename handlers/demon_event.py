"""Demon Invasion Event handler — random @mention challenges with accept/decline.

Flow:
1. Event triggers → @mentions company owner in group
2. Owner OR any shareholder can respond:
   - ⚔️ 独自迎战 — solo fight (anyone with stake in the company)
   - 👥 召集股东 — rally shareholders (opens waiting room)
   - 🏳️ 拒绝 — decline (owner only)
3. Rally mode: shareholders click "加入" to join, owner/any member clicks "开战" to start
4. Game plays via existing roulette system, then win/lose outcomes apply
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cache.redis_client import get_redis
from services.demon_event_service import (
    DEMON_EVENT_TIERS,
    apply_decline_penalty,
    apply_lose_penalty,
    apply_win_reward,
    get_event_tier,
    load_event_state,
    peek_event_state,
    pick_target_company,
    save_event_state,
    set_event_cooldown,
    _tier_by_key,
)
from services.roulette_service import (
    _current_turn_tg_id,
    _is_devil,
    _settle_game,
    create_demon_event_room,
    devil_execute_step,
    get_game_state,
    get_player_room,
    pop_pending_display,
    render_game_panel,
    ROOM_TTL,
)
from utils.panel_owner import mark_panel

router = Router()
logger = logging.getLogger(__name__)

DEVIL_STEP_DELAY = 1.2
ROUND_MSG_DELAY = 0.6
DEVIL_ANIMATION_MAX_STEPS = 120
_bot_ref = None

_RALLY_KEY = "demon_rally:{company_id}"
_RALLY_TTL = 90


def set_bot(bot):
    global _bot_ref
    _bot_ref = bot


# ── Helpers: check if tg_id is owner or shareholder ──────────────────────

async def _is_company_member(company_id: int, tg_id: int) -> bool:
    """Return True if tg_id is the owner or a shareholder of the company."""
    from db.engine import async_session
    from db.models import Company, Shareholder, User
    from sqlalchemy import select

    async with async_session() as session:
        company = await session.get(Company, company_id)
        if not company:
            return False
        owner = await session.get(User, company.owner_id)
        if owner and owner.tg_id == tg_id:
            return True
        result = await session.execute(
            select(User.tg_id)
            .join(Shareholder, Shareholder.user_id == User.id)
            .where(Shareholder.company_id == company_id)
        )
        shareholder_tg_ids = {row[0] for row in result.all()}
        return tg_id in shareholder_tg_ids


async def _get_company_owner_tg_id(company_id: int) -> int:
    from db.engine import async_session
    from db.models import Company, User
    async with async_session() as session:
        company = await session.get(Company, company_id)
        if not company:
            return 0
        owner = await session.get(User, company.owner_id)
        return owner.tg_id if owner else 0


# ── Rally (waiting room) state ───────────────────────────────────────────

async def _save_rally(company_id: int, players: list[dict]):
    r = await get_redis()
    await r.set(
        _RALLY_KEY.format(company_id=company_id),
        json.dumps(players, ensure_ascii=False),
        ex=_RALLY_TTL,
    )


async def _load_rally(company_id: int) -> list[dict] | None:
    r = await get_redis()
    raw = await r.get(_RALLY_KEY.format(company_id=company_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def _delete_rally(company_id: int):
    r = await get_redis()
    await r.delete(_RALLY_KEY.format(company_id=company_id))


# ── Scheduled trigger ────────────────────────────────────────────────────

async def demon_event_trigger_job():
    if not _bot_ref:
        return
    await _trigger_demon_event_for_random_target()


async def _trigger_demon_event_for_random_target(
    *, force_company_id: int | None = None, force_tg_id: int | None = None,
):
    from config import settings

    if force_company_id and force_tg_id:
        from db.engine import async_session
        from db.models import Company
        async with async_session() as session:
            company = await session.get(Company, force_company_id)
        if not company:
            return
        tier = get_event_tier(company.cp_points)
        if not tier:
            tier = DEMON_EVENT_TIERS[0]
        company_dict = {
            "id": company.id, "name": company.name,
            "owner_id": company.owner_id, "owner_tg_id": force_tg_id,
            "cp_points": company.cp_points, "employee_count": company.employee_count,
            "level": company.level,
        }
    else:
        result = await pick_target_company()
        if not result:
            return
        company_dict, tier = result

    company_id = company_dict["id"]
    owner_tg_id = company_dict["owner_tg_id"]

    await save_event_state(company_id, owner_tg_id, tier)
    await set_event_cooldown(company_id)

    text = _build_challenge_text(company_dict, tier)
    kb = _challenge_kb(company_id)

    chat_ids = settings.allowed_chat_id_set
    for chat_id in (chat_ids or []):
        try:
            await _bot_ref.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            logger.debug("Failed to send demon event to chat %s", chat_id, exc_info=True)


def _build_challenge_text(company_dict: dict, tier: dict) -> str:
    tg_id = company_dict["owner_tg_id"]
    mention = f'<a href="tg://user?id={tg_id}">{company_dict["name"]}老板</a>'
    lines = [
        f"👹 恶魔入侵！— {tier['emoji']}{tier['name']}",
        f"{'━' * 24}",
        f"{tier['devils']}个恶魔盯上了「{company_dict['name']}」！",
        f"  恶魔HP: {tier['devil_hp']} | 玩家HP: {tier['player_hp']} | 道具: {tier['items_per_round']}个/轮",
        "",
        f"⚔️ 独自迎战 → 一人挑战恶魔轮盘赌",
        f"👥 召集股东 → 邀请股东一起打（90秒等待）",
        f"🏳️ 拒绝挑战 → 恶魔报复",
        "",
        f"  胜利: +积分{int(tier['win_funds_pct']*100)}% | 声望+{tier['win_reputation']} | 营收+{int(tier['win_revenue_buff']*100)}%",
        f"  拒绝/失败: 积分-{int(tier['decline_funds_pct']*100)}% | 员工-{tier['decline_employee_min']}~{tier['decline_employee_max']}人",
        "",
        f"📢 {mention}，恶魔在等你！股东也可代为迎战",
        f"⏰ 90秒内未选择视为拒绝",
    ]
    return "\n".join(lines)


def _challenge_kb(company_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚔️ 独自迎战", callback_data=f"demon_event:solo:{company_id}"),
            InlineKeyboardButton(text="👥 召集股东", callback_data=f"demon_event:rally:{company_id}"),
        ],
        [InlineKeyboardButton(text="🏳️ 拒绝", callback_data=f"demon_event:decline:{company_id}")],
    ])


def _rally_kb(company_id: int, players: list[dict]) -> InlineKeyboardMarkup:
    player_names = ", ".join(p["name"][:6] for p in players) or "暂无"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 加入战斗", callback_data=f"demon_event:join:{company_id}")],
        [InlineKeyboardButton(text=f"⚔️ 开战 ({len(players)}人)", callback_data=f"demon_event:start:{company_id}")],
    ])


# ── Manual trigger command ───────────────────────────────────────────────

@router.message(Command("cp_demonevent"))
async def cmd_demon_event(message: types.Message):
    tg_id = message.from_user.id
    from db.engine import async_session
    from services.company_service import get_companies_by_owner
    from services.user_service import get_user_by_tg_id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await message.answer("请先注册")
            return
        companies = await get_companies_by_owner(session, user.id)
        if not companies:
            await message.answer("你还没有公司")
            return
        company = companies[0]

    global _bot_ref
    if not _bot_ref:
        _bot_ref = message.bot

    await _trigger_demon_event_for_random_target(
        force_company_id=company.id, force_tg_id=tg_id,
    )
    await message.answer("👹 恶魔入侵事件已触发！")


# ── Decline ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("demon_event:decline:"))
async def cb_demon_decline(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    # Only owner can decline
    owner_tg_id = await _get_company_owner_tg_id(company_id)
    if owner_tg_id != tg_id:
        await callback.answer("只有老板可以拒绝挑战", show_alert=True)
        return

    state = await load_event_state(company_id)
    if not state:
        await callback.answer("事件已过期", show_alert=True)
        try:
            await callback.message.edit_text("⏰ 恶魔入侵事件已过期。")
        except Exception:
            pass
        return

    from db.engine import async_session
    from db.models import Company
    async with async_session() as session:
        company = await session.get(Company, company_id)
        owner_user_id = company.owner_id if company else 0

    result_msg = await apply_decline_penalty(company_id, owner_user_id, state["tier"])
    await _delete_rally(company_id)

    try:
        await callback.message.edit_text(result_msg)
    except Exception:
        await callback.message.answer(result_msg)
    await callback.answer()


# ── Solo fight (owner or shareholder) ────────────────────────────────────

@router.callback_query(F.data.startswith("demon_event:solo:"))
async def cb_demon_solo(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    if not await _is_company_member(company_id, tg_id):
        await callback.answer("只有公司老板或股东可以迎战", show_alert=True)
        return

    state = await load_event_state(company_id)
    if not state:
        await callback.answer("事件已过期", show_alert=True)
        try:
            await callback.message.edit_text("⏰ 恶魔入侵事件已过期。")
        except Exception:
            pass
        return

    await _delete_rally(company_id)
    await _start_demon_game(callback, company_id, state["tier"], [
        {"tg_id": tg_id, "name": callback.from_user.full_name},
    ])


# ── Rally: open waiting room ────────────────────────────────────────────

@router.callback_query(F.data.startswith("demon_event:rally:"))
async def cb_demon_rally(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    if not await _is_company_member(company_id, tg_id):
        await callback.answer("只有公司老板或股东可以召集", show_alert=True)
        return

    state = await peek_event_state(company_id)
    if not state:
        await callback.answer("事件已过期", show_alert=True)
        return

    # Initialize rally with the caller as first member
    players = [{"tg_id": tg_id, "name": callback.from_user.full_name}]
    await _save_rally(company_id, players)

    tier = state["tier"]
    text = _rally_text(company_id, tier, players)
    kb = _rally_kb(company_id, players)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb)
        await mark_panel(sent.chat.id, sent.message_id, tg_id)
    await callback.answer("等待股东加入中…")


# ── Rally: shareholder joins ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("demon_event:join:"))
async def cb_demon_join(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    if not await _is_company_member(company_id, tg_id):
        await callback.answer("你不是这家公司的成员", show_alert=True)
        return

    players = await _load_rally(company_id)
    if players is None:
        await callback.answer("等待室已关闭", show_alert=True)
        return

    if any(p["tg_id"] == tg_id for p in players):
        await callback.answer("你已经在队伍中", show_alert=True)
        return

    existing = await get_player_room(tg_id)
    if existing:
        await callback.answer("你已在一场轮盘赌中", show_alert=True)
        return

    players.append({"tg_id": tg_id, "name": callback.from_user.full_name})
    await _save_rally(company_id, players)

    state = await peek_event_state(company_id)
    if not state:
        await callback.answer("事件已过期", show_alert=True)
        return

    text = _rally_text(company_id, state["tier"], players)
    kb = _rally_kb(company_id, players)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await callback.answer(f"已加入! 当前 {len(players)} 人")


# ── Rally: start game with current players ───────────────────────────────

@router.callback_query(F.data.startswith("demon_event:start:"))
async def cb_demon_start(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    players = await _load_rally(company_id)
    if not players:
        await callback.answer("等待室已关闭或无人加入", show_alert=True)
        return

    if not any(p["tg_id"] == tg_id for p in players):
        await callback.answer("你不在队伍中", show_alert=True)
        return

    state = await load_event_state(company_id)  # consume event
    if not state:
        await callback.answer("事件已过期", show_alert=True)
        try:
            await callback.message.edit_text("⏰ 恶魔入侵事件已过期。")
        except Exception:
            pass
        return

    await _delete_rally(company_id)
    await _start_demon_game(callback, company_id, state["tier"], players)


def _rally_text(company_id: int, tier: dict, players: list[dict]) -> str:
    names = "\n".join(f"  {i+1}. {p['name']}" for i, p in enumerate(players))
    return (
        f"👥 恶魔入侵 — 召集股东\n"
        f"{'━' * 24}\n"
        f"难度: {tier['emoji']} {tier['name']} | 恶魔×{tier['devils']}\n"
        f"\n"
        f"已加入 ({len(players)}人):\n{names}\n"
        f"\n"
        f"公司股东点「加入战斗」，准备好后点「开战」\n"
        f"⏰ 90秒内自动关闭"
    )


# ── Start the actual demon roulette game ─────────────────────────────────

async def _start_demon_game(
    callback: types.CallbackQuery,
    company_id: int,
    tier: dict,
    players: list[dict],
):
    """Create the roulette room and start the game."""
    # Check all players are free
    for p in players:
        existing = await get_player_room(p["tg_id"])
        if existing:
            await callback.answer(f"{p['name']} 已在另一场游戏中", show_alert=True)
            return

    leader_tg_id = players[0]["tg_id"]
    room_id = f"demon_{company_id}_{leader_tg_id}"

    # Create room with first player
    ok, msg, game_state = await create_demon_event_room(
        room_id=room_id,
        player_tg_id=leader_tg_id,
        player_company_id=company_id,
        player_name=players[0]["name"],
        devil_count=tier["devils"],
        devil_hp=tier["devil_hp"],
        player_hp=tier["player_hp"],
        items_per_round=tier["items_per_round"],
    )
    if not ok or not game_state:
        await callback.answer(f"创建失败: {msg}", show_alert=True)
        return

    # Add additional players to the room
    if len(players) > 1:
        from dataclasses import asdict
        from services.roulette_service import PlayerState, _save_state
        r = await get_redis()
        for p in players[1:]:
            game_state.players.insert(
                # Insert before devils
                len([x for x in game_state.players if not x.get("is_devil")]),
                asdict(PlayerState(
                    tg_id=p["tg_id"],
                    company_id=company_id,
                    name=p["name"],
                    hp=tier["player_hp"],
                    max_hp=tier["player_hp"],
                )),
            )
            game_state.turn_order.insert(0, p["tg_id"])
            await r.set(f"roulette_player:{p['tg_id']}", room_id, ex=ROOM_TTL)
        await _save_state(game_state)

    # Save meta for outcome handling
    r = await get_redis()
    await r.set(
        f"demon_event_meta:{room_id}",
        json.dumps({"company_id": company_id, "tier_key": tier["key"]}, ensure_ascii=False),
        ex=3600,
    )

    text = render_game_panel(game_state, leader_tg_id)
    from handlers.roulette import _game_kb
    kb = _game_kb(game_state, leader_tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await mark_panel(sent.chat.id, sent.message_id, leader_tg_id)
    await callback.answer("恶魔入侵开始!")

    if _is_devil(_current_turn_tg_id(game_state)):
        await _animate_devil_turn(callback, room_id, leader_tg_id)


# ── Devil animation (reuses roulette mechanics) ─────────────────────────

async def _animate_pending(callback, room_id: str, tg_id: int):
    for _ in range(20):
        await asyncio.sleep(ROUND_MSG_DELAY)
        msg, has_more, state = await pop_pending_display(room_id=room_id)
        if not state or msg is None:
            break
        text = render_game_panel(state, tg_id)
        from handlers.roulette import _game_kb
        kb = _game_kb(state, tg_id)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        if not has_more:
            break


async def _animate_devil_turn(callback, room_id: str, tg_id: int):
    from html import escape as html_escape
    for _ in range(DEVIL_ANIMATION_MAX_STEPS):
        state = await get_game_state(room_id)
        if not state:
            return
        if state.pending_display:
            await _animate_pending(callback, room_id, tg_id)
            continue
        if state.phase != "playing" or not _is_devil(_current_turn_tg_id(state)):
            if state.phase == "finished":
                await _handle_demon_event_finish(callback, room_id, state, tg_id)
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
            if state.phase == "finished":
                await _handle_demon_event_finish(callback, room_id, state, tg_id)
            return

        text = render_game_panel(state, tg_id)
        if state.phase == "finished":
            text += "\n\n" + html_escape(await _settle_game(state), quote=False)
        from handlers.roulette import _game_kb
        kb = _game_kb(state, tg_id)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass

        if state.phase == "finished":
            await _handle_demon_event_finish(callback, room_id, state, tg_id)
            return

        if not has_more and not state.pending_display:
            ns = await get_game_state(room_id)
            if not ns or ns.phase != "playing" or not _is_devil(_current_turn_tg_id(ns)):
                if ns and ns.phase == "finished":
                    await _handle_demon_event_finish(callback, room_id, ns, tg_id)
                return


async def _handle_demon_event_finish(callback, room_id: str, state, tg_id: int):
    r = await get_redis()
    meta_raw = await r.get(f"demon_event_meta:{room_id}")
    if not meta_raw:
        return
    await r.delete(f"demon_event_meta:{room_id}")

    try:
        meta = json.loads(meta_raw)
    except Exception:
        return

    company_id = meta["company_id"]
    tier = _tier_by_key(meta["tier_key"])
    if not tier:
        return

    from db.engine import async_session
    from db.models import Company
    async with async_session() as session:
        company = await session.get(Company, company_id)
        owner_user_id = company.owner_id if company else 0

    if not owner_user_id:
        return

    player_won = state.winner_tg_id > 0
    if player_won:
        result_msg = await apply_win_reward(company_id, owner_user_id, tier)
    else:
        result_msg = await apply_lose_penalty(company_id, owner_user_id, tier)

    try:
        await callback.message.answer(result_msg)
    except Exception:
        logger.debug("Failed to send demon event result", exc_info=True)
