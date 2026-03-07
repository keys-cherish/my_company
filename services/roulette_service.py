"""Devil Roulette game service.

Supports:
- 2-3 player PvP
- Solo vs devil AI

Redis keys:
- roulette_room:{room_id} -> JSON game state
- roulette_player:{tg_id} -> room_id (prevent multi-join)
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import asdict, dataclass, field
from html import escape as html_escape

from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_client import get_redis
from config import settings
from utils.concurrency import with_lock

logger = logging.getLogger(__name__)

# Core settings
ROOM_TTL = settings.roulette_room_ttl_seconds
MIN_BET = settings.roulette_min_bet
MAX_BET_PCT = settings.roulette_max_bet_pct

DEVIL_TG_ID = -1  # Sentinel for devil AI


def _escape_text(value: object) -> str:
    """Escape user-controlled text for Telegram HTML parse mode."""
    return html_escape(str(value), quote=False)


def _format_player_name(player: dict, *, mention: bool = False) -> str:
    """Render player name with optional inline mention link."""
    if player.get("is_devil"):
        return "魔鬼"
    safe_name = _escape_text(player.get("name", "?"))
    if mention:
        tg_id = int(player.get("tg_id", 0) or 0)
        if tg_id > 0:
            return f"<a href='tg://user?id={tg_id}'>{safe_name}</a>"
    return safe_name


async def consume_self_points(tg_id: int, amount: int) -> bool:
    """Atomically deduct user self_points balance for roulette bet."""
    if amount <= 0:
        return True
    from services.user_service import add_traffic_by_tg_id

    return await add_traffic_by_tg_id(
        tg_id,
        -amount,
        reason="roulette_bet",
    )

# Three rounds, escalating difficulty.
ROUND_CONFIG = [
    {"hp": 2, "shells": 4, "live_min": 1, "live_max": 2, "items": 2},
    {"hp": 3, "shells": 6, "live_min": 2, "live_max": 4, "items": 3},
    {"hp": 4, "shells": 8, "live_min": 3, "live_max": 5, "items": 3},
]

BASE_ITEM_KEYS = ["magnifier", "cigarette", "saw", "beer", "pill", "handcuffs"]
LATE_ROUND_ITEM_KEYS = ["adrenaline", "inverter", "phone"]
ITEM_EMOJI = {
    "magnifier": "🔍放大镜",
    "cigarette": "🚬香烟",
    "saw": "🪚手锯",
    "beer": "🍺啤酒",
    "pill": "💊药丸",
    "handcuffs": "⛓️手铐",
    "adrenaline": "💉肾上腺素",
    "inverter": "🔄逆转器",
    "phone": "📱一次性手机",
}
ITEM_SHORT = {
    "magnifier": "🔍",
    "cigarette": "🚬",
    "saw": "🪚",
    "beer": "🍺",
    "pill": "💊",
    "handcuffs": "⛓️",
    "adrenaline": "💉",
    "inverter": "🔄",
    "phone": "📱",
}
# Text-only item names for readable UI
ITEM_NAME = {
    "magnifier": "放大镜",
    "cigarette": "香烟",
    "saw": "手锯",
    "beer": "啤酒",
    "pill": "药丸",
    "handcuffs": "手铐",
    "adrenaline": "肾上腺素",
    "inverter": "逆转器",
    "phone": "一次性手机",
}


def _item_pool_for_round(round_index: int) -> list[str]:
    """Round-specific item pool.

    New items (adrenaline/inverter/phone) appear from round 3 onward.
    """
    if round_index >= 2:
        return [*BASE_ITEM_KEYS, *LATE_ROUND_ITEM_KEYS]
    return list(BASE_ITEM_KEYS)


@dataclass
class PlayerState:
    tg_id: int
    company_id: int
    name: str
    hp: int = 0
    max_hp: int = 0
    items: list[str] = field(default_factory=list)
    is_devil: bool = False
    alive: bool = True
    saw_active: bool = False  # next shot deals double damage
    known_shell: str | None = None  # "live" or "blank"


@dataclass
class GameState:
    room_id: str
    phase: str = "waiting"  # waiting / playing / finished
    bet: int = 0
    creator_tg_id: int = 0
    players: list[dict] = field(default_factory=list)
    current_round: int = 0
    shells: list[bool] = field(default_factory=list)  # True=live False=blank
    shell_index: int = 0
    turn_index: int = 0
    turn_order: list[int] = field(default_factory=list)
    action_log: list[str] = field(default_factory=list)
    handcuffed_tg_id: int = 0
    winner_tg_id: int = 0
    live_count: int = 0
    blank_count: int = 0
    forfeited_pool: int = 0  # total points already refunded via forfeit
    pending_display: list[str] = field(default_factory=list)  # round start msgs to animate

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(raw: str) -> GameState:
        return GameState(**json.loads(raw))


def _get_player(state: GameState, tg_id: int) -> dict | None:
    for p in state.players:
        if p["tg_id"] == tg_id:
            return p
    return None


def _alive_players(state: GameState) -> list[dict]:
    return [p for p in state.players if p["alive"]]


def _current_turn_tg_id(state: GameState) -> int:
    alive_tids = [
        tid
        for tid in state.turn_order
        if (_p := _get_player(state, tid)) and _p["alive"]
    ]
    if not alive_tids:
        return 0
    return alive_tids[state.turn_index % len(alive_tids)]


async def _save_state(state: GameState) -> None:
    r = await get_redis()
    await r.set(f"roulette_room:{state.room_id}", state.to_json(), ex=ROOM_TTL)


async def _load_state(room_id: str) -> GameState | None:
    r = await get_redis()
    raw = await r.get(f"roulette_room:{room_id}")
    if not raw:
        return None
    try:
        return GameState.from_json(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


async def _cleanup_room(room_id: str, player_tg_ids: list[int]) -> None:
    r = await get_redis()
    await r.delete(f"roulette_room:{room_id}")
    for tid in player_tg_ids:
        if tid != DEVIL_TG_ID:
            await r.delete(f"roulette_player:{tid}")


def _init_round(state: GameState) -> list[str]:
    """Initialize a new round and return round messages."""
    rnd = state.current_round
    if rnd >= len(ROUND_CONFIG):
        return []

    cfg = ROUND_CONFIG[rnd]
    msgs: list[str] = []
    round_max_hp = cfg["hp"]

    if rnd == 0:
        # Multiplayer (2+ human players) starts at 3 HP by default.
        alive_humans = [p for p in _alive_players(state) if not p.get("is_devil")]
        if len(alive_humans) >= 2:
            round_max_hp = max(round_max_hp, 3)

    for p in state.players:
        if p["alive"]:
            # First round: set initial HP. Later rounds: keep current HP, just raise max.
            if rnd == 0:
                p["hp"] = round_max_hp
                p["max_hp"] = round_max_hp
            else:
                # Raise max_hp but DON'T reset current HP
                p["max_hp"] = cfg["hp"]
                # Cap current HP to new max (in case it somehow exceeds)
                p["hp"] = min(p["hp"], cfg["hp"])
            p["items"] = []
            p["saw_active"] = False
            p["known_shell"] = None

    live_count = random.randint(cfg["live_min"], cfg["live_max"])
    blank_count = cfg["shells"] - live_count
    shells = [True] * live_count + [False] * blank_count
    random.shuffle(shells)
    state.shells = shells
    state.shell_index = 0
    state.live_count = live_count
    state.blank_count = blank_count
    state.handcuffed_tg_id = 0
    state.turn_index = 0

    line = f"— 第{rnd + 1}轮: {live_count}实弹 {blank_count}空弹 | 上限{round_max_hp}HP"
    if rnd > 0:
        line += " (奖池+50%)"
    msgs.append(line)

    item_pool = _item_pool_for_round(rnd)
    for p in _alive_players(state):
        items = random.choices(item_pool, k=cfg["items"])
        p["items"] = items
        item_text = ", ".join(ITEM_NAME.get(i, i) for i in items)
        name = "魔鬼" if p["is_devil"] else p["name"]
        msgs.append(f"  {name}获得: {item_text}")

    return msgs


def _advance_turn(state: GameState) -> None:
    """Move to next alive player, handling handcuff skip."""
    alive_tids = [
        tid
        for tid in state.turn_order
        if (_p := _get_player(state, tid)) and _p["alive"]
    ]
    if not alive_tids:
        return

    state.turn_index = (state.turn_index + 1) % len(alive_tids)
    next_tid = alive_tids[state.turn_index]

    if state.handcuffed_tg_id == next_tid:
        state.handcuffed_tg_id = 0
        name = "魔鬼" if next_tid == DEVIL_TG_ID else (_get_player(state, next_tid) or {}).get("name", "?")
        state.action_log.append(f"{name} 被铐住，跳过回合")
        state.turn_index = (state.turn_index + 1) % len(alive_tids)


def _check_round_end(state: GameState) -> list[str]:
    """Check whether current round/game ended and return messages."""
    alive = _alive_players(state)
    msgs: list[str] = []

    if len(alive) <= 1:
        state.phase = "finished"
        if alive:
            state.winner_tg_id = alive[0]["tg_id"]
            winner_name = "魔鬼" if alive[0]["is_devil"] else alive[0]["name"]
            msgs.append(f">>> {winner_name} 获胜!")
        else:
            msgs.append(">>> 全灭!")
        return msgs

    if state.shell_index >= len(state.shells):
        state.current_round += 1
        if state.current_round >= len(ROUND_CONFIG):
            alive.sort(key=lambda p: p["hp"], reverse=True)
            top_hp = alive[0]["hp"]
            tied = [p for p in alive if p["hp"] == top_hp]
            if len(tied) > 1:
                state.phase = "finished"
                state.winner_tg_id = 0
                tied_names = "、".join(
                    "魔鬼" if p.get("is_devil") else p["name"] for p in tied
                )
                msgs.append(f">>> 弹尽 {tied_names} {top_hp}HP平局")
            else:
                state.winner_tg_id = alive[0]["tg_id"]
                state.phase = "finished"
                winner_name = "魔鬼" if alive[0]["is_devil"] else alive[0]["name"]
                msgs.append(f">>> 弹尽 {winner_name} {alive[0]['hp']}HP获胜")
        else:
            # Store round start messages for animation
            round_msgs = _init_round(state)
            state.pending_display = round_msgs

    return msgs


def _do_shoot(state: GameState, shooter_tg_id: int, target_tg_id: int) -> list[str]:
    """Execute one shot and return messages."""
    shooter = _get_player(state, shooter_tg_id)
    target = _get_player(state, target_tg_id)
    if not shooter or not target:
        return ["玩家不存在"]
    if not shooter["alive"]:
        return ["你已出局"]
    if not target["alive"]:
        return ["目标已出局"]
    if state.shell_index >= len(state.shells):
        return ["没有可用子弹"]

    is_live = state.shells[state.shell_index]
    state.shell_index += 1
    remaining = state.shells[state.shell_index:]
    state.live_count = sum(1 for s in remaining if s)
    state.blank_count = sum(1 for s in remaining if not s)

    shooter_name = "魔鬼" if shooter["is_devil"] else shooter["name"]
    target_name = "魔鬼" if target["is_devil"] else target["name"]
    self_shot = shooter_tg_id == target_tg_id
    shooter["known_shell"] = None

    damage = 2 if shooter.get("saw_active") else 1
    saw_note = "(手锯x2)" if shooter.get("saw_active") else ""
    shooter["saw_active"] = False
    msgs: list[str] = []

    if is_live:
        target["hp"] -= damage
        if self_shot:
            msgs.append(f"{shooter_name} 对自己开枪 → 实弹! -{damage}HP{saw_note}")
        else:
            msgs.append(f"{shooter_name} → {target_name} 实弹! -{damage}HP{saw_note}")

        if target["hp"] <= 0:
            target["hp"] = 0
            target["alive"] = False
            msgs.append(f"  {target_name} 被淘汰!")
    else:
        if self_shot:
            msgs.append(f"{shooter_name} 对自己开枪 → 空弹, 再来一次")
        else:
            msgs.append(f"{shooter_name} → {target_name} 空弹")

    extra_turn = (not is_live) and self_shot and shooter["alive"]
    if not extra_turn:
        _advance_turn(state)

    msgs.extend(_check_round_end(state))
    return msgs


def _use_item(state: GameState, user_tg_id: int, item_key: str, target_tg_id: int = 0) -> list[str]:
    """Use one item and return messages."""
    player = _get_player(state, user_tg_id)
    if not player or not player["alive"]:
        return ["玩家已出局"]
    if item_key not in player.get("items", []):
        return ["你没有这个道具"]

    player["items"].remove(item_key)
    name = "魔鬼" if player["is_devil"] else player["name"]
    msgs: list[str] = []

    if item_key == "magnifier":
        if state.shell_index < len(state.shells):
            is_live = state.shells[state.shell_index]
            player["known_shell"] = "live" if is_live else "blank"
            result = "实弹" if is_live else "空弹"
            msgs.append(f"{name} 用放大镜偷看 → {result}")
        else:
            msgs.append(f"{name} 用放大镜 → 弹夹空了，无效")

    elif item_key == "cigarette":
        max_hp = player.get("max_hp", ROUND_CONFIG[state.current_round]["hp"])
        if player["hp"] < max_hp:
            player["hp"] += 1
            msgs.append(f"{name} 抽烟回血 +1HP ({player['hp']}/{max_hp})")
        else:
            msgs.append(f"{name} 抽烟 → 满血，无效")

    elif item_key == "saw":
        player["saw_active"] = True
        msgs.append(f"{name} 装上手锯 → 下一枪伤害x2")

    elif item_key == "adrenaline":
        alive_opponents = [p for p in _alive_players(state) if p["tg_id"] != user_tg_id]
        target: dict | None = None
        if target_tg_id:
            maybe_target = _get_player(state, target_tg_id)
            if maybe_target and maybe_target["alive"] and maybe_target["tg_id"] != user_tg_id:
                target = maybe_target
        if target is None and alive_opponents:
            stealable_targets = [
                p for p in alive_opponents if any(i != "adrenaline" for i in p.get("items", []))
            ]
            if stealable_targets:
                stealable_targets.sort(
                    key=lambda p: sum(1 for i in p.get("items", []) if i != "adrenaline"),
                    reverse=True,
                )
                target = stealable_targets[0]
            else:
                target = max(alive_opponents, key=lambda p: p.get("hp", 0))

        if not target:
            msgs.append(f"{name} 注射肾上腺素 → 没有可偷目标")
            return msgs

        stealable = [i for i in target.get("items", []) if i != "adrenaline"]
        t_name = "魔鬼" if target.get("is_devil") else target.get("name", "?")
        if not stealable:
            msgs.append(f"{name} 注射肾上腺素 → {t_name} 没有可偷道具")
            return msgs

        stolen = random.choice(stealable)
        target["items"].remove(stolen)
        stolen_name = ITEM_NAME.get(stolen, stolen)
        msgs.append(f"{name} 注射肾上腺素 → 从 {t_name} 偷到{stolen_name}并立刻使用")

        # Reuse existing item behavior by temporarily granting the stolen item.
        player["items"].append(stolen)
        if stolen == "handcuffs":
            msgs.extend(_use_item(state, user_tg_id, stolen, target_tg_id=target["tg_id"]))
        elif stolen == "phone":
            msgs.extend(_use_item(state, user_tg_id, stolen, target_tg_id=1))
        else:
            msgs.extend(_use_item(state, user_tg_id, stolen))

    elif item_key == "inverter":
        if state.shell_index < len(state.shells):
            state.shells[state.shell_index] = not state.shells[state.shell_index]
            remaining = state.shells[state.shell_index:]
            state.live_count = sum(1 for s in remaining if s)
            state.blank_count = sum(1 for s in remaining if not s)

            # Reverse changes the known truth of current shell; clear stale hints.
            for p in state.players:
                p["known_shell"] = None
            current_live = state.shells[state.shell_index]
            player["known_shell"] = "live" if current_live else "blank"
            result = "实弹" if current_live else "空弹"
            msgs.append(f"{name} 使用逆转器 → 当前子弹变为{result}")
        else:
            msgs.append(f"{name} 使用逆转器 → 弹夹空了，无效")

    elif item_key == "phone":
        if state.shell_index >= len(state.shells):
            msgs.append(f"{name} 用一次性手机 → 弹夹空了，无效")
            return msgs

        remaining = len(state.shells) - state.shell_index
        position = target_tg_id if target_tg_id > 0 else 1
        if position < 1 or position > remaining:
            # Invalid position should not consume phone.
            player["items"].append("phone")
            msgs.append(f"{name} 用一次性手机 → 位置无效 (1-{remaining})")
            return msgs

        probe_idx = state.shell_index + position - 1
        predicted_live = state.shells[probe_idx]
        result = "实弹" if predicted_live else "空弹"
        msgs.append(f"{name} 用一次性手机预言第{position}发 → {result}")
        if position == 1:
            player["known_shell"] = "live" if predicted_live else "blank"

    elif item_key == "beer":
        if state.shell_index < len(state.shells):
            ejected_live = state.shells[state.shell_index]
            state.shell_index += 1
            remaining = state.shells[state.shell_index:]
            state.live_count = sum(1 for s in remaining if s)
            state.blank_count = sum(1 for s in remaining if not s)
            player["known_shell"] = None
            result = "实弹" if ejected_live else "空弹"
            msgs.append(f"{name} 用啤酒退膛 → 退出{result}")
            msgs.extend(_check_round_end(state))
        else:
            msgs.append(f"{name} 用啤酒退膛 → 弹夹空了，无效")

    elif item_key == "pill":
        if random.random() < 0.5:
            max_hp = player.get("max_hp", ROUND_CONFIG[state.current_round]["hp"])
            player["hp"] = min(player["hp"] + 2, max_hp)
            msgs.append(f"{name} 吃药 → 运气好! +2HP ({player['hp']}/{max_hp})")
        else:
            player["hp"] -= 1
            msgs.append(f"{name} 吃药 → 副作用! -1HP ({player['hp']})")
            if player["hp"] <= 0:
                player["hp"] = 0
                player["alive"] = False
                msgs.append(f"  {name} 药物过量被淘汰!")
                msgs.extend(_check_round_end(state))

    elif item_key == "handcuffs":
        alive_opponents = [p for p in _alive_players(state) if p["tg_id"] != user_tg_id]

        if target_tg_id:
            if target_tg_id == state.handcuffed_tg_id:
                player["items"].append("handcuffs")
                msgs.append("该玩家已被铐住，不能重复使用")
            else:
                target = _get_player(state, target_tg_id)
                if target and target["alive"] and target["tg_id"] != user_tg_id:
                    state.handcuffed_tg_id = target_tg_id
                    t_name = "魔鬼" if target["is_devil"] else target["name"]
                    msgs.append(f"{name} 用手铐铐住 {t_name}")
                else:
                    player["items"].append("handcuffs")
                    msgs.append("手铐目标无效")
        elif alive_opponents:
            # Exclude already-handcuffed player from auto-target
            candidates = [p for p in alive_opponents if p["tg_id"] != state.handcuffed_tg_id]
            if not candidates:
                player["items"].append("handcuffs")
                msgs.append(f"{name} 手铐无效（对手已被铐住）")
            else:
                candidates.sort(key=lambda p: p["hp"], reverse=True)
                target = candidates[0]
                state.handcuffed_tg_id = target["tg_id"]
                t_name = "魔鬼" if target["is_devil"] else target["name"]
                msgs.append(f"{name} 用手铐铐住 {t_name}")
        else:
            player["items"].append("handcuffs")
            msgs.append(f"{name} 手铐无效")

    return msgs


def _devil_single_step(state: GameState) -> list[str]:
    """Execute ONE devil AI action (use item or shoot). Returns messages for that action.
    Returns empty list if devil has no more actions to take this turn."""
    devil = _get_player(state, DEVIL_TG_ID)
    if not devil or not devil["alive"]:
        return []
    if state.phase != "playing":
        return []
    if _current_turn_tg_id(state) != DEVIL_TG_ID:
        return []

    alive_opponents = [p for p in _alive_players(state) if p["tg_id"] != DEVIL_TG_ID]
    if not alive_opponents:
        return []

    making_mistake = random.random() < 0.10
    items = list(devil.get("items", []))
    known = devil.get("known_shell")

    # Priority: magnifier > phone > inverter > adrenaline > handcuffs > beer > cigarette > pill > saw > shoot
    if "magnifier" in items and not making_mistake and known is None:
        return _use_item(state, DEVIL_TG_ID, "magnifier")

    if "phone" in items and not making_mistake and known is None:
        return _use_item(state, DEVIL_TG_ID, "phone", target_tg_id=1)

    # Re-check known after possible magnifier usage
    known = devil.get("known_shell")

    if "inverter" in items and known == "live" and devil["hp"] <= 1 and not making_mistake:
        return _use_item(state, DEVIL_TG_ID, "inverter")

    known = devil.get("known_shell")

    if "adrenaline" in items and alive_opponents and not making_mistake:
        stealable_exists = any(
            i != "adrenaline" for opponent in alive_opponents for i in opponent.get("items", [])
        )
        if stealable_exists:
            strongest = max(alive_opponents, key=lambda p: p["hp"])
            return _use_item(state, DEVIL_TG_ID, "adrenaline", target_tg_id=strongest["tg_id"])

    if "handcuffs" in items and alive_opponents and not making_mistake:
        # Don't handcuff someone already handcuffed
        handcuff_candidates = [p for p in alive_opponents if p["tg_id"] != state.handcuffed_tg_id]
        if handcuff_candidates:
            strongest = max(handcuff_candidates, key=lambda p: p["hp"])
            return _use_item(state, DEVIL_TG_ID, "handcuffs", target_tg_id=strongest["tg_id"])

    if "beer" in items and known is None and not making_mistake:
        return _use_item(state, DEVIL_TG_ID, "beer")

    # Re-check known after possible beer eject
    known = devil.get("known_shell")

    if known == "live" and "saw" in items and not making_mistake:
        return _use_item(state, DEVIL_TG_ID, "saw")

    max_hp = devil.get("max_hp", ROUND_CONFIG[state.current_round]["hp"])
    if "cigarette" in items and devil["hp"] < max_hp and not making_mistake:
        return _use_item(state, DEVIL_TG_ID, "cigarette")

    if "pill" in items and devil["hp"] <= 1 and not making_mistake:
        result = _use_item(state, DEVIL_TG_ID, "pill")
        if not devil["alive"]:
            return result
        return result

    # No more items to use, time to shoot
    if state.phase != "playing" or state.shell_index >= len(state.shells):
        return []

    known = devil.get("known_shell")
    if known == "blank":
        # Known blank → always shoot self (free extra turn)
        return _do_shoot(state, DEVIL_TG_ID, DEVIL_TG_ID)
    elif known == "live":
        # Known live → never shoot self; mistake just picks random opponent
        if making_mistake:
            target = random.choice(alive_opponents)
        else:
            target = min(alive_opponents, key=lambda p: p["hp"])
        return _do_shoot(state, DEVIL_TG_ID, target["tg_id"])
    else:
        # Unknown shell
        if making_mistake:
            target = random.choice(alive_opponents + [devil])
            return _do_shoot(state, DEVIL_TG_ID, target["tg_id"])
        remaining = state.shells[state.shell_index:]
        live_ratio = sum(1 for s in remaining if s) / max(1, len(remaining))
        if live_ratio <= 0.35:
            return _do_shoot(state, DEVIL_TG_ID, DEVIL_TG_ID)
        else:
            weakest = min(alive_opponents, key=lambda p: p["hp"])
            return _do_shoot(state, DEVIL_TG_ID, weakest["tg_id"])


def _devil_turn(state: GameState) -> list[str]:
    """Execute entire devil AI turn at once (for backward compat). Returns all messages."""
    all_msgs: list[str] = []
    for _ in range(20):  # safety limit
        step_msgs = _devil_single_step(state)
        if not step_msgs:
            break
        all_msgs.extend(step_msgs)
        if state.phase != "playing":
            break
        if _current_turn_tg_id(state) != DEVIL_TG_ID:
            break
    return all_msgs


@with_lock("roulette_room:{room_id}")
async def pop_pending_display(
    *,
    room_id: str,
) -> tuple[str | None, bool, GameState | None]:
    """Pop one pending display message and add it to action_log.

    Returns (message, has_more, state). None message means nothing pending.
    """
    state = await _load_state(room_id)
    if not state or not state.pending_display:
        return None, False, state

    msg = state.pending_display.pop(0)
    state.action_log.append(msg)
    has_more = len(state.pending_display) > 0
    await _save_state(state)
    return msg, has_more, state


@with_lock("roulette_room:{room_id}")
async def devil_execute_step(
    *,
    room_id: str,
) -> tuple[bool, list[str], GameState | None]:
    """Execute ONE devil action step. Returns (has_more_actions, messages, state).

    The handler should call this repeatedly with delays to animate devil turns.
    """
    state = await _load_state(room_id)
    if not state:
        return False, [], None
    if state.phase != "playing":
        return False, [], state
    if _current_turn_tg_id(state) != DEVIL_TG_ID:
        return False, [], state

    step_msgs = _devil_single_step(state)
    if not step_msgs:
        return False, [], state

    state.action_log.extend(step_msgs)
    await _save_state(state)

    # Check if devil still has more actions
    has_more = (
        state.phase == "playing"
        and _current_turn_tg_id(state) == DEVIL_TG_ID
        and _get_player(state, DEVIL_TG_ID) is not None
        and (_get_player(state, DEVIL_TG_ID) or {}).get("alive", False)
    )

    return has_more, step_msgs, state


@with_lock("roulette_room:{room_id}")
async def create_room(
    *,
    room_id: str,
    creator_tg_id: int,
    creator_company_id: int,
    creator_name: str,
    bet: int,
) -> tuple[bool, str, GameState | None]:
    """Create a roulette room."""
    r = await get_redis()

    if await r.get(f"roulette_player:{creator_tg_id}"):
        return False, "❌ 你已经在一场轮盘赌中", None

    if bet < MIN_BET:
        return False, f"❌ 最低赌注为 {MIN_BET:,}", None

    state = GameState(
        room_id=room_id,
        phase="waiting",
        bet=bet,
        creator_tg_id=creator_tg_id,
        players=[
            asdict(
                PlayerState(
                    tg_id=creator_tg_id,
                    company_id=creator_company_id,
                    name=creator_name,
                )
            )
        ],
    )

    await _save_state(state)
    await r.set(f"roulette_player:{creator_tg_id}", room_id, ex=ROOM_TTL)

    return True, (
        "😈 轮盘赌房间已创建！\n"
        f"赌注：{bet:,} 积分/人\n"
        "等待其他玩家加入（2-3人）\n"
        "或点击「单挑魔鬼」开始 1v1"
    ), state


@with_lock("roulette_room:{room_id}")
async def join_room(
    *,
    room_id: str,
    tg_id: int,
    company_id: int,
    player_name: str,
) -> tuple[bool, str, GameState | None]:
    """Join an existing room."""
    r = await get_redis()

    if await r.get(f"roulette_player:{tg_id}"):
        return False, "❌ 你已经在一场轮盘赌中", None

    state = await _load_state(room_id)
    if not state:
        return False, "❌ 房间不存在或已过期", None
    if state.phase != "waiting":
        return False, "❌ 游戏已经开始", None
    if len(state.players) >= 3:
        return False, "❌ 房间已满（最多3人）", None
    if any(p["tg_id"] == tg_id for p in state.players):
        return False, "❌ 你已经在这个房间里", None

    state.players.append(
        asdict(
            PlayerState(
                tg_id=tg_id,
                company_id=company_id,
                name=player_name,
            )
        )
    )

    await _save_state(state)
    await r.set(f"roulette_player:{tg_id}", room_id, ex=ROOM_TTL)

    return True, f"✅ {player_name} 加入了轮盘赌！（{len(state.players)}/3）", state


@with_lock("roulette_room:{room_id}")
async def start_game(
    *,
    room_id: str,
    tg_id: int,
    solo_vs_devil: bool = False,
) -> tuple[bool, str, GameState | None]:
    """Start game. Only creator can start."""
    state = await _load_state(room_id)
    if not state:
        return False, "❌ 房间不存在或已过期", None
    if state.phase != "waiting":
        return False, "❌ 游戏已经开始", None
    if tg_id != state.creator_tg_id:
        return False, "❌ 只有房主可以开始游戏", None

    if solo_vs_devil:
        if len(state.players) != 1:
            return False, "❌ 单挑模式只能在房主独自一人时开启", None
        state.players.append(
            asdict(
                PlayerState(
                    tg_id=DEVIL_TG_ID,
                    company_id=0,
                    name="魔鬼",
                    is_devil=True,
                )
            )
        )
    elif len(state.players) < 2:
        return False, "❌ 至少需要2名玩家，或使用单挑模式", None

    state.phase = "playing"
    state.current_round = 0
    state.turn_order = [p["tg_id"] for p in state.players]
    random.shuffle(state.turn_order)

    init_msgs = _init_round(state)
    state.action_log = list(init_msgs)
    await _save_state(state)

    first_tid = _current_turn_tg_id(state)
    first_player = _get_player(state, first_tid)
    first_name = "魔鬼" if (first_player and first_player["is_devil"]) else (first_player or {}).get("name", "?")

    result = "\n".join(init_msgs) + f"\n\n{first_name} 先手"

    # Don't auto-execute devil turn here — handler will animate it step by step
    return True, result, state


@with_lock("roulette_room:{room_id}")
async def player_shoot(
    *,
    room_id: str,
    shooter_tg_id: int,
    target_tg_id: int,
) -> tuple[bool, str, GameState | None]:
    """Player shoots target."""
    state = await _load_state(room_id)
    if not state:
        return False, "❌ 游戏不存在", None
    if state.phase != "playing":
        return False, "❌ 游戏未在进行中", None
    if _current_turn_tg_id(state) != shooter_tg_id:
        return False, "❌ 还没轮到你", None

    shoot_msgs = _do_shoot(state, shooter_tg_id, target_tg_id)
    state.action_log.extend(shoot_msgs)

    # Don't auto-execute devil turn — handler will animate it step by step
    await _save_state(state)

    # Return only settlement text (shoot msgs are in the action_log shown by panel)
    settlement_text = ""
    if state.phase == "finished":
        settlement_text = await _settle_game(state)

    return True, settlement_text, state


@with_lock("roulette_room:{room_id}")
async def player_use_item(
    *,
    room_id: str,
    tg_id: int,
    item_key: str,
    target_tg_id: int = 0,
) -> tuple[bool, str, GameState | None]:
    """Player uses an item."""
    state = await _load_state(room_id)
    if not state:
        return False, "❌ 游戏不存在", None
    if state.phase != "playing":
        return False, "❌ 游戏未在进行中", None
    if _current_turn_tg_id(state) != tg_id:
        return False, "❌ 还没轮到你", None

    item_msgs = _use_item(state, tg_id, item_key, target_tg_id)
    state.action_log.extend(item_msgs)

    # Don't auto-execute devil turn — handler will animate it step by step
    await _save_state(state)

    # Return only settlement text (item msgs are in the action_log shown by panel)
    settlement_text = ""
    if state.phase == "finished":
        settlement_text = await _settle_game(state)

    return True, settlement_text, state


@with_lock("roulette_room:{room_id}")
async def cancel_game(
    *,
    room_id: str,
    tg_id: int,
) -> tuple[bool, str]:
    """Cancel waiting room or forfeit during playing."""
    state = await _load_state(room_id)
    if not state:
        return False, "❌ 游戏不存在"

    player = _get_player(state, tg_id)
    if not player:
        return False, "❌ 你不在这个房间里"

    if state.phase == "waiting":
        if tg_id != state.creator_tg_id:
            return False, "❌ 只有房主可以关闭等待中的房间"
        # Full refund for everyone.
        from services.user_service import add_traffic_by_tg_id

        if state.bet > 0:
            for p in state.players:
                if p.get("is_devil"):
                    continue
                try:
                    await add_traffic_by_tg_id(
                        p["tg_id"],
                        state.bet,
                        reason="roulette_waiting_cancel_refund",
                    )
                except Exception:
                    logger.exception("Failed to refund roulette self_points for tg_id=%s", p["tg_id"])

        await _cleanup_room(room_id, [p["tg_id"] for p in state.players])
        return True, "❌ 房间已关闭，已全额退还积分"

    if state.phase == "playing":
        if not player["alive"]:
            return False, "❌ 你已出局，无法再次放弃"

        player["alive"] = False
        player["hp"] = 0
        state.action_log.append(f"{player['name']} 放弃比赛 (-50%赌注)")

        refund = state.bet // 2
        if refund > 0:
            from services.user_service import add_traffic_by_tg_id

            try:
                await add_traffic_by_tg_id(
                    tg_id,
                    refund,
                    reason="roulette_forfeit_refund",
                )
            except Exception:
                logger.exception("Failed to refund roulette forfeit self_points")
        state.forfeited_pool += refund

        end_msgs = _check_round_end(state)
        state.action_log.extend(end_msgs)
        await _save_state(state)

        # Build result: victory announcement first, then settlement
        parts: list[str] = [f"🏳️ 你已放弃轮盘赌，退还 {refund:,} 积分"]
        if end_msgs:
            parts.append("\n".join(end_msgs))
        if state.phase == "finished":
            parts.append(await _settle_game(state))
        return True, "\n".join(parts)

    return False, "❌ 游戏已结束"


async def _settle_game(state: GameState) -> str:
    """Distribute winnings and cleanup room."""
    human_players = [p for p in state.players if not p.get("is_devil")]
    base_pot = state.bet * len(human_players)
    round_reached = min(state.current_round, len(ROUND_CONFIG) - 1)
    # Base 1.2x reward (first-round win = 20% profit), +50% per extra round
    multiplier = 1.2 * (1.5 ** round_reached)
    total_pot = int(base_pot * multiplier) - state.forfeited_pool
    total_pot = max(total_pot, 0)
    winner = _get_player(state, state.winner_tg_id)

    msgs: list[str] = []
    is_draw = False

    if state.winner_tg_id == 0 and len(_alive_players(state)) > 1:
        from services.user_service import add_traffic_by_tg_id

        alive_humans = [p for p in _alive_players(state) if not p.get("is_devil")]
        per_player = state.bet
        for p in alive_humans:
            try:
                await add_traffic_by_tg_id(p["tg_id"], per_player, reason="roulette_draw_refund")
            except Exception:
                logger.exception("Failed to refund draw for tg_id=%s", p["tg_id"])
        msgs.append(f"平局! 各退回 {per_player:,} 积分")
        is_draw = True
    elif winner and not winner.get("is_devil"):
        from services.user_service import add_traffic_by_tg_id, add_reputation, get_user_by_tg_id

        winnings = total_pot
        reputation_gain = 5 + len(human_players) * 3

        try:
            await add_traffic_by_tg_id(winner["tg_id"], winnings, reason="roulette_winnings")
        except Exception:
            logger.exception("Failed to settle roulette winnings (self_points)")

        try:
            from db.engine import async_session

            async with async_session() as session:
                async with session.begin():
                    user = await get_user_by_tg_id(session, winner["tg_id"])
                    if user:
                        await add_reputation(session, user.id, reputation_gain)
        except Exception:
            logger.exception("Failed to add roulette reputation")

        msgs.append(f"结算: {winner['name']} +{winnings:,}积分 +{reputation_gain}声望")
    elif winner and winner.get("is_devil"):
        msgs.append("魔鬼获胜! 积分被吞噬")
    else:
        msgs.append("全灭，积分消失")

    # Update win/loss/draw stats for all human players
    r = await get_redis()
    for p in human_players:
        tid = p["tg_id"]
        stats_key = f"roulette_stats:{tid}"
        raw = await r.get(stats_key)
        if raw:
            try:
                stats = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                stats = {"wins": 0, "losses": 0, "draws": 0}
        else:
            stats = {"wins": 0, "losses": 0, "draws": 0}

        if is_draw:
            stats["draws"] = stats.get("draws", 0) + 1
        elif winner and winner["tg_id"] == tid:
            stats["wins"] = stats.get("wins", 0) + 1
        else:
            stats["losses"] = stats.get("losses", 0) + 1

        await r.set(stats_key, json.dumps(stats))

    # Append win rate lines for all human players
    msgs.append("")
    msgs.append("📊 历史胜率:")
    for p in human_players:
        tid = p["tg_id"]
        raw = await r.get(f"roulette_stats:{tid}")
        stats = json.loads(raw) if raw else {"wins": 0, "losses": 0, "draws": 0}
        w = stats.get("wins", 0)
        l = stats.get("losses", 0)
        d = stats.get("draws", 0)
        total = w + l + d
        rate = (w / total * 100) if total > 0 else 0
        name = p.get("name", "?")
        msgs.append(f"  {name}: {w}胜 {l}负 {d}平 ({rate:.1f}%)")

    await _cleanup_room(state.room_id, [p["tg_id"] for p in state.players])
    return "\n".join(msgs)


@with_lock("roulette_room:{room_id}")
async def leave_room(
    *,
    room_id: str,
    tg_id: int,
) -> tuple[bool, str, GameState | None]:
    """Non-creator leaves a waiting room and gets a full refund."""
    state = await _load_state(room_id)
    if not state:
        return False, "❌ 房间不存在或已过期", None
    if state.phase != "waiting":
        return False, "❌ 游戏已经开始，无法退出", None
    if tg_id == state.creator_tg_id:
        return False, "❌ 房主请使用关闭房间", None

    player = _get_player(state, tg_id)
    if not player:
        return False, "❌ 你不在这个房间里", None

    state.players = [p for p in state.players if p["tg_id"] != tg_id]
    await _save_state(state)

    r = await get_redis()
    await r.delete(f"roulette_player:{tg_id}")

    # Refund bet
    if state.bet > 0:
        from services.user_service import add_traffic_by_tg_id

        try:
            await add_traffic_by_tg_id(
                tg_id,
                state.bet,
                reason="roulette_leave_refund",
            )
        except Exception:
            logger.exception("Failed to refund leave_room self_points for tg_id=%s", tg_id)

    return True, f"✅ 已退出房间，退还 {state.bet:,} 积分", state


async def get_player_room(tg_id: int) -> str | None:
    """Return current room_id for user, if any."""
    r = await get_redis()
    return await r.get(f"roulette_player:{tg_id}")


async def get_game_state(room_id: str) -> GameState | None:
    """Read-only game state query."""
    return await _load_state(room_id)


def render_game_panel(state: GameState, viewer_tg_id: int = 0) -> str:
    """Render game panel text — clean, readable, minimal emoji."""
    if state.phase == "waiting":
        names = ", ".join(_escape_text(p.get("name", "?")) for p in state.players)
        return (
            f"恶魔轮盘赌 - 等待中\n"
            f"{'─' * 22}\n"
            f"赌注: {state.bet:,} 积分/人\n"
            f"玩家: {names} ({len(state.players)}/3)\n"
            f"{'─' * 22}\n"
            f"等待玩家加入，或房主开始游戏"
        )

    rnd = min(state.current_round, len(ROUND_CONFIG) - 1) + 1
    lines = [f"恶魔轮盘赌 · 第{rnd}轮"]

    if state.phase == "finished":
        # --- Finished: show full action log with round spacing ---
        lines = ["恶魔轮盘赌 · 游戏结束"]
        lines.append("─" * 22)

        # Final HP display
        for p in state.players:
            name = _format_player_name(p)
            if not p.get("alive"):
                lines.append(f"  💀 {name}  [淘汰]")
            else:
                max_hp = max(1, int(p.get("max_hp", 1) or 1))
                hp = max(0, min(int(p.get("hp", 0) or 0), max_hp))
                hearts = "❤️" * hp + "🤍" * (max_hp - hp)
                lines.append(f"  🏆 {name}  {hearts}")

        lines.append("─" * 22)

        # Full action log with spacing between rounds
        if state.action_log:
            lines.append("📋 完整行动记录:")

            log_lines: list[str] = []
            for entry in state.action_log:
                safe = _escape_text(entry)
                # Round header lines start with "—" → add blank line before
                if entry.startswith("—"):
                    log_lines.append("")
                    log_lines.append(safe)
                # Item distribution lines (indented) keep tight
                elif entry.startswith("  "):
                    log_lines.append(safe)
                # Victory/elimination lines
                elif entry.startswith(">>>"):
                    log_lines.append("")
                    log_lines.append(safe)
                else:
                    log_lines.append(f"  {safe}")

            # Safety: truncate early entries if too long (leave room for settlement ~500 chars)
            MAX_PANEL_LEN = 3500
            header_len = len("\n".join(lines)) + 2
            while log_lines and header_len + len("\n".join(log_lines)) > MAX_PANEL_LEN:
                # Remove earliest non-empty entry
                for i, l in enumerate(log_lines):
                    if l.strip():
                        log_lines[i] = "  ...(省略)..."
                        # Remove consecutive skipped entries
                        while i + 1 < len(log_lines) and log_lines[i + 1].strip() == "...(省略)...":
                            log_lines.pop(i + 1)
                        break
                else:
                    break

            lines.extend(log_lines)

        return "\n".join(lines)

    # --- Playing phase: normal compact panel ---

    # Ammo info — prominent position at top
    remaining = max(0, len(state.shells) - state.shell_index)
    lines.append(f"🔫 {state.live_count}实弹 / {state.blank_count}空弹 (剩{remaining}发)")
    lines.append("─" * 22)

    # HP display — hearts
    for p in state.players:
        name = _format_player_name(p)
        if not p.get("alive"):
            lines.append(f"  💀 {name}  [淘汰]")
            continue
        max_hp = max(1, int(p.get("max_hp", 1) or 1))
        hp = max(0, min(int(p.get("hp", 0) or 0), max_hp))
        hearts = "❤️" * hp + "🤍" * (max_hp - hp)
        lines.append(f"  {name}  {hearts}")

    lines.append("─" * 22)

    # Current turn
    current = _current_turn_tg_id(state)
    cp = _get_player(state, current)
    if cp:
        c_name = _format_player_name(cp, mention=True)
        lines.append(f"▶ {c_name} 的回合")

    # Items — show current turn player's items as readable text
    current_tid = _current_turn_tg_id(state)
    holder = _get_player(state, current_tid)
    if holder and holder.get("alive") and holder.get("items"):
        item_text = ", ".join(ITEM_NAME.get(i, i) for i in holder["items"])
        holder_name = _format_player_name(holder)
        lines.append(f"  {holder_name}的道具: {item_text}")

    # Show viewer's own items if different from current player
    if viewer_tg_id and viewer_tg_id != current_tid:
        viewer = _get_player(state, viewer_tg_id)
        if viewer and viewer.get("alive") and viewer.get("items"):
            v_items = ", ".join(ITEM_NAME.get(i, i) for i in viewer["items"])
            lines.append(f"  你的道具: {v_items}")

    # Hint for known shell (only for viewer)
    if viewer_tg_id:
        viewer = _get_player(state, viewer_tg_id)
        if viewer and viewer.get("known_shell"):
            hint = "实弹!" if viewer["known_shell"] == "live" else "空弹"
            lines.append(f"  [偷看结果: {hint}]")

    # Action log — last 6 entries, each on clear separate line
    recent = state.action_log[-6:]
    if recent:
        lines.append("")
        lines.append("📋 行动记录:")
        for entry in recent:
            lines.append(f"  {_escape_text(entry)}")

    return "\n".join(lines)

