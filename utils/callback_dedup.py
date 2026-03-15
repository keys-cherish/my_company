"""Callback dedup middleware — prevent duplicate execution from network retries.

Uses Redis SET NX to ensure each callback_query.id is processed exactly once.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from cache.redis_client import get_redis

_logger = logging.getLogger(__name__)

# How long to remember a processed callback (seconds).
# Telegram retries happen within a few seconds; 30s is more than enough.
_DEDUP_TTL = 30


class CallbackDedupMiddleware(BaseMiddleware):
    """Drop duplicate callback_query invocations caused by network retries."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        cb_id = event.id
        if not cb_id:
            return await handler(event, data)

        r = await get_redis()
        key = f"cb_dedup:{cb_id}"

        # SET NX: returns True only for the first call
        is_new = await r.set(key, "1", nx=True, ex=_DEDUP_TTL)
        if not is_new:
            _logger.debug("Duplicate callback dropped: %s", cb_id)
            try:
                await event.answer()
            except Exception:
                pass
            return None

        return await handler(event, data)
