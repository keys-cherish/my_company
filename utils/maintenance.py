"""Global maintenance mode helpers and middleware."""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, types
from aiogram.types import TelegramObject

from cache.redis_client import get_redis
from commands import CMD_COMPENSATE, CMD_MAINTAIN
from handlers.common import is_super_admin

MAINTENANCE_MODE_KEY = "maintenance:global"
MAINTENANCE_PIN_KEY = "maintenance:pin"
COMPENSATION_PIN_KEY = "maintenance:compensation:pin"
MAINTENANCE_COMPENSATION_BONUS = 500

# Local cache to avoid querying Redis on every event
_maintenance_cache: dict[str, Any] = {"value": False, "ts": 0.0}
_CACHE_TTL = 10  # seconds


async def is_maintenance_mode() -> bool:
    now = time.monotonic()
    if now - _maintenance_cache["ts"] < _CACHE_TTL:
        return _maintenance_cache["value"]
    r = await get_redis()
    result = bool(await r.exists(MAINTENANCE_MODE_KEY))
    _maintenance_cache["value"] = result
    _maintenance_cache["ts"] = now
    return result


async def set_maintenance_mode(payload: dict[str, Any]) -> None:
    r = await get_redis()
    await r.set(MAINTENANCE_MODE_KEY, json.dumps(payload, ensure_ascii=False))
    _maintenance_cache["value"] = True
    _maintenance_cache["ts"] = time.monotonic()


async def clear_maintenance_mode() -> None:
    r = await get_redis()
    await r.delete(MAINTENANCE_MODE_KEY)
    _maintenance_cache["value"] = False
    _maintenance_cache["ts"] = time.monotonic()


def parse_command_name(text: str | None) -> str:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return ""
    token = raw.split(maxsplit=1)[0][1:]
    return token.split("@", 1)[0].lower()


class MaintenanceModeMiddleware(BaseMiddleware):
    """Block all interactions when maintenance mode is active."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not await is_maintenance_mode():
            return await handler(event, data)

        if isinstance(event, types.Message):
            uid = event.from_user.id if event.from_user else 0
            cmd = parse_command_name(event.text)
            if is_super_admin(uid) and cmd in {CMD_MAINTAIN, CMD_COMPENSATE}:
                return await handler(event, data)
            # Only reply to bot commands, silently ignore normal messages
            if cmd and cmd.startswith("cp_"):
                await event.answer("🔧 系统维护中，暂时暂停所有命令和操作，请稍后再试。")
            return None

        if isinstance(event, types.CallbackQuery):
            await event.answer("🔧 系统维护中，请稍后再试。", show_alert=True)
            return None

        return await handler(event, data)
