"""Throttle middleware — per-user rate limiting via Redis."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from cache.redis_client import get_redis

logger = logging.getLogger(__name__)

# 默认限流：每用户每秒2条消息，回调每秒3次
MSG_RATE_LIMIT = 2      # messages per window
CB_RATE_LIMIT = 3       # callbacks per window
RATE_WINDOW = 1          # seconds


class ThrottleMiddleware(BaseMiddleware):
    """Per-user rate limiter using Redis sliding window."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        tg_id = user.id

        if isinstance(event, CallbackQuery):
            key = f"throttle:cb:{tg_id}"
            limit = CB_RATE_LIMIT
        elif isinstance(event, Message):
            key = f"throttle:msg:{tg_id}"
            limit = MSG_RATE_LIMIT
        else:
            return await handler(event, data)

        r = await get_redis()
        async with r.pipeline(transaction=False) as pipe:
            pipe.incr(key)
            pipe.expire(key, RATE_WINDOW)
            results = await pipe.execute()
        current = results[0]

        if current > limit:
            if isinstance(event, CallbackQuery):
                await event.answer("操作太频繁，请稍后再试", show_alert=False)
            # 静默丢弃，不回复消息避免刷屏
            return None

        return await handler(event, data)
