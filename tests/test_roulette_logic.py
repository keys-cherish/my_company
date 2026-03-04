"""Regression tests for roulette safety rules."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from db.models import Company, User
from handlers.roulette import cb_roulette_create
from services.roulette_service import (
    MIN_BET,
    cancel_game,
    consume_points,
    create_room,
    get_game_state,
    get_player_room,
    join_room,
)
from services.user_service import get_traffic_by_tg_id

from tests.helpers.async_db_case import AsyncDBTestCase
from tests.helpers.fake_redis import FakeRedis


class TestRouletteLogic(AsyncDBTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake_redis = FakeRedis()

        async def _fake_get_redis():
            return self.fake_redis

        self._patcher_cache_redis = patch("cache.redis_client.get_redis", new=_fake_get_redis)
        self._patcher_roulette_redis = patch("services.roulette_service.get_redis", new=_fake_get_redis)
        self._patcher_db_async_session = patch("db.engine.async_session", new=self.Session)
        self._patcher_user_service_redis = patch("services.user_service.get_redis", new=_fake_get_redis)

        self._patcher_cache_redis.start()
        self._patcher_roulette_redis.start()
        self._patcher_db_async_session.start()
        self._patcher_user_service_redis.start()

        self.addCleanup(self._patcher_cache_redis.stop)
        self.addCleanup(self._patcher_roulette_redis.stop)
        self.addCleanup(self._patcher_db_async_session.stop)
        self.addCleanup(self._patcher_user_service_redis.stop)

    async def _new_user_company(self, session, tg_id: int, name: str, funds: int) -> tuple[User, Company]:
        user = User(tg_id=tg_id, tg_name=f"user-{tg_id}", traffic=funds, reputation=0)
        session.add(user)
        await session.flush()

        company = Company(
            name=name,
            owner_id=user.id,
            company_type="tech",
            total_funds=funds,
            daily_revenue=0,
            level=1,
            employee_count=10,
        )
        session.add(company)
        await session.flush()

        return user, company

    async def test_waiting_room_cancel_refunds_all_bets(self):
        """Cancel in waiting room refunds all players' roulette bets."""
        room_id = "r_waiting_refund"
        bet = MIN_BET

        async with self.Session() as session:
            async with session.begin():
                owner1, company1 = await self._new_user_company(session, 8001, "RouletteA", 100_000)
                owner2, company2 = await self._new_user_company(session, 8002, "RouletteB", 100_000)
                company1_id = company1.id
                company2_id = company2.id
                owner1_tg_id = owner1.tg_id
                owner2_tg_id = owner2.tg_id

        # Manually deduct personal balance (simulating what handler does)
        ok = await consume_points(owner1_tg_id, bet)
        self.assertTrue(ok)
        ok = await consume_points(owner2_tg_id, bet)
        self.assertTrue(ok)

        bal1_before = await get_traffic_by_tg_id(owner1_tg_id)
        bal2_before = await get_traffic_by_tg_id(owner2_tg_id)

        ok, _msg, _state = await create_room(
            room_id=room_id,
            creator_tg_id=owner1_tg_id,
            creator_company_id=company1_id,
            creator_name="RouletteA",
            bet=bet,
        )
        self.assertTrue(ok)

        ok, _msg, _state = await join_room(
            room_id=room_id,
            tg_id=owner2_tg_id,
            company_id=company2_id,
            player_name="RouletteB",
        )
        self.assertTrue(ok)

        ok, _msg = await cancel_game(room_id=room_id, tg_id=owner1_tg_id)
        self.assertTrue(ok)

        # Personal balances should be refunded (+bet back)
        bal1_after = await get_traffic_by_tg_id(owner1_tg_id)
        bal2_after = await get_traffic_by_tg_id(owner2_tg_id)
        self.assertEqual(bal1_after, bal1_before + bet)
        self.assertEqual(bal2_after, bal2_before + bet)

        self.assertIsNone(await get_player_room(owner1_tg_id))
        self.assertIsNone(await get_player_room(owner2_tg_id))
        self.assertIsNone(await get_game_state(room_id))

    async def test_join_room_ignores_cooldown_marker(self):
        room_id = "r_join_cd"

        async with self.Session() as session:
            async with session.begin():
                creator, creator_company = await self._new_user_company(session, 8011, "RouletteC", 100_000)
                joiner, joiner_company = await self._new_user_company(session, 8012, "RouletteD", 100_000)
                creator_tg_id = creator.tg_id
                creator_company_id = creator_company.id
                joiner_tg_id = joiner.tg_id
                joiner_company_id = joiner_company.id

        ok = await consume_points(creator_tg_id, MIN_BET)
        self.assertTrue(ok)
        ok, _msg, _state = await create_room(
            room_id=room_id,
            creator_tg_id=creator_tg_id,
            creator_company_id=creator_company_id,
            creator_name="RouletteC",
            bet=MIN_BET,
        )
        self.assertTrue(ok)

        await self.fake_redis.set(f"roulette_cd:{joiner_tg_id}", "1", ex=120)
        ok, _msg, _state = await join_room(
            room_id=room_id,
            tg_id=joiner_tg_id,
            company_id=joiner_company_id,
            player_name="RouletteD",
        )
        self.assertTrue(ok)
        self.assertEqual(await get_player_room(joiner_tg_id), room_id)

        state = await get_game_state(room_id)
        self.assertIsNotNone(state)
        self.assertEqual(len(state.players), 2)

    async def test_create_handler_rejects_non_owner_company_usage(self):
        async with self.Session() as session:
            async with session.begin():
                owner, company = await self._new_user_company(session, 8021, "OwnerCompany", 100_000)
                attacker = User(tg_id=8022, tg_name="attacker", traffic=100_000, reputation=0)
                session.add(attacker)
                await session.flush()

                company_id = company.id
                attacker_tg_id = attacker.tg_id

        callback = SimpleNamespace(
            data=f"roulette:create:{company_id}:{MIN_BET}",
            from_user=SimpleNamespace(id=attacker_tg_id),
            answer=AsyncMock(),
            message=SimpleNamespace(
                edit_text=AsyncMock(),
                answer=AsyncMock(),
                chat=SimpleNamespace(id=1),
            ),
        )

        with patch("handlers.roulette.create_room", new=AsyncMock()) as mock_create_room:
            await cb_roulette_create(callback)
            mock_create_room.assert_not_called()

        answer_args, answer_kwargs = callback.answer.await_args
        self.assertIn("老板", answer_args[0])
        self.assertTrue(answer_kwargs.get("show_alert"))

        # Balance should NOT be deducted (attacker is not owner)
        bal = await get_traffic_by_tg_id(attacker_tg_id)
        self.assertEqual(bal, 100_000)
