"""Small async in-memory Redis replacement for unit tests."""

from __future__ import annotations

import time


class FakeRedis:
    """Subset of redis.asyncio API used by this project."""

    def __init__(self):
        self._kv: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._sets: dict[str, set[str]] = {}
        self._lists: dict[str, list[str]] = {}

    def _cleanup_key(self, key: str) -> None:
        expires_at = self._expiry.get(key)
        if expires_at is not None and expires_at <= time.time():
            self._kv.pop(key, None)
            self._expiry.pop(key, None)

    async def get(self, key: str):
        self._cleanup_key(key)
        return self._kv.get(key)

    async def set(self, key: str, value, nx: bool = False, ex: int | None = None):
        self._cleanup_key(key)
        if nx and key in self._kv:
            return False
        self._kv[key] = str(value)
        if ex is not None:
            self._expiry[key] = time.time() + int(ex)
        else:
            self._expiry.pop(key, None)
        return True

    async def setex(self, key: str, ttl_seconds: int, value):
        self._kv[key] = str(value)
        self._expiry[key] = time.time() + int(ttl_seconds)
        return True

    async def exists(self, key: str) -> int:
        self._cleanup_key(key)
        if key in self._kv or key in self._hashes or key in self._sets or key in self._lists:
            return 1
        return 0

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            self._cleanup_key(key)
            found = False
            if key in self._kv:
                self._kv.pop(key, None)
                found = True
            if key in self._hashes:
                self._hashes.pop(key, None)
                found = True
            if key in self._sets:
                self._sets.pop(key, None)
                found = True
            if key in self._lists:
                self._lists.pop(key, None)
                found = True
            if found:
                self._expiry.pop(key, None)
                removed += 1
        return removed

    async def ttl(self, key: str) -> int:
        self._cleanup_key(key)
        if key not in self._kv:
            return -2
        expires_at = self._expiry.get(key)
        if expires_at is None:
            return -1
        remain = int(expires_at - time.time())
        if remain <= 0:
            self._kv.pop(key, None)
            self._expiry.pop(key, None)
            return -2
        return remain

    async def expire(self, key: str, ttl_seconds: int) -> bool:
        self._cleanup_key(key)
        if key not in self._kv and key not in self._hashes and key not in self._sets and key not in self._lists:
            return False
        self._expiry[key] = time.time() + int(ttl_seconds)
        return True

    async def incrby(self, key: str, amount: int) -> int:
        self._cleanup_key(key)
        current = int(self._kv.get(key, "0"))
        new_val = current + int(amount)
        self._kv[key] = str(new_val)
        return new_val

    async def incr(self, key: str) -> int:
        return await self.incrby(key, 1)

    async def eval(self, _script: str, _numkeys: int, *args):
        """Simplified eval that recognises two scripts by their key patterns:

        1) Points deduction: eval(script, 1, key, amount)
        2) Red packet grab: eval(script, 3, pk_key, grabs_key, results_key, tg_id)
        """
        if _numkeys == 1:
            # Points deduction script
            key, amount = args[0], args[1]
            current = int((await self.get(key)) or 0)
            amount_i = int(amount)
            if current < amount_i:
                return -1
            return await self.incrby(key, -amount_i)

        if _numkeys == 3:
            # Red packet grab script
            import random as _rng
            pk_key, grabs_key, results_key = args[0], args[1], args[2]
            tg_id = str(args[3])

            remaining = await self.hget(pk_key, "remaining")
            remaining_count = await self.hget(pk_key, "remaining_count")
            if remaining is None or remaining_count is None:
                return [-1, 0]
            remaining = int(remaining)
            remaining_count = int(remaining_count)
            if remaining_count <= 0:
                return [-1, 0]

            if await self.sismember(grabs_key, tg_id):
                return [-2, 0]

            if remaining_count == 1:
                amount = remaining
            else:
                avg = remaining // remaining_count
                max_grab = min(remaining - (remaining_count - 1), avg * 2)
                max_grab = max(max_grab, 1)
                amount = _rng.randint(1, max_grab)

            await self.hincrby(pk_key, "remaining", -amount)
            await self.hincrby(pk_key, "remaining_count", -1)
            await self.sadd(grabs_key, tg_id)
            await self.rpush(results_key, f"{tg_id}:{amount}")

            return [amount, remaining_count - 1]

        raise NotImplementedError(f"FakeRedis.eval: unsupported numkeys={_numkeys}")

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        z = self._zsets.setdefault(key, {})
        for member, score in mapping.items():
            z[str(member)] = float(score)
        return len(mapping)

    # ---- Hash commands ----
    async def hset(self, key: str, mapping: dict[str, str] | None = None, **kwargs) -> int:
        h = self._hashes.setdefault(key, {})
        data = {}
        if mapping:
            data.update(mapping)
        data.update(kwargs)
        for k, v in data.items():
            h[str(k)] = str(v)
        return len(data)

    async def hget(self, key: str, field: str):
        h = self._hashes.get(key, {})
        return h.get(str(field))

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        h = self._hashes.setdefault(key, {})
        current = int(h.get(str(field), "0"))
        new_val = current + int(amount)
        h[str(field)] = str(new_val)
        return new_val

    # ---- Set commands ----
    async def sadd(self, key: str, *members: str) -> int:
        s = self._sets.setdefault(key, set())
        added = 0
        for m in members:
            if str(m) not in s:
                s.add(str(m))
                added += 1
        return added

    async def sismember(self, key: str, member: str) -> int:
        s = self._sets.get(key, set())
        return 1 if str(member) in s else 0

    async def scard(self, key: str) -> int:
        return len(self._sets.get(key, set()))

    async def smembers(self, key: str) -> set[str]:
        return set(self._sets.get(key, set()))

    # ---- List commands ----
    async def rpush(self, key: str, *values: str) -> int:
        lst = self._lists.setdefault(key, [])
        for v in values:
            lst.append(str(v))
        return len(lst)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        lst = self._lists.get(key, [])
        if stop == -1:
            return lst[start:]
        return lst[start: stop + 1]

    async def llen(self, key: str) -> int:
        return len(self._lists.get(key, []))

    async def zrevrange(self, key: str, start: int, end: int, withscores: bool = False):
        z = self._zsets.get(key, {})
        ranked = sorted(z.items(), key=lambda item: item[1], reverse=True)

        real_end = len(ranked) - 1 if end == -1 else end
        sliced = ranked[start: real_end + 1]
        if withscores:
            return sliced
        return [member for member, _ in sliced]

    async def aclose(self):
        self._kv.clear()
        self._expiry.clear()
        self._zsets.clear()
        self._hashes.clear()
        self._sets.clear()
        self._lists.clear()
