#!/usr/bin/env python3
"""Unit tests for check_health.py — health evaluation + staleness thresholds."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_health  # noqa: E402

PT = timezone(timedelta(hours=-7))


class TestStalenessThreshold(unittest.TestCase):
    def test_weekend_active_hours_is_tight(self):
        sat_noon = datetime(2026, 6, 20, 12, 0, tzinfo=PT)  # Saturday 12 PT
        self.assertEqual(check_health.staleness_threshold(sat_noon), check_health.ACTIVE_MAX_MIN)

    def test_weekend_early_morning_uses_daily(self):
        # 8 AM PT is right after the overnight no-run gap → lenient, not tight,
        # so the morning health check doesn't false-alarm before runs resume.
        sat_8am = datetime(2026, 6, 20, 8, 0, tzinfo=PT)
        self.assertEqual(check_health.staleness_threshold(sat_8am), check_health.DAILY_MAX_MIN)

    def test_weekend_off_hours_uses_daily(self):
        sat_3am = datetime(2026, 6, 20, 3, 0, tzinfo=PT)
        self.assertEqual(check_health.staleness_threshold(sat_3am), check_health.DAILY_MAX_MIN)

    def test_weekday_uses_daily(self):
        wed_noon = datetime(2026, 6, 17, 12, 0, tzinfo=PT)  # Wednesday
        self.assertEqual(check_health.staleness_threshold(wed_noon), check_health.DAILY_MAX_MIN)

    def test_active_threshold_tolerates_github_jitter(self):
        # Routine GitHub cron jitter (≈100 min between runs) must NOT trip it.
        self.assertLess(105, check_health.ACTIVE_MAX_MIN)


class TestEvaluateHealth(unittest.TestCase):
    OK_STATE = {"last_run": {"status": "ok", "error_count": 0, "errors": []}}

    def _now(self):
        return datetime(2026, 6, 17, 12, 0, tzinfo=PT)  # weekday → 1560 threshold

    def test_healthy(self):
        issues = check_health.evaluate_health(self.OK_STATE, "", 30, self._now())
        self.assertEqual(issues, [])

    def test_workflow_failure_flagged(self):
        issues = check_health.evaluate_health(self.OK_STATE, "failure", 30, self._now())
        self.assertEqual(len(issues), 1)
        self.assertIn("workflow failed", issues[0])

    def test_script_errors_flagged(self):
        state = {"last_run": {"status": "error", "error_count": 2,
                              "errors": [{"phase": "scan", "message": "boom"},
                                         {"phase": "ratings", "message": "kaboom"}]}}
        issues = check_health.evaluate_health(state, "", 30, self._now())
        self.assertEqual(len(issues), 1)
        self.assertIn("scan: boom", issues[0])

    def test_staleness_flagged_when_too_old(self):
        issues = check_health.evaluate_health(self.OK_STATE, "", 2000, self._now())  # > 1560
        self.assertEqual(len(issues), 1)
        self.assertIn("No successful monitor run", issues[0])

    def test_staleness_not_flagged_within_threshold(self):
        issues = check_health.evaluate_health(self.OK_STATE, "", 1000, self._now())
        self.assertEqual(issues, [])

    def test_unknown_age_does_not_false_alarm(self):
        # API unavailable → age None → staleness skipped (no false alert)
        issues = check_health.evaluate_health(self.OK_STATE, "", None, self._now())
        self.assertEqual(issues, [])

    def test_weekend_jitter_does_not_trip(self):
        # ~100 min between weekend runs is normal GitHub cron jitter → no alert.
        sat = datetime(2026, 6, 20, 12, 0, tzinfo=PT)
        issues = check_health.evaluate_health(self.OK_STATE, "", 100, sat)
        self.assertEqual(issues, [])

    def test_weekend_sustained_outage_trips(self):
        # ~5h of silence during weekend active hours is a real outage → alert.
        sat = datetime(2026, 6, 20, 12, 0, tzinfo=PT)
        issues = check_health.evaluate_health(self.OK_STATE, "", 300, sat)  # > 240
        self.assertEqual(len(issues), 1)
        self.assertIn("No successful monitor run", issues[0])

    def test_multiple_issues_combine(self):
        state = {"last_run": {"status": "error", "error_count": 1,
                              "errors": [{"phase": "scan", "message": "boom"}]}}
        issues = check_health.evaluate_health(state, "failure", 2000, self._now())
        self.assertEqual(len(issues), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
