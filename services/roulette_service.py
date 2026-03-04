"""Devil Roulette game service.

Supports:
- 2-3 player PvP
- Solo vs devil AI

Redis keys:
- roulette_room:{room_id} -> JSON game state
- roulette_player:{tg_id} -> room_id (prevent multi-join)
- roulette_cd:{tg_id} -> cooldown marker
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import asdict, dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_client import get_redis
from config import settings
from utils.concurrency import with_lock

logger = logging.getLogger(__name__)

# Core settings
ROOM_TTL = settings.roulette_room_ttl_seconds
COOLDOWN_TTL = settings.roulette_cooldown_seconds
MIN_BET = settings.roulette_min_bet
MAX_BET_PCT = settings.roulette_max_bet_pct

DEVIL_TG_ID = -1  # Sentinel for devil AI


async def consume_points(tg_id: int, amount: int) -> bool:
    """Atomically deduct points if balance is sufficient. Returns True on success."""
    if amount <= 0:
        return True
    lua = """
local key = KEYS[1]
local amount = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', key) or '0')
if current < amount then
    return 0
end
redis.call('DECRBY', key, amount)
return 1
"""
    r = await get_redis()
    ok = await r.eval(lua, 1, f"points:{tg_id}", amount)
    return int(ok) == 1

# Three rounds, escalating difficulty.
ROUND_CONFIG = [
    {"hp": 2, "shells": 4, "live_min": 1, "live_max": 2, "items": 2},
    {"hp": 3, "shells": 6, "live_min": 2, "live_max": 4, "items": 3},
    {"hp": 4, "shells": 8, "live_min": 3, "live_max": 5, "items": 3},
]

ITEM_KEYS = ["magnifier", "cigarette", "saw", "beer", "pill", "handcuffs"]
ITEM_EMOJI = {
    "magnifier": "🔍放大镜",
    "cigarette": "🚬香烟",
    "saw": "🪚手锯",
    "beer": "🍺啤酒",
    "pill": "💊药丸",
    "handcuffs": "⛓️手铐",
}
ITEM_SHORT = {
    "magnifier": "🔍",
    "cigarette": "🚬",
    "saw": "🪚",
    "beer": "🍺",
    "pill": "💊",
    "handcuffs": "⛓️",
}


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

    for p in state.players:
        if p["alive"]:
            p["hp"] = cfg["hp"]
            p["max_hp"] = cfg["hp"]
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

    line = f"🔄 第{rnd + 1}轮 {live_count}🔴+{blank_count}⚪"
    if rnd > 0:
        line += " 奖池+50%"
    msgs.append(line)

    for p in _alive_players(state):
        items = random.choices(ITEM_KEYS, k=cfg["items"])
        p["items"] = items
        item_text = "".join(ITEM_SHORT.get(i, i) for i in items)
        name = "😈" if p["is_devil"] else p["name"]
        msgs.append(f"🎁{name}: {item_text}")

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
        name = "😈" if next_tid == DEVIL_TG_ID else (_get_player(state, next_tid) or {}).get("name", "?")
        state.action_log.append(f"⛓️{name}被铐，跳过")
        state.turn_index = (state.turn_index + 1) % len(alive_tids)


def _check_round_end(state: GameState) -> list[str]:
    """Check whether current round/game ended and return messages."""
    alive = _alive_players(state)
    msgs: list[str] = []

    if len(alive) <= 1:
        state.phase = "finished"
        if alive:
            state.winner_tg_id = alive[0]["tg_id"]
            winner_name = "😈" if alive[0]["is_devil"] else alive[0]["name"]
            msgs.append(f"🏆 {winner_name} 获胜！")
        else:
            msgs.append("💀 全灭")
        return msgs

    if state.shell_index >= len(state.shells):
        state.current_round += 1
        if state.current_round >= len(ROUND_CONFIG):
            alive.sort(key=lambda p: p["hp"], reverse=True)
            top_hp = alive[0]["hp"]
            tied = [p for p in alive if p["hp"] == top_hp]
            if len(tied) > 1:
                # HP tie → draw, no winner
                state.phase = "finished"
                state.winner_tg_id = 0
                tied_names = "、".join(
                    "😈" if p.get("is_devil") else p["name"] for p in tied
                )
                msgs.append(f"🔚 弹尽，{tied_names}以{top_hp}HP平局！各自退回赌注")
            else:
                state.winner_tg_id = alive[0]["tg_id"]
                state.phase = "finished"
                winner_name = "😈" if alive[0]["is_devil"] else alive[0]["name"]
                msgs.append(f"🔚 弹尽，{winner_name}以{alive[0]['hp']}HP获胜")
        else:
            msgs.extend(_init_round(state))

    return msgs


def _do_shoot(state: GameState, shooter_tg_id: int, target_tg_id: int) -> list[str]:
    """Execute one shot and return messages."""
    shooter = _get_player(state, shooter_tg_id)
    target = _get_player(state, target_tg_id)
    if not shooter or not target:
        return ["❌ 玩家不存在"]
    if not shooter["alive"]:
        return ["❌ 你已出局"]
    if not target["alive"]:
        return ["❌ 目标已出局"]
    if state.shell_index >= len(state.shells):
        return ["❌ 当前没有可用子弹"]

    is_live = state.shells[state.shell_index]
    state.shell_index += 1
    remaining = state.shells[state.shell_index:]
    state.live_count = sum(1 for s in remaining if s)
    state.blank_count = sum(1 for s in remaining if not s)

    shooter_name = "😈" if shooter["is_devil"] else shooter["name"]
    target_name = "😈" if target["is_devil"] else target["name"]
    self_shot = shooter_tg_id == target_tg_id
    shooter["known_shell"] = None

    damage = 2 if shooter.get("saw_active") else 1
    shooter["saw_active"] = False
    msgs: list[str] = []

    if is_live:
        target["hp"] -= damage
        if self_shot:
            msgs.append(f"🔫{shooter_name}→自己 🔴-{damage}HP")
        else:
            msgs.append(f"🔫{shooter_name}→{target_name} 🔴-{damage}HP")

        if target["hp"] <= 0:
            target["hp"] = 0
            target["alive"] = False
            msgs.append(f"💀{target_name}淘汰")
    else:
        if self_shot:
            msgs.append(f"🔫{shooter_name}→自己 ⚪+回合")
        else:
            msgs.append(f"🔫{shooter_name}→{target_name} ⚪")

    extra_turn = (not is_live) and self_shot and shooter["alive"]
    if not extra_turn:
        _advance_turn(state)

    msgs.extend(_check_round_end(state))
    return msgs


def _use_item(state: GameState, user_tg_id: int, item_key: str, target_tg_id: int = 0) -> list[str]:
    """Use one item and return messages."""
    player = _get_player(state, user_tg_id)
    if not player or not player["alive"]:
        return ["❌ 玩家已出局"]
    if item_key not in player.get("items", []):
        return ["❌ 你没有这个道具"]

    player["items"].remove(item_key)
    name = "😈" if player["is_devil"] else player["name"]
    msgs: list[str] = []

    if item_key == "magnifier":
        if state.shell_index < len(state.shells):
            is_live = state.shells[state.shell_index]
            player["known_shell"] = "live" if is_live else "blank"
            msgs.append(f"🔍{name}查看弹药")
        else:
            msgs.append(f"🔍{name}放大镜无效(空弹夹)")

    elif item_key == "cigarette":
        max_hp = player.get("max_hp", ROUND_CONFIG[state.current_round]["hp"])
        if player["hp"] < max_hp:
            player["hp"] += 1
            msgs.append(f"🚬{name}+1HP({player['hp']}/{max_hp})")
        else:
            msgs.append(f"🚬{name}满血无效")

    elif item_key == "saw":
        player["saw_active"] = True
        msgs.append(f"🪚{name}下一枪×2")

    elif item_key == "beer":
        if state.shell_index < len(state.shells):
            ejected_live = state.shells[state.shell_index]
            state.shell_index += 1
            remaining = state.shells[state.shell_index:]
            state.live_count = sum(1 for s in remaining if s)
            state.blank_count = sum(1 for s in remaining if not s)
            player["known_shell"] = None
            shell_name = "🔴" if ejected_live else "⚪"
            msgs.append(f"🍺{name}退膛{shell_name}")
            msgs.extend(_check_round_end(state))
        else:
            msgs.append(f"🍺{name}退膛无效(空弹夹)")

    elif item_key == "pill":
        if random.random() < 0.5:
            max_hp = player.get("max_hp", ROUND_CONFIG[state.current_round]["hp"])
            player["hp"] = min(player["hp"] + 2, max_hp)
            msgs.append(f"💊{name}+2HP({player['hp']}/{max_hp})")
        else:
            player["hp"] -= 1
            msgs.append(f"💊{name}-1HP({player['hp']})")
            if player["hp"] <= 0:
                player["hp"] = 0
                player["alive"] = False
                msgs.append(f"💀{name}药物过量")
                msgs.extend(_check_round_end(state))

    elif item_key == "handcuffs":
        alive_opponents = [p for p in _alive_players(state) if p["tg_id"] != user_tg_id]

        if target_tg_id:
            target = _get_player(state, target_tg_id)
            if target and target["alive"] and target["tg_id"] != user_tg_id:
                state.handcuffed_tg_id = target_tg_id
                t_name = "😈" if target["is_devil"] else target["name"]
                msgs.append(f"⛓️{name}铐住{t_name}")
            else:
                player["items"].append("handcuffs")
                msgs.append("❌ 手铐目标无效")
        elif alive_opponents:
            alive_opponents.sort(key=lambda p: p["hp"], reverse=True)
            target = alive_opponents[0]
            state.handcuffed_tg_id = target["tg_id"]
            t_name = "😈" if target["is_devil"] else target["name"]
            msgs.append(f"⛓️{name}铐住{t_name}")
        else:
            player["items"].append("handcuffs")
            msgs.append(f"⛓️{name}手铐无效")

    return msgs


def _devil_turn(state: GameState) -> list[str]:
    """Execute devil AI turn."""
    devil = _get_player(state, DEVIL_TG_ID)
    if not devil or not devil["alive"]:
        return []

    msgs: list[str] = []
    alive_opponents = [p for p in _alive_players(state) if p["tg_id"] != DEVIL_TG_ID]
    if not alive_opponents:
        return msgs

    making_mistake = random.random() < 0.10
    items = list(devil.get("items", []))

    if "magnifier" in items and not making_mistake:
        msgs.extend(_use_item(state, DEVIL_TG_ID, "magnifier"))
        items = list(devil.get("items", []))

    known = devil.get("known_shell")

    if known == "live" and "saw" in items and not making_mistake:
        msgs.extend(_use_item(state, DEVIL_TG_ID, "saw"))
        items = list(devil.get("items", []))

    if "handcuffs" in items and alive_opponents and not making_mistake:
        strongest = max(alive_opponents, key=lambda p: p["hp"])
        msgs.extend(_use_item(state, DEVIL_TG_ID, "handcuffs", target_tg_id=strongest["tg_id"]))
        items = list(devil.get("items", []))

    if "beer" in items and known is None and not making_mistake:
        msgs.extend(_use_item(state, DEVIL_TG_ID, "beer"))
        items = list(devil.get("items", []))
        known = devil.get("known_shell")

    max_hp = devil.get("max_hp", ROUND_CONFIG[state.current_round]["hp"])
    if "cigarette" in items and devil["hp"] < max_hp and not making_mistake:
        msgs.extend(_use_item(state, DEVIL_TG_ID, "cigarette"))
        items = list(devil.get("items", []))

    if "pill" in items and devil["hp"] <= 1 and not making_mistake:
        msgs.extend(_use_item(state, DEVIL_TG_ID, "pill"))
        if not devil["alive"]:
            return msgs

    if state.phase != "playing" or state.shell_index >= len(state.shells):
        return msgs

    known = devil.get("known_shell")
    if making_mistake:
        target = random.choice(alive_opponents + [devil])
        msgs.extend(_do_shoot(state, DEVIL_TG_ID, target["tg_id"]))
    elif known == "blank":
        msgs.extend(_do_shoot(state, DEVIL_TG_ID, DEVIL_TG_ID))
    elif known == "live":
        weakest = min(alive_opponents, key=lambda p: p["hp"])
        msgs.extend(_do_shoot(state, DEVIL_TG_ID, weakest["tg_id"]))
    else:
        remaining = state.shells[state.shell_index:]
        live_ratio = sum(1 for s in remaining if s) / max(1, len(remaining))
        if live_ratio <= 0.35:
            msgs.extend(_do_shoot(state, DEVIL_TG_ID, DEVIL_TG_ID))
        else:
            weakest = min(alive_opponents, key=lambda p: p["hp"])
            msgs.extend(_do_shoot(state, DEVIL_TG_ID, weakest["tg_id"]))

    if state.phase == "playing" and _current_turn_tg_id(state) == DEVIL_TG_ID and devil["alive"]:
        msgs.extend(_devil_turn(state))

    return msgs


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

    cd_key = f"roulette_cd:{creator_tg_id}"
    if await r.get(cd_key):
        ttl = await r.ttl(cd_key)
        mins = max(1, (ttl if ttl > 0 else 60) // 60)
        return False, f"❌ 冷却中，还需 {mins} 分钟", None

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

    cd_key = f"roulette_cd:{tg_id}"
    if await r.get(cd_key):
        ttl = await r.ttl(cd_key)
        mins = max(1, (ttl if ttl > 0 else 60) // 60)
        return False, f"❌ 冷却中，还需 {mins} 分钟", None

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
                    name="😈魔鬼",
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
    first_name = "😈" if (first_player and first_player["is_devil"]) else (first_player or {}).get("name", "?")

    result = "\n".join(init_msgs) + f"\n\n🎯 {first_name} 先手"

    devil_msgs: list[str] = []
    if first_tid == DEVIL_TG_ID:
        devil_msgs = _devil_turn(state)
        state.action_log.extend(devil_msgs)
        await _save_state(state)

    if devil_msgs:
        result += "\n" + "\n".join(devil_msgs)

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

    devil_msgs: list[str] = []
    if state.phase == "playing":
        while _current_turn_tg_id(state) == DEVIL_TG_ID:
            devil = _get_player(state, DEVIL_TG_ID)
            if not devil or not devil["alive"]:
                break
            turn_msgs = _devil_turn(state)
            devil_msgs.extend(turn_msgs)
            state.action_log.extend(turn_msgs)
            if state.phase != "playing":
                break

    await _save_state(state)

    all_msgs = "\n".join(shoot_msgs)
    if devil_msgs:
        all_msgs += "\n" + "\n".join(devil_msgs)

    if state.phase == "finished":
        all_msgs += "\n\n" + await _settle_game(state)

    return True, all_msgs, state


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

    devil_msgs: list[str] = []
    if state.phase == "playing":
        while _current_turn_tg_id(state) == DEVIL_TG_ID:
            devil = _get_player(state, DEVIL_TG_ID)
            if not devil or not devil["alive"]:
                break
            turn_msgs = _devil_turn(state)
            devil_msgs.extend(turn_msgs)
            state.action_log.extend(turn_msgs)
            if state.phase != "playing":
                break

    await _save_state(state)

    all_msgs = "\n".join(item_msgs)
    if devil_msgs:
        all_msgs += "\n" + "\n".join(devil_msgs)

    if state.phase == "finished":
        all_msgs += "\n\n" + await _settle_game(state)

    return True, all_msgs, state


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
        # Full refund for everyone (points).
        from services.user_service import add_points

        if state.bet > 0:
            for p in state.players:
                if p.get("is_devil"):
                    continue
                try:
                    await add_points(p["tg_id"], state.bet)
                except Exception:
                    logger.exception("Failed to refund roulette points for tg_id=%s", p["tg_id"])

        await _cleanup_room(room_id, [p["tg_id"] for p in state.players])
        return True, "❌ 房间已关闭，已全额退还积分"

    if state.phase == "playing":
        if not player["alive"]:
            return False, "❌ 你已出局，无法再次放弃"

        player["alive"] = False
        player["hp"] = 0
        state.action_log.append(f"🏳️{player['name']}放弃(-50%)")

        refund = state.bet // 2
        if refund > 0:
            from services.user_service import add_points

            try:
                await add_points(tg_id, refund)
            except Exception:
                logger.exception("Failed to refund roulette forfeit points")
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
    """Distribute winnings (points) and cleanup room."""
    r = await get_redis()
    human_players = [p for p in state.players if not p.get("is_devil")]
    base_pot = state.bet * len(human_players)
    # Pot increases 50% per additional round
    round_reached = min(state.current_round, len(ROUND_CONFIG) - 1)
    multiplier = 1.5 ** round_reached
    # Subtract already-refunded forfeit amounts to prevent inflation
    total_pot = int(base_pot * multiplier) - state.forfeited_pool
    total_pot = max(total_pot, 0)
    winner = _get_player(state, state.winner_tg_id)

    msgs: list[str] = []

    # Draw: refund all surviving human players
    if state.winner_tg_id == 0 and len(_alive_players(state)) > 1:
        from services.user_service import add_points

        alive_humans = [p for p in _alive_players(state) if not p.get("is_devil")]
        per_player = state.bet  # each gets their original bet back
        for p in alive_humans:
            try:
                await add_points(p["tg_id"], per_player)
            except Exception:
                logger.exception("Failed to refund draw for tg_id=%s", p["tg_id"])
        msgs.append(f"🤝 平局！各退回 {per_player:,} 积分")
    elif winner and not winner.get("is_devil"):
        from services.user_service import add_points, add_reputation, get_user_by_tg_id

        winnings = total_pot
        reputation_gain = 5 + len(human_players) * 3

        try:
            await add_points(winner["tg_id"], winnings)
        except Exception:
            logger.exception("Failed to settle roulette winnings (points)")

        try:
            from db.engine import async_session

            async with async_session() as session:
                async with session.begin():
                    user = await get_user_by_tg_id(session, winner["tg_id"])
                    if user:
                        await add_reputation(session, user.id, reputation_gain)
        except Exception:
            logger.exception("Failed to add roulette reputation")

        msgs.append(f"💰 {winner['name']} +{winnings:,}积分 +{reputation_gain}声望")
    elif winner and winner.get("is_devil"):
        msgs.append("😈 魔鬼获胜！积分被吞噬。")
    else:
        msgs.append("💀 全灭，积分消失。")

    for p in human_players:
        await r.set(f"roulette_cd:{p['tg_id']}", "1", ex=COOLDOWN_TTL)

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
        from services.user_service import add_points

        try:
            await add_points(tg_id, state.bet)
        except Exception:
            logger.exception("Failed to refund leave_room points for tg_id=%s", tg_id)

    return True, f"✅ 已退出房间，退还 {state.bet:,} 积分", state


async def get_player_room(tg_id: int) -> str | None:
    """Return current room_id for user, if any."""
    r = await get_redis()
    return await r.get(f"roulette_player:{tg_id}")


async def get_game_state(room_id: str) -> GameState | None:
    """Read-only game state query."""
    return await _load_state(room_id)


def render_game_panel(state: GameState, viewer_tg_id: int = 0) -> str:
    """Render game panel text."""
    if state.phase == "waiting":
        names = ", ".join(p["name"] for p in state.players)
        return (
            "😈 恶魔轮盘赌 - 等待玩家\n"
            + ("━" * 20)
            + "\n"
            + f"💰 赌注: {state.bet:,}积分/人\n"
            + f"👥 玩家: {names} ({len(state.players)}/3)\n"
            + ("━" * 20)
            + "\n等待更多玩家加入，或由房主开始游戏"
        )

    rnd = min(state.current_round, len(ROUND_CONFIG) - 1) + 1
    lines = [f"😈 恶魔轮盘赌 R{rnd}", "━" * 20]

    # HP display with hearts
    for p in state.players:
        name = "😈" if p.get("is_devil") else p.get("name", "?")
        if not p.get("alive"):
            lines.append(f"💀 {name}")
            continue
        max_hp = max(1, int(p.get("max_hp", 1) or 1))
        hp = max(0, min(int(p.get("hp", 0) or 0), max_hp))
        hearts = "❤️" * hp + "🖤" * (max_hp - hp)
        lines.append(f"{name} {hearts}")

    # Ammo
    remaining = max(0, len(state.shells) - state.shell_index)
    lines.append(f"🔫 {state.live_count}🔴+{state.blank_count}⚪ ({remaining}发)")

    # Current turn
    if state.phase == "playing":
        current = _current_turn_tg_id(state)
        cp = _get_player(state, current)
        if cp:
            c_name = "😈" if cp.get("is_devil") else cp.get("name", "?")
            lines.append(f"🎯 {c_name}的回合")

    # Current turn player's items (shared panel friendly).
    current_tid = _current_turn_tg_id(state)
    holder = _get_player(state, current_tid)
    if holder and holder.get("alive") and holder.get("items"):
        item_text = "".join(ITEM_SHORT.get(i, i) for i in holder["items"])
        holder_name = "😈" if holder.get("is_devil") else holder.get("name", "?")
        lines.append(f"🎁 {holder_name}: {item_text}")

    # Show viewer's own items if they are not the current turn player
    if viewer_tg_id and viewer_tg_id != current_tid:
        viewer = _get_player(state, viewer_tg_id)
        if viewer and viewer.get("alive") and viewer.get("items"):
            v_items = "".join(ITEM_SHORT.get(i, i) for i in viewer["items"])
            lines.append(f"🎒 你的道具: {v_items}")

    # Action log (last 5 with category-aware compaction)
    recent = state.action_log[-5:]
    if recent:
        lines.append("━" * 20)
        for entry in recent:
            lines.append(entry)

    return "\n".join(lines)

