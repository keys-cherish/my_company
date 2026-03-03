"""打卡、红包、流量兑换功能测试。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch, MagicMock

from services.checkin_service import do_checkin, _parse_streak_rewards, _parse_bonus_pool
from services.redpacket_service import create_redpacket, grab_redpacket, get_redpacket_results, find_lucky_king

from tests.helpers.fake_redis import FakeRedis


class TestCheckinRewardsConfig(unittest.TestCase):
    """打卡奖励配置解析测试。"""

    def test_parse_streak_rewards_default(self):
        rewards = _parse_streak_rewards()
        self.assertEqual(len(rewards), 7)
        self.assertEqual(rewards[0], 300)
        self.assertEqual(rewards[-1], 3000)
        # 奖励递增
        for i in range(1, len(rewards)):
            self.assertGreaterEqual(rewards[i], rewards[i - 1])

    def test_parse_bonus_pool(self):
        pool = _parse_bonus_pool()
        self.assertGreater(len(pool), 0)
        for val in pool:
            self.assertGreater(val, 0)


class TestCheckinLogic(unittest.IsolatedAsyncioTestCase):
    """打卡逻辑测试。"""

    async def asyncSetUp(self):
        self.fake_redis = FakeRedis()
        async def _fake_get_redis():
            return self.fake_redis
        self._redis_patcher = patch("services.checkin_service.get_redis", new=_fake_get_redis)
        self._redis_patcher.start()
        self.addCleanup(self._redis_patcher.stop)

    async def test_first_checkin_succeeds(self):
        success, msg, reward = await do_checkin(9001)
        self.assertTrue(success)
        self.assertGreater(reward, 0)
        self.assertIn("打卡成功", msg)
        self.assertIn("连续打卡: 1", msg)

    async def test_double_checkin_same_day_rejected(self):
        await do_checkin(9002)
        success, msg, reward = await do_checkin(9002)
        self.assertFalse(success)
        self.assertEqual(reward, 0)
        self.assertIn("已经打过卡", msg)

    async def test_streak_increments(self):
        """模拟连续两天打卡，验证连续天数递增。"""
        import datetime as dt
        from utils.timezone import BJ_TZ

        # 第1天打卡
        success1, msg1, _ = await do_checkin(9003)
        self.assertTrue(success1)
        self.assertIn("连续打卡: 1", msg1)

        # 手动修改last日期为昨天，模拟第二天
        yesterday = (dt.datetime.now(BJ_TZ).date() - dt.timedelta(days=1)).isoformat()
        today = dt.datetime.now(BJ_TZ).date().isoformat()
        await self.fake_redis.set(f"checkin:last:9003", yesterday)

        success2, msg2, _ = await do_checkin(9003)
        self.assertTrue(success2)
        self.assertIn("连续打卡: 2", msg2)


class TestRedpacketCreation(unittest.IsolatedAsyncioTestCase):
    """红包创建测试。"""

    async def asyncSetUp(self):
        self.fake_redis = FakeRedis()
        async def _fake_get_redis():
            return self.fake_redis
        self._redis_patcher = patch("services.redpacket_service.get_redis", new=_fake_get_redis)
        self._redis_patcher.start()
        self.addCleanup(self._redis_patcher.stop)

    async def test_create_valid_redpacket(self):
        ok, msg, packet_id = await create_redpacket(1001, "测试公司", 5000, 5)
        self.assertTrue(ok)
        self.assertNotEqual(packet_id, "")

    async def test_create_redpacket_below_minimum(self):
        from config import settings
        ok, msg, _ = await create_redpacket(1001, "测试公司", settings.redpacket_min_amount - 1, 1)
        self.assertFalse(ok)
        self.assertIn("至少", msg)

    async def test_create_redpacket_above_maximum(self):
        from config import settings
        ok, msg, _ = await create_redpacket(1001, "测试公司", settings.redpacket_max_amount + 1, 1)
        self.assertFalse(ok)
        self.assertIn("最多", msg)

    async def test_create_redpacket_too_many_splits(self):
        from config import settings
        ok, msg, _ = await create_redpacket(1001, "测试公司", 5000, settings.redpacket_max_count + 1)
        self.assertFalse(ok)
        self.assertIn("最多", msg)

    async def test_create_redpacket_amount_less_than_count(self):
        """总金额等于500但要拆成501份 → 每份不足1积分。"""
        ok, msg, _ = await create_redpacket(1001, "测试公司", 500, 501)
        self.assertFalse(ok)

    async def test_grab_redpacket_success(self):
        ok, _, packet_id = await create_redpacket(1001, "测试公司", 5000, 3)
        self.assertTrue(ok)
        # 三个不同用户抢
        got1, msg1, amt1 = await grab_redpacket(2001, packet_id)
        self.assertTrue(got1)
        self.assertGreater(amt1, 0)

        got2, msg2, amt2 = await grab_redpacket(2002, packet_id)
        self.assertTrue(got2)
        self.assertGreater(amt2, 0)

        got3, msg3, amt3 = await grab_redpacket(2003, packet_id)
        self.assertTrue(got3)
        self.assertGreater(amt3, 0)

        # 总和应等于总金额
        self.assertEqual(amt1 + amt2 + amt3, 5000)

    async def test_grab_redpacket_duplicate(self):
        ok, _, packet_id = await create_redpacket(1001, "测试公司", 5000, 3)
        self.assertTrue(ok)
        await grab_redpacket(2001, packet_id)
        got, msg, amt = await grab_redpacket(2001, packet_id)
        self.assertFalse(got)
        self.assertIn("已经抢过", msg)

    async def test_grab_redpacket_exhausted(self):
        ok, _, packet_id = await create_redpacket(1001, "测试公司", 500, 1)
        self.assertTrue(ok)
        await grab_redpacket(2001, packet_id)
        got, msg, _ = await grab_redpacket(2002, packet_id)
        self.assertFalse(got)
        self.assertIn("抢完", msg)

    async def test_grab_results_and_lucky_king(self):
        ok, _, packet_id = await create_redpacket(1001, "测试公司", 10000, 3)
        self.assertTrue(ok)
        await grab_redpacket(2001, packet_id)
        await grab_redpacket(2002, packet_id)
        await grab_redpacket(2003, packet_id)
        results = await get_redpacket_results(packet_id)
        self.assertEqual(len(results), 3)
        king = await find_lucky_king(packet_id)
        self.assertIsNotNone(king)
        # king should have the largest amount
        max_amt = max(r[1] for r in results)
        self.assertEqual(king[1], max_amt)


class TestTrafficExchangeConfig(unittest.TestCase):
    """流量兑换配置测试。"""

    def test_traffic_rate_in_config(self):
        from config import settings
        # 100积分 = 1MB
        self.assertEqual(settings.traffic_exchange_rate, 100)

    def test_traffic_api_url_default_empty(self):
        from config import settings
        self.assertEqual(settings.traffic_exchange_api_url, "")

    def test_daily_limit_positive(self):
        from config import settings
        # 每日上限 5TB = 5,000,000 MB
        self.assertGreater(settings.traffic_exchange_daily_limit_mb, 0)
        self.assertEqual(settings.traffic_exchange_daily_limit_mb, 5_000_000)
