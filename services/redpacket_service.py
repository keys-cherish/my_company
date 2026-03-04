"""公司红包系统 — 发红包/抢红包。

Redis keys:
  redpacket:{packet_id}        — Hash: sender_tg_id, total, remaining, count, remaining_count, company_name, ts, password
  redpacket_grabs:{packet_id}  — Set of tg_ids who already grabbed
  redpacket_results:{packet_id} — List of "tg_id:amount" for result display
"""

from __future__ import annotations

import datetime as dt
import random
import uuid

from cache.redis_client import get_redis
from config import settings


def _generate_packet_id() -> str:
    return uuid.uuid4().hex[:12]


async def create_redpacket(
    tg_id: int,
    company_name: str,
    total_amount: int,
    count: int,
    password: str = "",
) -> tuple[bool, str, str]:
    """Create a red packet.

    Args:
        tg_id: Sender's Telegram ID
        company_name: Company name
        total_amount: Total amount in the packet
        count: Number of shares
        password: Optional password (口令)

    Returns (success, message, packet_id).
    """
    if total_amount < settings.redpacket_min_amount:
        return False, f"❌ 红包金额至少 {settings.redpacket_min_amount:,} 积分", ""
    if total_amount > settings.redpacket_max_amount:
        return False, f"❌ 红包金额最多 {settings.redpacket_max_amount:,} 积分", ""
    if count < 1:
        return False, "❌ 红包个数至少为 1", ""
    if count > settings.redpacket_max_count:
        return False, f"❌ 红包个数最多 {settings.redpacket_max_count} 个", ""
    if total_amount < count:
        return False, "❌ 总金额不能少于红包个数（每个至少1积分）", ""

    packet_id = _generate_packet_id()
    r = await get_redis()

    mapping = {
        "sender_tg_id": str(tg_id),
        "company_name": company_name,
        "total": str(total_amount),
        "remaining": str(total_amount),
        "count": str(count),
        "remaining_count": str(count),
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if password:
        mapping["password"] = password

    await r.hset(f"redpacket:{packet_id}", mapping=mapping)
    await r.expire(f"redpacket:{packet_id}", settings.redpacket_expire_seconds)

    return True, "", packet_id


async def check_password(packet_id: str, password: str) -> tuple[bool, str]:
    """Check if password matches.

    Returns (matches, error_message).
    If packet has no password, always returns True.
    """
    r = await get_redis()
    stored_pw = await r.hget(f"redpacket:{packet_id}", "password")
    if not stored_pw:
        return True, ""
    stored_str = stored_pw if isinstance(stored_pw, str) else stored_pw.decode()
    if stored_str == password:
        return True, ""
    return False, "❌ 口令错误！"


async def has_password(packet_id: str) -> bool:
    """Check if packet requires password."""
    r = await get_redis()
    pw = await r.hget(f"redpacket:{packet_id}", "password")
    return bool(pw)

    return True, "", packet_id


# Lua script for atomically grabbing a red packet share
_GRAB_LUA = """
local pk = KEYS[1]
local grabs_key = KEYS[2]
local results_key = KEYS[3]
local tg_id = ARGV[1]

-- Check if packet exists
local remaining = tonumber(redis.call('HGET', pk, 'remaining'))
local remaining_count = tonumber(redis.call('HGET', pk, 'remaining_count'))
if remaining == nil or remaining_count == nil or remaining_count <= 0 then
    return {-1, 0}  -- packet gone
end

-- Check duplicate
if redis.call('SISMEMBER', grabs_key, tg_id) == 1 then
    return {-2, 0}  -- already grabbed
end

-- Calculate amount (拼手气: random between 1 and 2×avg, last person gets rest)
local amount
if remaining_count == 1 then
    amount = remaining
else
    local avg = math.floor(remaining / remaining_count)
    local max_grab = math.min(remaining - (remaining_count - 1), avg * 2)
    max_grab = math.max(max_grab, 1)
    amount = math.random(1, max_grab)
end

-- Update state
redis.call('HINCRBY', pk, 'remaining', -amount)
redis.call('HINCRBY', pk, 'remaining_count', -1)
redis.call('SADD', grabs_key, tg_id)
redis.call('RPUSH', results_key, tg_id .. ':' .. amount)

return {amount, remaining_count - 1}
"""


async def grab_redpacket(tg_id: int, packet_id: str) -> tuple[bool, str, int]:
    """Grab a share from a red packet.

    Returns (success, message, amount).
    """
    r = await get_redis()
    pk = f"redpacket:{packet_id}"

    # Check existence
    exists = await r.exists(pk)
    if not exists:
        return False, "❌ 红包已过期或不存在", 0

    result = await r.eval(
        _GRAB_LUA,
        3,
        pk,
        f"redpacket_grabs:{packet_id}",
        f"redpacket_results:{packet_id}",
        str(tg_id),
    )

    code = int(result[0])
    if code == -1:
        return False, "🧧 红包已被抢完了！", 0
    if code == -2:
        return False, "🧧 你已经抢过这个红包了", 0

    amount = code
    left = int(result[1])
    return True, f"🧧 恭喜抢到 {amount:,} 积分！（剩余{left}个）", amount


async def get_redpacket_info(packet_id: str) -> dict | None:
    """Get red packet info for display."""
    r = await get_redis()
    data = await r.hgetall(f"redpacket:{packet_id}")
    if not data:
        return None
    # Decode bytes if necessary
    info = {}
    for k, v in data.items():
        key = k if isinstance(k, str) else k.decode()
        val = v if isinstance(v, str) else v.decode()
        info[key] = val
    return info


async def get_redpacket_results(packet_id: str) -> list[tuple[int, int]]:
    """Get grab results: list of (tg_id, amount)."""
    r = await get_redis()
    results = await r.lrange(f"redpacket_results:{packet_id}", 0, -1)
    parsed = []
    for item in results:
        s = item if isinstance(item, str) else item.decode()
        parts = s.split(":")
        parsed.append((int(parts[0]), int(parts[1])))
    return parsed


async def find_lucky_king(packet_id: str) -> tuple[int, int] | None:
    """Find the person who grabbed the most. Returns (tg_id, amount) or None."""
    results = await get_redpacket_results(packet_id)
    if not results:
        return None
    return max(results, key=lambda x: x[1])


async def save_grabber_display_name(packet_id: str, tg_id: int, display_name: str) -> None:
    """Persist grabber display name snapshot for detail rendering."""
    name = (display_name or "").strip().replace("\n", " ")
    if not name:
        return
    if len(name) > 48:
        name = name[:48]

    r = await get_redis()
    name_key = f"redpacket_names:{packet_id}"
    await r.hset(name_key, str(tg_id), name)

    ttl = await r.ttl(f"redpacket:{packet_id}")
    if ttl and ttl > 0:
        await r.expire(name_key, ttl)
    else:
        await r.expire(name_key, settings.redpacket_expire_seconds)


async def get_grabber_display_names(packet_id: str) -> dict[int, str]:
    """Get cached grabber display names keyed by tg_id."""
    r = await get_redis()
    raw = await r.hgetall(f"redpacket_names:{packet_id}")
    if not raw:
        return {}

    result: dict[int, str] = {}
    for k, v in raw.items():
        try:
            tid = int(k if isinstance(k, str) else k.decode())
        except (ValueError, TypeError):
            continue
        name = v if isinstance(v, str) else v.decode()
        if name:
            result[tid] = name
    return result
