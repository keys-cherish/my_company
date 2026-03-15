"""Devil Roulette game service.

Supports:
- 2-3 player PvP
- Co-op vs devil AI (多人单挑)
- Hell mode vs multiple devils (地狱模式, base 10x then +1.5x each round)

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
BET_KEY_TTL = ROOM_TTL + 300  # extra 5 min for refund detection
MIN_BET = settings.roulette_min_bet
MAX_BET_PCT = settings.roulette_max_bet_pct
HELL_BASE_MULTIPLIER = 10.0
HELL_ROUND_GROWTH = 1.5
HELL_ENTRY_MULTIPLIER = 5  # hell mode costs 5x the base bet

DEVIL_TG_ID = -1  # Sentinel for devil AI (backward compat)
DEVIL_TG_IDS = list(range(-1, -11, -1))  # [-1, -2, ..., -10]
DEVIL_NAMES = {i: f"魔鬼{chr(64 - i)}" for i in DEVIL_TG_IDS}  # 魔鬼A ~ 魔鬼J


def _is_devil(tg_id: int) -> bool:
    """Check if a tg_id represents a devil AI player."""
    return tg_id < 0


def _escape_text(value: object) -> str:
    """Escape user-controlled text for Telegram HTML parse mode."""
    return html_escape(str(value), quote=False)


def _format_multiplier(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_player_name(player: dict, *, mention: bool = False) -> str:
    """Render player name with optional inline mention link."""
    if player.get("is_devil"):
        return _escape_text(player.get("name", "魔鬼"))
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
    from services.user_service import add_points_by_tg_id

    return await add_points_by_tg_id(
        tg_id,
        -amount,
        reason="roulette_bet",
    )

# Three rounds, escalating difficulty.
ROUND_CONFIG = [
    {"hp": 2, "shells": 4, "live_min": 1, "live_max": 2, "items": 0},
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
    handcuffed_tg_ids: list[int] = field(default_factory=list)
    winner_tg_id: int = 0
    live_count: int = 0
    blank_count: int = 0
    forfeited_pool: int = 0  # total points already refunded via forfeit
    game_mode: str = "pvp"  # pvp / coop / hell
    devil_count: int = 0
    pending_display: list[str] = field(default_factory=list)  # round start msgs to animate

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(raw: str) -> GameState:
        data = json.loads(raw)
        # Backward compat: migrate old handcuffed_tg_id (int) → handcuffed_tg_ids (list)
        old_val = data.pop("handcuffed_tg_id", None)
        if "handcuffed_tg_ids" not in data:
            data["handcuffed_tg_ids"] = [old_val] if old_val else []
        return GameState(**data)


def _get_player(state: GameState, tg_id: int) -> dict | None:
    for p in state.players:
        if p["tg_id"] == tg_id:
            return p
    return None


def _alive_players(state: GameState) -> list[dict]:
    return [p for p in state.players if p["alive"]]


def _clear_player_round_state(player: dict) -> None:
    player["items"] = []
    player["saw_active"] = False
    player["known_shell"] = None


def _eliminate_player(state: GameState, player: dict) -> None:
    player["hp"] = 0
    player["alive"] = False
    _clear_player_round_state(player)

    player_tg_id = player["tg_id"]
    if player_tg_id in state.handcuffed_tg_ids:
        state.handcuffed_tg_ids = [tid for tid in state.handcuffed_tg_ids if tid != player_tg_id]


def _alive_human_players(state: GameState) -> list[dict]:
    return [p for p in _alive_players(state) if not p.get("is_devil")]


def _alive_devil_players(state: GameState, *, exclude_tg_id: int = 0) -> list[dict]:
    return [
        p for p in _alive_players(state)
        if p.get("is_devil") and p["tg_id"] != exclude_tg_id
    ]


def _pick_devil_focus_target(state: GameState, alive_opponents: list[dict]) -> dict | None:
    if not alive_opponents:
        return None

    return min(
        alive_opponents,
        key=lambda p: (
            0 if p["tg_id"] in state.handcuffed_tg_ids else 1,
            int(p.get("hp", 0) or 0),
            -sum(1 for item in p.get("items", []) if item != "adrenaline"),
            int(p.get("tg_id", 0) or 0),
        ),
    )


def _pick_devil_handcuff_target(
    state: GameState,
    alive_opponents: list[dict],
    focus_target: dict | None,
) -> dict | None:
    available = [p for p in alive_opponents if p["tg_id"] not in state.handcuffed_tg_ids]
    if not available:
        return None

    if focus_target and len(available) > 1:
        off_focus = [p for p in available if p["tg_id"] != focus_target["tg_id"]]
        if off_focus:
            available = off_focus

    return max(
        available,
        key=lambda p: (
            int(p.get("hp", 0) or 0),
            sum(1 for item in p.get("items", []) if item != "adrenaline"),
            -int(p.get("tg_id", 0) or 0),
        ),
    )


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
        if not _is_devil(tid):
            await r.delete(f"roulette_player:{tid}")
            await r.delete(f"roulette_bet:{tid}")


def _init_round(state: GameState) -> list[str]:
    """Initialize a new round and return round messages."""
    rnd = state.current_round
    # Reuse last round config for rounds beyond defined ones
    cfg = ROUND_CONFIG[min(rnd, len(ROUND_CONFIG) - 1)]
    msgs: list[str] = []
    round_max_hp = cfg["hp"]
    item_recipients: list[dict] = []

    if rnd == 0:
        # Multiplayer (2+ human players) starts at 3 HP by default.
        alive_humans = [p for p in _alive_players(state) if not p.get("is_devil")]
        if len(alive_humans) >= 2:
            round_max_hp = max(round_max_hp, 3)

    for p in state.players:
        if not p["alive"]:
            _clear_player_round_state(p)
            continue

        if rnd > 0 and int(p.get("hp", 0) or 0) <= 0:
            _eliminate_player(state, p)
            continue

        # First round: set initial HP. Later rounds: keep current HP, just raise max.
        if rnd == 0:
            p["hp"] = round_max_hp
            p["max_hp"] = round_max_hp
        else:
            # Raise max_hp but DON'T reset current HP
            p["max_hp"] = cfg["hp"]
            # Cap current HP to new max (in case it somehow exceeds)
            p["hp"] = min(p["hp"], cfg["hp"])

        _clear_player_round_state(p)
        item_recipients.append(p)

    live_count = random.randint(cfg["live_min"], cfg["live_max"])
    blank_count = cfg["shells"] - live_count
    shells = [True] * live_count + [False] * blank_count
    random.shuffle(shells)
    state.shells = shells
    state.shell_index = 0
    state.live_count = live_count
    state.blank_count = blank_count
    state.handcuffed_tg_ids = []
    state.turn_index = 0

    line = f"— 第{rnd + 1}轮: {live_count}实弹 {blank_count}空弹 | 上限{round_max_hp}HP"
    if rnd > 0:
        line += " (奖池+50%)"
    msgs.append(line)

    if cfg["items"] > 0:
        item_pool = _item_pool_for_round(rnd)
        for p in item_recipients:
            items = random.choices(item_pool, k=cfg["items"])
            p["items"] = items
            item_text = ", ".join(ITEM_NAME.get(i, i) for i in items)
            name = p["name"]
            msgs.append(f"  {name}获得: {item_text}")

    return msgs


def _advance_turn(state: GameState) -> None:
    """Move to next alive player, handling consecutive handcuff skips."""
    alive_tids = [
        tid
        for tid in state.turn_order
        if (_p := _get_player(state, tid)) and _p["alive"]
    ]
    if not alive_tids:
        return

    skip_guard = len(alive_tids)
    while skip_guard > 0:
        state.turn_index = (state.turn_index + 1) % len(alive_tids)
        next_tid = alive_tids[state.turn_index]
        if next_tid not in state.handcuffed_tg_ids:
            return

        state.handcuffed_tg_ids.remove(next_tid)
        name = (_get_player(state, next_tid) or {}).get("name", "?")
        state.action_log.append(f"{name} 被铐住，跳过回合")
        skip_guard -= 1


def _check_round_end(state: GameState) -> list[str]:
    """Check whether current round/game ended and return messages."""
    alive = _alive_players(state)
    msgs: list[str] = []

    if state.game_mode in ("coop", "hell"):
        # Team-based: game ends when all humans or all devils are eliminated
        alive_humans = [p for p in alive if not p.get("is_devil")]
        alive_devils = [p for p in alive if p.get("is_devil")]

        if not alive_humans:
            state.phase = "finished"
            state.winner_tg_id = alive_devils[0]["tg_id"] if alive_devils else 0
            msgs.append(">>> 魔鬼获胜! 所有玩家被淘汰!")
            return msgs
        if not alive_devils:
            state.phase = "finished"
            state.winner_tg_id = alive_humans[0]["tg_id"]
            names = ", ".join(p["name"] for p in alive_humans)
            msgs.append(f">>> 人类获胜! {names} 存活!")
            return msgs
    else:
        # PvP: last player standing
        if len(alive) <= 1:
            state.phase = "finished"
            if alive:
                state.winner_tg_id = alive[0]["tg_id"]
                winner_name = alive[0]["name"]
                msgs.append(f">>> {winner_name} 获胜!")
            else:
                msgs.append(">>> 全灭!")
            return msgs

    if state.shell_index >= len(state.shells):
        state.current_round += 1
        # Keep going — reuse last round config if past the defined rounds
        round_msgs = _init_round(state)
        state.pending_display = round_msgs

    return msgs


def _recover_stalled_devil_turn(state: GameState) -> list[str]:
    """Best-effort recovery for impossible devil turns that would otherwise stall the room."""
    if state.phase != "playing":
        return []

    current = _current_turn_tg_id(state)
    if not _is_devil(current):
        return []

    if not _alive_human_players(state):
        return _check_round_end(state)

    if state.shell_index >= len(state.shells):
        return _check_round_end(state)

    devil = _get_player(state, current)
    devil_name = (devil or {}).get("name", "魔鬼")
    _advance_turn(state)
    return [f"{devil_name} 陷入迟疑，回合被强制推进"]


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

    shooter_name = shooter["name"]
    target_name = target["name"]
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
            _eliminate_player(state, target)
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
    name = player["name"]
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
        max_hp = player.get("max_hp", ROUND_CONFIG[min(state.current_round, len(ROUND_CONFIG) - 1)]["hp"])
        if player["hp"] < max_hp:
            player["hp"] += 1
            msgs.append(f"{name} 抽烟回血 +1HP ({player['hp']}/{max_hp})")
        else:
            player["items"].append("cigarette")
            msgs.append(f"{name} 满血了，香烟已退回")

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
            player["items"].append("adrenaline")
            msgs.append(f"{name} 注射肾上腺素 → 没有可偷目标")
            return msgs

        stealable = [i for i in target.get("items", []) if i != "adrenaline"]
        t_name = target.get("name", "?")
        if not stealable:
            player["items"].append("adrenaline")
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
        position = random.randint(1, remaining)
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
            max_hp = player.get("max_hp", ROUND_CONFIG[min(state.current_round, len(ROUND_CONFIG) - 1)]["hp"])
            player["hp"] = min(player["hp"] + 2, max_hp)
            msgs.append(f"{name} 吃药 → 运气好! +2HP ({player['hp']}/{max_hp})")
        else:
            player["hp"] -= 1
            msgs.append(f"{name} 吃药 → 副作用! -1HP ({player['hp']})")
            if player["hp"] <= 0:
                _eliminate_player(state, player)
                msgs.append(f"  {name} 药物过量被淘汰!")
                msgs.extend(_check_round_end(state))

    elif item_key == "handcuffs":
        alive_opponents = [p for p in _alive_players(state) if p["tg_id"] != user_tg_id]

        if target_tg_id:
            if target_tg_id in state.handcuffed_tg_ids:
                player["items"].append("handcuffs")
                msgs.append("该玩家已被铐住，不能重复铐")
            else:
                target = _get_player(state, target_tg_id)
                if target and target["alive"] and target["tg_id"] != user_tg_id:
                    state.handcuffed_tg_ids.append(target_tg_id)
                    t_name = target["name"]
                    msgs.append(f"{name} 用手铐铐住 {t_name}")
                else:
                    player["items"].append("handcuffs")
                    msgs.append("手铐目标无效")
        elif alive_opponents:
            candidates = [p for p in alive_opponents if p["tg_id"] not in state.handcuffed_tg_ids]
            if not candidates:
                player["items"].append("handcuffs")
                msgs.append(f"{name} 手铐无效（所有对手已被铐住）")
            else:
                candidates.sort(key=lambda p: p["hp"], reverse=True)
                target = candidates[0]
                state.handcuffed_tg_ids.append(target["tg_id"])
                t_name = target["name"]
                msgs.append(f"{name} 用手铐铐住 {t_name}")
        else:
            player["items"].append("handcuffs")
            msgs.append(f"{name} 手铐无效")

    return msgs


def _devil_single_step(state: GameState, devil_tg_id: int = DEVIL_TG_ID) -> list[str]:
    """Execute ONE devil AI action (use item or shoot). Returns messages for that action.
    Returns empty list if devil has no more actions to take this turn."""
    devil = _get_player(state, devil_tg_id)
    if not devil or not devil["alive"]:
        return []
    if state.phase != "playing":
        return []
    if _current_turn_tg_id(state) != devil_tg_id:
        return []

    # Devils only target humans
    alive_opponents = _alive_human_players(state)
    if not alive_opponents:
        return []

    alive_allies = _alive_devil_players(state, exclude_tg_id=devil_tg_id)
    focus_target = _pick_devil_focus_target(state, alive_opponents)
    handcuff_target = _pick_devil_handcuff_target(state, alive_opponents, focus_target)
    no_mistake_mode = state.game_mode == "hell" and devil["hp"] <= 1
    making_mistake = False if no_mistake_mode else random.random() < 0.10
    items = list(devil.get("items", []))
    known = devil.get("known_shell")

    # Priority: magnifier > phone > inverter > adrenaline > handcuffs > beer > cigarette > pill > saw > shoot
    if "magnifier" in items and not making_mistake and known is None:
        return _use_item(state, devil_tg_id, "magnifier")

    if "phone" in items and not making_mistake and known is None:
        return _use_item(state, devil_tg_id, "phone", target_tg_id=1)

    # Re-check known after possible magnifier usage
    known = devil.get("known_shell")

    if "inverter" in items and known == "live" and devil["hp"] <= 1 and not making_mistake:
        return _use_item(state, devil_tg_id, "inverter")

    known = devil.get("known_shell")

    if (
        no_mistake_mode
        and "handcuffs" in items
        and len(alive_opponents) > 1
        and handcuff_target is not None
    ):
        return _use_item(state, devil_tg_id, "handcuffs", target_tg_id=handcuff_target["tg_id"])

    if "adrenaline" in items and alive_opponents and not making_mistake:
        stealable_targets = [
            opponent
            for opponent in alive_opponents
            if any(item != "adrenaline" for item in opponent.get("items", []))
        ]
        if stealable_targets:
            if focus_target and any(p["tg_id"] == focus_target["tg_id"] for p in stealable_targets):
                adrenaline_target = focus_target
            else:
                adrenaline_target = max(
                    stealable_targets,
                    key=lambda p: (
                        sum(1 for item in p.get("items", []) if item != "adrenaline"),
                        int(p.get("hp", 0) or 0),
                    ),
                )
            return _use_item(
                state,
                devil_tg_id,
                "adrenaline",
                target_tg_id=adrenaline_target["tg_id"],
            )

    if "handcuffs" in items and alive_opponents and not making_mistake:
        if handcuff_target is not None:
            return _use_item(state, devil_tg_id, "handcuffs", target_tg_id=handcuff_target["tg_id"])

    if "beer" in items and known is None and not making_mistake:
        return _use_item(state, devil_tg_id, "beer")

    # Re-check known after possible beer eject
    known = devil.get("known_shell")

    if known == "live" and "saw" in items and not making_mistake:
        return _use_item(state, devil_tg_id, "saw")

    max_hp = devil.get("max_hp", ROUND_CONFIG[min(state.current_round, len(ROUND_CONFIG) - 1)]["hp"])
    if "cigarette" in items and devil["hp"] < max_hp and not making_mistake:
        return _use_item(state, devil_tg_id, "cigarette")

    if (
        "pill" in items
        and devil["hp"] >= 2
        and devil["hp"] < max_hp
        and not making_mistake
    ):
        result = _use_item(state, devil_tg_id, "pill")
        return result

    # No more items to use, time to shoot
    if state.phase != "playing" or state.shell_index >= len(state.shells):
        return []

    known = devil.get("known_shell")
    if known == "blank":
        # Known blank → always shoot self (free extra turn)
        return _do_shoot(state, devil_tg_id, devil_tg_id)
    elif known == "live":
        # Known live → never shoot self; mistake just picks random opponent
        if making_mistake:
            target = random.choice(alive_opponents)
        else:
            target = focus_target or min(alive_opponents, key=lambda p: p["hp"])
        return _do_shoot(state, devil_tg_id, target["tg_id"])
    else:
        # Unknown shell
        if making_mistake:
            target = random.choice(alive_opponents + [devil])
            return _do_shoot(state, devil_tg_id, target["tg_id"])
        remaining = state.shells[state.shell_index:]
        live_ratio = sum(1 for s in remaining if s) / max(1, len(remaining))
        if live_ratio <= 0.35:
            return _do_shoot(state, devil_tg_id, devil_tg_id)
        else:
            target = focus_target or min(alive_opponents, key=lambda p: p["hp"])
            return _do_shoot(state, devil_tg_id, target["tg_id"])


def _devil_turn(state: GameState, devil_tg_id: int = DEVIL_TG_ID) -> list[str]:
    """Execute entire devil AI turn at once (for backward compat). Returns all messages."""
    all_msgs: list[str] = []
    for _ in range(20):  # safety limit
        step_msgs = _devil_single_step(state, devil_tg_id)
        if not step_msgs:
            break
        all_msgs.extend(step_msgs)
        if state.phase != "playing":
            break
        if _current_turn_tg_id(state) != devil_tg_id:
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
    Handles any devil's turn (supports multiple devils in hell mode).
    """
    state = await _load_state(room_id)
    if not state:
        return False, [], None
    if state.phase != "playing":
        return False, [], state

    current = _current_turn_tg_id(state)
    if not _is_devil(current):
        return False, [], state

    step_msgs = _devil_single_step(state, current)
    if not step_msgs:
        step_msgs = _recover_stalled_devil_turn(state)
        if not step_msgs:
            await _save_state(state)
            return False, [], state

    state.action_log.extend(step_msgs)
    await _save_state(state)

    # Check if a devil still has more actions (same devil continues, or next devil)
    next_current = _current_turn_tg_id(state)
    has_more = (
        state.phase == "playing"
        and (_is_devil(next_current) or bool(state.pending_display))
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
    await r.set(f"roulette_bet:{creator_tg_id}", str(bet), ex=BET_KEY_TTL)

    return True, (
        "😈 轮盘赌房间已创建！\n"
        f"赌注：{bet:,} 积分/人\n"
        "等待其他玩家加入（2-3人）\n"
        "或点击「恶魔模式」挑战魔鬼"
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
    await r.set(f"roulette_bet:{tg_id}", str(state.bet), ex=BET_KEY_TTL)

    # Auto-start when room is full (3 players)
    if len(state.players) >= 3:
        return True, f"✅ {player_name} 加入！房间已满，游戏自动开始！", state
    return True, f"✅ {player_name} 加入了轮盘赌！（{len(state.players)}/3）", state


@with_lock("roulette_room:{room_id}")
async def start_game(
    *,
    room_id: str,
    tg_id: int,
    mode: str = "pvp",
) -> tuple[bool, str, GameState | None]:
    """Start game. Only creator can start.

    mode: "pvp" (player vs player), "coop" (humans vs 1 devil), "hell" (humans vs 2-3 devils)
    """
    state = await _load_state(room_id)
    if not state:
        return False, "❌ 房间不存在或已过期", None
    if state.phase != "waiting":
        return False, "❌ 游戏已经开始", None
    if tg_id != state.creator_tg_id:
        return False, "❌ 只有房主可以开始游戏", None

    if mode == "coop":
        # Co-op: all humans vs 1 devil
        state.game_mode = "coop"
        state.devil_count = 1
        state.players.append(
            asdict(PlayerState(tg_id=DEVIL_TG_IDS[0], company_id=0, name="魔鬼", is_devil=True))
        )
    elif mode == "hell":
        # Hell mode: humans vs multiple devils — charge extra entry fee (5x total)
        extra_per_player = state.bet * (HELL_ENTRY_MULTIPLIER - 1)
        human_players = [p for p in state.players if not p.get("is_devil")]
        for p in human_players:
            ok = await consume_self_points(p["tg_id"], extra_per_player)
            if not ok:
                # Refund already-charged extras for earlier players in this loop
                for prev in human_players:
                    if prev["tg_id"] == p["tg_id"]:
                        break
                    from services.user_service import add_points_by_tg_id
                    await add_points_by_tg_id(prev["tg_id"], extra_per_player, reason="hell_entry_refund")
                return False, f"❌ {p['name']} 积分不足（地狱模式入场费 {state.bet * HELL_ENTRY_MULTIPLIER:,}）", None
        # Do NOT modify state.bet — keep original bet for display and settlement.
        # The extra entry fee is already deducted; the hell multiplier handles reward scaling.
        # Update roulette_bet keys to reflect actual total paid (for TTL refund).
        r = await get_redis()
        total_paid = state.bet * HELL_ENTRY_MULTIPLIER
        for p in human_players:
            await r.set(f"roulette_bet:{p['tg_id']}", str(total_paid), ex=BET_KEY_TTL)

        num_humans = len(human_players)
        devil_count = 2 if num_humans <= 1 else 3
        state.game_mode = "hell"
        state.devil_count = devil_count
        for i in range(devil_count):
            devil_name = DEVIL_NAMES[DEVIL_TG_IDS[i]]
            state.players.append(
                asdict(PlayerState(tg_id=DEVIL_TG_IDS[i], company_id=0, name=devil_name, is_devil=True))
            )
    elif len(state.players) < 2:
        return False, "❌ 至少需要2名玩家，或使用恶魔模式", None
    else:
        state.game_mode = "pvp"

    state.phase = "playing"
    state.current_round = 0
    if state.game_mode == "hell":
        human_turn_order = [p["tg_id"] for p in state.players if not p.get("is_devil")]
        devil_turn_order = [p["tg_id"] for p in state.players if p.get("is_devil")]
        random.shuffle(human_turn_order)
        random.shuffle(devil_turn_order)
        state.turn_order = [*human_turn_order, *devil_turn_order]
    else:
        state.turn_order = [p["tg_id"] for p in state.players]
        random.shuffle(state.turn_order)

    init_msgs = _init_round(state)
    state.action_log = list(init_msgs)
    await _save_state(state)

    first_tid = _current_turn_tg_id(state)
    first_player = _get_player(state, first_tid)
    first_name = (first_player or {}).get("name", "?")

    result = "\n".join(init_msgs) + f"\n\n{first_name} 先手"

    # Don't auto-execute devil turn here — handler will animate it step by step
    return True, result, state


async def create_demon_event_room(
    *,
    room_id: str,
    player_tg_id: int,
    player_company_id: int,
    player_name: str,
    devil_count: int,
    devil_hp: int,
    player_hp: int,
    items_per_round: int,
) -> tuple[bool, str, GameState | None]:
    """Create and start a demon event roulette room with custom parameters.

    Used by the demon invasion event system. No bet required (event handles rewards/penalties).
    """
    state = GameState(
        room_id=room_id,
        phase="waiting",
        bet=0,
        creator_tg_id=player_tg_id,
        players=[asdict(PlayerState(
            tg_id=player_tg_id,
            company_id=player_company_id,
            name=player_name,
        ))],
    )

    # Add devils
    actual_count = min(devil_count, len(DEVIL_TG_IDS))
    for i in range(actual_count):
        devil_name = DEVIL_NAMES[DEVIL_TG_IDS[i]]
        state.players.append(asdict(PlayerState(
            tg_id=DEVIL_TG_IDS[i], company_id=0, name=devil_name, is_devil=True,
        )))

    state.game_mode = "hell"
    state.devil_count = actual_count
    state.phase = "playing"
    state.current_round = 0

    # Override HP for all players
    for p in state.players:
        if p.get("is_devil"):
            p["hp"] = devil_hp
            p["max_hp"] = devil_hp
        else:
            p["hp"] = player_hp
            p["max_hp"] = player_hp

    # Build turn order: humans first, then devils
    human_turn = [p["tg_id"] for p in state.players if not p.get("is_devil")]
    devil_turn = [p["tg_id"] for p in state.players if p.get("is_devil")]
    random.shuffle(human_turn)
    random.shuffle(devil_turn)
    state.turn_order = [*human_turn, *devil_turn]

    # Override ROUND_CONFIG items for this room by using items_per_round
    # (handled via _init_round which reads ROUND_CONFIG, so we init manually)
    cfg = ROUND_CONFIG[0]
    live_count = random.randint(cfg["live_min"], cfg["live_max"])
    blank_count = cfg["shells"] - live_count
    shells = [True] * live_count + [False] * blank_count
    random.shuffle(shells)
    state.shells = shells
    state.shell_index = 0
    state.live_count = live_count
    state.blank_count = blank_count
    state.handcuffed_tg_ids = []
    state.turn_index = 0

    # Distribute items
    if items_per_round > 0:
        item_pool = _item_pool_for_round(0)
        for p in state.players:
            if p["alive"]:
                p["items"] = random.choices(item_pool, k=items_per_round)

    init_msg = f"— 恶魔入侵: {live_count}实弹 {blank_count}空弹 | 恶魔×{actual_count}"
    state.action_log = [init_msg]

    # Set player room mapping
    r = await get_redis()
    await r.set(f"roulette_player:{player_tg_id}", room_id, ex=ROOM_TTL)
    await _save_state(state)

    first_tid = _current_turn_tg_id(state)
    first_player = _get_player(state, first_tid)
    first_name = (first_player or {}).get("name", "?")

    return True, f"{init_msg}\n\n{first_name} 先手", state


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
        from services.user_service import add_points_by_tg_id

        if state.bet > 0:
            for p in state.players:
                if p.get("is_devil"):
                    continue
                try:
                    await add_points_by_tg_id(
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

        _eliminate_player(state, player)
        state.action_log.append(f"{player['name']} 放弃比赛 (-50%赌注)")

        refund = state.bet // 2
        if refund > 0:
            from services.user_service import add_points_by_tg_id

            try:
                await add_points_by_tg_id(
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


async def _apply_loser_employee_penalty(tg_id: int, company_id: int, round_reached: int = 0) -> int:
    """Loser randomly loses employees, scaling with rounds survived.

    Round 0: 1-2, Round 1: 3-5, Round 2+: 6-15 (capped by actual headcount).
    """
    if company_id <= 0:
        return 0
    if round_reached <= 0:
        lo, hi = 1, 2
    elif round_reached == 1:
        lo, hi = 3, 5
    else:
        lo, hi = 6, 15
    try:
        from db.engine import async_session
        from db.models import Company

        async with async_session() as session:
            async with session.begin():
                company = await session.get(Company, company_id)
                if not company or company.employee_count <= 1:
                    return 0
                loss = random.randint(lo, min(hi, company.employee_count - 1))
                company.employee_count -= loss
                await session.flush()
                return loss
    except Exception:
        logger.debug("Failed to apply loser employee penalty", exc_info=True)
        return 0


async def _settle_game(state: GameState) -> str:
    """Distribute winnings and cleanup room."""
    from services.user_service import add_points_by_tg_id, add_reputation, get_user_by_tg_id, get_self_points

    human_players = [p for p in state.players if not p.get("is_devil")]

    # Demon event rooms (bet=0): skip point/reputation settlement,
    # just record stats and show outcome. Actual rewards handled by demon_event handler.
    if state.bet == 0:
        winner = _get_player(state, state.winner_tg_id)
        msgs: list[str] = []
        human_winners: list[dict] = []
        if winner and winner.get("is_devil"):
            msgs.append("👹 恶魔获胜！等待结算...")
        elif winner:
            alive_humans = [p for p in human_players if p.get("alive")]
            human_winners = alive_humans
            names = ", ".join(p["name"] for p in alive_humans)
            msgs.append(f"🎉 人类获胜！{names} 存活！等待结算...")
        else:
            msgs.append("全灭...")

        # Update stats only
        r = await get_redis()
        for p in human_players:
            tid = p["tg_id"]
            stats_key = f"roulette_stats:{tid}"
            raw = await r.get(stats_key)
            stats = json.loads(raw) if raw else {"wins": 0, "losses": 0, "draws": 0}
            if any(w["tg_id"] == tid for w in human_winners):
                stats["wins"] = stats.get("wins", 0) + 1
            else:
                stats["losses"] = stats.get("losses", 0) + 1
            await r.set(stats_key, json.dumps(stats))

        await _cleanup_room(state.room_id, [p["tg_id"] for p in state.players])
        return "\n".join(msgs)

    base_pot = state.bet * len(human_players)
    round_reached = state.current_round  # 0-indexed

    if state.game_mode == "hell":
        # Hell mode: base 10x, then each extra round scales by 1.5x
        multiplier = HELL_BASE_MULTIPLIER * (HELL_ROUND_GROWTH ** round_reached)
        total_pot = int(base_pot * multiplier) - state.forfeited_pool
    else:
        # PvP / Coop: base 1.2x, +50% per extra round
        multiplier = 1.2 * (1.5 ** min(round_reached, len(ROUND_CONFIG) - 1))
        total_pot = int(base_pot * multiplier) - state.forfeited_pool
    total_pot = max(total_pot, 0)

    winner = _get_player(state, state.winner_tg_id)
    msgs: list[str] = []
    is_draw = False
    human_winners: list[dict] = []  # humans who get winnings

    if state.game_mode in ("coop", "hell"):
        # Team-based settlement
        alive_humans = [p for p in human_players if p.get("alive")]
        if winner and winner.get("is_devil"):
            msgs.append("魔鬼获胜! 积分被吞噬")
        elif alive_humans:
            human_winners = alive_humans
            per_player = total_pot // len(alive_humans)
            remainder = total_pot - per_player * len(alive_humans)
            reputation_gain = 5 + (10 if state.game_mode == "hell" else 3) * len(human_players)

            for i, p in enumerate(alive_humans):
                share = per_player + (remainder if i == 0 else 0)
                try:
                    await add_points_by_tg_id(p["tg_id"], share, reason="roulette_winnings")
                except Exception:
                    logger.exception("Failed to settle roulette winnings")
                try:
                    from db.engine import async_session
                    async with async_session() as session:
                        async with session.begin():
                            user = await get_user_by_tg_id(session, p["tg_id"])
                            if user:
                                await add_reputation(session, user.id, reputation_gain)
                except Exception:
                    logger.exception("Failed to add roulette reputation")
                new_balance = await get_self_points(p["tg_id"])
                msgs.append(
                    f"结算: {p['name']} +{share:,}积分 +{reputation_gain}声望"
                    f" (余额: {new_balance:,})"
                )
            if state.game_mode == "hell":
                msgs.append(f"🔥 地狱模式 x{_format_multiplier(multiplier)} 奖励倍率!")
        else:
            msgs.append("全灭，积分消失")
    else:
        # PvP settlement
        if state.winner_tg_id == 0 and len(_alive_players(state)) > 1:
            alive_humans = [p for p in _alive_players(state) if not p.get("is_devil")]
            per_player = state.bet
            for p in alive_humans:
                try:
                    await add_points_by_tg_id(p["tg_id"], per_player, reason="roulette_draw_refund")
                except Exception:
                    logger.exception("Failed to refund draw for tg_id=%s", p["tg_id"])
            msgs.append(f"平局! 各退回 {per_player:,} 积分")
            is_draw = True
        elif winner and not winner.get("is_devil"):
            human_winners = [winner]
            winnings = total_pot
            reputation_gain = 5 + len(human_players) * 3
            try:
                await add_points_by_tg_id(winner["tg_id"], winnings, reason="roulette_winnings")
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
            new_balance = await get_self_points(winner["tg_id"])
            msgs.append(
                f"结算: {winner['name']} +{winnings:,}积分 +{reputation_gain}声望"
                f" (余额: {new_balance:,})"
            )
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
        elif any(w["tg_id"] == tid for w in human_winners):
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

    # Loser penalty: random employee loss
    if not is_draw:
        losers = [p for p in human_players if not any(w["tg_id"] == p["tg_id"] for w in human_winners)]
        for p in losers:
            emp_loss = await _apply_loser_employee_penalty(p["tg_id"], p.get("company_id", 0), round_reached)
            if emp_loss > 0:
                msgs.append(f"💀 {p['name']} 公司损失 {emp_loss} 名员工（赌狗的代价）")

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
    await r.delete(f"roulette_bet:{tg_id}")

    # Refund bet
    if state.bet > 0:
        from services.user_service import add_points_by_tg_id

        try:
            await add_points_by_tg_id(
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


async def check_ttl_refund(tg_id: int) -> int:
    """Check if a game expired due to TTL and refund 50% of bet.

    Returns refunded amount (0 if nothing to refund).
    """
    r = await get_redis()
    # If player key still exists, room might still be alive — no refund
    room_id = await r.get(f"roulette_player:{tg_id}")
    if room_id:
        state = await _load_state(room_id)
        if state:
            return 0  # room still exists, no refund needed
        # Room gone but player key lingers — clean up player key
        await r.delete(f"roulette_player:{tg_id}")

    # Check if bet key exists (outlives room by 5 min)
    raw_bet = await r.get(f"roulette_bet:{tg_id}")
    if not raw_bet:
        return 0

    await r.delete(f"roulette_bet:{tg_id}")
    try:
        bet = int(raw_bet)
    except (TypeError, ValueError):
        return 0

    refund = bet  # full refund for TTL expiry (likely game bug/devil stuck)
    if refund > 0:
        from services.user_service import add_points_by_tg_id

        try:
            await add_points_by_tg_id(tg_id, refund, reason="roulette_ttl_refund")
        except Exception:
            logger.exception("Failed to refund TTL-expired roulette bet for tg_id=%s", tg_id)
            return 0
    return refund


async def get_game_state(room_id: str) -> GameState | None:
    """Read-only game state query."""
    return await _load_state(room_id)


def render_game_panel(state: GameState, viewer_tg_id: int = 0) -> str:
    """Render game panel text — clean, readable, minimal emoji."""
    mode_label = {"pvp": "", "coop": " [协力]", "hell": " [地狱]"}.get(state.game_mode, "")
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

    rnd = state.current_round + 1
    lines = [f"恶魔轮盘赌{mode_label} · 第{rnd}轮"]

    if state.phase == "finished":
        # --- Finished: show full action log with round spacing ---
        lines = [f"恶魔轮盘赌{mode_label} · 游戏结束"]
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

    # HP display — hearts
    lines.append("─" * 22)
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

    # Action log — only entries from current round (after last round header "—")
    current_round_log: list[str] = []
    for entry in state.action_log:
        if entry.startswith("—"):
            current_round_log = [entry]  # reset on round header
        else:
            current_round_log.append(entry)
    recent = current_round_log[-8:]
    if recent:
        lines.append("")
        lines.append("📋 行动记录:")
        for entry in recent:
            lines.append(f"  {_escape_text(entry)}")

    # Shells info at bottom
    remaining = len(state.shells) - state.shell_index
    lines.append("")
    lines.append(f"🔫 {state.live_count}实弹 / {state.blank_count}空弹 (剩{remaining}发)")

    return "\n".join(lines)

