"""Boundary and equivalence-class tests for daily settlement."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from db.models import Company, Product, User
from services.settlement_service import settle_company

from tests.helpers.async_db_case import AsyncDBTestCase
from tests.helpers.fake_redis import FakeRedis


class TestSettlementLogic(AsyncDBTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake_redis = FakeRedis()

        async def _fake_get_redis():
            return self.fake_redis

        self._redis_patcher = patch("cache.redis_client.get_redis", new=_fake_get_redis)
        self._redis_patcher.start()
        self.addCleanup(self._redis_patcher.stop)
    async def _make_company(
        self,
        session,
        tg_id: int,
        name: str,
        funds: int,
        employee_count: int,
        product_income: int = 0,
    ) -> Company:
        owner = User(tg_id=tg_id, tg_name=f"owner-{tg_id}", traffic=0, reputation=0)
        session.add(owner)
        await session.flush()

        company = Company(
            name=name,
            company_type="tech",
            owner_id=owner.id,
            total_funds=funds,
            daily_revenue=0,
            level=1,
            employee_count=employee_count,
        )
        session.add(company)
        await session.flush()

        if product_income > 0:
            product = Product(
                company_id=company.id,
                name=f"{name}-P1",
                tech_id="basic_internet",
                version=1,
                daily_income=product_income,
                quality=10,
            )
            session.add(product)
            await session.flush()

        return company

    async def _settle_with_patches(self, session, company: Company, extra_cost_override: dict | None = None):
        default_extra = {
            "office_cost": 0, "training_cost": 0, "regulation_cost": 0,
            "insurance_cost": 0, "work_cost_adjust": 0, "culture_maintenance": 0,
        }
        extra = extra_cost_override or default_extra
        with patch("services.settlement_service.check_and_complete_research", new=AsyncMock(return_value=[])), \
            patch("services.settlement_service.get_cooperation_bonus", new=AsyncMock(return_value=0.0)), \
            patch("services.settlement_service.get_total_estate_income", new=AsyncMock(return_value=0)), \
            patch("services.settlement_service.roll_daily_events", new=AsyncMock(return_value=[])), \
            patch("services.settlement_service.update_leaderboard", new=AsyncMock(return_value=None)), \
            patch("services.ad_service.get_ad_boost", new=AsyncMock(return_value=0.0)), \
            patch("services.shop_service.get_income_buff_multiplier", new=AsyncMock(return_value=1.0)), \
            patch("services.settlement_service.get_company_revenue_debuff", new=AsyncMock(return_value=0.0)), \
            patch("services.settlement_service.save_recent_events", new=AsyncMock(return_value=None)), \
            patch("services.settlement_service.calc_extra_operating_costs", return_value=extra), \
            patch("services.settlement_service.run_regulation_audit", return_value={"fine": 0, "sampled_hours": 8, "overtime_hours": 0, "risk": 0}):
            return await settle_company(session, company)

    async def test_negative_profit_should_reduce_company_funds_boundary(self):
        async with self.Session() as session:
            async with session.begin():
                company = await self._make_company(
                    session=session,
                    tg_id=4001,
                    name="LossCo",
                    funds=100_000,
                    employee_count=10,
                    product_income=0,
                )
                initial_funds = company.total_funds
                # Force very high extra costs to guarantee a loss
                big_extra = {
                    "office_cost": 50_000, "training_cost": 0, "regulation_cost": 0,
                    "insurance_cost": 0, "work_cost_adjust": 0, "culture_maintenance": 0,
                }
                report, _events = await self._settle_with_patches(session, company, extra_cost_override=big_extra)

                profit = report.total_income - report.operating_cost
                self.assertLess(profit, 0)

            updated = await session.get(Company, company.id)
            expected = initial_funds + (report.total_income - report.operating_cost)
            self.assertEqual(updated.total_funds, expected)

    async def test_positive_profit_should_increase_company_funds_equivalence_class(self):
        async with self.Session() as session:
            async with session.begin():
                company = await self._make_company(
                    session=session,
                    tg_id=4002,
                    name="ProfitCo",
                    funds=1_000,
                    employee_count=1,
                    product_income=5_000,
                )
                initial_funds = company.total_funds
                report, _events = await self._settle_with_patches(session, company)

                profit = report.total_income - report.operating_cost
                self.assertGreater(profit, 0)

            updated = await session.get(Company, company.id)
            expected = initial_funds + (report.total_income - report.operating_cost)
            self.assertEqual(updated.total_funds, expected)
