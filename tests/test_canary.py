#!/usr/bin/env python3
"""Unit tests for canary_cbva.evaluate — the contract-violation decision logic."""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub playwright + dotenv so canary_cbva (and monitor) import without a browser.
for mod in ("playwright", "playwright.async_api", "dotenv"):
    sys.modules.setdefault(mod, MagicMock())

import canary_cbva  # noqa: E402  (must come after stubs)


def s(api, text, url="https://cbva.com/tournaments/1/2"):
    return {"url": url, "api": api, "text": text}


class TestCanaryEvaluate(unittest.TestCase):
    def test_all_agree_is_ok(self):
        samples = [s("open", "open")] * 5 + [s("waitlist", "waitlist")] * 2
        ok, _ = canary_cbva.evaluate(samples)
        self.assertTrue(ok)

    def test_empty_is_inconclusive_ok(self):
        ok, lines = canary_cbva.evaluate([])
        self.assertTrue(ok)
        self.assertTrue(any("INCONCLUSIVE" in l for l in lines))

    def test_label_change_breaks_contract(self):
        # API says registerable, text scrape returns unknown → buttons renamed
        samples = [s("open", "unknown")] * 6
        ok, lines = canary_cbva.evaluate(samples)
        self.assertFalse(ok)
        self.assertTrue(any("agreement" in l.lower() for l in lines))

    def test_api_shape_change_breaks_contract(self):
        # Capacity data missing everywhere → tRPC division shape changed
        samples = [s(None, "open")] * 10
        ok, lines = canary_cbva.evaluate(samples)
        self.assertFalse(ok)
        self.assertTrue(any("capacity data missing" in l.lower() for l in lines))

    def test_few_registerable_not_judged(self):
        # Only 2 registerable (< MIN_REGISTERABLE): don't fail on agreement,
        # and API data is present so no shape failure either.
        samples = [s("closed", "unknown")] * 8 + [s("open", "unknown")] * 2
        ok, lines = canary_cbva.evaluate(samples)
        self.assertTrue(ok)
        self.assertTrue(any("not judged" in l.lower() for l in lines))

    def test_minor_disagreement_tolerated(self):
        # 1 of 6 disagree (83% agree) ≥ 80% threshold → still ok
        samples = [s("open", "open")] * 5 + [s("waitlist", "open")]
        ok, _ = canary_cbva.evaluate(samples)
        self.assertTrue(ok)

    def test_majority_disagreement_fails(self):
        # 3 of 6 disagree (50%) < 80% → fail
        samples = [s("open", "open")] * 3 + [s("open", "unknown")] * 3
        ok, _ = canary_cbva.evaluate(samples)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
