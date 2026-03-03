"""Unit tests for labor-hour regulation audit logic."""

from __future__ import annotations

import datetime as dt
import unittest

from db.models import CompanyOperationProfile
from services.operations_service import (
    LEGAL_WORK_HOURS,
    REGULATION_FINE_CAP_RATE,
    get_overtime_risk_boost,
    maybe_regulation_fine,
    run_regulation_audit,
)


class TestRegulationAudit(unittest.TestCase):
    def _profile(self, *, work_hours: int, company_id: int = 1) -> CompanyOperationProfile:
        return CompanyOperationProfile(
            company_id=company_id,
            work_hours=work_hours,
            office_level="standard",
            training_level="none",
            insurance_level="basic",
            culture=30,
            ethics=35,
            regulation_pressure=70,
        )

    def test_overtime_hours_should_match_sampled_minus_legal_limit(self):
        now = dt.datetime(2026, 3, 2, tzinfo=dt.UTC)
        profile = self._profile(work_hours=12, company_id=11)
        result = run_regulation_audit(profile, income_total=100_000, now=now)

        sampled = int(result["sampled_hours"])
        overtime = int(result["overtime_hours"])
        self.assertEqual(overtime, max(0, sampled - LEGAL_WORK_HOURS))
        self.assertGreaterEqual(overtime, 1)

    def test_overtime_profile_should_have_higher_risk_than_compliant_profile(self):
        now = dt.datetime(2026, 3, 2, tzinfo=dt.UTC)
        compliant = self._profile(work_hours=7, company_id=12)
        overtime = self._profile(work_hours=12, company_id=12)

        risk_compliant = float(run_regulation_audit(compliant, income_total=100_000, now=now)["risk"])
        risk_overtime = float(run_regulation_audit(overtime, income_total=100_000, now=now)["risk"])
        self.assertGreater(risk_overtime, risk_compliant)

    def test_overtime_3_to_4h_should_increase_risk_boost_with_doubled_slope(self):
        # 1-2h segment slope = 0.10/h; 3-4h segment slope = 0.20/h.
        self.assertAlmostEqual(get_overtime_risk_boost(1), 0.10, places=6)
        self.assertAlmostEqual(get_overtime_risk_boost(2), 0.20, places=6)
        self.assertAlmostEqual(get_overtime_risk_boost(3), 0.40, places=6)
        self.assertAlmostEqual(get_overtime_risk_boost(4), 0.60, places=6)

    def test_fine_should_not_exceed_cap_ratio(self):
        now = dt.datetime(2026, 3, 2, tzinfo=dt.UTC)
        profile = self._profile(work_hours=14, company_id=13)
        income_total = 500_000

        result = run_regulation_audit(profile, income_total=income_total, now=now)
        fine = int(result["fine"])
        fine_cap = int(income_total * REGULATION_FINE_CAP_RATE)
        self.assertLessEqual(fine, fine_cap)

    def test_wrapper_should_match_audit_fine(self):
        now = dt.datetime(2026, 3, 2, tzinfo=dt.UTC)
        profile = self._profile(work_hours=10, company_id=14)
        income_total = 200_000

        self.assertEqual(
            maybe_regulation_fine(profile, income_total, now),
            int(run_regulation_audit(profile, income_total, now)["fine"]),
        )
