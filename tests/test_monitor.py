"""
Unit tests for monitor.py pure-Python helpers.

Runs without Playwright, CBVA credentials, or network access.
Covers: name matching, division labels, team/player extraction,
        score formatting, bracket math, and outcome detection.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# ── Stub out playwright so monitor can be imported without a browser install ──
for mod in ("playwright", "playwright.async_api", "dotenv"):
    sys.modules.setdefault(mod, MagicMock())

# Add repo root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import monitor  # noqa: E402  (must come after stubs)


# ── _names_match ──────────────────────────────────────────────────────────────

class TestNamesMatch(unittest.TestCase):

    def test_exact_match(self):
        self.assertTrue(monitor._names_match("Ramon Sua", "Ramon Sua"))

    def test_case_insensitive(self):
        self.assertTrue(monitor._names_match("RAMON SUA", "ramon sua"))

    def test_leading_trailing_whitespace(self):
        self.assertTrue(monitor._names_match("  Ramon Sua  ", "Ramon Sua"))

    def test_last_name_plus_first_two_chars(self):
        # "Ra" matches "Ra", "Sua" == "Sua" → match
        self.assertTrue(monitor._names_match("Ramon Sua", "Rachel Sua"))

    def test_different_last_name(self):
        self.assertFalse(monitor._names_match("Ramon Smith", "Ramon Sua"))

    def test_different_first_two_chars(self):
        # "Jo" ≠ "Ra" → no match even with same last name
        self.assertFalse(monitor._names_match("John Sua", "Ramon Sua"))

    def test_single_word_no_match(self):
        self.assertFalse(monitor._names_match("Ramon", "Sua"))

    def test_single_word_exact(self):
        self.assertTrue(monitor._names_match("Ramon", "Ramon"))


# ── _division_label ───────────────────────────────────────────────────────────

class TestDivisionLabel(unittest.TestCase):

    def _div(self, display, gender="male", max_age=None):
        return {"division": {"display": display}, "gender": gender, "maxAge": max_age}

    def test_mens_open(self):
        self.assertEqual(monitor._division_label(self._div("Open")), "Men's OPEN")

    def test_womens_aa(self):
        self.assertEqual(monitor._division_label(self._div("AA", gender="female")), "Women's AA")

    def test_boys_with_age(self):
        self.assertEqual(monitor._division_label(self._div("B", gender="male", max_age=16)), "Boys B")

    def test_girls_with_age(self):
        self.assertEqual(monitor._division_label(self._div("A", gender="female", max_age=18)), "Girls A")

    def test_coed(self):
        self.assertEqual(monitor._division_label(self._div("BB", gender="coed")), "Coed BB")

    def test_malformed_no_level(self):
        # Empty dict → default gender "male", empty level → "Men's" (no level to strip)
        self.assertEqual(monitor._division_label({}), "Men's")


# ── _extract_teams ────────────────────────────────────────────────────────────

class TestExtractTeams(unittest.TestCase):

    def test_direct_list(self):
        teams = [{"id": 1}, {"id": 2}]
        self.assertEqual(monitor._extract_teams(teams), teams)

    def test_dict_with_teams_key(self):
        teams = [{"id": 1}]
        self.assertEqual(monitor._extract_teams({"teams": teams}), teams)

    def test_dict_with_entries_key(self):
        entries = [{"id": 9}]
        self.assertEqual(monitor._extract_teams({"entries": entries}), entries)

    def test_dict_with_registrations_key(self):
        regs = [{"id": 7}]
        self.assertEqual(monitor._extract_teams({"registrations": regs}), regs)

    def test_unknown_shape_returns_empty(self):
        self.assertEqual(monitor._extract_teams({"something": []}), [])

    def test_none_returns_empty(self):
        self.assertEqual(monitor._extract_teams(None), [])


# ── _extract_players ──────────────────────────────────────────────────────────

class TestExtractPlayers(unittest.TestCase):

    def test_profiles_key_direct(self):
        ps = [{"firstName": "Ramon"}]
        self.assertEqual(monitor._extract_players({"profiles": ps}), ps)

    def test_players_key_direct(self):
        ps = [{"firstName": "Selina"}]
        self.assertEqual(monitor._extract_players({"players": ps}), ps)

    def test_nested_under_team(self):
        ps = [{"firstName": "Kyle"}]
        self.assertEqual(monitor._extract_players({"team": {"profiles": ps}}), ps)

    def test_nested_players_key(self):
        ps = [{"firstName": "Ana"}]
        self.assertEqual(monitor._extract_players({"team": {"players": ps}}), ps)

    def test_empty_team(self):
        self.assertEqual(monitor._extract_players({}), [])

    def test_non_list_value_ignored(self):
        self.assertEqual(monitor._extract_players({"profiles": "bad"}), [])


# ── _ordinal ──────────────────────────────────────────────────────────────────

class TestOrdinal(unittest.TestCase):

    def test_1st(self):  self.assertEqual(monitor._ordinal(1),  "1st")
    def test_2nd(self):  self.assertEqual(monitor._ordinal(2),  "2nd")
    def test_3rd(self):  self.assertEqual(monitor._ordinal(3),  "3rd")
    def test_4th(self):  self.assertEqual(monitor._ordinal(4),  "4th")
    def test_11th(self): self.assertEqual(monitor._ordinal(11), "11th")
    def test_12th(self): self.assertEqual(monitor._ordinal(12), "12th")
    def test_13th(self): self.assertEqual(monitor._ordinal(13), "13th")
    def test_21st(self): self.assertEqual(monitor._ordinal(21), "21st")
    def test_22nd(self): self.assertEqual(monitor._ordinal(22), "22nd")


# ── _score_str ────────────────────────────────────────────────────────────────

class TestScoreStr(unittest.TestCase):

    def _set(self, a, b, status=None):
        s = {"teamAScore": a, "teamBScore": b}
        if status:
            s["status"] = status
        return s

    def test_single_set(self):
        self.assertEqual(monitor._score_str([self._set(21, 15)]), "21-15")

    def test_two_sets(self):
        self.assertEqual(monitor._score_str([self._set(21, 18), self._set(21, 14)]), "21-18, 21-14")

    def test_not_started_skipped(self):
        self.assertEqual(monitor._score_str([self._set(0, 0, "not_started")]), "not started")

    def test_empty_list(self):
        self.assertEqual(monitor._score_str([]), "not started")

    def test_none(self):
        self.assertEqual(monitor._score_str(None), "not started")

    def test_real_zeros_skipped_when_status_none(self):
        # 0-0 with status=None is treated as not_started (matches CBVA API behavior)
        self.assertEqual(monitor._score_str([self._set(0, 0)]), "not started")

    def test_real_zeros_shown_with_completed_status(self):
        # 0-0 with an explicit non-not_started status does render
        self.assertEqual(monitor._score_str([self._set(0, 0, "completed")]), "0-0")


# ── _build_seed_map ───────────────────────────────────────────────────────────

class TestBuildSeedMap(unittest.TestCase):

    def test_basic(self):
        matches = [
            {"teamAId": 10, "teamASeed": 1, "teamBId": 20, "teamBSeed": 8},
            {"teamAId": 30, "teamASeed": 4, "teamBId": 40, "teamBSeed": 5},
        ]
        sm = monitor._build_seed_map(matches)
        self.assertEqual(sm[10], 1)
        self.assertEqual(sm[20], 8)
        self.assertEqual(sm[30], 4)
        self.assertEqual(sm[40], 5)

    def test_null_seeds_skipped(self):
        matches = [{"teamAId": 10, "teamASeed": None, "teamBId": 20, "teamBSeed": 3}]
        sm = monitor._build_seed_map(matches)
        self.assertNotIn(10, sm)
        self.assertEqual(sm[20], 3)

    def test_empty(self):
        self.assertEqual(monitor._build_seed_map([]), {})


# ── _finish_place ─────────────────────────────────────────────────────────────

class TestFinishPlace(unittest.TestCase):

    def _matches(self, round_counts: dict[int, int]) -> list[dict]:
        """Build a fake match list: {round_num: team_count_in_round}."""
        matches = []
        for r, n in round_counts.items():
            for _ in range(n):
                matches.append({"round": r})
        return matches

    def test_lost_in_final_is_2nd(self):
        # Final has 1 match; loser is 2nd
        matches = self._matches({0: 8, 1: 4, 2: 2, 3: 1})
        self.assertEqual(monitor._finish_place(matches, lost_round=3), "2nd")

    def test_lost_in_semis_is_3rd_4th(self):
        matches = self._matches({0: 8, 1: 4, 2: 2, 3: 1})
        self.assertEqual(monitor._finish_place(matches, lost_round=2), "3rd–4th")

    def test_lost_in_quarters_is_5th_8th(self):
        matches = self._matches({0: 8, 1: 4, 2: 2, 3: 1})
        self.assertEqual(monitor._finish_place(matches, lost_round=1), "5th–8th")

    def test_lost_in_r1_of_32_team(self):
        # R0: 16 matches, R1: 8, R2: 4, R3: 2, R4: 1
        matches = self._matches({0: 16, 1: 8, 2: 4, 3: 2, 4: 1})
        self.assertEqual(monitor._finish_place(matches, lost_round=0), "17th–32nd")


# ── _player_outcome ───────────────────────────────────────────────────────────

class TestPlayerOutcome(unittest.TestCase):

    def _match(self, round_num, a_id, b_id, winner_id, a_seed=None, b_seed=None):
        return {
            "round": round_num,
            "teamAId": a_id, "teamBId": b_id,
            "teamASeed": a_seed, "teamBSeed": b_seed,
            "winnerId": winner_id,
            "status": "completed",
            "sets": [],
        }

    def test_champion(self):
        # Wins R0 and R1 (final), R1 is max_round → champion
        matches = [
            self._match(0, a_id=1, b_id=2, winner_id=1),
            self._match(1, a_id=1, b_id=3, winner_id=1),
        ]
        out = monitor._player_outcome(matches, {"team_id": 1})
        self.assertTrue(out["is_champion"])
        self.assertIsNone(out["finish_round"])

    def test_lost_in_round_0(self):
        matches = [self._match(0, a_id=1, b_id=2, winner_id=2)]
        out = monitor._player_outcome(matches, {"team_id": 1})
        self.assertFalse(out["is_champion"])
        self.assertEqual(out["finish_round"], 0)

    def test_won_then_lost(self):
        matches = [
            self._match(0, a_id=1, b_id=2, winner_id=1),
            self._match(1, a_id=1, b_id=3, winner_id=3),
        ]
        out = monitor._player_outcome(matches, {"team_id": 1})
        self.assertFalse(out["is_champion"])
        self.assertEqual(out["finish_round"], 1)
        self.assertFalse(out["last_won"])

    def test_empty_data_returns_empty(self):
        self.assertEqual(monitor._player_outcome([], {"team_id": 1}), {})

    def test_match_by_seed(self):
        matches = [self._match(0, a_id=10, b_id=20, winner_id=20, a_seed=1, b_seed=8)]
        out = monitor._player_outcome(matches, {"team_seed": 1})
        self.assertFalse(out["is_champion"])
        self.assertEqual(out["finish_round"], 0)

    def test_skips_non_completed_matches(self):
        m = self._match(0, a_id=1, b_id=2, winner_id=1)
        m["status"] = "in_progress"
        out = monitor._player_outcome([m], {"team_id": 1})
        # Non-completed matches don't affect win/loss tracking
        self.assertFalse(out["is_champion"])
        self.assertIsNone(out["finish_round"])
        self.assertIsNone(out["last_round"])


# ── load_users ────────────────────────────────────────────────────────────────

class TestLoadUsers(unittest.TestCase):

    def _write_users(self, data):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir="."
        )
        json.dump(data, tmp)
        tmp.close()
        return tmp.name

    def test_valid_users(self):
        path = self._write_users([
            {"email": "a@b.com", "players": ["Player One"]},
            {"email": "c@d.com", "players": ["Player Two", "Player Three"]},
        ])
        orig = monitor.USERS_FILE
        monitor.USERS_FILE = path
        try:
            users = monitor.load_users()
            self.assertEqual(len(users), 2)
            self.assertEqual(users[0]["email"], "a@b.com")
        finally:
            monitor.USERS_FILE = orig
            Path(path).unlink(missing_ok=True)

    def test_filters_entries_without_email(self):
        path = self._write_users([
            {"email": "", "players": ["X"]},
            {"email": "ok@test.com", "players": ["Y"]},
        ])
        orig = monitor.USERS_FILE
        monitor.USERS_FILE = path
        try:
            users = monitor.load_users()
            self.assertEqual(len(users), 1)
        finally:
            monitor.USERS_FILE = orig
            Path(path).unlink(missing_ok=True)

    def test_filters_entries_without_players(self):
        path = self._write_users([
            {"email": "ok@test.com", "players": []},
            {"email": "ok2@test.com", "players": ["Y"]},
        ])
        orig = monitor.USERS_FILE
        monitor.USERS_FILE = path
        try:
            users = monitor.load_users()
            self.assertEqual(len(users), 1)
        finally:
            monitor.USERS_FILE = orig
            Path(path).unlink(missing_ok=True)

    def test_missing_file_returns_empty(self):
        orig = monitor.USERS_FILE
        monitor.USERS_FILE = "/tmp/does_not_exist_cbva.json"
        try:
            users = monitor.load_users()
            self.assertEqual(users, [])
        finally:
            monitor.USERS_FILE = orig


# ── Regression: PLAYER_NAMES bug ──────────────────────────────────────────────

class TestPlayerNamesBugRegression(unittest.TestCase):
    """
    The scan phase crashed with `NameError: name 'PLAYER_NAMES' is not defined`.
    The fix: line 432 of monitor.py must reference the local `player_names`
    parameter, not a non-existent module-level `PLAYER_NAMES`.
    """

    def test_PLAYER_NAMES_not_in_module(self):
        self.assertFalse(
            hasattr(monitor, "PLAYER_NAMES"),
            "PLAYER_NAMES must not exist as a module-level name — "
            "scan_today_tournaments uses its `player_names` parameter instead",
        )

    def test_player_names_param_used_in_scan(self):
        import inspect
        src = inspect.getsource(monitor.scan_today_tournaments)
        self.assertNotIn(
            "PLAYER_NAMES", src,
            "scan_today_tournaments must not reference PLAYER_NAMES",
        )
        self.assertIn(
            "player_names", src,
            "scan_today_tournaments must use its player_names parameter",
        )


# ── Regression: false playoff email ──────────────────────────────────────────

class TestPlayoffGate(unittest.TestCase):
    """
    CBVA publishes the complete bracket at tournament start with every match
    in "scheduled" status before play begins.  The playoff email must NOT fire
    just because the player's seed appears in the bracket — it must wait until
    at least one of their matches has moved past "scheduled"/"not_started".
    """

    def _scheduled_match(self, a_seed, b_seed):
        return {
            "teamASeed": a_seed, "teamBSeed": b_seed,
            "teamAId": None, "teamBId": None,
            "status": "scheduled",
            "winnerId": None, "sets": [],
        }

    def _active_match(self, a_seed, b_seed, status="in_progress"):
        return {
            "teamASeed": a_seed, "teamBSeed": b_seed,
            "teamAId": None, "teamBId": None,
            "status": status,
            "winnerId": None, "sets": [],
        }

    def _check_results_playoff_logic(self, data, entry):
        """
        Extract just the playoff guard logic from check_results to test it
        without async/network overhead.
        """
        our_seed    = entry.get("team_seed")
        team_id     = entry.get("team_id")
        bracket_tid = entry.get("bracket_team_id")

        in_bracket = False
        if team_id:
            in_bracket = any(
                m.get("teamAId") == team_id or m.get("teamBId") == team_id
                for m in data
            )
        if not in_bracket and bracket_tid:
            in_bracket = any(
                m.get("teamAId") == bracket_tid or m.get("teamBId") == bracket_tid
                for m in data
            )
        if not in_bracket and our_seed:
            in_bracket = any(
                m.get("teamASeed") == our_seed or m.get("teamBSeed") == our_seed
                for m in data
            )

        if not in_bracket:
            return False

        player_matches = [
            m for m in data
            if (team_id     and (m.get("teamAId") == team_id     or m.get("teamBId") == team_id))
            or (bracket_tid and (m.get("teamAId") == bracket_tid or m.get("teamBId") == bracket_tid))
            or (our_seed    and (m.get("teamASeed") == our_seed  or m.get("teamBSeed") == our_seed))
        ]
        return any(
            m.get("status") not in ("scheduled", "not_started", None)
            for m in player_matches
        )

    def test_no_email_when_all_scheduled(self):
        # Simulates bracket published at tournament start — all matches "scheduled"
        data = [
            self._scheduled_match(9, 8),   # Selina's R0 match (seed 9 vs 8)
            self._scheduled_match(5, 12),
            self._scheduled_match(1, None),
        ]
        entry = {"team_seed": 9}
        self.assertFalse(self._check_results_playoff_logic(data, entry))

    def test_email_fires_when_match_in_progress(self):
        data = [
            self._active_match(9, 8, "in_progress"),
            self._scheduled_match(5, 12),
        ]
        entry = {"team_seed": 9}
        self.assertTrue(self._check_results_playoff_logic(data, entry))

    def test_email_fires_when_match_completed(self):
        data = [
            self._active_match(9, 8, "completed"),
        ]
        entry = {"team_seed": 9}
        self.assertTrue(self._check_results_playoff_logic(data, entry))

    def test_no_email_when_player_not_in_bracket(self):
        # Player seed 3 not in this bracket
        data = [self._scheduled_match(9, 8)]
        entry = {"team_seed": 3}
        self.assertFalse(self._check_results_playoff_logic(data, entry))

    def test_source_has_match_started_guard(self):
        import inspect
        src = inspect.getsource(monitor.check_results)
        self.assertIn(
            "match_started", src,
            "check_results must have a match_started guard to prevent "
            "false playoff emails on bracket initialisation",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
