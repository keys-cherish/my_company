"""Regression tests for company-side spending rules."""

from __future__ import annotations

from unittest.mock import patch

from db.models import Company, User
from services.research_service import start_research
from services.shop_service import buy_item

from tests.helpers.async_db_case import AsyncDBTestCase
from tests.helpers.fake_redis import FakeRedis


class TestCompanySpendingLogic(AsyncDBTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake_redis = FakeRedis()

        async def _fake_get_redis():
            return self.fake_redis

        self._shop_redis_patcher = patch("services.shop_service.get_redis", new=_fake_get_redis)
        self._user_redis_patcher = patch("services.user_service.get_redis", new=_fake_get_redis)
        self._roadshow_redis_patcher = patch("services.roadshow_service.get_redis", new=_fake_get_redis)
        self._fundlog_redis_patcher = patch("services.fundlog_service.get_redis", new=_fake_get_redis)
        self._shop_redis_patcher.start()
        self._user_redis_patcher.start()
        self._roadshow_redis_patcher.start()
        self._fundlog_redis_patcher.start()
        self.addCleanup(self._shop_redis_patcher.stop)
        self.addCleanup(self._user_redis_patcher.stop)
        self.addCleanup(self._roadshow_redis_patcher.stop)
        self.addCleanup(self._fundlog_redis_patcher.stop)

    async def _new_user_and_company(self, session, tg_id: int, *, traffic: int, funds: int) -> tuple[User, Company]:
        user = User(tg_id=tg_id, tg_name=f"user-{tg_id}", traffic=traffic, reputation=100)
        session.add(user)
        await session.flush()
        company = Company(
            name=f"Company-{tg_id}",
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

    async def test_shop_purchase_uses_company_funds_not_user_wallet(self):
        async with self.Session() as session:
            async with session.begin():
                user, company = await self._new_user_and_company(
                    session, 6101, traffic=0, funds=10_000
                )
                ok, _msg = await buy_item(session, user.tg_id, company.id, "precision_marketing")
                self.assertTrue(ok)

                await session.refresh(user)
                await session.refresh(company)
                self.assertEqual(user.traffic, 0)
                self.assertEqual(company.total_funds, 7_000)

    async def test_research_cost_uses_company_funds_not_user_wallet(self):
        tech_tree = {
            "t1": {
                "name": "Test Tech",
                "cost": 1_000,
                "duration_seconds": 60,
                "prerequisites": [],
                "required_employees": 1,
                "required_reputation": 0,
            }
        }

        async with self.Session() as session:
            async with session.begin():
                user, company = await self._new_user_and_company(
                    session, 6102, traffic=0, funds=1_000
                )
                with (
                    patch("services.research_service._load_tech_tree", return_value=tech_tree),
                    patch("services.research_service.is_tech_allowed_for_company", return_value=True),
                ):
                    ok, msg = await start_research(session, company.id, user.id, "t1")
                self.assertTrue(ok, msg=f"Research failed: {msg}")

                await session.refresh(user)
                await session.refresh(company)
                self.assertEqual(user.traffic, 0)
                self.assertEqual(company.total_funds, 0)
