"""Tests for progressive R&D/upgrade requirements and cost growth."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select

from db.models import Company, Product, ResearchProgress, User
from keyboards.menus import tech_list_kb
from services.product_service import create_product, upgrade_product
from services.research_service import get_available_techs, start_research

from tests.helpers.async_db_case import AsyncDBTestCase
from tests.helpers.fake_redis import FakeRedis


class TestProgressiveRndCosts(AsyncDBTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake_redis = FakeRedis()

        async def _fake_get_redis():
            return self.fake_redis

        # Product upgrade uses services.product_service.get_redis for cooldown.
        # Currency deduction uses services.user_service.get_redis in add_traffic.
        # Product rules also use cache.redis_client.get_redis for cooldown check.
        self._patcher_product_redis = patch("services.product_service.get_redis", new=_fake_get_redis)
        self._patcher_user_redis = patch("services.user_service.get_redis", new=_fake_get_redis)
        self._patcher_rules_redis = patch("services.rules.product_rules.get_redis", new=_fake_get_redis)
        self._patcher_product_redis.start()
        self._patcher_user_redis.start()
        self._patcher_rules_redis.start()
        self.addCleanup(self._patcher_product_redis.stop)
        self.addCleanup(self._patcher_user_redis.stop)
        self.addCleanup(self._patcher_rules_redis.stop)

    async def test_product_upgrade_requires_more_people_and_reputation_by_version(self):
        async with self.Session() as session:
            async with session.begin():
                owner = User(tg_id=7101, tg_name="owner-7101", traffic=0, reputation=10)
                session.add(owner)
                await session.flush()

                company = Company(
                    name="UpgradeGateCo",
                    owner_id=owner.id,
                    company_type="tech",
                    total_funds=1_000_000,
                    daily_revenue=0,
                    level=3,
                    employee_count=6,
                )
                session.add(company)
                await session.flush()

                product = Product(
                    company_id=company.id,
                    name="CoreProduct",
                    tech_id="basic_internet",
                    version=4,
                    daily_income=2_000,
                    quality=60,
                )
                session.add(product)
                await session.flush()

                ok, msg = await upgrade_product(session, product.id, owner.id)
                self.assertFalse(ok)
                self.assertIn("员工不足", msg)

                company.employee_count = 12
                ok, msg = await upgrade_product(session, product.id, owner.id)
                self.assertFalse(ok)
                self.assertIn("声望不足", msg)

                owner.reputation = 120
                funds_before = company.total_funds
                ok, msg = await upgrade_product(session, product.id, owner.id)
                self.assertTrue(ok)
                self.assertIn("升级到v5", msg)

            updated_product = await session.get(Product, product.id)
            updated_company = await session.get(Company, company.id)
            self.assertEqual(updated_product.version, 5)

            expected_cost = int(800 * (1.3 ** (4 - 1)))  # base_cost * 1.3^(old_version-1)
            self.assertEqual(updated_company.total_funds, funds_before - expected_cost)

    async def test_research_requires_progressive_employee_and_reputation(self):
        async with self.Session() as session:
            async with session.begin():
                owner = User(tg_id=7201, tg_name="owner-7201", traffic=1_000_000, reputation=0)
                session.add(owner)
                await session.flush()

                company = Company(
                    name="ResearchGateCo",
                    owner_id=owner.id,
                    company_type="tech",
                    total_funds=1_000_000,
                    daily_revenue=0,
                    level=1,
                    employee_count=1,
                )
                session.add(company)
                await session.flush()

                # Make completed_count = 1 so dynamic requirements rise.
                session.add(
                    ResearchProgress(
                        company_id=company.id,
                        tech_id="basic_internet",
                        status="completed",
                    )
                )
                await session.flush()

                ok, msg = await start_research(session, company.id, owner.id, "social_platform")
                self.assertFalse(ok)
                self.assertIn("员工不足", msg)

                company.employee_count = 3
                ok, msg = await start_research(session, company.id, owner.id, "social_platform")
                self.assertFalse(ok)
                self.assertIn("声望不足", msg)

                owner.reputation = 20
                funds_before = company.total_funds
                ok, msg = await start_research(session, company.id, owner.id, "social_platform")
                self.assertTrue(ok)
                self.assertIn("开始研究", msg)

            updated_company = await session.get(Company, company.id)
            # social_platform base cost = 3000; completed_count=1 => *1.2
            # tech company focus discount applies => *0.9
            expected_cost = int(3000 * 1.2 * 0.9)
            self.assertEqual(updated_company.total_funds, funds_before - expected_cost)

            rp = (
                await session.execute(
                    select(ResearchProgress).where(
                        ResearchProgress.company_id == company.id,
                        ResearchProgress.tech_id == "social_platform",
                    )
                )
            ).scalar_one_or_none()
            self.assertIsNotNone(rp)
            self.assertEqual(rp.status, "researching")

    async def test_research_list_cost_matches_popup_and_actual_deduction(self):
        async with self.Session() as session:
            async with session.begin():
                owner = User(tg_id=7251, tg_name="owner-7251", traffic=0, reputation=100)
                session.add(owner)
                await session.flush()

                company = Company(
                    name="ResearchCostUI",
                    owner_id=owner.id,
                    company_type="tech",
                    total_funds=20_000,
                    daily_revenue=0,
                    level=1,
                    employee_count=5,
                )
                session.add(company)
                await session.flush()

                # Ensure cloud_computing is available and completed_count = 1.
                session.add(
                    ResearchProgress(
                        company_id=company.id,
                        tech_id="basic_internet",
                        status="completed",
                    )
                )
                await session.flush()

                available = await get_available_techs(session, company.id)
                cloud = next(t for t in available if t["tech_id"] == "cloud_computing")
                expected_cost = int(5000 * 1.2 * 0.9)  # base * growth(1 completed) * focus discount
                self.assertEqual(cloud["research_cost"], expected_cost)

                kb = tech_list_kb([cloud], company.id)
                button_text = kb.inline_keyboard[0][0].text
                self.assertIn("5,400", button_text)

                funds_before = company.total_funds
                ok, msg = await start_research(session, company.id, owner.id, "cloud_computing")
                self.assertTrue(ok)
                self.assertIn("5,400", msg)

            updated_company = await session.get(Company, company.id)
            self.assertEqual(updated_company.total_funds, funds_before - expected_cost)

    async def test_create_product_cost_grows_with_existing_products(self):
        async with self.Session() as session:
            async with session.begin():
                owner = User(tg_id=7301, tg_name="owner-7301", traffic=0, reputation=20)
                session.add(owner)
                await session.flush()

                company = Company(
                    name="CreateScaleCo",
                    owner_id=owner.id,
                    company_type="tech",
                    total_funds=20_000,
                    daily_revenue=0,
                    level=1,
                    employee_count=5,
                )
                session.add(company)
                await session.flush()

                # Unlock simple_website template.
                session.add(
                    ResearchProgress(
                        company_id=company.id,
                        tech_id="basic_internet",
                        status="completed",
                    )
                )
                await session.flush()

                funds_before_first = company.total_funds
                p1, msg1 = await create_product(session, company.id, owner.id, "simple_website", custom_name="P1")
                self.assertIsNotNone(p1)
                self.assertIn("打造成功", msg1)

                owner.reputation = 5
                p2, msg2 = await create_product(session, company.id, owner.id, "simple_website", custom_name="P2")
                self.assertIsNone(p2)
                self.assertIn("声望不足", msg2)

                owner.reputation = 20
                funds_before_second = company.total_funds
                p2, msg2 = await create_product(session, company.id, owner.id, "simple_website", custom_name="P2")
                self.assertIsNotNone(p2)
                self.assertIn("研发投入", msg2)

            updated_company = await session.get(Company, company.id)
            first_cost = 1500
            second_cost = int(1500 * (1 + 0.30))
            self.assertEqual(
                updated_company.total_funds,
                funds_before_first - first_cost - second_cost,
            )

    async def test_daily_create_counter_increments_after_successful_create(self):
        async with self.Session() as session:
            async with session.begin():
                owner = User(tg_id=7351, tg_name="owner-7351", traffic=0, reputation=200)
                session.add(owner)
                await session.flush()

                company = Company(
                    name="CreateCounterCo",
                    owner_id=owner.id,
                    company_type="tech",
                    total_funds=50_000,
                    daily_revenue=0,
                    level=1,
                    employee_count=10,
                )
                session.add(company)
                await session.flush()

                session.add(
                    ResearchProgress(
                        company_id=company.id,
                        tech_id="basic_internet",
                        status="completed",
                    )
                )
                await session.flush()

                product, msg = await create_product(
                    session,
                    company.id,
                    owner.id,
                    "simple_website",
                    custom_name="CounterP1",
                )
                self.assertIsNotNone(product, msg=msg)

                from services.product_service import _daily_create_counter_key
                key = _daily_create_counter_key(company.id)
                self.assertEqual(await self.fake_redis.get(key), "1")

    async def test_daily_limit_not_reset_by_delete(self):
        async with self.Session() as session:
            async with session.begin():
                owner = User(tg_id=7361, tg_name="owner-7361", traffic=0, reputation=300)
                session.add(owner)
                await session.flush()

                company = Company(
                    name="DeleteBypassCo",
                    owner_id=owner.id,
                    company_type="tech",
                    total_funds=200_000,
                    daily_revenue=0,
                    level=1,
                    employee_count=20,
                )
                session.add(company)
                await session.flush()

                session.add(
                    ResearchProgress(
                        company_id=company.id,
                        tech_id="basic_internet",
                        status="completed",
                    )
                )
                await session.flush()

                p1, msg1 = await create_product(session, company.id, owner.id, "simple_website", custom_name="D1")
                p2, msg2 = await create_product(session, company.id, owner.id, "simple_website", custom_name="D2")
                p3, msg3 = await create_product(session, company.id, owner.id, "simple_website", custom_name="D3")
                self.assertIsNotNone(p1, msg=msg1)
                self.assertIsNotNone(p2, msg=msg2)
                self.assertIsNotNone(p3, msg=msg3)

                await session.delete(p2)
                await session.flush()

                p4, msg4 = await create_product(session, company.id, owner.id, "simple_website", custom_name="D4")
                self.assertIsNone(p4)
                self.assertTrue(msg4)

    async def test_upgrade_cooldown_persists_after_delete_and_recreate_same_tech(self):
        async with self.Session() as session:
            async with session.begin():
                owner = User(tg_id=7371, tg_name="owner-7371", traffic=0, reputation=300)
                session.add(owner)
                await session.flush()

                company = Company(
                    name="UpgradeCdCo",
                    owner_id=owner.id,
                    company_type="tech",
                    total_funds=500_000,
                    daily_revenue=0,
                    level=1,
                    employee_count=20,
                )
                session.add(company)
                await session.flush()

                session.add(
                    ResearchProgress(
                        company_id=company.id,
                        tech_id="basic_internet",
                        status="completed",
                    )
                )
                await session.flush()

                p1, msg1 = await create_product(session, company.id, owner.id, "simple_website", custom_name="U1")
                self.assertIsNotNone(p1, msg=msg1)

                ok, up_msg = await upgrade_product(session, p1.id, owner.id)
                self.assertTrue(ok, msg=up_msg)

                await session.delete(p1)
                await session.flush()

                p2, msg2 = await create_product(session, company.id, owner.id, "simple_website", custom_name="U2")
                self.assertIsNotNone(p2, msg=msg2)

                ok2, up_msg2 = await upgrade_product(session, p2.id, owner.id)
                self.assertFalse(ok2)
                self.assertTrue(up_msg2)
