"""Tests for employee workforce income (人力产出收益)."""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, patch

from config import settings
from db.models import Company, Product, User
from services.company_service import calc_employee_income
from services.settlement_service import settle_company

from tests.helpers.async_db_case import AsyncDBTestCase
from tests.helpers.fake_redis import FakeRedis


class TestCalcEmployeeIncome(AsyncDBTestCase):
    """Unit tests for calc_employee_income() function."""

    def test_zero_employees_returns_zero(self):
        base, eff = calc_employee_income(0, 10000)
        self.assertEqual(base, 0)
        self.assertEqual(eff, 0)

    def test_negative_employees_returns_zero(self):
        base, eff = calc_employee_income(-5, 10000)
        self.assertEqual(base, 0)
        self.assertEqual(eff, 0)

    def test_basic_output_scales_with_employee_count(self):
        base1, _ = calc_employee_income(10, 0)
        base2, _ = calc_employee_income(50, 0)
        # base_output = count * salary_base * 1.5
        self.assertEqual(base1, int(10 * settings.employee_salary_base * 1.5))
        self.assertEqual(base2, int(50 * settings.employee_salary_base * 1.5))
        self.assertGreater(base2, base1)

    def test_efficiency_bonus_depends_on_product_income(self):
        _, eff_zero = calc_employee_income(10, 0)
        _, eff_high = calc_employee_income(10, 50000)
        self.assertEqual(eff_zero, 0)
        self.assertGreater(eff_high, 0)

    def test_efficiency_bonus_formula(self):
        emp = 100
        prod = 10000
        _, eff = calc_employee_income(emp, prod)
        effective = min(emp, settings.employee_effective_cap_for_progress)
        expected = int(prod * effective * 0.002)
        self.assertEqual(eff, expected)

    def test_soft_cap_limits_effective_employees(self):
        cap = settings.employee_effective_cap_for_progress
        # Beyond cap, efficiency_bonus should be capped (base still scales)
        _, eff_at_cap = calc_employee_income(cap, 10000)
        _, eff_over = calc_employee_income(cap + 500, 10000)
        self.assertEqual(eff_at_cap, eff_over)
        # But base output still scales with actual employee count
        base_at_cap, _ = calc_employee_income(cap, 10000)
        base_over, _ = calc_employee_income(cap + 500, 10000)
        self.assertGreater(base_over, base_at_cap)

    def test_employee_income_exceeds_salary_cost(self):
        """With default settings, hiring employees should be net-profitable."""
        emp = 10
        base, eff = calc_employee_income(emp, 0)
        salary = emp * settings.employee_salary_base
        # Base output alone should be higher than salary
        self.assertGreater(base, salary,
                           f"Base output ({base}) should exceed salary cost ({salary})")


class TestSettlementWithEmployeeIncome(AsyncDBTestCase):
    """Integration tests: employee income appears in settlement."""

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

    async def _settle_with_patches(self, session, company: Company):
        extra = {
            "office_cost": 0, "training_cost": 0, "regulation_cost": 0,
            "insurance_cost": 0, "work_cost_adjust": 0, "culture_maintenance": 0,
        }
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

    async def test_employee_income_in_report(self):
        """Settlement report should include employee_income > 0 when employees exist."""
        async with self.Session() as session:
            async with session.begin():
                company = await self._make_company(
                    session=session,
                    tg_id=5001,
                    name="EmpIncomeCo",
                    funds=100_000,
                    employee_count=20,
                    product_income=5_000,
                )
                report, _events = await self._settle_with_patches(session, company)

                self.assertGreater(report.employee_income, 0)
                # employee_income should contribute to total_income
                self.assertGreaterEqual(report.total_income, report.product_income + report.employee_income)

    async def test_more_employees_more_income(self):
        """Company with more employees should earn more employee income."""
        async with self.Session() as session:
            async with session.begin():
                co_small = await self._make_company(
                    session=session,
                    tg_id=5002,
                    name="SmallCo",
                    funds=100_000,
                    employee_count=5,
                    product_income=3_000,
                )
                report_small, _ = await self._settle_with_patches(session, co_small)

                co_large = await self._make_company(
                    session=session,
                    tg_id=5003,
                    name="LargeCo",
                    funds=100_000,
                    employee_count=50,
                    product_income=3_000,
                )
                report_large, _ = await self._settle_with_patches(session, co_large)

                self.assertGreater(report_large.employee_income, report_small.employee_income)

    async def test_zero_employees_zero_employee_income(self):
        """Company with no employees should have zero employee income."""
        async with self.Session() as session:
            async with session.begin():
                company = await self._make_company(
                    session=session,
                    tg_id=5004,
                    name="NobodyCo",
                    funds=100_000,
                    employee_count=0,
                    product_income=5_000,
                )
                report, _events = await self._settle_with_patches(session, company)
                self.assertEqual(report.employee_income, 0)
